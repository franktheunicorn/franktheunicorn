"""Create SecurityReviewGuidance for review guidance from CVE-introducing commits."""

from django.db import migrations, models
from django.utils import timezone


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0075_introduced_scan"),
    ]

    operations = [
        migrations.CreateModel(
            name="SecurityReviewGuidance",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                ("guidance_text", models.TextField(blank=True, default="")),
                ("source_report_count", models.IntegerField(default=0)),
                ("source_commit_count", models.IntegerField(default=0)),
                ("is_active", models.BooleanField(default=True)),
                ("created_at", models.DateTimeField(default=timezone.now)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "project",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=models.CASCADE,
                        related_name="review_guidance",
                        to="core.project",
                    ),
                ),
            ],
            options={"ordering": ["-updated_at"]},
        ),
    ]
