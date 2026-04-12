from django.contrib import admin

from .models import Note


@admin.register(Note)
class NoteAdmin(admin.ModelAdmin):
	list_display = ("id", "original_filename", "created_at")
	search_fields = ("original_filename", "s3_key")

# Register your models here.
