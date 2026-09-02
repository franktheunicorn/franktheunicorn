"""Stamp existing recheck verdicts as the cloud agent's.

``recheck_method`` arrived in 0050 alongside the git sweep, which writes
``"git"``. Every verdict that predates it came from the cloud agent, so leaving
them blank makes ``""`` mean both "never checked" and "the agent answered" —
which is the one distinction the column was added to preserve.
"""

from typing import Any

from django.db import migrations


def _stamp_agent(apps: Any, schema_editor: Any) -> None:
    apps.get_model("core", "SecurityReport").objects.filter(recheck_method="").exclude(
        recheck_status=""
    ).update(recheck_method="agent")


def _unstamp(apps: Any, schema_editor: Any) -> None:
    apps.get_model("core", "SecurityReport").objects.filter(recheck_method="agent").update(
        recheck_method=""
    )


class Migration(migrations.Migration):
    dependencies = [("core", "0050_branch_match_and_recheck_method")]

    operations = [migrations.RunPython(_stamp_agent, _unstamp)]
