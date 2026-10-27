"""SecurityReport.patch_eval_*: the patch evaluator's verdict and draft ticket.

Sibling of the exploit-verification fields. A coding agent reads the proposed
patch and judges it on its merits (good / questionable / bad), then drafts a
JIRA-style ticket for the non-security version of the change. For a report ruled
"not a security issue" whose patch is still a decent cleanup — the security path
doesn't apply, so the operator needs the patch reviewed and a ticket to file the
normal way.
"""

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0073_securityreport_poc_plausible_verdict"),
    ]

    operations = [
        migrations.AddField(
            model_name="securityreport",
            name="patch_eval_requested",
            field=models.BooleanField(default=False),
        ),
        migrations.AddField(
            model_name="securityreport",
            name="patch_eval_verdict",
            field=models.CharField(blank=True, default="", max_length=20),
        ),
        migrations.AddField(
            model_name="securityreport",
            name="patch_eval_result",
            field=models.TextField(blank=True, default=""),
        ),
        migrations.AddField(
            model_name="securityreport",
            name="patch_eval_checked_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
    ]
