from django.conf import settings
from django.contrib import messages
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone

from .forms import UploadNoteForm
from .models import Note
from .services import AWSProcessingError, process_file, upload_to_s3, validate_file_extension


def upload_file(request):
	if request.method == "POST":
		form = UploadNoteForm(request.POST, request.FILES)
		if form.is_valid():
			uploaded_file = form.cleaned_data["file"]

			try:
				validate_file_extension(uploaded_file.name)
			except ValueError as exc:
				messages.error(request, str(exc))
				return render(request, "notes/upload.html", {"form": form})

			note = Note.objects.create(
				file=uploaded_file,
				original_filename=uploaded_file.name,
			)

			if not settings.AWS_S3_BUCKET:
				message = "AWS_S3_BUCKET is not configured. Please set it in your environment variables."
				note.summary = message
				note.save(update_fields=["summary"])
				messages.error(request, message)
				return redirect("notes:result", note_id=note.id)

			s3_key = f"notes/{timezone.now().strftime('%Y%m%d%H%M%S')}_{uploaded_file.name}"
			try:
				uploaded_file.seek(0)
				upload_to_s3(uploaded_file, s3_key)
				extracted_text, summary = process_file(settings.AWS_S3_BUCKET, s3_key)
			except AWSProcessingError as exc:
				note.summary = str(exc)
				note.save(update_fields=["summary"])
				messages.error(request, str(exc))
				return redirect("notes:result", note_id=note.id)

			note.s3_key = s3_key
			note.extracted_text = extracted_text
			note.summary = summary
			note.save(update_fields=["s3_key", "extracted_text", "summary"])

			return redirect("notes:result", note_id=note.id)
	else:
		form = UploadNoteForm()

	return render(request, "notes/upload.html", {"form": form})


def result(request, note_id: int):
	note = get_object_or_404(Note, id=note_id)
	return render(request, "notes/result.html", {"note": note})
