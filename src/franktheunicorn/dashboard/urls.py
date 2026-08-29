"""URL routing for the dashboard app."""

from django.urls import path

from franktheunicorn.dashboard import views

app_name = "dashboard"

urlpatterns = [
    path("", views.index, name="index"),
    path("lookup/", views.lookup_pr, name="lookup_pr"),
    path(
        "pr/github/<str:owner>/<str:repo>/<int:pr_number>/", views.pr_by_coords, name="pr_by_coords"
    ),
    path("pr/<int:pr_id>/", views.pr_detail, name="pr_detail"),
    path("set-workspace/", views.set_workspace, name="set_workspace"),
    # Finding actions (htmx)
    path("draft/<int:draft_id>/approve/", views.approve_draft, name="approve_draft"),
    path("draft/<int:draft_id>/reject/", views.reject_draft, name="reject_draft"),
    path("draft/<int:draft_id>/edit/", views.edit_draft, name="edit_draft"),
    path("draft/<int:draft_id>/recall/", views.recall_draft, name="recall_draft"),
    path("pr/<int:pr_id>/post/", views.post_review, name="post_review"),
    path("pr/<int:pr_id>/run-agents/", views.run_agents, name="run_agents"),
    path(
        "pr/<int:pr_id>/regenerate-findings/",
        views.regenerate_findings,
        name="regenerate_findings",
    ),
    path("pr/<int:pr_id>/run-dual-tests/", views.run_dual_tests, name="run_dual_tests"),
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
    # Bulk import: a zip of report files, same importer as import_security_zip.
    path("security/upload/", views.security_report_upload, name="security_upload"),
    # The undo for one: delete every report that came from a named archive.
    path("security/drop-archive/", views.security_archive_drop, name="security_archive_drop"),
    # Round-trip through a shared spreadsheet, for a backlog other people rule on.
    # ".csv" in the path so a browser and a shell both name the download sensibly.
    path("security/export.csv", views.security_report_export_csv, name="security_export_csv"),
    path("security/import-csv/", views.security_report_import_csv, name="security_import_csv"),
    # Read-only transparency: everything the email scanner has looked at.
    path("security/email-activity/", views.email_activity, name="email_activity"),
    # Learned triage guidance overview (iterative learning loop).
    path("security/guidance/", views.security_guidance_list, name="security_guidance"),
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
        "security/<int:report_id>/feedback/",
        views.security_report_feedback,
        name="security_feedback",
    ),
    path(
        "security/<int:report_id>/sandbox/",
        views.security_report_sandbox,
        name="security_sandbox",
    ),
    # Deep verification: an agent reads the code, per active branch, and says
    # whether the reported vulnerability is actually there.
    path(
        "security/<int:report_id>/verify/",
        views.security_report_verify,
        name="security_verify",
    ),
    path(
        "security/<int:report_id>/map-versions/",
        views.security_report_map_versions,
        name="security_map_versions",
    ),
    path(
        "security/<int:report_id>/find-introduction/",
        views.security_report_find_introduction,
        name="security_find_introduction",
    ),
    path(
        "security/<int:report_id>/cve-check/",
        views.security_report_cve_check,
        name="security_cve_check",
    ),
]
