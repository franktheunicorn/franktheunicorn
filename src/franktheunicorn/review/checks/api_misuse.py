"""API-misuse sub-check.

Identifies third-party function calls in the diff, fetches their
upstream docs (PyPI + readthedocs, or Maven Central + javadoc.io),
and asks the LLM to flag misuse: complexity-on-large-input, deprecated
APIs, ignored return values, wrong threading/async semantics, and
other footguns documented upstream but invisible at the call site.

Disabled by default; opt in via:

    api_misuse:
      enabled: true
    llm_checks: ["api-misuse"]

The check is a thin wrapper: heavy lifting lives in
``call_extraction`` (diff → :class:`CallSite`) and
``data_access.package_registry`` (call site → :class:`PackageDocs`).
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from franktheunicorn.data_access.package_registry import resolve_call_docs
from franktheunicorn.data_access.package_registry._helpers import format_docs_block
from franktheunicorn.review.call_extraction import extract_calls
from franktheunicorn.review.checks import BaseCheck
from franktheunicorn.review.prompt import build_user_message, finding_schema_json

if TYPE_CHECKING:
    from franktheunicorn.config.models import APIMisuseConfig
    from franktheunicorn.review.backends.base import PRContext

logger = logging.getLogger(__name__)


_SYSTEM_PROMPT = """\
You are an API-misuse reviewer. You receive (1) a unified diff and (2) \
upstream documentation snippets for the third-party functions touched by \
that diff. Your ONLY job is to flag cases where the diff calls these \
functions in a way that contradicts what the upstream docs say.

Focus on:
- Complexity-on-large-input (docs say O(N^2) or worse, caller is in a \
loop or operates on a list/queryset/dataframe whose size is unbounded)
- Calls to functions documented as deprecated, removed, or scheduled for \
removal in a future version
- Ignored return values that the docs flag as significant
- Wrong threading / async / blocking semantics (e.g. calling a sync \
function inside an async coroutine without an executor)
- Argument values that contradict documented constraints (allowed values, \
required types, deprecated kwargs)
- Missing required cleanup (close/release/__exit__) the docs prescribe

Do NOT comment on style, naming, test coverage, security, architecture, \
or anything unrelated to documented misuse. If the calls all look correct \
per the supplied docs, return an empty findings array.

If the supplied docs are sparse or empty for a call, do NOT speculate — \
say nothing about that call.

Return your review as a JSON object: {{"findings": [...]}}
Each finding must match this schema:
{schema}

Set severity to one of: critical, important, nit, informational.
- "critical" for calls to deprecated/removed APIs whose replacement \
behaviour is not equivalent.
- "important" for documented complexity issues likely to bite at scale, \
or wrong concurrency semantics.
- "nit" for minor style hints from the docs (e.g. preferred kwarg names).

Prefix every finding's title with "api-misuse:". Cite the doc URL in the \
body when one is supplied. If you have no findings, return: {{"findings": []}}
"""


_DOCS_BLOCK_HEADER = "\n[Upstream docs for third-party calls in this PR]\n"


class APIMisuseCheck(BaseCheck):
    """Detects misuse of third-party APIs by consulting upstream docs."""

    name = "api-misuse"

    def __init__(
        self,
        config: APIMisuseConfig | None = None,
    ) -> None:
        from franktheunicorn.config.models import APIMisuseConfig as _Cfg

        self._config = config if config is not None else _Cfg()

    def build_prompt(self, diff: str, pr_context: PRContext) -> tuple[str, str]:
        sites = extract_calls(diff, project_package=self._config.first_party_package)
        docs = (
            resolve_call_docs(sites, self._config, diff=diff)
            if (sites and self._config.enabled)
            else []
        )

        system_prompt = _SYSTEM_PROMPT.format(schema=finding_schema_json())
        user_message = build_user_message(diff, pr_context)
        if docs:
            user_message = user_message + _DOCS_BLOCK_HEADER + format_docs_block(docs)
        return system_prompt, user_message
