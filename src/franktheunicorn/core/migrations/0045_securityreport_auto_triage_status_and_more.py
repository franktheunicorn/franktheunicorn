from typing import Any

from django.db import migrations, models


def _restage_legacy_machine_expected_behavior(apps: Any, schema_editor: Any) -> None:
    """Reconcile reports the old machine filed straight into ``status``.

    Before the staging design, triage wrote ``status="expected-behavior"``
    directly for reports it assessed as documented behaviour. Under the new
    design the machine writes its verdict to ``auto_triage_status`` and leaves
    ``status`` to the operator — so those legacy rows are now indistinguishable
    from operator rulings, and the bulk re-triage loop (which skips
    operator-ruled reports) can't reach them. They were never operator rulings,
    so re-stage them: move the verdict into ``auto_triage_status`` and put the
    report back in the ``new`` queue for the operator to re-confirm or change.

    Only rows with no operator notes and no CVE are touched — a row with notes
    or a CVE was handled by a person, and a person's ruling stays.
    """
    SecurityReport = apps.get_model("core", "SecurityReport")
    SecurityReport.objects.filter(
        status="expected-behavior",
        operator_notes="",
        matched_cve_id="",
    ).update(status="new", auto_triage_status="expected-behavior")


def _unstage_legacy_machine_expected_behavior(apps: Any, schema_editor: Any) -> None:
    """Reverse: put the verdict back into status and clear the stage.

    Only reverses rows we moved — those sitting in ``new`` with
    ``auto_triage_status="expected-behavior"`` and no notes/CVE. A row that
    arrived at that state through the new triage path also matches, but the
    reverse is only ever run on a rollback to the pre-migration schema, where
    the new path didn't exist yet, so the collision is theoretical.
    """
    SecurityReport = apps.get_model("core", "SecurityReport")
    SecurityReport.objects.filter(
        status="new",
        auto_triage_status="expected-behavior",
        operator_notes="",
        matched_cve_id="",
    ).update(status="expected-behavior", auto_triage_status="")


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0044_securityreport_introduced_at_and_more"),
    ]

    operations = [
        migrations.AddField(
            model_name="securityreport",
            name="auto_triage_status",
            field=models.CharField(blank=True, default="", max_length=20),
        ),
        migrations.AddField(
            model_name="securityreport",
            name="affected_versions",
            field=models.TextField(blank=True, default=""),
        ),
        migrations.RunPython(
            _restage_legacy_machine_expected_behavior,
            _unstage_legacy_machine_expected_behavior,
        ),
    ]
