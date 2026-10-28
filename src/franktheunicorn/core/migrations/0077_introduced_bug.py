"""Create IntroducedBug for non-security bugs found by the introduced-CVE scan."""

from django.db import migrations, models
from django.utils import timezone
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0076_review_guidance"),
    ]

    operations = [
        migrations.CreateModel(
            name="IntroducedBug",
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
                ("commit", models.CharField(blank=True, default="", max_length=40)),
                ("file_path", models.CharField(blank=True, default="", max_length=500)),
                ("line_start", models.IntegerField(blank=True, null=True)),
                ("line_end", models.IntegerField(blank=True, null=True)),
                ("category", models.CharField(default="other", max_length=30)),
                ("severity", models.CharField(default="minor", max_length=20)),
                ("title", models.CharField(max_length=500)),
                ("description", models.TextField(blank=True, default="")),
                ("proposed_patch", models.TextField(blank=True, default="")),
                ("status", models.CharField(default="new", max_length=20)),
                ("created_at", models.DateTimeField(default=timezone.now)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "project",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="introduced_bugs",
                        to="core.project",
                    ),
                ),
                (
                    "scan_run",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="bugs",
                        to="core.introducedscanrun",
                    ),
                ),
            ],
        ),
        migrations.AddField(
            model_name="introducedscanrun",
            name="bug_count",
            field=models.IntegerField(default=0),
        ),
    ]

