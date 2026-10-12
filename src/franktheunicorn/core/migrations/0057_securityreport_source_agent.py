from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0056_pullrequest_base_branch"),
    ]

    operations = [
        migrations.AddField(
            model_name="securityreport",
            name="source_agent",
            field=models.CharField(blank=True, db_index=True, default="", max_length=100),
        ),
    ]
