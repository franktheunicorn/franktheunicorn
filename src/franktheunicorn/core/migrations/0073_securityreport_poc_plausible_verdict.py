"""SecurityReport.poc_plausible_verdict: the model's explicit Y/N/M on real.

The triage analysis step used to write only ``poc_plausible`` (a nullable bool),
so a deliberate "maybe" was indistinguishable from "the model didn't say".
This adds the explicit tristate and backfills it from the bool so the list can
sort real-first and the badge can show M on reports already triaged.
"""

from typing import Any

from django.db import migrations, models


def backfill_verdict_from_bool(apps: Any, schema_editor: Any) -> None:
    SecurityReport = apps.get_model("core", "SecurityReport")
    SecurityReport.objects.filter(poc_plausible=True).update(poc_plausible_verdict="yes")
    SecurityReport.objects.filter(poc_plausible=False).update(poc_plausible_verdict="no")


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0072_tentative_cve_match"),
    ]

    operations = [
        migrations.AddField(
            model_name="securityreport",
            name="poc_plausible_verdict",
            field=models.CharField(
                blank=True,
                choices=[
                    ("", "Not assessed"),
                    ("yes", "Yes — real"),
                    ("no", "No — not real"),
                    ("maybe", "Maybe"),
                ],
                db_index=True,
                default="",
                max_length=10,
            ),
        ),
        migrations.RunPython(backfill_verdict_from_bool, migrations.RunPython.noop),
    ]
