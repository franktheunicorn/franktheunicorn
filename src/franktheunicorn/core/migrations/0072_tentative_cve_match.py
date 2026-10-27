"""TentativeCveMatch: agent-matched links from pasted CVE ids to reports.

The operator pastes their currently-assigned-but-not-public CVEs and an agent
tentatively ties each to a SecurityReport already in the backlog. The link is
tentative until the operator confirms (which writes the CVE id onto the
report's ``matched_cve_id``) or rejects it. Upserted by (project, cve_id) so
re-pasting an updated list is an update, not a duplicate.
"""

import django.db.models.deletion
import django.utils.timezone
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0071_securityreport_exploit_verification"),
    ]

    operations = [
        migrations.CreateModel(
            name="TentativeCveMatch",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True, primary_key=True, serialize=False, verbose_name="ID"
                    ),
                ),
                ("cve_id", models.CharField(max_length=50)),
                ("cve_description", models.TextField(blank=True, default="")),
                (
                    "confidence",
                    models.CharField(
                        blank=True,
                        choices=[
                            ("", "Not attempted"),
                            ("high", "High"),
                            ("medium", "Medium"),
                            ("low", "Low"),
                            ("none", "No match"),
                        ],
                        default="",
                        max_length=10,
                    ),
                ),
                ("reasoning", models.TextField(blank=True, default="")),
                (
                    "status",
                    models.CharField(
                        choices=[
                            ("pending", "Pending"),
                            ("tentative", "Tentative"),
                            ("no_match", "No match"),
                            ("confirmed", "Confirmed"),
                            ("rejected", "Rejected"),
                        ],
                        default="pending",
                        max_length=10,
                    ),
                ),
                ("matched_at", models.DateTimeField(blank=True, null=True)),
                ("created_at", models.DateTimeField(default=django.utils.timezone.now)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "project",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="cve_matches",
                        to="core.project",
                    ),
                ),
                (
                    "report",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="tentative_cve_matches",
                        to="core.securityreport",
                    ),
                ),
            ],
            options={"ordering": ["-created_at"]},
        ),
        migrations.AddConstraint(
            model_name="tentativecvematch",
            constraint=models.UniqueConstraint(
                fields=("project", "cve_id"), name="unique_tentative_cve_match_per_project"
            ),
        ),
        migrations.AlterField(
            model_name="workercommand",
            name="command",
            field=models.CharField(
                choices=[
                    ("run_dual_tests", "Run differential tests"),
                    ("run_security_sandbox", "Run security report sandbox"),
                    ("poll_security_rechecks", "Wait on batch security rechecks"),
                    ("run_security_triage", "Run LLM triage on security report"),
                    ("run_agents", "Force-run review agents"),
                    ("check_fix_landed", "Check whether one report's fix landed upstream"),
                    ("check_fixes_landed", "Sweep: check whether known fixes landed upstream"),
                    ("scan_zombie_prs", "Sweep: closed-unmerged PRs whose JIRA is still open"),
                    ("assess_zombie_pr", "Re-run the zombie assessment on one PR"),
                    (
                        "match_cves_to_reports",
                        "Tentatively match pasted CVE ids to security reports",
                    ),
                ],
                max_length=50,
            ),
        ),
    ]
