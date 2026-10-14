from typing import Any

from django.db import migrations, models

#: security.malicious_prompt.PREFILTER_AGENT, spelled out: a migration must not
#: import application code that may later change or move.
_PREFILTER_AGENT = "franktheunicorn-prefilter"


def _label_prefilter_reports(apps: Any, schema_editor: Any) -> None:
    """Reports frank's own malicious-prompt pre-filter filed are an agent's output.

    Matched on what ``file_security_report`` has always written: its reporter name
    and the ``[prefilter:pr-…]`` marker it puts in the notes. Without this, every
    one filed before the field existed would count as person-sent.
    """
    SecurityReport = apps.get_model("core", "SecurityReport")
    SecurityReport.objects.filter(
        source_agent="",
        reporter_name="franktheunicorn (auto pre-filter)",
        operator_notes__contains="[prefilter:pr-",
    ).update(source_agent=_PREFILTER_AGENT)


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0065_securityreport_cve_record_workflow_states"),
    ]

    operations = [
        migrations.AddField(
            model_name="securityreport",
            name="source_agent",
            field=models.CharField(blank=True, db_index=True, default="", max_length=100),
        ),
        migrations.AddField(
            model_name="securityrecheckrun",
            name="agent_scope",
            field=models.CharField(default="all", max_length=100),
        ),
        migrations.RunPython(_label_prefilter_reports, migrations.RunPython.noop),
    ]
