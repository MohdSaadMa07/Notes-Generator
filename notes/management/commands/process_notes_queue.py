import json
import time

from django.core.management.base import BaseCommand
from django.conf import settings

from notes.services import get_aws_client, process_note_by_id, AWSProcessingError


class Command(BaseCommand):
    help = "Process notes from SQS queue."

    def add_arguments(self, parser):
        parser.add_argument("--sleep", type=int, default=5, help="Seconds to wait between polls")

    def handle(self, *args, **options):
        if not settings.SQS_QUEUE_URL:
            self.stderr.write("SQS_QUEUE_URL is not configured.")
            return

        sqs = get_aws_client("sqs")
        sleep_seconds = options["sleep"]

        while True:
            response = sqs.receive_message(
                QueueUrl=settings.SQS_QUEUE_URL,
                MaxNumberOfMessages=5,
                WaitTimeSeconds=10,
            )

            messages = response.get("Messages", [])
            if not messages:
                time.sleep(sleep_seconds)
                continue

            for message in messages:
                receipt_handle = message.get("ReceiptHandle")
                try:
                    payload = json.loads(message.get("Body", "{}"))
                    note_id = int(payload.get("note_id"))
                    s3_key = payload.get("s3_key")
                    if not note_id or not s3_key:
                        raise AWSProcessingError("Invalid SQS message payload.")

                    process_note_by_id(note_id, s3_key)
                    sqs.delete_message(QueueUrl=settings.SQS_QUEUE_URL, ReceiptHandle=receipt_handle)
                except Exception as exc:  # noqa: BLE001
                    self.stderr.write(f"Failed to process message: {exc}")
