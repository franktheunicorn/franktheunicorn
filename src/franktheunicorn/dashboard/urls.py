from django.urls import path

from franktheunicorn.dashboard import views

app_name = "dashboard"

urlpatterns = [
    path("", views.index, name="index"),
    path("pr/<int:pr_id>/", views.pr_detail, name="pr_detail"),
]
