"""Add the IntroducedScanRun table and the introduced-scan report source.

The scheduled introduced-CVE scan files its findings as SecurityReport rows
with source="introduced-scan" (a new SOURCE_CHOICES value) and records one
IntroducedScanRun per project per run for scheduling and audit.
"""

from django.db import migrations, models
from django.utils import timezone


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0074_securityreport_patch_evaluation"),
    ]

    operations = [
        migrations.AlterField(
            model_name="securityreport",
            name="source",
            field=models.CharField(
                choices=[
                    ("paste", "Pasted"),
                    ("email", "Email"),
                    ("zip", "Zip import"),
                    ("introduced-scan", "Introduced-CVE scan"),
                ],
                default="paste",
                max_length=20,
            ),
        ),
        migrations.CreateModel(
            name="IntroducedScanRun",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True, primary_key=True, serialize=False, verbose_name="ID"
                    ),
                ),
                ("started_at", models.DateTimeField(default=timezone.now)),
                ("finished_at", models.DateTimeField(blank=True, null=True)),
                ("cadence", models.CharField(default="daily", max_length=10)),
                ("commit_range", models.CharField(blank=True, default="", max_length=200)),
                ("finding_count", models.IntegerField(default=0)),
                ("verified_count", models.IntegerField(default=0)),
                ("log", models.TextField(blank=True, default="")),
                ("error", models.TextField(blank=True, default="")),
                (
                    "project",
                    models.ForeignKey(
                        on_delete=models.deletion.CASCADE,
                        related_name="introduced_scan_runs",
                        to="core.project",
                    ),
                ),
            ],
        ),
    ]
