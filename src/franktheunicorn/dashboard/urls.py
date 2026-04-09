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
    path("draft/<int:draft_id>/recall/", views.recall_draft, name="recall_draft"),
    path("pr/<int:pr_id>/post/", views.post_review, name="post_review"),
    # Agent feedback (v1.25)
    path("pr/<int:pr_id>/compose-feedback/", views.compose_feedback, name="compose_feedback"),
    path("pr/<int:pr_id>/send-feedback/", views.send_feedback, name="send_feedback"),
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
    # Merge queue (v2)
    path("merge-queue/", views.merge_queue_view, name="merge_queue"),
    path("pr/<int:pr_id>/merge/", views.merge_pr, name="merge_pr"),
    # Security report triage
    path("security/", views.security_report_list, name="security_list"),
    path("security/new/", views.security_report_create, name="security_create"),
    path(
        "security/<int:report_id>/",
        views.security_report_detail,
        name="security_detail",
    ),
    path(
        "security/<int:report_id>/triage/",
        views.security_report_triage,
        name="security_triage",
    ),
    path(
        "security/<int:report_id>/verdict/",
        views.security_report_verdict,
        name="security_verdict",
    ),
    path(
        "security/<int:report_id>/sandbox/",
        views.security_report_sandbox,
        name="security_sandbox",
    ),
    path(
        "security/<int:report_id>/cve-check/",
        views.security_report_cve_check,
        name="security_cve_check",
    ),
]
