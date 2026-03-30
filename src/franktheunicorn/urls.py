from django.urls import include, path

urlpatterns = [
    path("", include("franktheunicorn.dashboard.urls")),
]
