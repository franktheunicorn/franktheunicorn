from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0086_ci_failure_queue"),
    ]

    operations = [
        migrations.AddField(
            model_name="pullrequest",
            name="merge_intent_by_association",
            field=models.CharField(blank=True, default="", max_length=32),
        ),
    ]
