from django.db import models


class Note(models.Model):
	file = models.FileField(upload_to="uploads/")
	original_filename = models.CharField(max_length=255)
	s3_key = models.CharField(max_length=512, blank=True)
	extracted_text = models.TextField(blank=True)
	summary = models.TextField(blank=True)
	created_at = models.DateTimeField(auto_now_add=True)

	def __str__(self) -> str:
		return f"Note {self.id} - {self.original_filename}"
