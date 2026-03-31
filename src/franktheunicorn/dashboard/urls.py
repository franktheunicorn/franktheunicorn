"""URL routing for the dashboard app."""

from django.urls import path

from franktheunicorn.dashboard import views

app_name = "dashboard"

urlpatterns = [
    path("", views.index, name="index"),
    path("pr/<int:pr_id>/", views.pr_detail, name="pr_detail"),
    path("set-workspace/", views.set_workspace, name="set_workspace"),
    # Finding actions (htmx)
    path("draft/<int:draft_id>/approve/", views.approve_draft, name="approve_draft"),
    path("draft/<int:draft_id>/reject/", views.reject_draft, name="reject_draft"),
    path("draft/<int:draft_id>/edit/", views.edit_draft, name="edit_draft"),
    path("pr/<int:pr_id>/post/", views.post_review, name="post_review"),
    # Anti-pattern manager
    path("anti-patterns/", views.anti_pattern_list, name="anti_patterns"),
    path("anti-patterns/create/", views.anti_pattern_create, name="anti_pattern_create"),
    path(
        "anti-patterns/<int:ap_id>/delete/", views.anti_pattern_delete, name="anti_pattern_delete"
    ),
    path(
        "anti-patterns/<int:ap_id>/toggle/", views.anti_pattern_toggle, name="anti_pattern_toggle"
    ),
    # Stats
    path("stats/", views.stats, name="stats"),
]
