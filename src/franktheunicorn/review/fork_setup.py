"""Fork setup — PRs whose CI never ran, and the setting that would make it run.

The "Fork Setup" queue. Some PRs are not failing CI; they are not running it.
Two reasons, one on each side:

* **The author's fork.** Apache Spark runs a PR's tests in the contributor's
  fork (``.github/workflows/build_main.yml``, on push) and mirrors that run
  onto the PR as one ``Build`` check (``update_build_status.js``). When the
  fork has no usable run for the head commit, the check is written as
  ``action_required`` guidance instead — "PR build setup requires attention" —
  with a generic two-item list: enable Actions, don't use ``master``. Which of
  those it is, or whether it is something else, is answerable by reading the
  fork: its workflow's ``state`` (``disabled_fork`` until somebody presses
  "I understand my workflows"), the PR's branch name, the fork's runs for the
  head, and whether the branch has the workflow file at all (a branch cut from
  an old ``master`` does not). That is what this module does, so the comment
  names the one setting and links the exact page.
* **Upstream.** GitHub holds ``pull_request`` workflow runs from a first-time
  contributor's fork until a maintainer approves them ("1 workflow awaiting
  approval"). That is the operator's button, not the author's comment — and a
  run waits for approval *because* its workflow file comes from a stranger's
  branch, so the panel says when the file is not on the base branch at all.

Nothing here posts, approves or decides on its own. The scan finds and
diagnoses (forge reads, no model); the comment goes out on Post after a live
re-read; the approval goes out on Approve after a live re-list. A PR whose red
check is fork-setup guidance is left out of the CI Failures tab
(``ForkSetupAssessment.covers_q``): there is no log there to explain.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from django.db.models import F
from django.utils import timezone

from franktheunicorn.core.models import ForkSetupAssessment, PullRequest, WorkerCommand

if TYPE_CHECKING:
    from franktheunicorn.backends.base import ForgeClient
    from franktheunicorn.config.models import ForkSetupConfig, OperatorConfig, ProjectConfig
    from franktheunicorn.core.models import Project

logger = logging.getLogger(__name__)

#: The worker commands: one PR (interactive), and the sweep (bulk).
COMMAND = "diagnose_fork_setup"
SCAN_COMMAND = "scan_fork_setup"

#: Spark's fork-side PR build: runs on push to any branch but a fork's master.
SPARK_BUILD_WORKFLOW = ".github/workflows/build_main.yml"
SPARK_CONTRIBUTING = "https://spark.apache.org/contributing.html#pull-request"

#: What can be wrong, and what the panel calls it.
KINDS: dict[str, str] = {
    "awaiting-approval": "Workflow runs awaiting maintainer approval",
    "actions-not-enabled": "GitHub Actions not enabled on the fork",
    "actions-unreadable": "GitHub Actions look disabled on the fork",
    "workflow-disabled": "Build workflow disabled on the fork",
    "from-fork-master": "PR opened from the fork's master branch",
    "fork-deleted": "The fork was deleted",
    "branch-too-old": "Branch predates the build workflow",
    "build-skipped": "The fork's build was skipped",
    "startup-failure": "The fork's build could not start",
    "no-run": "No build in the fork for this commit",
    "run-not-linked": "Build exists; the PR check has not caught up",
    "check-needs-action": "A check needs action",
}

#: Who has to do something: the author (a comment), a maintainer (a button),
#: or nobody (the mirror check will catch up on its own).
WHO = ("author", "maintainer", "none")

_DISABLED_WORKFLOW_STATES = ("disabled_manually", "disabled_inactivity")

# The workflow files a PR touches are the ones to read before approving its runs.
_WORKFLOW_DIR = ".github/workflows/"

#: Waiting runs shown per PR, and distinct workflow files checked against base.
_MAX_RUNS = 10
_MAX_BASE_CHECKS = 4


# --------------------------------------------------------------------------
# The pure half: facts in, problems out, a comment from the problems
# --------------------------------------------------------------------------


@dataclass
class Problem:
    """One reason CI did not run, whose it is, and what to do about it."""

    kind: str
    who: str
    summary: str
    remediation: str = ""
    evidence: str = ""
    url: str = ""

    def to_dict(self) -> dict[str, str]:
        return {
            "kind": self.kind,
            "who": self.who,
            "summary": self.summary,
            "remediation": self.remediation,
            "evidence": self.evidence,
            "url": self.url,
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, object]) -> Problem:
        kind = str(raw.get("kind") or "check-needs-action")
        who = str(raw.get("who") or "author")
        return cls(
            kind=kind if kind in KINDS else "check-needs-action",
            who=who if who in WHO else "author",
            summary=str(raw.get("summary") or ""),
            remediation=str(raw.get("remediation") or ""),
            evidence=str(raw.get("evidence") or ""),
            url=str(raw.get("url") or ""),
        )

    @property
    def label(self) -> str:
        return KINDS.get(self.kind, KINDS["check-needs-action"])


def problems_from(raw: object) -> list[Problem]:
    """``ForkSetupAssessment.problems`` back into :class:`Problem` objects."""
    if not isinstance(raw, list):
        return []
    return [Problem.from_dict(item) for item in raw if isinstance(item, dict)]


@dataclass
class SetupCheck:
    """A check on the PR's head that concluded ``action_required``."""

    name: str
    url: str = ""
    title: str = ""
    summary: str = ""


@dataclass
class Facts:
    """Everything the forge said that bears on "why didn't CI run".

    ``workflows`` is ``None`` when the fork refused to list them (403/404) and
    ``fork_read`` is False when it was never asked (not needed, or the forge
    cannot say). ``workflow_at_head`` is ``None`` when not asked or unknown.
    """

    upstream: str
    sha: str
    branch: str = ""
    base_branch: str = ""
    #: ``owner/repo`` of the PR's head repository; ``""`` when unknown.
    fork: str = ""
    #: The forge said the head repository is gone (``head.repo`` is null).
    fork_deleted: bool = False
    jira: str = ""
    setup_checks: list[SetupCheck] = field(default_factory=list)
    awaiting_runs: list[dict[str, Any]] = field(default_factory=list)
    fork_read: bool = False
    workflows: list[dict[str, Any]] | None = None
    runs: list[dict[str, Any]] = field(default_factory=list)
    workflow_at_head: bool | None = None


def _fill(text: str, facts: Facts, build_workflow: str, run_url: str = "") -> str:
    """Plain replacement, not ``str.format``: shell snippets carry braces."""
    workflow_file = build_workflow.rsplit("/", 1)[-1] or "the build workflow"
    new_branch = (facts.jira or "my-change").replace(" ", "-")
    return (
        text.replace("{fork}", facts.fork or "your fork")
        .replace("{branch}", facts.branch or "your PR branch")
        .replace("{base_branch}", facts.base_branch or "master")
        .replace("{workflow}", workflow_file)
        .replace("{new_branch}", new_branch)
        .replace("{run_url}", run_url or "the run's page")
        .replace("{upstream}", facts.upstream)
    )


_PUSH_TO_START = (
    "Then push a commit to `{branch}` to start the build (an empty one is fine: "
    '`git commit --allow-empty -m "Trigger CI" && git push`).'
)

# Fork-side problems. Every UI label quoted is the one GitHub shows; every
# Spark claim is what build_main.yml / update_build_status.js actually do.
_TEXT: dict[str, tuple[str, str]] = {
    "fork-deleted": (
        "The repository this PR was opened from no longer exists, so no build can run for "
        "it and no new commit can be pushed to it.",
        "Fork `{upstream}` again, push your branch to the new fork, and open a new PR from "
        "it (link this one so the review history is easy to find).",
    ),
    "from-fork-master": (
        "This PR comes from your fork's `master` branch. The PR build skips pushes to a "
        "fork's `master` (that branch is kept for syncing with `{upstream}`), so it never "
        "runs for this PR.",
        "GitHub can't change a PR's source branch, so: make a branch from it "
        "(`git checkout -b {new_branch} && git push origin {new_branch}`), open a new PR "
        "from that branch, and close this one with a link to the new one. Afterwards the "
        "**Sync fork** button on your fork resets `master` to match `{upstream}`.",
    ),
    "actions-not-enabled": (
        "GitHub Actions is not enabled on your fork, `{fork}`: GitHub turns workflows off "
        "on a new fork until its owner says otherwise. The PR's tests run in your fork, so "
        "nothing has run for this PR.",
        "Open https://github.com/{fork}/actions and press **I understand my workflows, go "
        "ahead and enable them**. " + _PUSH_TO_START,
    ),
    "actions-unreadable": (
        "GitHub would not list the Actions workflows on your fork, `{fork}`, which usually "
        "means Actions are disabled for it in the repository settings. The PR's tests run in "
        "your fork, so nothing has run for this PR.",
        "In https://github.com/{fork}/settings/actions, under **Actions permissions**, choose "
        "**Allow all actions and reusable workflows** and save. " + _PUSH_TO_START,
    ),
    "workflow-disabled": (
        "The `{workflow}` workflow is disabled on your fork, `{fork}`, so pushing to the PR "
        "branch does not start a build.",
        "Open https://github.com/{fork}/actions/workflows/{workflow} and press **Enable "
        "workflow**. " + _PUSH_TO_START,
    ),
    "branch-too-old": (
        "Your branch does not have `{workflow}`: it was cut from an older `{base_branch}`, so "
        "the push that should have started the PR build had no workflow to run.",
        "Rebase onto the latest `{base_branch}` and push: `git fetch upstream && git rebase "
        "upstream/{base_branch} && git push --force-with-lease origin {branch}` (with "
        "`upstream` pointing at https://github.com/{upstream}).",
    ),
    "build-skipped": (
        "Your fork's build for this commit was skipped ({run_url}). That happens on a push "
        "to the fork's `master`, or when the commit message carries a skip marker such as "
        "`[skip ci]`.",
        "Push a new commit to `{branch}` without a skip marker in its message.",
    ),
    "startup-failure": (
        "Your fork's build for this commit failed before any job started ({run_url}). On a "
        "fork that is usually the Actions permissions setting: the workflow uses actions "
        "from outside your account, and the fork only allows some.",
        "In https://github.com/{fork}/settings/actions, under **Actions permissions**, choose "
        "**Allow all actions and reusable workflows** and save; then press **Re-run all "
        "jobs** on {run_url}.",
    ),
    "no-run": (
        "There is no build in your fork, `{fork}`, for this commit, so the PR has nothing "
        "to report.",
        "Check that Actions are enabled on the fork (https://github.com/{fork}/actions; on a "
        "new fork there is an **I understand my workflows, go ahead and enable them** "
        "button). " + _PUSH_TO_START,
    ),
    "run-not-linked": (
        "Your fork's build for this commit exists ({run_url}); the PR's check has not been "
        "linked to it yet. That catches up on its own.",
        "",
    ),
}


def _problem(
    kind: str, facts: Facts, build_workflow: str, *, run: Mapping[str, Any] | None = None
) -> Problem:
    run_url = str((run or {}).get("url") or "")
    summary, remediation = _TEXT[kind]
    return Problem(
        kind=kind,
        who="none" if kind == "run-not-linked" else "author",
        summary=_fill(summary, facts, build_workflow, run_url),
        remediation=_fill(remediation, facts, build_workflow, run_url),
        url=run_url or (f"https://github.com/{facts.fork}/actions" if facts.fork else ""),
    )


def fork_problem(facts: Facts, build_workflow: str, *, spark: bool) -> Problem | None:
    """Why the fork has no usable build for the head, first answer wins.

    ``None`` when the fork is the upstream repository itself (a maintainer's
    branch): there is no fork to fix, and the check's own text is the answer.
    """
    if facts.fork_deleted:
        return _problem("fork-deleted", facts, build_workflow)
    if facts.fork and facts.fork.lower() == facts.upstream.lower():
        return None
    if spark and facts.branch == "master":
        return _problem("from-fork-master", facts, build_workflow)
    if not facts.fork or not facts.fork_read:
        return _problem("no-run", facts, build_workflow)
    if facts.workflows is None:
        return _problem("actions-unreadable", facts, build_workflow)
    workflow = next((w for w in facts.workflows if w.get("path") == build_workflow), None)
    state = str((workflow or {}).get("state") or "")
    if state == "disabled_fork":
        return _problem("actions-not-enabled", facts, build_workflow)
    if state in _DISABLED_WORKFLOW_STATES:
        return _problem("workflow-disabled", facts, build_workflow)
    builds = [
        r for r in facts.runs if r.get("path") == build_workflow and r.get("branch") == facts.branch
    ]
    if builds:
        newest = max(builds, key=lambda r: int(r.get("id") or 0))
        conclusion = str(newest.get("conclusion") or "")
        if conclusion == "skipped":
            return _problem("build-skipped", facts, build_workflow, run=newest)
        if conclusion == "startup_failure":
            return _problem("startup-failure", facts, build_workflow, run=newest)
        return _problem("run-not-linked", facts, build_workflow, run=newest)
    if facts.workflow_at_head is False:
        return _problem("branch-too-old", facts, build_workflow)
    return _problem("no-run", facts, build_workflow)


def _check_problem(check: SetupCheck) -> Problem:
    """A project we cannot read the fork for: say what the check said."""
    said = check.title.strip()
    return Problem(
        kind="check-needs-action",
        who="author",
        summary=(
            f"The `{check.name}` check needs action before CI can run"
            + (f": *{said}*." if said else ".")
        ),
        remediation=(
            "The check's page says what it needs"
            + (f": {check.url}" if check.url else "")
            + ". After fixing it, push a commit to the PR branch to start CI again."
        ),
        evidence=check.summary.strip()[:1_500],
        url=check.url,
    )


def _approval_problem(facts: Facts) -> Problem:
    runs = facts.awaiting_runs[:_MAX_RUNS]
    names = ", ".join(f"`{r.get('name') or r.get('path') or r.get('id')}`" for r in runs)
    off_base = [str(r.get("path")) for r in runs if r.get("on_base") is False]
    summary = (
        f"{len(facts.awaiting_runs)} workflow run(s) on `{facts.upstream}` wait for a "
        f"maintainer to approve them: {names}. GitHub holds runs from a first-time "
        "contributor's fork until someone with write access does."
    )
    if off_base:
        files = ", ".join(f"`{path}`" for path in off_base)
        verb = "is" if len(off_base) == 1 else "are"
        summary += (
            f" {files} {verb} not on `{facts.base_branch or 'the base branch'}`: the workflow "
            "comes from this PR's branch. Read it before approving; often the author only "
            "needs to rebase."
        )
    return Problem(
        kind="awaiting-approval",
        who="maintainer",
        summary=summary,
        remediation="Approve the runs (the button here, or **Approve workflows to run** on the PR).",
        url=str(runs[0].get("url") or "") if runs else "",
    )


def diagnose(facts: Facts, *, build_workflow: str, spark: bool) -> list[Problem]:
    """The problems these facts show: maintainer's first, then the author's.

    Pure. A setup check on a project whose fork-side workflow is known reads
    the fork's own state; otherwise the check's own text is the diagnosis.
    """
    problems: list[Problem] = []
    if facts.awaiting_runs:
        problems.append(_approval_problem(facts))
    if facts.setup_checks:
        found = fork_problem(facts, build_workflow, spark=spark) if build_workflow else None
        if found is not None:
            problems.append(found)
        else:
            problems.extend(_check_problem(check) for check in facts.setup_checks)
    return problems


def compose_comment(
    problems: Sequence[Problem],
    *,
    author: str,
    head_sha: str,
    base_branch: str,
    cfg: ForkSetupConfig,
) -> str:
    """The comment for the author: only the author's problems, between the
    project's own opening and closing lines. ``""`` when there is nothing for
    the author to do (an approval is the maintainer's; a lagging check is no one's).
    """
    from franktheunicorn.backends.mentions import escape_handles, normalize_allowlist

    theirs = [p for p in problems if p.who == "author"]
    if not theirs:
        return ""

    def template(text: str) -> str:
        return (
            text.replace("{author}", author)
            .replace("{sha}", head_sha[:7])
            .replace("{base_branch}", base_branch or "main")
        )

    sections: list[str] = []
    for index, problem in enumerate(theirs, start=1):
        head = f"**{index}. {problem.label}**" if len(theirs) > 1 else f"**{problem.label}**"
        block = [head, "", problem.summary.strip()]
        if problem.remediation.strip():
            block += ["", f"**What to do:** {problem.remediation.strip()}"]
        if problem.evidence.strip():
            quoted = "\n".join(f"> {line}" for line in problem.evidence.strip().splitlines())
            block += ["", quoted]
        sections.append("\n".join(block))
    body = "\n\n".join(
        part
        for part in (template(cfg.comment_intro), *sections, template(cfg.comment_outro))
        if part
    )
    return escape_handles(body, normalize_allowlist([author]))


def build_workflow_for(project_config: ProjectConfig) -> str:
    """The fork-side PR build workflow: configured, or the Spark profile's."""
    from franktheunicorn.review.ci_failure import uses_spark_profile

    configured = project_config.fork_setup.build_workflow.strip()
    if configured:
        return configured
    return SPARK_BUILD_WORKFLOW if uses_spark_profile(project_config) else ""


def touched_workflows(pr: PullRequest) -> list[str]:
    """The workflow files this PR changes — what to read before approving it."""
    names: list[str] = []
    for item in pr.changed_files or []:
        name = item.get("filename") if isinstance(item, dict) else item
        if isinstance(name, str) and name.startswith(_WORKFLOW_DIR) and name not in names:
            names.append(name)
    return names


# --------------------------------------------------------------------------
# Reading the forge
# --------------------------------------------------------------------------


def _setup_checks(raw_checks: Iterable[Mapping[str, Any]]) -> list[SetupCheck]:
    return [
        SetupCheck(name=str(c.get("name") or ""), url=str(c.get("url") or ""))
        for c in raw_checks
        if c.get("conclusion") == "action_required" and c.get("name")
    ]


def stored_setup_checks(pr: PullRequest) -> list[SetupCheck]:
    """``action_required`` checks from the PR's stored checks for its current head."""
    from franktheunicorn.scoring.merge_readiness import current_checks

    return _setup_checks(current_checks(pr))


def _annotate_on_base(
    client: ForgeClient, owner: str, repo: str, base: str, runs: list[dict[str, Any]]
) -> None:
    """Mark each waiting run with whether its workflow file is on the base branch."""
    seen: dict[str, bool | None] = {}
    for run in runs:
        path = str(run.get("path") or "")
        if not path or not base:
            run["on_base"] = None
            continue
        if path not in seen:
            if len(seen) >= _MAX_BASE_CHECKS:
                seen[path] = None
            else:
                try:
                    seen[path] = client.file_exists(owner, repo, path, base)
                except Exception:
                    seen[path] = None
        run["on_base"] = seen[path]


def gather(
    client: ForgeClient,
    pr: PullRequest,
    build_workflow: str,
    *,
    awaiting: list[dict[str, Any]] | None = None,
) -> tuple[Facts, dict[str, Any], str]:
    """Read what bears on *pr*'s CI not running. ``(facts, detail, note)``.

    Raises when the PR itself cannot be read: "could not look" must not read
    as "nothing wrong". Everything after that is best-effort and says so in
    *note*. *awaiting* is the scan's project-wide listing, already filtered to
    this head, so a sweep does not list the runs once per PR.
    """
    owner, repo = pr.project.owner, pr.project.repo
    detail = client.get_pull_request(owner, repo, pr.number)
    head = detail.get("head") if isinstance(detail.get("head"), dict) else {}
    base = detail.get("base") if isinstance(detail.get("base"), dict) else {}
    head_repo = head.get("repo") if isinstance(head, dict) else None
    sha = str((head or {}).get("sha") or pr.head_sha)
    facts = Facts(
        upstream=f"{owner}/{repo}",
        sha=sha,
        branch=str((head or {}).get("ref") or pr.head_branch),
        base_branch=str((base or {}).get("ref") or pr.base_branch),
        fork=str(head_repo.get("full_name") or "") if isinstance(head_repo, dict) else "",
        fork_deleted=isinstance(head, dict) and "repo" in head and head_repo is None,
        jira=pr.jira_ticket_id,
    )
    notes: list[str] = []

    try:
        raw_checks = client.get_commit_checks(owner, repo, sha)
    except NotImplementedError:
        facts.setup_checks = stored_setup_checks(pr)
    except Exception as exc:
        facts.setup_checks = stored_setup_checks(pr)
        notes.append(f"could not re-read the checks ({type(exc).__name__}); used stored ones")
    else:
        facts.setup_checks = _setup_checks(raw_checks)
    if facts.setup_checks and not build_workflow:
        # No fork-side workflow to read: the check's own words are the diagnosis.
        try:
            outputs = {
                str(f.get("check")): f
                for f in client.get_commit_check_failures(owner, repo, sha)
                if f.get("conclusion") == "action_required"
            }
        except Exception:
            outputs = {}
        for check in facts.setup_checks:
            out = outputs.get(check.name) or {}
            check.title = str(out.get("title") or "")
            check.summary = str(out.get("summary") or "")
            check.url = check.url or str(out.get("url") or "")

    if awaiting is None:
        try:
            awaiting = client.list_runs_awaiting_approval(owner, repo, head_sha=sha)
        except NotImplementedError:
            awaiting = []
        except Exception as exc:
            awaiting = []
            notes.append(f"could not list runs awaiting approval ({type(exc).__name__})")
    facts.awaiting_runs = [dict(r) for r in awaiting if r.get("head_sha") in ("", sha)][:_MAX_RUNS]
    if facts.awaiting_runs:
        _annotate_on_base(client, owner, repo, facts.base_branch, facts.awaiting_runs)

    reads_fork = (
        facts.setup_checks
        and build_workflow
        and facts.fork
        and facts.fork.lower() != facts.upstream.lower()
        and facts.branch != "master"
    )
    if reads_fork:
        fork_owner, _, fork_repo = facts.fork.partition("/")
        try:
            facts.workflows = client.list_repo_workflows(fork_owner, fork_repo)
            facts.runs = (
                client.list_workflow_runs_for_commit(fork_owner, fork_repo, sha)
                if facts.workflows is not None
                else []
            )
            facts.fork_read = True
        except NotImplementedError:
            # A forge that cannot list a fork's workflows: fork_read stays False,
            # and the diagnosis falls back to the generic "no run" advice.
            logger.debug("%s cannot read fork workflows", type(client).__name__)
        except Exception as exc:
            notes.append(f"could not read the fork {facts.fork} ({type(exc).__name__})")
        has_build = any(
            r.get("path") == build_workflow and r.get("branch") == facts.branch for r in facts.runs
        )
        if facts.fork_read and facts.workflows is not None and not has_build:
            try:
                facts.workflow_at_head = client.file_exists(
                    fork_owner, fork_repo, build_workflow, sha
                )
            except Exception:
                facts.workflow_at_head = None
    return facts, detail, "; ".join(notes)


def _client_for(
    project_config: ProjectConfig, operator_config: OperatorConfig
) -> ForgeClient | None:
    try:
        from franktheunicorn.backends import make_client
        from franktheunicorn.config.resolver import get_forge_entry

        return make_client(get_forge_entry(operator_config, project_config.forge))
    except Exception:
        logger.warning(
            "Fork setup for %s has no forge client; nothing can be read",
            project_config.full_name,
            exc_info=True,
        )
        return None


def _project_config_for(pr: PullRequest) -> ProjectConfig:
    from franktheunicorn.review.ci_failure import _project_config_for as resolve

    return resolve(pr)


# --------------------------------------------------------------------------
# Rows: diagnose one, sweep a project
# --------------------------------------------------------------------------


def _reset(row: ForkSetupAssessment, pr: PullRequest) -> None:
    """Point *row* at the PR's current head and forget the old diagnosis.

    The posted / approved fields stay: "you told them last week" is true
    whichever head it was on.
    """
    row.head_sha = pr.head_sha
    row.status = "unchecked"
    row.problems = []
    row.kinds = []
    row.needs_author = False
    row.needs_maintainer = False
    row.awaiting_runs = []
    row.comment_body = ""
    row.edited_body = ""
    row.anti_pattern_hits = []
    row.attempts = 0
    row.last_error = ""
    row.checked_at = None
    row.disposition = "pending"
    row.dismiss_reason = ""
    row.dismiss_note = ""


def check(
    row: ForkSetupAssessment,
    project_config: ProjectConfig,
    client: ForgeClient,
    *,
    awaiting: list[dict[str, Any]] | None = None,
) -> str:
    """Diagnose *row*'s PR and write the result. Returns a one-line outcome.

    The row ends ``diagnosed`` (with problems), ``resolved`` (nothing to do,
    or the PR closed), or ``unchecked`` with ``last_error`` saying why.
    """
    from franktheunicorn.review.antipattern import (
        check_against_anti_patterns,
        record_anti_pattern_matches,
    )
    from franktheunicorn.review.ci_failure import uses_spark_profile

    pr = row.pull_request
    if row.head_sha != pr.head_sha:
        _reset(row, pr)
    build_workflow = build_workflow_for(project_config)
    row.attempts += 1
    try:
        facts, detail, note = gather(client, pr, build_workflow, awaiting=awaiting)
    except Exception as exc:
        row.last_error = f"could not read the PR ({type(exc).__name__}: {exc})"[:500]
        row.save()
        logger.warning("Fork setup for %s#%d: %s", pr.project.full_name, pr.number, row.last_error)
        return f"PR #{pr.number}: {row.last_error}"
    if detail.get("state") not in (None, "open"):
        row.status = "resolved"
        row.last_error = "the PR is no longer open"
        row.save()
        return f"PR #{pr.number}: no longer open; resolved"
    if facts.sha and pr.head_sha and facts.sha != pr.head_sha:
        row.last_error = (
            f"the PR has new commits ({pr.head_sha[:7]} -> {facts.sha[:7]}) the poller has "
            "not stored yet; checked again after the next refresh"
        )[:500]
        row.save()
        return f"PR #{pr.number}: {row.last_error}"

    problems = diagnose(
        facts, build_workflow=build_workflow, spark=uses_spark_profile(project_config)
    )
    row.fork_full_name = facts.fork
    row.awaiting_runs = facts.awaiting_runs
    row.problems = [p.to_dict() for p in problems]
    row.kinds = [p.kind for p in problems]
    row.needs_author = any(p.who == "author" for p in problems)
    row.needs_maintainer = any(p.who == "maintainer" for p in problems)
    body = compose_comment(
        problems,
        author=pr.author,
        head_sha=pr.head_sha,
        base_branch=facts.base_branch,
        cfg=project_config.fork_setup,
    )
    matches = check_against_anti_patterns(body, pr.project) if body else []
    record_anti_pattern_matches(matches)
    row.comment_body = body
    row.edited_body = ""
    row.anti_pattern_hits = [m.pattern_text[:200] for m in matches]
    row.status = "diagnosed" if row.needs_author or row.needs_maintainer else "resolved"
    row.checked_at = timezone.now()
    row.last_error = note[:500]
    row.save()
    outcome = f"PR #{pr.number}: {', '.join(row.kinds) or 'nothing to do'}" + (
        f" ({note})" if note else ""
    )
    logger.debug("Fork setup — %s %s", pr.project.full_name, outcome)
    return outcome


def diagnose_pr(pr: PullRequest, operator_config: OperatorConfig) -> str:
    """The worker command: diagnose one PR, whatever ``fork_setup.enabled`` says."""
    if pr.state != "open":
        return f"PR #{pr.number} is {pr.state}; nothing to diagnose"
    project_config = _project_config_for(pr)
    row, _created = ForkSetupAssessment.objects.get_or_create(
        pull_request=pr, defaults={"head_sha": pr.head_sha}
    )
    client = _client_for(project_config, operator_config)
    if client is None:
        row.last_error = "no forge client is configured for this project"
        row.attempts += 1
        row.save()
        return f"PR #{pr.number}: {row.last_error}"
    try:
        return check(row, project_config, client)
    finally:
        client.close()


@dataclass
class ScanResult:
    """One project's sweep, for the command log."""

    project: str
    candidates: int = 0
    awaiting: int = 0
    diagnosed: int = 0
    needs_author: int = 0
    needs_maintainer: int = 0
    cleared: int = 0
    waiting: int = 0
    note: str = ""

    def summary(self) -> str:
        line = (
            f"{self.project}: {self.candidates} candidate PR(s) ({self.awaiting} with runs "
            f"awaiting approval), {self.diagnosed} diagnosed -> {self.needs_author} need the "
            f"author, {self.needs_maintainer} need a maintainer; {self.cleared} cleared, "
            f"{self.waiting} left for the next run"
        )
        return f"{line} ({self.note})" if self.note else line


def scan_project(
    project: Project, project_config: ProjectConfig, client: ForgeClient
) -> ScanResult:
    """Find this project's candidates, give each a row, diagnose up to the budget.

    Candidates are open PRs with an ``action_required`` check stored for their
    head, plus open PRs whose head has upstream runs awaiting approval (one
    listing for the whole project). A row the sweep no longer finds a reason
    for is cleared, so an approval somebody gave on GitHub drops it too.
    """
    result = ScanResult(project=project.full_name)
    open_prs = PullRequest.objects.filter(project=project, state="open").exclude(head_sha="")
    stored = [
        pr
        for pr in open_prs.filter(ci_status="fail", ci_checks_sha=F("head_sha"))
        if stored_setup_checks(pr)
    ]
    awaiting_by_sha: dict[str, list[dict[str, Any]]] = {}
    listed = True
    try:
        for run in client.list_runs_awaiting_approval(project.owner, project.repo):
            awaiting_by_sha.setdefault(str(run.get("head_sha") or ""), []).append(run)
    except NotImplementedError:
        listed = False
    except Exception as exc:
        listed = False
        result.note = f"could not list runs awaiting approval ({type(exc).__name__})"
        logger.warning("Fork setup for %s: %s", project.full_name, result.note)
    by_pk = {pr.pk: pr for pr in stored}
    waiting_prs = list(open_prs.filter(head_sha__in=[s for s in awaiting_by_sha if s]))
    for pr in waiting_prs:
        by_pk.setdefault(pr.pk, pr)
    result.awaiting = len(waiting_prs)
    candidates = sorted(
        by_pk.values(),
        key=lambda pr: pr.github_updated_at or pr.created_at,
        reverse=True,
    )
    result.candidates = len(candidates)

    rows = {
        row.pull_request_id: row
        for row in ForkSetupAssessment.objects.filter(pull_request__project=project).select_related(
            "pull_request", "pull_request__project"
        )
    }
    to_check: list[ForkSetupAssessment] = []
    for pr in candidates:
        row = rows.get(pr.pk)
        if row is None:
            row, _ = ForkSetupAssessment.objects.get_or_create(
                pull_request=pr, defaults={"head_sha": pr.head_sha}
            )
        elif row.head_sha != pr.head_sha or row.status == "resolved":
            _reset(row, pr)
            row.save()
        row.pull_request = pr
        if row.status == "unchecked" and row.attempts < ForkSetupAssessment.MAX_ATTEMPTS:
            to_check.append(row)

    # Rows still in the tab that this sweep found no reason for.
    candidate_pks = set(by_pk)
    for pk, row in rows.items():
        if pk in candidate_pks or row.status != "diagnosed" or not row.is_current:
            continue
        pr = row.pull_request
        changed: list[str] = []
        if row.needs_maintainer and listed:
            row.needs_maintainer = False
            row.awaiting_runs = []
            changed.append("needs_maintainer")
        if row.needs_author and pr.ci_checks_sha == pr.head_sha and not stored_setup_checks(pr):
            row.needs_author = False
            changed.append("needs_author")
        if changed:
            if not row.needs_author and not row.needs_maintainer:
                row.status = "resolved"
            row.save()
            result.cleared += 1

    budget = project_config.fork_setup.max_per_run
    for row in to_check[:budget]:
        try:
            check(
                row,
                project_config,
                client,
                awaiting=awaiting_by_sha.get(row.head_sha, []) if listed else None,
            )
        except Exception:
            logger.exception(
                "Fork setup check failed for %s#%d", project.full_name, row.pull_request.number
            )
            ForkSetupAssessment.objects.filter(pk=row.pk).update(attempts=F("attempts") + 1)
            continue
        result.diagnosed += 1
        result.needs_author += int(row.needs_author)
        result.needs_maintainer += int(row.needs_maintainer)
    result.waiting = max(0, len(to_check) - budget)
    return result


def scan(
    project_configs: Iterable[object],
    operator_config: OperatorConfig,
    *,
    every_project: bool = False,
) -> list[ScanResult]:
    """The sweep: every project with ``fork_setup.enabled``, or all of them on a press."""
    from franktheunicorn.config.models import ProjectConfig as _ProjectConfig
    from franktheunicorn.core.models import Project as _Project

    results: list[ScanResult] = []
    for pc in project_configs:
        if not isinstance(pc, _ProjectConfig) or not pc.enabled:
            continue
        if not every_project and not pc.fork_setup.enabled:
            continue
        project = _Project.objects.filter(
            owner__iexact=pc.owner, repo__iexact=pc.repo, enabled=True
        ).first()
        if project is None:
            continue
        client = _client_for(pc, operator_config)
        if client is None:
            continue
        try:
            result = scan_project(project, pc, client)
        except Exception:
            logger.exception("Fork setup sweep failed for %s", pc.full_name)
            continue
        finally:
            client.close()
        results.append(result)
        if result.candidates or result.cleared:
            logger.info("Fork setup — %s", result.summary())
    return results


# --------------------------------------------------------------------------
# What the operator did
# --------------------------------------------------------------------------


def record_posted(row: ForkSetupAssessment, body: str, comment_id: int | None = None) -> None:
    from franktheunicorn.core.models import OperatorAction

    edited = body.strip() != row.comment_body.strip()
    row.edited_body = body if edited else ""
    row.disposition = "posted"
    row.posted_at = timezone.now()
    row.posted_sha = row.head_sha
    row.forge_comment_id = comment_id
    row.post_count += 1
    row.save()
    OperatorAction.objects.create(
        action_type="post_fork_setup_comment",
        pull_request=row.pull_request,
        notes=f"{', '.join(row.kinds)}{' (edited)' if edited else ''}"[:1000],
    )


def record_dismissal(row: ForkSetupAssessment, reason: str, note: str = "") -> None:
    """ "Don't post this", and why. "Wrong diagnosis" is logged with the kinds,
    which is how a rule that misreads a fork gets noticed and fixed."""
    from franktheunicorn.core.models import OperatorAction

    row.disposition = "dismissed"
    row.dismiss_reason = reason
    row.dismiss_note = note
    row.save()
    if reason == "wrong":
        logger.info(
            "Fork-setup diagnosis for PR #%d dismissed as wrong (kinds: %s): %s",
            row.pull_request.number,
            ", ".join(row.kinds) or "none",
            note or "no note",
        )
    OperatorAction.objects.create(
        action_type="dismiss_fork_setup",
        pull_request=row.pull_request,
        notes=f"{reason}: {note}".strip(": ")[:1000],
    )


def restore(row: ForkSetupAssessment) -> None:
    from franktheunicorn.core.models import OperatorAction

    row.disposition = "pending"
    row.dismiss_reason = ""
    row.dismiss_note = ""
    row.save()
    OperatorAction.objects.create(action_type="restore_fork_setup", pull_request=row.pull_request)


def _live_head(client: ForgeClient, row: ForkSetupAssessment) -> tuple[str, str]:
    """``(blocker, live_sha)``: why the PR's live state rules an action out."""
    pr = row.pull_request
    try:
        detail = client.get_pull_request(pr.project.owner, pr.project.repo, pr.number)
    except Exception as exc:
        return f"could not read the PR's current state ({type(exc).__name__})", ""
    if detail.get("state") not in (None, "open"):
        return "the PR is no longer open", ""
    head = detail.get("head")
    live_sha = str(head.get("sha") or "") if isinstance(head, dict) else ""
    if live_sha and row.head_sha and live_sha != row.head_sha:
        return (
            f"the PR has new commits since this was checked ({row.head_sha[:7]} -> "
            f"{live_sha[:7]}); re-check it"
        ), live_sha
    return "", live_sha


def post_blocker(client: ForgeClient, row: ForkSetupAssessment) -> str:
    """Why the comment must not go out right now, or ``""``.

    Live, not from the row: still open, same head, and the setup check still
    there. An author who fixed their fork pushes a commit, which moves the
    head; the one case where nothing moves (the run existed, the mirror was
    slow) is never drafted. Writes ``needs_author`` back when the check is gone.
    """
    blocker, _sha = _live_head(client, row)
    if blocker:
        return blocker
    pr = row.pull_request
    try:
        raw = client.get_commit_checks(pr.project.owner, pr.project.repo, row.head_sha)
    except NotImplementedError:
        return ""
    except Exception as exc:
        return f"could not read the PR's current checks ({type(exc).__name__})"
    if raw and not _setup_checks(raw):
        row.needs_author = False
        if not row.needs_maintainer:
            row.status = "resolved"
        row.save(update_fields=["needs_author", "status", "updated_at"])
        return "no check on this commit needs setup any more (the build has been linked)"
    return ""


@dataclass
class ApprovalResult:
    approved: list[int] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)
    blocker: str = ""

    def message(self) -> str:
        if self.blocker:
            return f"Not approved: {self.blocker}."
        parts: list[str] = []
        if self.approved:
            parts.append(f"Approved {len(self.approved)} workflow run(s); they start now.")
        if self.failed:
            parts.append(f"Could not approve: {'; '.join(self.failed)}.")
        return " ".join(parts) or "Nothing is waiting for approval on this commit any more."


def approve(client: ForgeClient, row: ForkSetupAssessment) -> ApprovalResult:
    """Approve the runs waiting on this PR's head, after listing them live.

    Only what GitHub lists as waiting for this head right now is approved —
    not what the row remembers — so a run another maintainer already approved
    is not re-sent, and a push since the check approves nothing.
    """
    from franktheunicorn.core.models import OperatorAction

    result = ApprovalResult()
    blocker, _sha = _live_head(client, row)
    if blocker:
        result.blocker = blocker
        return result
    pr = row.pull_request
    owner, repo = pr.project.owner, pr.project.repo
    try:
        waiting = client.list_runs_awaiting_approval(owner, repo, head_sha=row.head_sha)
    except Exception as exc:
        result.blocker = f"could not list the waiting runs ({type(exc).__name__})"
        return result
    for run in waiting:
        if run.get("head_sha") not in ("", row.head_sha):
            continue
        run_id = int(run["id"])
        try:
            client.approve_workflow_run(owner, repo, run_id)
        except Exception as exc:
            status = getattr(getattr(exc, "response", None), "status_code", None)
            why = (
                "the token lacks write access to the repository's Actions"
                if status == 403
                else type(exc).__name__
            )
            result.failed.append(f"{run.get('name') or run_id} ({why})")
            continue
        result.approved.append(run_id)
    remaining = [r for r in row.awaiting_runs if int(r.get("id") or 0) not in result.approved]
    if not result.failed:
        remaining = []
    row.awaiting_runs = remaining
    row.needs_maintainer = bool(remaining)
    if result.approved:
        row.approved_at = timezone.now()
        row.approved_run_ids = [*row.approved_run_ids, *result.approved][-50:]
    row.problems = [
        p for p in row.problems if row.needs_maintainer or p.get("kind") != "awaiting-approval"
    ]
    row.kinds = [str(p.get("kind")) for p in row.problems]
    if not row.needs_author and not row.needs_maintainer:
        row.status = "resolved"
    row.save()
    if result.approved:
        OperatorAction.objects.create(
            action_type="approve_fork_workflows",
            pull_request=pr,
            notes=f"runs {', '.join(str(i) for i in result.approved)}"[:1000],
        )
    return result


@dataclass
class TabState:
    auto_projects: list[str] = field(default_factory=list)
    in_flight: int = 0
    last_scan: WorkerCommand | None = None


def tab_state(project_configs: Iterable[object]) -> TabState:
    from franktheunicorn.config.models import ProjectConfig as _ProjectConfig
    from franktheunicorn.security.queue import in_flight_statuses

    return TabState(
        auto_projects=sorted(
            pc.full_name
            for pc in project_configs
            if isinstance(pc, _ProjectConfig) and pc.enabled and pc.fork_setup.enabled
        ),
        in_flight=WorkerCommand.objects.filter(
            command__in=(COMMAND, SCAN_COMMAND), status__in=in_flight_statuses()
        ).count(),
        last_scan=WorkerCommand.objects.filter(command=SCAN_COMMAND)
        .order_by("-created_at")
        .first(),
    )
