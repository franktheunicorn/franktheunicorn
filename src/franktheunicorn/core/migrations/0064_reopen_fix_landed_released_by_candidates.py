import re
from typing import Any

from django.db import migrations

# A frozen copy of security.release_tags' default rule (no prefix): a
# migration must not change meaning when that module does.
_FINAL_RE = re.compile(
    r"^v?(\d+(?:\.\d+)+)(?:[.-]?post(\d+))?(?:-incubating)?$", re.IGNORECASE
)


def reopen_candidate_releases(apps: Any, schema_editor: Any) -> None:
    """Downgrade ``released`` verdicts that no final release tag supports.

    Before this migration any tag containing the fix commit, a release
    candidate included, answered ``released``, and ``released`` is terminal:
    the sweep never looked at those rows again. Rows whose recorded tags hold
    no final release become ``merged``: a tag containing the commit proves at
    least that, by the new rule too. ``merged`` is not terminal, so the next
    sweep answers them under the project's own tag rule (``released`` again
    for a ``release_tag_prefix`` or ``release_tag_pattern`` that makes their
    tags count). Downgraded rather than emptied: an empty status would let an
    indeterminate non-answer replace a verdict git already proved. Only git
    verdicts: an agent's ``released`` was its own call.
    """
    SecurityReport = apps.get_model("core", "SecurityReport")
    for report in SecurityReport.objects.filter(
        fix_landed_status="released", fix_landed_method="git"
    ).only("pk", "fix_landed_detail"):
        detail = report.fix_landed_detail if isinstance(report.fix_landed_detail, dict) else {}
        tags = [str(tag) for tag in detail.get("tags", []) or []]
        if any("/" not in tag and _FINAL_RE.match(tag.strip()) for tag in tags):
            continue
        SecurityReport.objects.filter(pk=report.pk).update(
            fix_landed_status="merged",
            fix_landed_detail={
                **{k: v for k, v in detail.items() if k not in ("tags", "tag_count")},
                "other_tags": tags,
                "note": (
                    "downgraded from released: an earlier check counted a tag that is not "
                    "a final release as one; the next check re-reads the tags"
                ),
            },
        )


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0063_pullrequest_author_association"),
    ]

    operations = [
        migrations.RunPython(reopen_candidate_releases, migrations.RunPython.noop),
    ]
