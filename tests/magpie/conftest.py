"""A tiny local stand-in for the apache/magpie repository.

The sync code fetches one commit by sha with a depth-one, blob-filtered fetch,
which GitHub serves without being asked. A local repository has to be told to
(``uploadpack.allowAnySHA1InWant``) and may refuse the filter, which exercises
the fallback. No test here touches the network.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from pathlib import Path

import pytest


def _git(repo: Path, *args: str) -> str:
    done = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        check=True,
        env={
            "GIT_AUTHOR_NAME": "t",
            "GIT_AUTHOR_EMAIL": "t@x",
            "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@x",
            "GIT_AUTHOR_DATE": "2026-10-11T12:00:00+00:00",
            "GIT_COMMITTER_DATE": "2026-10-11T12:00:00+00:00",
            "PATH": "/usr/bin:/bin:/usr/local/bin",
            "HOME": str(repo.parent),
        },
    )
    return done.stdout.strip()


@dataclass
class FakeUpstream:
    """A git repository with two commits and one tag, plus the shas of each."""

    path: Path
    first: str = ""
    second: str = ""
    files: dict[str, str] = field(default_factory=dict)

    def write(self, rel: str, text: str) -> None:
        target = self.path / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
        self.files[rel] = text

    def commit(self, message: str) -> str:
        _git(self.path, "add", "-A")
        _git(self.path, "commit", "-q", "-m", message)
        return _git(self.path, "rev-parse", "HEAD")


@pytest.fixture
def upstream(tmp_path: Path) -> FakeUpstream:
    repo = tmp_path / "upstream"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    # What GitHub does by default and a bare local repo does not.
    _git(repo, "config", "uploadpack.allowAnySHA1InWant", "true")
    _git(repo, "config", "uploadpack.allowFilter", "true")
    up = FakeUpstream(path=repo)
    up.write("NOTICE", "Apache Magpie\nCopyright 2026 The Apache Software Foundation\n")
    up.write(
        "plugins/magpie-pr-management/skills/code-review/criteria/testing.md",
        "<!-- SPDX-License-Identifier: Apache-2.0 -->\n# Testing\n\nA test that cannot fail is not a test.\n",
    )
    up.write(
        "plugins/magpie-security/skills/issue-triage/dispositions.md",
        "# Dispositions\n\nVALID / INVALID / INFO-ONLY\n",
    )
    up.first = up.commit("first")
    _git(repo, "tag", "-a", "0.1.0", "-m", "release")
    up.write(
        "plugins/magpie-pr-management/skills/code-review/criteria/testing.md",
        "<!-- SPDX-License-Identifier: Apache-2.0 -->\n# Testing\n\nA test that cannot fail is not a test. Neither is one that passes on the base branch.\n",
    )
    up.second = up.commit("second")
    return up
