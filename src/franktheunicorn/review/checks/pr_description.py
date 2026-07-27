"""PR description template auditor sub-check.

Fetches the upstream PR template from the target repo and asks an LLM
to evaluate whether the PR description properly follows it: filled-in
sections, answered questions, no leftover placeholder text.

HTML comments (<!-- ... -->) in the template are instructions to the
author that don't render in GitHub's UI. They are stripped before the
check so the LLM only evaluates visible content.
"""

from __future__ import annotations

import json
import logging
import re
from typing import TYPE_CHECKING

import httpx

from franktheunicorn.data_access.github.template_fetcher import TemplateFetcher
from franktheunicorn.review.backends.base import BaseLLMBackend, parse_llm_response
from franktheunicorn.review.checks import BaseCheck
from franktheunicorn.review.prompt import finding_schema_json

if TYPE_CHECKING:
    from franktheunicorn.config.models import LLMBackendConfig
    from franktheunicorn.core.models import PullRequest
    from franktheunicorn.review.backends.base import PRContext, ReviewFinding

logger = logging.getLogger(__name__)

_HTML_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)

_SYSTEM_PROMPT = """\
You are a PR description auditor. Your ONLY job is to evaluate whether \
the pull request description properly follows the repository's PR template.

You will be given:
- The PR template (required sections and questions the author must fill in)
- The actual PR description the author submitted

Evaluate:
- Are all required sections present and filled with real content?
- Are there sections left as placeholder text (e.g. "TODO", "N/A", "TBD", \
"[describe here]", "[add details]", or the literal template text copied \
verbatim without changes)?
- Are there questions in the template that have not been answered at all?
- Is the description entirely empty or contains only boilerplate with no \
real content?

Note: HTML comments (<!-- ... -->) in the template are author-facing \
instructions and do not render on GitHub. Do NOT penalise the author for \
removing them or for omitting their literal text in the description.

Do NOT comment on code quality, writing style, grammar, or anything \
unrelated to whether the template structure has been followed.

If the description adequately follows the template, return an empty \
findings array.

Return your review as a JSON object: {{"findings": [...]}}
Each finding must match this schema:
{schema}

Set severity to "important" if a required section is completely missing \
or entirely unanswered. Set severity to "nit" for minor gaps such as a \
single placeholder word left in an otherwise complete description.
Set file_path to "" and line_number to null for all findings (description \
is not a code file).
If you have no findings, return: {{"findings": []}}
"""


def _strip_html_comments(text: str) -> str:
    return _HTML_COMMENT_RE.sub("", text).strip()


class PRDescriptionCheck(BaseCheck):
    """Audits the PR description against the repo's PR template."""

    name = "pr-description"

    def build_prompt(self, diff: str, pr_context: PRContext) -> tuple[str, str]:
        # This check uses scan() instead; build_prompt is a fallback stub.
        return (
            _SYSTEM_PROMPT.format(schema=finding_schema_json()),
            pr_context.pr_body or "(empty)",
        )

    def scan(
        self,
        pr: PullRequest,
        diff: str,
        backend_config: LLMBackendConfig,
    ) -> list[ReviewFinding]:
        from franktheunicorn.review.backends import get_backend

        owner: str = pr.project.owner
        repo: str = pr.project.repo
        pr_body: str = pr.body or ""

        template_text = _fetch_template(owner, repo)
        if not template_text:
            logger.debug("pr-description: no template for %s/%s, skipping", owner, repo)
            return []

        visible_template = _strip_html_comments(template_text)
        if not visible_template:
            logger.debug(
                "pr-description: template for %s/%s is all HTML comments, skipping",
                owner,
                repo,
            )
            return []

        system_prompt = _SYSTEM_PROMPT.format(schema=finding_schema_json())
        user_message = _build_user_message(visible_template, pr_body)

        backend = get_backend(backend_config)
        if not isinstance(backend, BaseLLMBackend):
            return []

        try:
            raw_text = backend.metered_call(
                system_prompt,
                user_message,
                action_type="check:pr-description",
                project_id=pr.project_id,
                pr_id=pr.pk,
            )
        except Exception:
            logger.exception("pr-description: LLM call failed for %s/%s#%d", owner, repo, pr.number)
            return []

        return parse_llm_response(raw_text)


def _fetch_template(owner: str, repo: str) -> str:
    """Fetch PR template text; returns empty string if none found or on error."""
    try:
        fetcher = TemplateFetcher(client=httpx.Client(timeout=10.0))
        result = fetcher.fetch(owner, repo)
        return result.text
    except Exception:
        logger.debug(
            "pr-description: error fetching template for %s/%s", owner, repo, exc_info=True
        )
        return ""


def _build_user_message(template: str, pr_body: str) -> str:
    body_display = pr_body.strip() if pr_body.strip() else "(empty)"
    return json.dumps(
        {
            "pr_template": template,
            "pr_description": body_display,
        },
        ensure_ascii=False,
        indent=2,
    )
