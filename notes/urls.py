from django.urls import path

from . import views

app_name = "notes"

urlpatterns = [
    path("", views.upload_file, name="upload"),
    path("result/<int:note_id>/", views.result, name="result"),
]
