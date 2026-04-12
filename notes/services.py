import logging
import time
import requests
from pathlib import Path
import re

import boto3
from botocore.exceptions import BotoCoreError, ClientError
from django.conf import settings

logger = logging.getLogger(__name__)


ALLOWED_EXTENSIONS = {".pdf", ".png", ".jpg", ".jpeg", ".tiff", ".tif"}


class AWSProcessingError(Exception):
    pass


def validate_file_extension(file_name: str) -> None:
    extension = Path(file_name).suffix.lower()
    if extension not in ALLOWED_EXTENSIONS:
        raise ValueError("Unsupported file type. Upload a PDF or image file.")


def get_aws_client(service_name: str):
    return boto3.client(service_name, region_name=settings.AWS_REGION)


def upload_to_s3(local_file, key: str) -> str:
    s3_client = get_aws_client("s3")
    try:
        s3_client.upload_fileobj(local_file, settings.AWS_S3_BUCKET, key)
        return f"s3://{settings.AWS_S3_BUCKET}/{key}"
    except (BotoCoreError, ClientError) as exc:
        logger.exception("S3 upload failed: %s", exc)
        raise AWSProcessingError("Failed to upload file to S3.") from exc


def extract_text_with_textract(bucket: str, key: str) -> str:
    textract = get_aws_client("textract")
    file_ext = Path(key).suffix.lower()

    try:
        if file_ext == ".pdf":
            return _extract_text_async_textract(textract, bucket, key)

        response = textract.detect_document_text(
            Document={"S3Object": {"Bucket": bucket, "Name": key}}
        )
        return _blocks_to_text(response.get("Blocks", []))
    except (BotoCoreError, ClientError) as exc:
        logger.exception("Textract extraction failed: %s", exc)
        raise AWSProcessingError(f"Failed to extract text using Textract: {exc}") from exc


def _blocks_to_text(blocks: list[dict]) -> str:
    lines = [
        block.get("Text", "")
        for block in blocks
        if block.get("BlockType") == "LINE"
    ]
    raw_text = "\n".join(lines).strip()
    return clean_extracted_text(raw_text)


def _extract_text_async_textract(textract, bucket: str, key: str) -> str:
    start = textract.start_document_text_detection(
        DocumentLocation={"S3Object": {"Bucket": bucket, "Name": key}}
    )
    job_id = start["JobId"]

    all_blocks = []
    next_token = None

    # Poll until job completes, then page through all results.
    while True:
        params = {"JobId": job_id}
        if next_token:
            params["NextToken"] = next_token

        result = textract.get_document_text_detection(**params)
        status = result.get("JobStatus")

        if status in {"IN_PROGRESS"}:
            time.sleep(2)
            continue

        if status != "SUCCEEDED":
            raise AWSProcessingError("Textract async job failed.")

        all_blocks.extend(result.get("Blocks", []))
        next_token = result.get("NextToken")
        if not next_token:
            break

    return _blocks_to_text(all_blocks)


def extract_key_phrases_with_comprehend(text: str) -> list[str]:
    if not text.strip():
        return []

    comprehend = get_aws_client("comprehend")
    try:
        response = comprehend.detect_key_phrases(
            Text=text[:5000],
            LanguageCode=settings.AWS_COMPREHEND_LANGUAGE,
        )
    except (BotoCoreError, ClientError) as exc:
        logger.exception("Comprehend key phrase extraction failed: %s", exc)
        raise AWSProcessingError("Failed to extract key phrases with Comprehend.") from exc

    return [item.get("Text", "") for item in response.get("KeyPhrases", []) if item.get("Text")]


def build_fallback_summary(key_phrases: list[str], extracted_text: str) -> str:
    if key_phrases:
        top_phrases = key_phrases[:12]
        bullets = "\n".join([f"- {phrase}" for phrase in top_phrases])
        return f"Key points:\n{bullets}"

    if extracted_text.strip():
        first_lines = "\n".join(extracted_text.splitlines()[:8])
        return f"Extracted highlights:\n{first_lines}"

    return "No text could be extracted from the uploaded file."


def build_local_summary(text: str, sentence_count: int = 6) -> str:
    if not text.strip():
        return ""

    if is_form_like(text):
        return build_form_summary(text)

    sentences = re.split(r"(?<=[.!?])\s+", text.strip())
    sentences = [s.strip() for s in sentences if len(s.strip().split()) >= 6]
    if len(sentences) <= sentence_count:
        return format_bullets(sentences)

    stopwords = {
        "the", "and", "for", "that", "with", "this", "from", "are", "was", "were",
        "have", "has", "had", "will", "shall", "would", "should", "can", "could",
        "may", "might", "not", "but", "about", "into", "over", "under", "between",
        "such", "their", "there", "which", "while", "where", "what", "when", "who",
        "how", "your", "you", "our", "ours", "they", "them", "then", "than",
    }

    words = re.findall(r"[A-Za-z']+", text.lower())
    frequencies: dict[str, int] = {}
    for word in words:
        if word in stopwords:
            continue
        frequencies[word] = frequencies.get(word, 0) + 1

    sentence_scores: list[tuple[int, float]] = []
    for index, sentence in enumerate(sentences):
        tokens = re.findall(r"[A-Za-z']+", sentence.lower())
        if not tokens:
            continue
        score = sum(frequencies.get(token, 0) for token in tokens)
        sentence_scores.append((index, float(score)))

    top = sorted(sentence_scores, key=lambda item: item[1], reverse=True)[:sentence_count]
    top_indices = sorted(index for index, _score in top)
    selected = [sentences[i].strip() for i in top_indices if sentences[i].strip()]
    return format_bullets(selected)


def generate_summary_with_ollama(text: str) -> str:
    if not settings.OLLAMA_MODEL or not text.strip():
        return ""

    prompt = (
        "Summarize the content into clean, readable bullet points. "
        "If the document is a form, list key fields and required details. "
        "Avoid repeated lines, fluff, and broken words. "
        "Use 6-10 bullets max.\n\n"
        f"{text[:8000]}"
    )

    payload = {
        "model": settings.OLLAMA_MODEL,
        "prompt": prompt,
        "stream": False,
    }

    try:
        response = requests.post(
            f"{settings.OLLAMA_BASE_URL}/api/generate",
            json=payload,
            timeout=60,
        )
        response.raise_for_status()
        data = response.json()
        summary = data.get("response", "").strip()
        return clean_summary_text(summary)
    except requests.RequestException as exc:
        logger.warning("Ollama summary failed: %s", exc)
        return ""


def process_file(bucket: str, key: str) -> tuple[str, str]:
    extracted_text = extract_text_with_textract(bucket=bucket, key=key)
    key_phrases = extract_key_phrases_with_comprehend(extracted_text)

    summary = generate_summary_with_ollama(extracted_text)

    if not summary:
        summary = build_local_summary(extracted_text)

    if not summary:
        summary = build_fallback_summary(key_phrases, extracted_text)

    return extracted_text, summary


def clean_extracted_text(text: str) -> str:
    if not text:
        return ""

    lines = [line.strip() for line in text.splitlines()]
    cleaned = []
    seen = set()

    for line in lines:
        if not line:
            continue

        if len(line) <= 2:
            continue

        # Remove repeated single-letter sequences and excessive spacing.
        line = re.sub(r"\b([A-Z])\s+\1\s+\1(\s+\1)+\b", r"\1", line)
        line = re.sub(r"\s{2,}", " ", line)

        lower = line.lower()
        if lower in seen:
            continue
        seen.add(lower)
        cleaned.append(line)

    return "\n".join(cleaned).strip()


def clean_summary_text(summary: str) -> str:
    if not summary:
        return ""

    lines = [line.strip(" -\t") for line in summary.splitlines() if line.strip()]
    return format_bullets(lines)


def format_bullets(lines: list[str]) -> str:
    filtered = []
    seen = set()
    for line in lines:
        clean = re.sub(r"\s{2,}", " ", line.strip())
        if not clean:
            continue
        lower = clean.lower()
        if lower in seen:
            continue
        seen.add(lower)
        filtered.append(clean)

    return "\n".join([f"- {line}" for line in filtered])


def is_form_like(text: str) -> bool:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not lines:
        return False

    field_keywords = {"name", "roll", "email", "mobile", "signature", "date", "time", "department", "semester", "student"}
    field_hits = sum(1 for line in lines if any(word in line.lower() for word in field_keywords))
    colon_lines = sum(1 for line in lines if ":" in line)
    short_lines = sum(1 for line in lines if len(line.split()) <= 3)

    short_ratio = short_lines / max(len(lines), 1)
    field_ratio = (field_hits + colon_lines) / max(len(lines), 1)

    return field_ratio >= 0.25 and short_ratio >= 0.35


def build_form_summary(text: str) -> str:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    cleaned = []
    seen = set()

    for line in lines:
        normalized = re.sub(r"\s{2,}", " ", line)
        if len(normalized) > 80:
            continue
        lower = normalized.lower()
        if lower in seen:
            continue
        seen.add(lower)
        cleaned.append(normalized)

    header = cleaned[:2]
    fields = [line for line in cleaned if ":" in line or line.endswith("NO.")]
    fields = fields[:10]

    summary_lines = []
    if header:
        summary_lines.append("Form document")
        summary_lines.extend(header)
    if fields:
        summary_lines.append("Key fields:")
        summary_lines.extend(fields)

    if not summary_lines:
        summary_lines = cleaned[:8]

    return format_bullets(summary_lines)
