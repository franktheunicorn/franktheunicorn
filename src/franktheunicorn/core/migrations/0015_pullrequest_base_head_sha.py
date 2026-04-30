from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0014_add_llm_backend_fallback"),
    ]

    operations = [
        migrations.AddField(
            model_name="pullrequest",
            name="base_sha",
            field=models.CharField(blank=True, default="", max_length=64),
        ),
        migrations.AddField(
            model_name="pullrequest",
            name="head_sha",
            field=models.CharField(blank=True, default="", max_length=64),
        ),
    ]
