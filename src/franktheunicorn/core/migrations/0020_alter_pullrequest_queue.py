from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0019_reviewdraft_diff_source"),
    ]

    operations = [
        migrations.AlterField(
            model_name="pullrequest",
            name="queue",
            field=models.CharField(
                choices=[
                    ("review", "Review"),
                    ("ai-generated", "AI-Generated"),
                    ("new-contributor", "New Contributor"),
                    ("consider-closing", "Consider Closing"),
                    ("needs-triage", "Needs Triage"),
                    ("your-prs", "Your PRs"),
                    ("wip", "WIP"),
                ],
                default="review",
                max_length=50,
            ),
        ),
    ]
