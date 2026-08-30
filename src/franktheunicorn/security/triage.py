"""Security report triage pipeline.

Uses existing LLM backends to parse and analyze security reports,
then checks CVE databases for duplicates.
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import TYPE_CHECKING, NamedTuple

from franktheunicorn.security.cve_lookup import search_cves
from franktheunicorn.security.prompt import build_parse_prompt, build_triage_prompt

_VALID_SEVERITIES: frozenset[str] = frozenset(
    {"critical", "high", "medium", "low", "informational"}
)

# Statuses the machine may touch. Under the staging design the machine writes
# its verdict to ``auto_triage_status`` and leaves ``status`` alone except for
# the transient in-flight ``triaging`` claim, so ``status`` is the operator's
# field. That means ``expected-behavior`` is no longer auto-managed — the
# machine used to file into it directly, and now it stages the suggestion
# instead, so a report sitting in ``expected-behavior`` got there by an
# operator's Agree click or verdict, and a re-triage must not overwrite it.
# The learning loop still works: a re-triage writes a fresh suggestion to
# ``auto_triage_status`` for the operator to agree to.
_AUTO_MANAGED_STATUSES: frozenset[str] = frozenset({"new", "triaging"})


#: A cheap-close pattern plus how the guards treat it — see _ACTOR_GUARD_RE.
class _AuthDisabledPattern(NamedTuple):
    pattern: re.Pattern[str]
    actor_guarded: bool
    is_config: bool = False


#: Phrases that mean the report's scenario only holds with authentication
#: switched off. That is the operator disabling a security feature, not a
#: vulnerability — the feature exists precisely to stop the thing being
#: reported — so the report can be closed from its own text without spending
#: the two LLM calls. Deliberately narrow: "unauthenticated" and "no
#: authentication required" are NOT here, because an endpoint that should
#: authenticate and doesn't is a real finding; only an explicit disabled/off
#: precondition qualifies, and even then the guard below gets a veto.
_AUTH_DISABLED_RES: tuple[_AuthDisabledPattern, ...] = (
    # "authentication is disabled", "auth was turned off", "spark.authenticate
    # is disabled" (the \b before auth sits on the dot, and the alternation
    # covers the config-property verb form).
    _AuthDisabledPattern(
        re.compile(
            r"\bauth(?:entication|enticate)?\s+(?:is|was|were|are|be|been|being|to\s+be|"
            r"must\s+be|has\s+been|have\s+been|needs?\s+to\s+be|turned|switched)\s+"
            r"(?:(?:turned|switched)\s+)?(?:disabled|off)\b",
            re.IGNORECASE,
        ),
        actor_guarded=False,
    ),
    # "with/when/if/requires authentication disabled" — no copula needed.
    # "unless" is a doubt-word and lives in the negation guard instead:
    # "safe unless disabled" and "fails unless disabled" are opposite claims
    # with the same shape, and the cheap close can't tell them apart.
    _AuthDisabledPattern(
        re.compile(
            r"\b(?:with|when|if|requires?|requiring|needs?|needing|assumes?|assuming|"
            r"after|given)\s+(?:[\w-]+\s+){0,3}auth(?:entication|enticate)?\s+"
            r"(?:is\s+|being\s+)?(?:disabled|turned\s+off|switched\s+off|off)\b",
            re.IGNORECASE,
        ),
        actor_guarded=False,
    ),
    # The setup step itself: "disable authentication", "turning off auth".
    _AuthDisabledPattern(
        re.compile(
            r"\b(?:disabl(?:e|es|ed|ing)|turn(?:s|ed|ing)?\s+off|switch(?:es|ed|ing)?\s+off)"
            r"\s+(?:the\s+)?auth(?:entication|enticate)?\b",
            re.IGNORECASE,
        ),
        actor_guarded=True,
    ),
    # The structured spelling, straight out of a Preconditions section:
    # "spark.authenticate=false", "auth.enabled: off", "authentication_enabled
    # disabled". Only false-y values — a report about an insecure *default*
    # ("...=false out of the box") still matches, which is fine: the close
    # message says how to reopen, and the docs already tell the operator to
    # turn it on. "no"/"none"/"0" are deliberately not values: "auth no longer
    # required" is the unauthenticated-endpoint class above. The (?!-) keeps
    # "auth off-by-one" out. The property name is captured so the vetoes in
    # _config_match_is_vetoed can read it. The prefix is bounded because
    # report text is attacker-controlled: unbounded, [\w.-]* backtracks
    # quadratically on dot-runs ("a." * 20000 was 30s of worker time).
    _AuthDisabledPattern(
        re.compile(
            r"\b([\w.-]{0,60}auth(?:entication|enticate)?(?:[._]enabled)?)\s*[=:\s]\s*"
            r"(?:false|off|disabled)\b(?!-)",
            re.IGNORECASE,
        ),
        actor_guarded=True,
        is_config=True,
    ),
)

#: Words in the run-up to a match that mean it isn't the precondition we're
#: looking for. "Works even when auth is off" is a real finding, and "does not
#: require auth to be disabled" is a boast, not a precondition. A veto costs
#: two LLM calls, a wrong close costs a real report marked invalid — so the
#: guard fires on any doubt.
_NEGATION_GUARD_RE = re.compile(
    r"\b(?:not|never|no|without|even|despite|regardless|whether|still|instead|rather|"
    r"unless|cannot)\b|n't",
    re.IGNORECASE,
)

#: The extra veto for the action/setting patterns. "Disable authentication" is
#: a precondition when it's a setup step and a real finding when the bug is
#: that the attacker gets to — "attacker can disable authentication", "a
#: crafted POST causes the server to disable authentication" — so who does the
#: disabling decides. The second half of the list is the effect-phrasing
#: version: when the *bug itself* switches auth off, the subject is the
#: vulnerability, not the attacker — "the vulnerability disables
#: authentication", "it is possible to set spark.authenticate=false remotely".
#: These words are deliberately absent from the state patterns' guard: in
#: "anyone can read the shuffle files when authentication is disabled" the
#: "can" belongs to the main clause and says nothing about who switched auth
#: off.
_ACTOR_GUARD_RE = re.compile(
    r"\b(?:attacker|bypass(?:es|ing)?|allows?|lets?|can|could|able|causes?|makes?|"
    r"forces?|leads?|results?|vulnerabilit(?:y|ies)|bug|exploit|crafted|packet|frame|"
    r"patch|possible|payload)\b",
    re.IGNORECASE,
)

#: "with auth off and on", "disabled or enabled" — a both-states claim is the
#: strongest real-finding signal there is, and the disambiguator sits *after*
#: the match, where the other guards never look.
_FORWARD_GUARD_RE = re.compile(r"\s*(?:and|or)\s+(?:on|enabled|true)\b", re.IGNORECASE)

#: A true-y auth assignment anywhere in the report vetoes a false-y config
#: match: "tested auth=false and auth=true, same result" is a comparison, not
#: a precondition. Prefix bounded like the config pattern's — same attacker-
#: controlled text, same quadratic backtracking otherwise.
_AUTH_ENABLED_CONFIG_RE = re.compile(
    r"\b[\w.-]{0,60}auth(?:entication|enticate)?(?:[._]enabled)?\s*[=:\s]\s*"
    r"(?:true|on|enabled)\b(?!-)",
    re.IGNORECASE,
)

#: A config dump looks exactly like a precondition to the config pattern —
#: spark.authenticate=false is Spark's *default*, so a report that pastes its
#: environment would be hijacked by its own context. The tell is company: a
#: setting called out as a precondition stands alone, a dump is a run of
#: key=value lines. "=" always counts; ":" only with a config-ish key
#: (a dot/underscore/dash in it), so prose like "Steps: connect to the
#: master" doesn't, and a bare "Preconditions:" header has no value to match.
_CONFIG_LINE_RE = re.compile(r"^\s*[\w.-]+\s*=|^\s*[\w.-]*[._-][\w.-]*\s*:\s*\S")


def _looks_like_config_line(line: str) -> bool:
    # The slice keeps the regex linear: its character classes backtrack
    # quadratically on very long lines, and report text is attacker-
    # controlled. A config line's key and separator sit well inside 160
    # chars; only the value runs long.
    return bool(_CONFIG_LINE_RE.match(line.strip()[:160]))


#: Property-name tokens that flip the meaning of "=false": disable_auth=false
#: and require_auth=false both mean the protection is ON — the real-finding
#: class the prose patterns are careful to exclude.
_CONFIG_NAME_VETO_TOKENS = frozenset({"no", "noauth", "disable", "disabled", "require", "required"})

#: How far before a match the guards look. One sentence's worth for negation.
#: The actor guard looks back to the start of the line, capped: a modal on
#: another line is not about this verb, but "an attacker can use the
#: /api/v2/config endpoint to set spark.authenticate=false" puts sixty chars
#: between the actor and the setting.
_GUARD_WINDOW_CHARS = 100
_ACTOR_GUARD_WINDOW_CHARS = 120
_FORWARD_GUARD_WINDOW_CHARS = 20

#: How far after a match the alternative-precondition guard looks. Bounded to
#: the clause the match sits in (up to the next ``,`, `;`, `.` or newline, or
#: this many chars) so an "or" in a later sentence about something else can't
#: veto a real auth-disabled-only finding.
_ALT_PRECONDITION_WINDOW_CHARS = 100

#: An alternative precondition joined to the auth-disabled one by "or" —
#: "spark.authenticate=false ... OR shared-secret multi-tenant deployment".
#: That "or" means the vulnerability does not *require* auth disabled: it
#: also holds in the other branch, so the cheap close (built for "scenario
#: only holds with auth off") must not fire. Case-insensitive because prose
#: "or" is the same doubt — the close vetoes on doubt, and a veto costs two
#: LLM calls where a wrong close costs a real report.
_ALT_PRECONDITION_RE = re.compile(r"\bor\b", re.IGNORECASE)

#: The clause terminators that bound the alternative-precondition window.
_CLAUSE_TERMINATOR_RE = re.compile(r"[,;.\n]")

#: Cap on the evidence line stored in the summary and the log.
_EVIDENCE_MAX_CHARS = 200

if TYPE_CHECKING:
    from franktheunicorn.config.models import LLMBackendConfig, OperatorConfig, ProjectConfig
    from franktheunicorn.core.models import SecurityReport
    from franktheunicorn.review.backends.base import BaseLLMBackend

logger = logging.getLogger(__name__)


class TriageIncompleteError(RuntimeError):
    """Triage ran to completion but the model produced no usable verdict.

    Raised rather than returned so the ``WorkerCommand`` lands in ``failed``
    instead of ``completed``. The report itself is left in the ``new`` queue by
    ``_restore_untriaged_status``, so nothing is lost — but the operator is told
    the run came back empty rather than being shown whatever the last run said.
    """


def triage_report(
    report: SecurityReport,
    project_config: ProjectConfig | None,
    operator_config: OperatorConfig,
) -> SecurityReport:
    """Run full triage pipeline on a security report.

    A report whose own text says the scenario needs authentication disabled is
    closed first, without a model. Otherwise, per backend in fallthrough
    order: parse the raw text into structured fields, then assess POC validity
    and expected behavior. CVE lookup and duplicate detection run once between
    the two, as context for the analysis. Results are saved to the report.
    """
    logger.info("Starting triage for security report #%d", report.pk)

    # The operator can rule from the still-open detail page while the worker
    # fetches and starts; don't let this instance's stale copy talk the cheap
    # close (or the "triaging" claim below) into overwriting a verdict.
    report.status = _current_status(report)

    # The cheapest close comes first, before a backend is even resolved.
    # Gated on never-been-triaged: once a verdict field is set the report gets
    # the full pipeline, so an operator reopening it (status back to "new") is
    # honoured rather than re-closed by the same regex. The two booleans count
    # because a terse reply can set one alone and leave the text fields empty.
    if (
        report.status == "new"
        and not report.triage_summary
        and not report.poc_assessment
        and report.poc_plausible is None
        and not report.is_expected_behavior
    ):
        evidence = requires_auth_disabled_evidence(f"{report.title}\n{report.raw_text}")
        if evidence:
            _close_requires_auth_disabled(report, evidence)
            return report

    # Resolve the backends *before* mutating status — otherwise a deployment
    # with no LLM backends (e.g. email auto-triage on but llm_backends empty)
    # strands every report in "triaging" and it drops out of the "new" queue.
    backends = _get_triage_backends(operator_config)
    if not backends:
        # Raised, not returned. Returning normally left the WorkerCommand
        # "completed", which the report page reads as "a run finished and the
        # model's answer had nothing usable in it — re-running is worth a try".
        # No model was ever called, so re-running is worth nothing, forever. Only
        # the dashboard button checks llm_backends; the email poller and the zip
        # import queue commands without looking.
        logger.warning("No LLM backend configured; skipping triage for report #%d.", report.pk)
        # Before raising, not after: this returns *early*, so the try/finally
        # below — the thing whose whole job is "never leave a report in
        # triaging" — is never entered. A report stranded there by an earlier
        # killed run would stay stranded, invisible in the "new" queue, with its
        # command now "failed" so nothing requeues it. The only door out was a
        # dashboard button that refuses on llm_backends first.
        _restore_untriaged_status(report)
        raise TriageIncompleteError(f"No LLM backend configured; cannot triage report #{report.pk}")

    # Only claim the status when it's ours to claim. A report the operator
    # already ruled on (valid / invalid / duplicate) keeps that verdict through
    # a re-triage — overwriting it with "triaging" here is what made
    # _analyze_report's _AUTO_MANAGED_STATUSES guard unable to protect it.
    if report.status in _AUTO_MANAGED_STATUSES:
        report.status = "triaging"
        report.save(update_fields=["status", "updated_at"])

    # Whatever happens below, the report must not be left sitting in
    # "triaging": that status is invisible in the "new" queue, so a model that
    # times out or answers with unparseable JSON would silently swallow the
    # report. _analyze_report moves it off "triaging" on success; this puts it
    # back in the queue on every other path.
    produced_verdict = False
    no_verdict_reason = ""
    attempts = 0
    context_checked = False
    try:
        project_context = _load_project_context(report, project_config)
        security_model = _resolve_security_model(project_config)

        from franktheunicorn.security.learning import resolve_triage_guidance

        learned_guidance = resolve_triage_guidance(report.project)

        for backend in backends:
            attempts += 1
            if attempts > 1:
                logger.info(
                    "Falling through to backend %s for report #%d after: %s",
                    backend.label,
                    report.pk,
                    no_verdict_reason,
                )
            logger.info("Parsing report #%d via LLM (%s)", report.pk, backend.label)
            parsed = _parse_report(report, backend)
            logger.info(
                "Parse complete for report #%d: severity=%r component=%r",
                report.pk,
                report.assessed_severity,
                report.parsed_component[:60] if report.parsed_component else "",
            )

            # CVE lookup and duplicate detection run once, after the first
            # parse that populated fields — they read those fields (the CVE
            # matches feed the analysis prompt), and NVD is rate-limited, so
            # per-backend re-runs buy nothing. On a fallthrough the first
            # backend's parse may have raised, leaving nothing to read; the
            # last backend is the last chance to run them at all.
            #
            # Both are guarded, unlike every other step here, because they were
            # the ones that weren't. Each is *optional context* sitting next to
            # the two calls that matter, and search_cves only catches
            # httpx.HTTPError and TimeoutException — NVD answers 200 with an
            # HTML maintenance page under load, so .json() raises
            # JSONDecodeError, which is a ValueError and escapes both. That
            # aborted the run after the parse call was already billed and
            # before the call that produces the verdict. The duplicate check
            # writes a link on the row, never a verdict — duplicates are the
            # operator's call.
            if not context_checked and (parsed or attempts == len(backends)):
                context_checked = True
                try:
                    _check_cves(report, operator_config)
                except Exception:
                    logger.warning(
                        "CVE lookup failed for report #%d; triaging without it",
                        report.pk,
                        exc_info=True,
                    )
                try:
                    _check_duplicates(report, operator_config)
                except Exception:
                    logger.warning(
                        "Duplicate detection failed for report #%d; triaging without it",
                        report.pk,
                        exc_info=True,
                    )

            logger.info("Analyzing report #%d via LLM (%s)", report.pk, backend.label)
            outcome = _analyze_report(
                report,
                backend,
                project_context,
                security_model=security_model,
                cve_candidates=report.cve_matches,
                learned_guidance=learned_guidance,
            )
            produced_verdict = outcome.wrote_verdict
            no_verdict_reason = outcome.reason
            # An analyze call that *raised* (unknown model, backend down, rate
            # limit) is worth trying the next backend on. A call that answered
            # with nothing usable is the model's behaviour, and every remaining
            # backend is likely to answer the same way — don't bill them all.
            if produced_verdict or not outcome.call_failed:
                break
    finally:
        _restore_untriaged_status(report)

    if not produced_verdict:
        # Has to reach the caller. The worker marks a command that returns
        # normally as "completed", and on a *re-triage* the report still carries
        # the previous run's fields — so a silent return presented a stale
        # verdict as this run's answer, on the one page where being wrong about
        # a vulnerability matters most.
        if attempts > 1:
            no_verdict_reason = f"{no_verdict_reason} (tried {attempts} backends)"
        raise TriageIncompleteError(
            f"Triage produced no verdict for report #{report.pk}: {no_verdict_reason}"
        )

    logger.info(
        "Triage complete for report #%d: severity=%r status=%r poc_plausible=%s",
        report.pk,
        report.assessed_severity,
        report.status,
        report.poc_plausible,
    )

    return report


def requires_auth_disabled_evidence(text: str) -> str:
    """The line suggesting the scenario needs authentication switched off, or "".

    Cheap string match over the report, run before any LLM call — see
    ``_AUTH_DISABLED_RES`` for what qualifies and the guards for what vetoes a
    match. The returned line is evidence: it goes into the triage summary and
    the log so the close is auditable rather than a verdict from nowhere.
    """
    enabled_assignment = _AUTH_ENABLED_CONFIG_RE.search(text) is not None
    for entry in _AUTH_DISABLED_RES:
        for match in entry.pattern.finditer(text):
            window = text[max(0, match.start() - _GUARD_WINDOW_CHARS) : match.start()]
            if _NEGATION_GUARD_RE.search(window):
                continue
            if entry.actor_guarded:
                line_start = text.rfind("\n", 0, match.start()) + 1
                near = text[
                    max(line_start, match.start() - _ACTOR_GUARD_WINDOW_CHARS) : match.start()
                ]
                if _ACTOR_GUARD_RE.search(near):
                    continue
            if _FORWARD_GUARD_RE.match(
                text[match.end() : match.end() + _FORWARD_GUARD_WINDOW_CHARS]
            ):
                continue
            if _alt_precondition_follows(text, match):
                continue
            if entry.is_config and _config_match_is_vetoed(text, match, enabled_assignment):
                continue
            return _evidence_line(text, match)
    return ""


def _alt_precondition_follows(text: str, match: re.Match[str]) -> bool:
    """Whether an "or" joins the auth-disabled match to another precondition.

    "spark.authenticate=false ... OR shared-secret multi-tenant deployment"
    is a branching precondition: the vulnerability holds in *either* branch,
    so it does not require auth disabled and the cheap close must not fire.

    Bounded to the clause the match sits in (up to the next ``,`, `;`, `.` or
    newline, or ``_ALT_PRECONDITION_WINDOW_CHARS`` chars) so an "or" in a later
    sentence about something else can't veto a real auth-disabled-only
    finding. Parenthetical asides are stripped first — "(Spark DEFAULT; 0
    preconditions per brief calibration)" sits between the config and the OR
    and its `;` would otherwise end the clause before the OR that the guard
    is looking for.
    """
    tail = text[match.end() : match.end() + _ALT_PRECONDITION_WINDOW_CHARS]
    # Strip parenthetical asides: a `;` or `,` inside "(...)" is part of the
    # aside, not a clause boundary. Repeat for one level of nesting.
    while True:
        stripped = re.sub(r"\([^()]*\)", " ", tail)
        if stripped == tail:
            break
        tail = stripped
    terminator = _CLAUSE_TERMINATOR_RE.search(tail)
    if terminator is not None:
        tail = tail[: terminator.start()]
    return _ALT_PRECONDITION_RE.search(tail) is not None


def procedural_close_if_evidence(report: SecurityReport) -> bool:
    """Run the procedural auth-disabled close on *report*; True if it closed.

    Public door onto the cheap close for the bulk re-triage button, which runs
    it synchronously before queuing any LLM work — a report whose own text
    says the scenario needs auth off is closed in milliseconds and never billed.
    Same gate as the in-pipeline close: never-been-triaged only, so a report
    the operator reopened (status back to ``new`` but with an old summary) is
    not re-closed by the same regex. Re-reads status first, for the same reason
    ``triage_report`` does — the operator can rule from the detail page while
    the bulk button's loop is mid-flight.
    """
    stored = _current_status(report)
    if stored != "new":
        return False
    if (
        report.triage_summary
        or report.poc_assessment
        or report.poc_plausible is not None
        or report.is_expected_behavior
    ):
        return False
    evidence = requires_auth_disabled_evidence(f"{report.title}\n{report.raw_text}")
    if not evidence:
        return False
    _close_requires_auth_disabled(report, evidence)
    return True


def _config_match_is_vetoed(text: str, match: re.Match[str], enabled_assignment: bool) -> bool:
    """The vetoes only the structured spelling needs — it can't tell a
    precondition from its surroundings, so the surroundings decide: a property
    name whose "=false" means ON, a true-y auth assignment anywhere else in
    the report, or a config line next door all send the report to the LLM."""
    name_tokens = set(re.split(r"[._-]+", match.group(1).lower()))
    if name_tokens & _CONFIG_NAME_VETO_TOKENS:
        return True
    if enabled_assignment:
        return True
    line_start = text.rfind("\n", 0, match.start()) + 1
    line_end = text.find("\n", match.end())
    line_end = len(text) if line_end == -1 else line_end
    prev_line = next(
        (line for line in reversed(text[:line_start].splitlines()) if line.strip()), ""
    )
    next_line = next((line for line in text[line_end:].splitlines() if line.strip()), "")
    return bool(_looks_like_config_line(prev_line) or _looks_like_config_line(next_line))


def _evidence_line(text: str, match: re.Match[str]) -> str:
    """The line containing *match*, whitespace-collapsed.

    A line longer than _EVIDENCE_MAX_CHARS is windowed around the match: a
    bare prefix truncation can cut the matched text off entirely, and the
    match is the evidence.
    """
    line_start = text.rfind("\n", 0, match.start()) + 1
    line_end = text.find("\n", match.end())
    line = text[line_start : line_end if line_end != -1 else len(text)]
    if len(line) > _EVIDENCE_MAX_CHARS:
        start = min(
            max(0, match.start() - line_start - _EVIDENCE_MAX_CHARS // 4),
            len(line) - _EVIDENCE_MAX_CHARS,
        )
        line = line[start : start + _EVIDENCE_MAX_CHARS]
    return " ".join(line.split())


def _close_requires_auth_disabled(report: SecurityReport, evidence: str) -> None:
    """Close *report* as not-a-vulnerability: its scenario needs auth switched off.

    Writes the machine's verdict to ``auto_triage_status`` and the reasoning to
    ``triage_summary`` — the same fields an LLM run would set, so the detail
    page renders it like any other triage result — and leaves ``status`` alone.
    ``status`` is the operator's field; the suggestion is staged for an Agree
    click rather than applied. "invalid" is the one auto-close triage makes:
    the report describes the absence of a feature the documentation already
    tells the operator to turn on. The LLM path marks the same scenario class
    "expected-behavior" instead — the regex close is stickier because a literal
    "spark.authenticate=false" is the more certain signal.
    """
    report.auto_triage_status = "invalid"
    report.triage_summary = (
        "Closed without model triage: the report's own text says the scenario "
        "requires authentication to be disabled/turned off. That is a "
        "configuration choice, not a vulnerability — authentication exists "
        f"precisely to prevent it. Matched: {evidence!r}. If this is wrong, "
        "re-run triage; the full pipeline will assess it."
    )
    report.save(update_fields=["auto_triage_status", "triage_summary", "updated_at"])
    logger.info(
        "Closed security report #%d without LLM triage: the scenario requires "
        "authentication to be disabled (matched %r).",
        report.pk,
        evidence,
    )


def _current_status(report: SecurityReport) -> str:
    """The report's status as stored, not as this instance last saw it.

    Triage is a background command, so an operator verdict can land mid-run.
    Falls back to the in-memory value if the row can't be re-read.
    """
    try:
        stored = type(report).objects.filter(pk=report.pk).values_list("status", flat=True).first()
    except Exception:
        logger.debug("Could not re-read status for report #%d", report.pk, exc_info=True)
        return str(report.status)
    return str(stored) if stored is not None else str(report.status)


def _restore_untriaged_status(report: SecurityReport) -> None:
    """Return a report still marked ``triaging`` to the ``new`` queue.

    Runs from a ``finally``, so it swallows its own errors: raising here would
    replace whatever exception was already on its way out with a much less
    informative one.
    """
    # Re-read: an operator verdict set while triage ran must not be undone.
    stored_status = _current_status(report)
    if stored_status != "triaging":
        report.status = stored_status
        return
    try:
        report.status = "new"
        report.save(update_fields=["status", "updated_at"])
    except Exception:
        logger.exception("Could not return report #%d to the new queue", report.pk)
        return
    logger.warning(
        "Triage produced no verdict for report #%d; returned it to the new queue.",
        report.pk,
    )


def _get_triage_backends(operator_config: OperatorConfig) -> list[BaseLLMBackend]:
    """Triage's backends, in fallthrough order.

    Its own ``security_triage.llm_backend`` first if configured, then
    ``llm_backends`` in order. The override exists because ``llm_backends[0]``
    is shared by three unrelated consumers — triage, ``review/shepherding.py``
    and the ``llm_checks`` path — so "which model does triage use" was not
    separately expressible, and picking one for triage picked it for the other
    two. That is a real bind rather than a tidiness complaint: triage reads
    security reports and wants the strongest model available, while the per-PR
    check paths run on every PR and want the cheap local one.

    A list rather than one backend because a misconfigured or down backend
    ("unknown model", connection refused) should cost a fallthrough, not the
    report's verdict: the worker marks the command failed and the report sits
    in the new queue until someone notices. Dedup is on the connection
    identity, not just provider+model: two entries differing only in
    ``api_key_env`` or ``reviewer`` are different backends (a rate-limited key
    and a different remote box are exactly what fallthrough is for).
    """
    from franktheunicorn.review.backends import get_backend
    from franktheunicorn.review.backends.base import BaseLLMBackend

    configs: list[LLMBackendConfig] = []
    override = operator_config.security_triage.llm_backend
    if override is not None:
        logger.info(
            "Triage is using security_triage.llm_backend (%s/%s), not llm_backends[0].",
            override.provider,
            override.model or "default model",
        )
        configs.append(override)
    configs.extend(operator_config.llm_backends)

    backends: list[BaseLLMBackend] = []
    seen: set[tuple[str, str, str, str, str]] = set()
    for cfg in configs:
        key = (cfg.provider, cfg.model, cfg.base_url, cfg.api_key_env, cfg.reviewer)
        if key in seen:
            continue
        seen.add(key)
        backend = get_backend(cfg)
        if isinstance(backend, BaseLLMBackend):
            backends.append(backend)
        else:
            logger.warning(
                "Triage backend %s/%s is not an LLM backend; skipping it.",
                cfg.provider,
                cfg.model or "default model",
            )
    return backends


def _get_triage_backend(operator_config: OperatorConfig) -> BaseLLMBackend | None:
    """The first of :func:`_get_triage_backends`, for callers that only want to
    know which backend triage would use."""
    backends = _get_triage_backends(operator_config)
    return backends[0] if backends else None


def _call_llm(
    backend: BaseLLMBackend,
    system_prompt: str,
    user_message: str,
    *,
    action_type: str,
    project_id: int | None = None,
) -> dict[str, object] | None:
    """Call the LLM backend and parse JSON response. Returns None on failure.

    Goes through the backend's metered-call path so the triage call's token
    usage is recorded as a CostRecord (previously each caller recorded cost
    separately, which the raw ``_call_api`` bypass silently skipped).
    """
    raw_response = backend.metered_call(
        system_prompt,
        user_message,
        action_type=action_type,
        project_id=project_id,
    )
    return _safe_json_parse(raw_response)


#: Spellings a model actually uses for yes and no. Anything outside both sets is
#: not an answer, and guessing which way it leans is how a hedge becomes a verdict.
_TRUE_WORDS = frozenset({"true", "yes", "y", "1", "plausible", "confirmed", "likely"})
_FALSE_WORDS = frozenset({"false", "no", "n", "0", "implausible", "not plausible", "unlikely"})


def _coerce_tristate(value: object) -> bool | None:
    """Coerce an LLM JSON value to True, False, or None for "didn't say".

    ``value.lower() == "true"`` mapped everything else to False, which is not a
    missing answer but an affirmative negative one — and this feeds a green
    "POC: Not Plausible" badge on a live vulnerability report. Measured against
    the old helper: 'unknown', 'unclear', 'maybe', 'n/a' and '' all returned
    False, and so did **'yes'**, so a model that said the POC works was displayed
    as saying it doesn't.

    None here is a real state: ``poc_plausible`` is nullable and the dashboard
    renders nothing for it rather than picking a side.
    """
    if isinstance(value, bool):
        return value
    if value is None:
        return None
    if isinstance(value, int | float):
        return bool(value)
    if isinstance(value, str):
        word = value.strip().lower()
        if word in _TRUE_WORDS:
            return True
        if word in _FALSE_WORDS:
            return False
        return None
    return None


def _coerce_bool(value: object) -> bool:
    """Tri-state collapsed to a bool, for fields that aren't nullable.

    ``is_expected_behavior`` has no null, and "the model didn't say" is not a
    reason to claim behaviour is expected — so an unrecognised value is False
    here, which is the conservative direction for that field specifically.
    """
    return _coerce_tristate(value) is True


def _parse_report(report: SecurityReport, backend: BaseLLMBackend) -> bool:
    """Parse raw report text into structured fields via LLM.

    Returns whether it populated anything — the caller gates the CVE and
    duplicate lookups on that, and on a backend fallthrough the first parse
    may have raised while the next one succeeds.
    """
    system_prompt, user_message = build_parse_prompt(report.raw_text)
    project_id = report.project_id if report.project else None

    try:
        parsed = _call_llm(
            backend,
            system_prompt,
            user_message,
            action_type="security-parse",
            project_id=project_id,
        )
    except Exception:
        logger.exception("Failed to parse security report %d", report.pk)
        return False

    if parsed:
        report.title = report.title or str(parsed.get("title", ""))[:500]
        report.parsed_component = str(parsed.get("component", ""))[:500]
        report.parsed_poc = str(parsed.get("poc", ""))
        report.parsed_impact = str(parsed.get("impact", ""))
        severity = str(parsed.get("severity", "unknown")).lower()
        report.assessed_severity = severity if severity in _VALID_SEVERITIES else "unknown"

        if not report.reporter_name and parsed.get("reporter_name"):
            report.reporter_name = str(parsed["reporter_name"])[:255]
        if not report.reporter_email and parsed.get("reporter_email"):
            report.reporter_email = str(parsed["reporter_email"])[:255]

        report.save(
            update_fields=[
                "title",
                "parsed_component",
                "parsed_poc",
                "parsed_impact",
                "assessed_severity",
                "reporter_name",
                "reporter_email",
                "updated_at",
            ]
        )
        return True
    return False


class _AnalysisOutcome(NamedTuple):
    """What ``_analyze_report`` did: whether a verdict was written, why not,
    and whether the LLM call itself raised (the caller's fallthrough signal)."""

    wrote_verdict: bool
    reason: str
    call_failed: bool


def _suggested_status(report: SecurityReport) -> str:
    """The machine's suggested verdict from what triage populated.

    ``expected-behavior`` beats everything (it's not a vulnerability at all);
    a plausible POC that isn't expected behaviour is ``valid``; an implausible
    one is ``invalid``; a model that said neither leaves it blank for
    "inconclusive". This is what the Agree button copies into ``status`` and
    what the version/verify follow-on gates on — "looks valid" is exactly
    ``valid``, so an inconclusive or expected-behavior report doesn't spend
    the agent runs.
    """
    if report.is_expected_behavior:
        return "expected-behavior"
    if report.poc_plausible is True:
        return "valid"
    if report.poc_plausible is False:
        return "invalid"
    return ""


def _analyze_report(
    report: SecurityReport,
    backend: BaseLLMBackend,
    project_context: str,
    security_model: str = "",
    cve_candidates: list[object] | None = None,
    learned_guidance: str = "",
) -> _AnalysisOutcome:
    """Run triage analysis on parsed report.

    Deliberately does not raise on an LLM failure (callers rely on that), so
    the return value is the only way the caller can tell "assessed" from "the
    model was unreachable" — and the dashboard needs that distinction to avoid
    presenting a previous run's verdict as this one's.

    The reason is carried out rather than only logged, because the caller turns
    this into the operator-visible error and "Triage produced no verdict" told
    them nothing about which of the three causes it was.
    """
    system_prompt, user_message = build_triage_prompt(
        parsed_component=report.parsed_component,
        parsed_poc=report.parsed_poc,
        parsed_impact=report.parsed_impact,
        project_context=project_context,
        security_model=security_model,
        cve_candidates=cve_candidates,
        learned_guidance=learned_guidance,
    )

    project_id = report.project_id if report.project else None

    try:
        analysis = _call_llm(
            backend,
            system_prompt,
            user_message,
            action_type="security-triage",
            project_id=project_id,
        )
    except Exception as exc:
        from franktheunicorn.review.backends.base import looks_offline

        if looks_offline(exc):
            # The commonest cause by far, and it is a configuration state rather
            # than a bug — so a warning naming it, not a traceback through httpx.
            detail = str(exc).splitlines()[0][:200] if str(exc).strip() else type(exc).__name__
            logger.warning(
                "Triage of report #%d could not reach the LLM backend (%s). Start it, or "
                "configure a backend that is up.",
                report.pk,
                detail,
            )
            return _AnalysisOutcome(
                False, f"the LLM backend is not reachable ({detail})", call_failed=True
            )
        logger.exception("Failed to analyze security report %d", report.pk)
        return _AnalysisOutcome(
            False, f"the LLM call raised {type(exc).__name__}: {exc}"[:300], call_failed=True
        )

    # "Did the model answer?", not "is the dict non-empty". A reply with none of
    # these keys — a wrong key name is entirely ordinary — used to count as a
    # verdict: the command landed "completed" instead of "failed", and since all
    # five fields are overwritten unconditionally and listed in update_fields, a
    # re-triage *destroyed* the previous run's verdict and stored blanks.
    if analysis and any(
        key in analysis
        for key in (
            "poc_plausible",
            "poc_assessment",
            "is_expected_behavior",
            "expected_behavior_explanation",
            "triage_summary",
        )
    ):
        # Absent key means "the model didn't assess this", which is what the
        # field's null=True is for — NOT "not plausible".
        report.poc_plausible = _coerce_tristate(analysis.get("poc_plausible"))
        report.poc_assessment = str(analysis.get("poc_assessment", ""))
        report.is_expected_behavior = _coerce_bool(analysis.get("is_expected_behavior", False))
        report.expected_behavior_explanation = str(
            analysis.get("expected_behavior_explanation", "")
        )
        report.triage_summary = str(analysis.get("triage_summary", ""))

        severity = str(analysis.get("assessed_severity", "")).lower()
        if severity in _VALID_SEVERITIES:
            report.assessed_severity = severity

        # The machine's verdict is staged, not applied. ``status`` is the
        # operator's field — a non-auto status is unambiguously a ruling — so
        # triage writes its suggestion to ``auto_triage_status`` and leaves
        # ``status`` alone. Three cases, told apart by whether *this run*
        # claimed the in-flight ``triaging`` status at the top of triage_report:
        #
        # 1. Auto-managed and the operator didn't rule mid-run (stored status
        #    is still ``triaging``): stage the suggestion, restore ``status``
        #    to ``new``.
        # 2. We claimed ``triaging`` and the operator ruled from the detail
        #    page while the LLM was thinking (in-memory is still ``triaging``,
        #    stored is now a verdict): keep their verdict and do NOT re-stage
        #    a contradicting suggestion — a "Triage suggests: valid [Agree]"
        #    banner on a report just ruled invalid is one misclick from undoing
        #    them, and it would bill the version/verify follow-on on a
        #    non-vulnerability.
        # 3. The operator had ruled *before* this run (we never claimed
        #    ``triaging``; this is an explicit re-triage they asked for):
        #    stage the fresh suggestion for them to consider without
        #    overwriting their verdict.
        #
        # Re-read status first: triage runs in the worker, so this instance's
        # copy is minutes stale.
        stored_status = _current_status(report)
        staged_fields: list[str] = [
            "poc_plausible",
            "poc_assessment",
            "is_expected_behavior",
            "expected_behavior_explanation",
            "triage_summary",
            "assessed_severity",
            "status",
            "updated_at",
        ]
        if stored_status in _AUTO_MANAGED_STATUSES:
            report.auto_triage_status = _suggested_status(report)
            report.status = "new"
            staged_fields.append("auto_triage_status")
        elif report.status != "triaging":
            # Case 3: explicit re-triage of an operator-ruled report. Stage
            # the fresh suggestion; keep their status.
            report.auto_triage_status = _suggested_status(report)
            report.status = stored_status
            staged_fields.append("auto_triage_status")
        else:
            # Case 2: the operator ruled mid-run. Keep their verdict; leave the
            # staging field as the verdict form left it (cleared).
            report.status = stored_status

        report.save(update_fields=staged_fields)
        return _AnalysisOutcome(True, "", call_failed=False)
    return _AnalysisOutcome(
        False,
        "the model replied without any of the fields triage reads (poc_plausible, "
        "poc_assessment, is_expected_behavior, expected_behavior_explanation, "
        "triage_summary) — check the model is one that follows a JSON schema",
        call_failed=False,
    )


def _check_cves(report: SecurityReport, operator_config: OperatorConfig) -> None:
    """Search NVD for matching CVEs."""
    keyword = report.parsed_component or report.title
    if not keyword:
        logger.debug("Skipping CVE lookup for report #%d: no keyword available", report.pk)
        return

    logger.info("CVE lookup for report #%d (keyword=%r)", report.pk, keyword[:80])
    api_key_env = operator_config.security_triage.nvd_api_key_env
    matches = search_cves(keyword, api_key_env=api_key_env)
    logger.info("CVE lookup complete for report #%d: %d match(es)", report.pk, len(matches))

    # Always save results (even empty) so stale matches are cleared on re-run.
    report.cve_matches = [m.to_dict() for m in matches]
    report.save(update_fields=["cve_matches", "updated_at"])


def _check_duplicates(report: SecurityReport, operator_config: OperatorConfig) -> None:
    """Link *report* to an earlier report of the same hole, if there is one.

    Local string comparison, no model call — see ``security.duplicates`` for why
    (500 reports is 125,000 pairs, and a model call per pair is a bill rather than
    a feature).

    Clears a previous link when a re-triage genuinely finds nothing, for the same
    reason ``_check_cves`` always saves: a stale link from an earlier run presented
    as this run's answer is worse than no link, because the operator has no way to
    tell it's stale.

    Two things are never cleared. A link the *operator* set by hand — marked by a
    NULL ``duplicate_confidence``, since detection always records a score — because
    that is a decision and not ours to revoke. And anything at all when the check
    **declined to run**: switching the feature off used to delete every existing
    link on the next re-triage, and report it as "found no match above the
    threshold", which is a negative result invented by a check that never happened.
    ``Detection.ran`` is what tells those apart.
    """
    from franktheunicorn.security.duplicates import detect_for_report

    config = operator_config.security_triage.duplicates
    outcome = detect_for_report(report, config)
    if not outcome.ran:
        logger.debug(
            "Duplicate detection did not run for report #%d (%s); leaving any existing link alone.",
            report.pk,
            outcome.declined or "no reason given",
        )
        return
    if outcome.match is not None:
        return
    if report.duplicate_of_id is not None and report.duplicate_confidence is not None:
        logger.info(
            "Clearing report #%d's previous duplicate link to #%s: this run compared it "
            "and found no match above the threshold (%.2f).",
            report.pk,
            report.duplicate_of_id,
            config.threshold,
        )
        report.duplicate_of = None
        report.duplicate_confidence = None
        report.duplicate_reason = ""
        report.save(
            update_fields=[
                "duplicate_of",
                "duplicate_confidence",
                "duplicate_reason",
                "updated_at",
            ]
        )


def _read_file(path: Path, max_chars: int = 5000) -> str | None:
    """Read a file's text content, returning None on failure."""
    try:
        return path.read_text(encoding="utf-8", errors="replace")[:max_chars]
    except OSError:
        logger.debug("Failed to read %s", path, exc_info=True)
        return None


# Conventional in-repo threat-model files, in priority order. SECURITY.md is
# deliberately excluded — by convention it is a vulnerability-reporting policy,
# not a statement of the project's trust boundaries.
_SECURITY_MODEL_FILENAMES: tuple[str, ...] = (
    ".frank/security-model.md",
    "SECURITY_MODEL.md",
    "SECURITY-MODEL.md",
    "THREAT_MODEL.md",
    "docs/security-model.md",
    "docs/threat-model.md",
)


def _resolve_repo_path(project_config: ProjectConfig | None) -> Path | None:
    """Return the checked-out repo directory for a project, or None.

    None when there is no project config, no configured repos dir, or the repo
    has not been cloned yet.
    """
    if project_config is None:
        return None

    from django.conf import settings

    repos_dir_str = getattr(settings, "FRANK_REPOS_DIR", "")
    if not repos_dir_str:
        return None

    repo_path = Path(repos_dir_str) / project_config.owner / project_config.repo
    return repo_path if repo_path.is_dir() else None


def _resolve_security_model(project_config: ProjectConfig | None) -> str:
    """Resolve the project's security model (trust boundaries) for triage.

    Precedence:
      1. Inline ``security_model`` prose in the project YAML (explicit override).
      2. An explicit ``security_model_file`` path, loaded from the repo.
      3. A conventional threat-model file auto-discovered in the repo.
      4. Empty string (triage falls back to README/SECURITY.md context only).

    Files are read fresh from the checked-out base repo each time (no cache),
    and paths are constrained to inside the repo directory.
    """
    if project_config is None:
        return ""

    inline = project_config.security_model.strip()
    if inline:
        return inline

    repo_path = _resolve_repo_path(project_config)
    if repo_path is None:
        return ""

    explicit = project_config.security_model_file.strip()
    if explicit:
        # Explicit path wins over auto-discovery. Constrain it to the repo.
        text = _read_repo_file(repo_path, explicit)
        return text.strip() if text else ""

    for name in _SECURITY_MODEL_FILENAMES:
        text = _read_repo_file(repo_path, name)
        if text and text.strip():
            return text.strip()
    return ""


def _read_repo_file(repo_path: Path, relative: str, max_chars: int = 8000) -> str | None:
    """Read a repo-relative file, refusing to escape the repo directory."""
    candidate = (repo_path / relative).resolve()
    if not candidate.is_relative_to(repo_path.resolve()):
        return None
    if not candidate.is_file():
        return None
    return _read_file(candidate, max_chars=max_chars)


def _load_project_context(
    report: SecurityReport,
    project_config: ProjectConfig | None,
) -> str:
    """Load project README and docs for triage context."""
    if report.project is None:
        return ""

    repo_path = _resolve_repo_path(project_config)
    if repo_path is None:
        return ""

    parts: list[str] = []

    # Read first available README variant.
    for name in ("README.md", "README.rst", "README.txt", "README"):
        readme = repo_path / name
        if readme.is_file():
            text = _read_file(readme)
            if text:
                parts.append(f"### README\n{text}")
            break

    # Read SECURITY.md if present (usually the reporting policy).
    text = _read_file(repo_path / "SECURITY.md", max_chars=3000)
    if text:
        parts.append(f"### SECURITY.md\n{text}")

    # Read a security guidance doc if present. Many projects keep their actual
    # security posture here rather than in SECURITY.md — Apache Spark, for
    # example, documents auth/encryption/trust boundaries in docs/security.md.
    # This is supporting context, separate from the authoritative
    # trust-boundary `security_model`.
    for name in ("docs/security.md", "docs/security.rst", "docs/SECURITY.md"):
        text = _read_repo_file(repo_path, name, max_chars=4000)
        if text and text.strip():
            parts.append(f"### {name}\n{text}")
            break

    # Read the reported component file if identifiable and safe.
    if report.parsed_component:
        text = _read_repo_file(repo_path, report.parsed_component, max_chars=5000)
        if text:
            parts.append(f"### Source: {report.parsed_component}\n{text}")

    return "\n\n".join(parts)


def _safe_json_parse(raw_text: str) -> dict[str, object] | None:
    """Parse JSON from an LLM response, tolerating prose around it.

    Two stages, because a fence is not the only way a model wraps its answer.
    Stripping the fence and calling ``json.loads`` handles the well-behaved case;
    when there is no fence it doesn't, and "Here's my assessment: {...} Hope that
    helps!" fails outright.

    That used to be tolerable and isn't any more. The ``agent-cli`` backend runs a
    coding-agent CLI, which narrates by default — and the local models people put in
    ``llm_backends`` do too. A strict parse turns a perfectly good triage answer into
    "the LLM response is not valid JSON", after the call has been paid for.

    So the fallback scans for a balanced JSON object, reusing the verifier's scan
    rather than writing a second one: it tries multiple candidates (a model
    describing code emits braces in prose), searches **tail-first** so a JSON object
    quoted in the *report* cannot outrank the model's real answer, and is bounded by
    a work budget instead of being quadratic on model-controlled text.
    """
    from franktheunicorn.review.backends.base import _CODE_FENCE_RE

    raw_text = raw_text.strip()
    if not raw_text:
        return None

    fence_match = _CODE_FENCE_RE.search(raw_text)
    candidate_text = fence_match.group(1) if fence_match else raw_text

    try:
        data = json.loads(candidate_text)
    except json.JSONDecodeError:
        pass
    else:
        if isinstance(data, dict):
            return data

    from franktheunicorn.review.backends.base import json_object_candidates

    for blob in json_object_candidates(raw_text):
        try:
            data = json.loads(blob)
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(data, dict) and data:
            return data

    logger.warning(
        "No JSON object found in the LLM response for security triage (%d chars, starting %r).",
        len(raw_text),
        raw_text[:120],
    )
    return None
