from django import forms


class UploadNoteForm(forms.Form):
    file = forms.FileField(
        label="Select PDF or image",
        help_text="Supported: PDF, PNG, JPG, JPEG, TIFF",
    )
