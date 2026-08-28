"""An LLM backend that is a coding-agent CLI rather than an HTTP API.

The other backends in this package hold an API key and speak to a service. This
one shells out to ``claude``/``cursor-agent``/``codex`` — possibly on another
machine over SSH — and reads what comes back on stdout. Which means it works
wherever one of those CLIs is already logged in, with no key for frank to hold.

**It borrows, it does not re-describe.** ``reviewer`` names an entry in
``agent_cli_reviewers`` and takes its ``cli_path``, argv shape, ``trust_args``,
``extra_args`` and — the point of this for a remote setup — its whole ``remote``
block. Exactly the way ``SecurityVerifierConfig.reviewer`` does. There is one
description of how to reach the box the agent runs on, and adding a second would
be the thing that eventually disagrees with the first.

**The parse has to be lenient.** The API backends can demand JSON and get it.
A coding-agent CLI cannot be relied on for that: it narrates, it fences, it adds
a sentence afterwards. So the response is scanned for a balanced JSON object with
the same tail-first, budget-bounded scan the security verifier uses — shared
rather than reimplemented, because that function has three separately-learned
properties in it (multiple candidates, last-first for injection resistance, and a
work budget) and a second copy would have none of them.

**It needs somewhere to stand.** Every one of these CLIs runs *in a directory*,
and some refuse to run in one nobody has vouched for. The verifier gets a checkout
because ``verify_report`` calls ``prepare_repo`` first; triage reads a report and
has no checkout at all. So this backend prepares a small scratch directory of its
own — see :meth:`_working_dir`. Deliberately not a repo: nothing here needs source
code, and pointing a shell-capable agent at a checkout to answer a text question
is a larger grant than the question needs.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from franktheunicorn.review.backends.base import (
    BaseLLMBackend,
    ReviewResult,
    parse_llm_review,
)

if TYPE_CHECKING:
    from franktheunicorn.config.models import AgentCLIReviewerConfig, LLMBackendConfig
    from franktheunicorn.review.backends.base import PRContext
    from franktheunicorn.review.tool_executor import ToolExecutor

logger = logging.getLogger(__name__)

#: Where the agent is run when it doesn't need a repo. Under the remote workspace
#: dir so it inherits whatever the operator already set up, and named for what it
#: is so nobody wonders later.
_SCRATCH_SUBDIR = "llm-scratch"


class AgentCLIBackend(BaseLLMBackend):
    """Drive a coding-agent CLI as if it were an LLM API.

    No ``_sdk_module`` and no ``_default_key_env``: there is no package to import
    and no key to resolve, because auth belongs to the CLI. Setting
    ``_default_key_env`` would make :meth:`generate_review` refuse every call for a
    missing env var that is not needed.
    """

    def __init__(self, config: LLMBackendConfig) -> None:
        super().__init__(config)
        self._reviewer: AgentCLIReviewerConfig | None = None
        self._resolve_failed = False

    def _resolve_reviewer(self) -> AgentCLIReviewerConfig | None:
        """The ``agent_cli_reviewers`` entry named by ``reviewer``, or None.

        Loaded lazily and cached: the operator config is read from disk, and doing
        that in ``__init__`` would make constructing a backend touch the filesystem.

        Resolution failure is logged **once**. A backend used on the review path is
        constructed per PR, and a name that doesn't resolve would otherwise emit the
        same line for every PR in every cycle.
        """
        if self._reviewer is not None:
            return self._reviewer
        if self._resolve_failed:
            return None

        from franktheunicorn.config.loader import get_operator_config

        name = (self._config.reviewer or "").strip()
        operator_config = get_operator_config()
        if not name:
            # Not defaulted to "claude": a silent default here means an operator who
            # misspelled the key gets a different model than the one they configured,
            # with nothing said about it.
            logger.error(
                "llm_backends entry with provider=agent-cli has no `reviewer`. It must "
                "name an agent_cli_reviewers entry to borrow the CLI and remote config "
                "from (have: %s).",
                ", ".join(rc.name for rc in operator_config.agent_cli_reviewers) or "none",
            )
            self._resolve_failed = True
            return None

        for candidate in operator_config.agent_cli_reviewers:
            if candidate.name == name:
                self._reviewer = candidate
                return candidate

        logger.error(
            "llm_backends provider=agent-cli names reviewer=%r, which matches no "
            "agent_cli_reviewers entry (have: %s). This backend cannot run.",
            name,
            ", ".join(rc.name for rc in operator_config.agent_cli_reviewers) or "none",
        )
        self._resolve_failed = True
        return None

    def _working_dir(self, reviewer: AgentCLIReviewerConfig, executor: ToolExecutor) -> str | None:
        """A directory to run the agent in, or None if one can't be had.

        A scratch directory rather than a checkout. Nothing this backend does needs
        source — it is answering a question about text — and handing a
        shell-capable agent a repo to answer it would be a bigger grant than the job
        requires.

        In local mode the CLI simply runs in the current working directory, which is
        the worker's own and already trusted. Remotely we ``mkdir -p`` a scratch path
        under the configured workspace dir, because a CLI invoked with a ``cd`` to a
        path that doesn't exist fails in a way that reads like the CLI being broken.
        """
        if reviewer.remote.mode != "ssh":
            return "."

        scratch = f"{reviewer.remote.remote_workspace_dir.rstrip('/')}/{_SCRATCH_SUBDIR}"
        made = executor.run(["mkdir", "-p", scratch], cwd=".", timeout=60)
        if made is None or not made.ok:
            detail = "no result" if made is None else (made.stderr or "").strip()[:200]
            logger.error(
                "Could not create the agent-cli scratch directory %s on the remote (%s). "
                "Check remote.ssh_command works from this host and that "
                "remote_workspace_dir is writable.",
                scratch,
                detail,
            )
            return None
        return scratch

    def _call_api(self, system_prompt: str, user_message: str, api_key: str) -> str:
        """Run the agent and return its stdout.

        *api_key* is accepted for interface compatibility and unused: auth is the
        CLI's own, which is the reason to use this backend at all.

        System prompt and user message are concatenated. These CLIs take one prompt
        argument and have no separate system channel, so the alternative is dropping
        the system prompt — which carries the review format instructions.
        """
        del api_key

        from franktheunicorn.review.tool_executor import (
            looks_like_workspace_trust_refusal,
            make_executor,
            workspace_trust_advice,
        )

        reviewer = self._resolve_reviewer()
        if reviewer is None:
            return ""

        prompt = f"{system_prompt}\n\n{user_message}" if system_prompt else user_message
        # The verifier's model override, same shape: the backend's own `model` wins
        # over the borrowed entry's, and an empty one means "whatever the CLI
        # defaults to".
        borrowed = reviewer.model_copy(update={"model": self._config.model or reviewer.model})

        executor = make_executor(reviewer.remote)
        cwd = self._working_dir(reviewer, executor)
        if cwd is None:
            return ""

        argv = [*borrowed.cli_argv, *borrowed.build_invocation(prompt)]
        timeout = self._config.cli_timeout_seconds or reviewer.timeout_seconds
        result = executor.run(argv, cwd=cwd, timeout=timeout)

        if result is None:
            logger.error(
                "The %s CLI produced no result within %ds. For remote.mode ssh check "
                "that ssh_command works from this host and that %s exists there.",
                reviewer.name,
                timeout,
                borrowed.cli_argv[0],
            )
            return ""
        if not result.ok:
            advice = (
                " " + workspace_trust_advice(reviewer.name)
                if looks_like_workspace_trust_refusal(result.stderr, result.stdout)
                else ""
            )
            logger.error(
                "The %s CLI exited %d: %s%s",
                reviewer.name,
                result.returncode,
                (result.stderr or result.stdout or "").strip()[:500] or "(no output)",
                advice,
            )
            return ""

        # Token counts stay at zero: a CLI does not report usage, and inventing a
        # number would put a fabricated figure in the cost widget. Zero tokens makes
        # record_cost no-op, which is the honest outcome — the spend happened on the
        # CLI's own account, not through a key frank holds.
        return result.stdout

    def generate_review(self, diff: str, pr_context: PRContext) -> ReviewResult:
        """Same contract as the API backends, minus the API-key gate.

        Overridden rather than inherited because the base implementation refuses
        early when ``_default_key_env`` is set and unresolved. That check is right for
        every other backend and meaningless here.
        """
        from franktheunicorn.review.prompt import build_system_prompt, build_user_message

        if self._resolve_reviewer() is None:
            return ReviewResult()

        try:
            raw_text = self._invoke(
                build_system_prompt(pr_context), build_user_message(diff, pr_context), ""
            )
        except Exception:
            logger.exception("agent-cli backend failed while running the agent.")
            return ReviewResult()

        return parse_llm_review(self._extract_json(raw_text))

    def complete(self, prompt: str, *, system: str = "") -> str:
        """Raw completion. Returns "" on any failure, like the other backends."""
        try:
            return self._invoke(system, prompt, "")
        except Exception:
            logger.exception("agent-cli backend failed while running the agent.")
            return ""

    @staticmethod
    def _extract_json(raw_text: str) -> str:
        """Reduce an agent's narrated answer to the JSON object in it.

        The reason this backend can't just hand stdout to the strict parsers: a CLI
        agent told to emit JSON will still say "Here's my review:" first and add a
        closing remark after, and it fences the block. ``parse_llm_review`` does its
        own tolerant extraction, but it anchors forward — so a prose brace before the
        real object costs the whole response.

        Reuses the verifier's scan rather than writing a second one. That function
        carries three properties learned separately and painfully: it tries more than
        one candidate (agents describe code, and ``the `{` handling in parser.py``
        opens a depth that never closes), it searches **tail-first** (so a JSON object
        quoted in the *input* cannot outrank the model's actual answer), and it is
        bounded by a work budget (the naive version was quadratic on agent-controlled
        text — 6.5s for 16 KB of unbalanced braces).

        Falls through to the original text when nothing balanced is found, so
        ``parse_llm_review`` still gets its own go at it.
        """
        from franktheunicorn.security.verifier import _json_object_candidates

        for candidate in _json_object_candidates(raw_text):
            # "findings" or "overall_vibe" — the keys parse_llm_review wants. Checked
            # as substrings before parsing so a `${FOO}`-shaped stray from the prose
            # doesn't win by being syntactically valid and semantically empty.
            if '"findings"' in candidate or '"overall_vibe"' in candidate:
                return candidate
        return raw_text
