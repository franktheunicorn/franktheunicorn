"""Rename ReviewDraft.github_comment_id to forge_comment_id.

The field originally tracked GitHub review-comment IDs only. With the
multi-forge backend abstraction, it now stores whatever ID the source
forge returns (GitHub review-comment ID, Gitea pull-comment ID, GitLab
note ID). The rename makes the model truthful.
"""

from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0016_agentvibe"),
    ]

    operations = [
        migrations.RenameField(
            model_name="reviewdraft",
            old_name="github_comment_id",
            new_name="forge_comment_id",
        ),
    ]
