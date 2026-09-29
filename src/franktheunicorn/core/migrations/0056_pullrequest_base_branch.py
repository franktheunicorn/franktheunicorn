from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0055_alter_securityrecheckrun_kind"),
    ]

    operations = [
        migrations.AddField(
            model_name="pullrequest",
            name="base_branch",
            field=models.CharField(blank=True, default="", max_length=255),
        ),
    ]
