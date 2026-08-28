"""Tests for "have I already got this one?" — dedup against your own backlog.

The properties worth pinning are about not being confidently wrong. The feature is
a heuristic on a security page, so it must link the same hole reported twice, must
*not* link every finding in a scanner bundle to every other (they share the tool's
boilerplate), must never claim a verdict, and must not tie itself in knots when run
repeatedly over a backlog.
"""

from __future__ import annotations

from typing import Any

import pytest

from franktheunicorn.config.models import SecurityDuplicateConfig
from franktheunicorn.security.duplicates import (
    build_signature,
    detect_across_backlog,
    detect_for_report,
    find_duplicate,
    link_duplicate,
    resolve_canonical,
    score_pair,
    would_create_cycle,
)
from tests.factories import ProjectFactory, SecurityReportFactory


def _config(**overrides: Any) -> SecurityDuplicateConfig:
    return SecurityDuplicateConfig(**overrides)


_RPC_REPORT = {
    "title": "Unsafe deserialization in the Spark RPC endpoint",
    "parsed_component": "core",
    "raw_text": (
        "Sending a crafted serialized payload to the RPC endpoint on port 7077 "
        "causes core/src/main/scala/org/apache/spark/rpc/NettyRpcEnv.scala to "
        "deserialize attacker data without validation, giving remote code execution."
    ),
}


@pytest.mark.django_db
class TestScoring:
    def test_the_same_hole_reported_twice_is_linked(self) -> None:
        """Different wording, same bug, same file. This is the case the whole
        feature exists for — a disclosure forwarded by two different people."""
        project = ProjectFactory(owner="apache", repo="spark")
        first = SecurityReportFactory(project=project, **_RPC_REPORT)
        second = SecurityReportFactory(
            project=project,
            title="RCE via deserialization in Spark RPC endpoint",
            parsed_component="core",
            raw_text=(
                "The RPC endpoint deserializes untrusted serialized data. See "
                "core/src/main/scala/org/apache/spark/rpc/NettyRpcEnv.scala — no "
                "validation of the payload, so remote code execution is possible."
            ),
        )

        match = find_duplicate(second, [first], _config())

        assert match is not None
        assert match.report_id == first.pk
        assert match.score >= _config().threshold
        assert match.reason  # a bare number is not something an operator can check

    def test_two_different_bugs_in_the_same_project_are_not_linked(self) -> None:
        project = ProjectFactory(owner="apache", repo="spark")
        first = SecurityReportFactory(project=project, **_RPC_REPORT)
        other = SecurityReportFactory(
            project=project,
            title="Stored XSS in the Spark History Server UI",
            parsed_component="history-server",
            raw_text=(
                "An application name containing a script tag is rendered unescaped "
                "in core/src/main/resources/org/apache/spark/ui/static/historypage.js "
                "so any viewer executes it."
            ),
        )

        assert find_duplicate(other, [first], _config()) is None

    def test_a_scanner_bundle_sharing_boilerplate_is_not_all_one_duplicate(self) -> None:
        """The failure mode that makes a naive implementation useless. Every entry in
        a scanner archive carries the same preamble, so body overlap alone links all
        of them to each other and the operator learns nothing."""
        project = ProjectFactory(owner="apache", repo="spark")
        boilerplate = (
            "Scan performed by acme-scanner v4.2 against the repository at the "
            "revision below. Confidence: medium. Category: input validation. "
            "Remediation guidance: validate all untrusted input before use. "
            "This finding was produced automatically and requires manual review. "
        )
        a = SecurityReportFactory(
            project=project,
            title="Unvalidated path in FileServer",
            parsed_component="core",
            raw_text=boilerplate + "core/src/main/scala/org/apache/spark/FileServer.scala",
        )
        b = SecurityReportFactory(
            project=project,
            title="Weak random in SessionTokenGenerator",
            parsed_component="core",
            raw_text=boilerplate
            + "core/src/main/scala/org/apache/spark/SessionTokenGenerator.scala",
        )

        assert find_duplicate(b, [a], _config()) is None

    def test_the_same_scanner_finding_in_a_different_archive_is_certain(self) -> None:
        """Not "similar" — the same tool ran twice on the same code and numbered it
        the same. Identity, so it short-circuits."""
        project = ProjectFactory(owner="apache", repo="spark")
        a = SecurityReportFactory(
            project=project, finding_id="f0042", source_archive="scan-january.zip", **_RPC_REPORT
        )
        b = SecurityReportFactory(
            project=project,
            finding_id="f0042",
            source_archive="scan-february.zip",
            title="something else entirely",
            raw_text="unrelated words",
        )

        match = score_pair(build_signature(b), build_signature(a), _config())

        assert match.score == 1.0
        assert "same scanner finding id" in match.reason

    def test_the_same_finding_id_within_one_archive_is_not_a_duplicate(self) -> None:
        """Within one archive the ids are unique, so equality there would mean the
        row is being compared with itself."""
        project = ProjectFactory(owner="apache", repo="spark")
        a = SecurityReportFactory(
            project=project,
            finding_id="f0042",
            source_archive="scan.zip",
            title="alpha",
            raw_text="completely unrelated text about alpha",
        )
        b = SecurityReportFactory(
            project=project,
            finding_id="f0042",
            source_archive="scan.zip",
            title="beta",
            raw_text="completely unrelated text about beta",
        )

        assert score_pair(build_signature(b), build_signature(a), _config()).score < 1.0

    def test_an_identical_title_is_treated_as_certain(self) -> None:
        project = ProjectFactory(owner="apache", repo="spark")
        a = SecurityReportFactory(project=project, title="RCE in RPC", raw_text="one account")
        b = SecurityReportFactory(project=project, title="RCE in RPC", raw_text="another account")

        assert score_pair(build_signature(b), build_signature(a), _config()).score == 1.0

    def test_trust_identical_title_can_be_turned_off(self) -> None:
        """For a scanner whose titles are templated to the point of colliding
        between genuinely different findings."""
        project = ProjectFactory(owner="apache", repo="spark")
        a = SecurityReportFactory(project=project, title="Potential issue found", raw_text="alpha")
        b = SecurityReportFactory(project=project, title="Potential issue found", raw_text="beta")

        match = score_pair(
            build_signature(b), build_signature(a), _config(trust_identical_title=False)
        )

        assert match.score < 1.0

    def test_a_shared_component_alone_does_not_link_anything(self) -> None:
        """On a project like Spark half the backlog is "core", so the component is
        corroboration and not evidence."""
        project = ProjectFactory(owner="apache", repo="spark")
        a = SecurityReportFactory(
            project=project,
            title="Weak cipher negotiated during shuffle transfer",
            parsed_component="core",
            raw_text="The shuffle service accepts an obsolete cipher suite.",
        )
        b = SecurityReportFactory(
            project=project,
            title="Log file written world readable by the executor",
            parsed_component="core",
            raw_text="Executor logs land with permissive filesystem modes.",
        )

        assert find_duplicate(b, [a], _config()) is None

    def test_a_tie_prefers_the_earlier_report(self) -> None:
        """The earlier one carries the triage, the verification rows and possibly the
        operator's notes, so it is the one worth keeping as canonical."""
        project = ProjectFactory(owner="apache", repo="spark")
        first = SecurityReportFactory(project=project, **_RPC_REPORT)
        second = SecurityReportFactory(project=project, **_RPC_REPORT)
        third = SecurityReportFactory(project=project, **_RPC_REPORT)

        match = find_duplicate(third, [second, first], _config())

        assert match is not None
        assert match.report_id == first.pk

    def test_zero_weights_are_rejected_at_config_time(self) -> None:
        """Otherwise every pair scores 0.0 and the feature silently never fires —
        which presents as "duplicate detection is broken" with a config that looks
        deliberate."""
        with pytest.raises(ValueError, match="must be"):
            SecurityDuplicateConfig(title_weight=0.0, body_weight=0.0, path_weight=0.0)

    @pytest.mark.parametrize("bad", [-0.1, 1.5])
    def test_a_threshold_outside_zero_to_one_is_rejected(self, bad: float) -> None:
        with pytest.raises(ValueError, match="between 0 and 1"):
            SecurityDuplicateConfig(threshold=bad)


@pytest.mark.django_db
class TestLinking:
    def test_the_link_and_its_reasoning_are_both_stored(self) -> None:
        project = ProjectFactory(owner="apache", repo="spark")
        first = SecurityReportFactory(project=project, **_RPC_REPORT)
        second = SecurityReportFactory(project=project, **_RPC_REPORT)

        assert detect_for_report(second, _config()) is not None

        second.refresh_from_db()
        assert second.duplicate_of_id == first.pk
        assert second.duplicate_confidence is not None
        assert second.duplicate_reason

    def test_it_never_sets_the_status_to_duplicate(self) -> None:
        """A heuristic making verdicts is a heuristic that hides vulnerabilities.
        The link is a pointer for the operator; the ruling stays theirs."""
        project = ProjectFactory(owner="apache", repo="spark")
        SecurityReportFactory(project=project, **_RPC_REPORT)
        second = SecurityReportFactory(project=project, status="new", **_RPC_REPORT)

        detect_for_report(second, _config())

        second.refresh_from_db()
        assert second.status == "new"

    def test_reports_are_only_compared_within_a_project(self) -> None:
        """A Spark report cannot duplicate a Kafka one, and comparing across projects
        is both wrong and quadratic in the whole table."""
        spark = ProjectFactory(owner="apache", repo="spark")
        kafka = ProjectFactory(owner="apache", repo="kafka")
        SecurityReportFactory(project=spark, **_RPC_REPORT)
        elsewhere = SecurityReportFactory(project=kafka, **_RPC_REPORT)

        assert detect_for_report(elsewhere, _config()) is None
        elsewhere.refresh_from_db()
        assert elsewhere.duplicate_of_id is None

    def test_a_report_with_no_project_is_skipped_loudly(self, caplog: Any) -> None:
        import logging

        SecurityReportFactory(project=ProjectFactory(owner="a", repo="b"), **_RPC_REPORT)
        orphan = SecurityReportFactory(project=None, **_RPC_REPORT)

        with caplog.at_level(logging.INFO):
            assert detect_for_report(orphan, _config()) is None

        assert "no project" in caplog.text

    def test_finding_nothing_is_logged_as_explicitly_as_finding_something(
        self, caplog: Any
    ) -> None:
        """Per CLAUDE.md: "no duplicate found" and "the check never ran" must not
        look the same."""
        import logging

        project = ProjectFactory(owner="apache", repo="spark")
        SecurityReportFactory(project=project, title="alpha bug", raw_text="alpha alpha alpha")
        subject = SecurityReportFactory(
            project=project, title="beta bug", raw_text="beta beta beta"
        )

        with caplog.at_level(logging.INFO):
            assert detect_for_report(subject, _config()) is None

        assert "No duplicate found" in caplog.text
        assert "threshold" in caplog.text

    def test_being_switched_off_is_honoured_and_says_so(self, caplog: Any) -> None:
        import logging

        project = ProjectFactory(owner="apache", repo="spark")
        SecurityReportFactory(project=project, **_RPC_REPORT)
        second = SecurityReportFactory(project=project, **_RPC_REPORT)

        # Scoped to this logger by name: settings.LOGGING sets a level on the
        # `franktheunicorn` logger, so DEBUG records are dropped there before
        # caplog's root handler ever sees them.
        #
        # DEBUG rather than INFO on purpose, against the usual "a gate that stops
        # configured work logs at INFO" rule. This gate is a deliberate off-switch
        # rather than a surprise, and it fires once per report — the same reasoning
        # that keeps per-PR review detail at DEBUG, where INFO would be 900 lines a
        # cycle on Spark. It still names the setting.
        with caplog.at_level(logging.DEBUG, logger="franktheunicorn.security.duplicates"):
            assert detect_for_report(second, _config(enabled=False)) is None

        second.refresh_from_db()
        assert second.duplicate_of_id is None
        assert "duplicates.enabled" in caplog.text

    def test_a_link_is_resolved_to_the_end_of_the_chain(self) -> None:
        """C duplicating B where B duplicates A should leave C pointing at A, not at
        a row that is itself a pointer."""
        project = ProjectFactory(owner="apache", repo="spark")
        a = SecurityReportFactory(project=project, **_RPC_REPORT)
        b = SecurityReportFactory(project=project, duplicate_of=a, **_RPC_REPORT)
        c = SecurityReportFactory(project=project, **_RPC_REPORT)

        match = find_duplicate(c, [b], _config())
        assert match is not None
        assert link_duplicate(c, match)

        c.refresh_from_db()
        assert c.duplicate_of_id == a.pk

    def test_resolve_canonical_survives_a_hand_made_cycle(self) -> None:
        """The write path refuses to create one, but the Django admin can."""
        project = ProjectFactory(owner="apache", repo="spark")
        a = SecurityReportFactory(project=project, **_RPC_REPORT)
        b = SecurityReportFactory(project=project, duplicate_of=a, **_RPC_REPORT)
        a.duplicate_of = b
        a.save(update_fields=["duplicate_of"])

        # The property that matters is that it returns rather than spinning.
        assert resolve_canonical(a).pk in {a.pk, b.pk}

    def test_a_cycle_is_refused_rather_than_created(self) -> None:
        """Reachable in ordinary use: re-running detection over a backlog compares
        every report against every other, so A->B on one pass and B->A on the next is
        what an unguarded implementation does."""
        project = ProjectFactory(owner="apache", repo="spark")
        a = SecurityReportFactory(project=project, **_RPC_REPORT)
        b = SecurityReportFactory(project=project, duplicate_of=a, **_RPC_REPORT)

        assert would_create_cycle(a, b)

        match = find_duplicate(a, [b], _config())
        assert match is not None
        assert link_duplicate(a, match) is False

        a.refresh_from_db()
        assert a.duplicate_of_id is None

    def test_relinking_the_same_pair_is_a_no_op(self) -> None:
        project = ProjectFactory(owner="apache", repo="spark")
        SecurityReportFactory(project=project, **_RPC_REPORT)
        second = SecurityReportFactory(project=project, **_RPC_REPORT)

        match = detect_for_report(second, _config())
        assert match is not None
        second.refresh_from_db()

        assert link_duplicate(second, match) is False

    def test_deleting_the_original_does_not_delete_its_duplicates(self) -> None:
        """They would be the only remaining record of the finding."""
        from franktheunicorn.core.models import SecurityReport

        project = ProjectFactory(owner="apache", repo="spark")
        a = SecurityReportFactory(project=project, **_RPC_REPORT)
        b = SecurityReportFactory(project=project, duplicate_of=a, **_RPC_REPORT)

        a.delete()

        assert SecurityReport.objects.filter(pk=b.pk).exists()
        b.refresh_from_db()
        assert b.duplicate_of_id is None


@pytest.mark.django_db
class TestBacklogSweep:
    def test_links_point_backwards_in_time(self) -> None:
        """So the canonical report is the one carrying the accumulated triage."""
        project = ProjectFactory(owner="apache", repo="spark")
        first = SecurityReportFactory(project=project, **_RPC_REPORT)
        second = SecurityReportFactory(project=project, **_RPC_REPORT)
        third = SecurityReportFactory(project=project, **_RPC_REPORT)

        assert detect_across_backlog([third, first, second], _config()) == 2

        first.refresh_from_db()
        second.refresh_from_db()
        third.refresh_from_db()
        assert first.duplicate_of_id is None
        assert second.duplicate_of_id == first.pk
        assert third.duplicate_of_id == first.pk

    def test_a_second_sweep_changes_nothing(self) -> None:
        """Idempotence is the property that makes it safe to run on a schedule, and
        the one a cycle bug would break."""
        project = ProjectFactory(owner="apache", repo="spark")
        reports = [SecurityReportFactory(project=project, **_RPC_REPORT) for _ in range(3)]

        assert detect_across_backlog(reports, _config()) == 2
        for report in reports:
            report.refresh_from_db()
        assert detect_across_backlog(reports, _config()) == 0

    def test_projects_do_not_cross_contaminate(self) -> None:
        spark = ProjectFactory(owner="apache", repo="spark")
        kafka = ProjectFactory(owner="apache", repo="kafka")
        a = SecurityReportFactory(project=spark, **_RPC_REPORT)
        b = SecurityReportFactory(project=kafka, **_RPC_REPORT)

        assert detect_across_backlog([a, b], _config()) == 0

    def test_five_hundred_reports_is_affordable(self) -> None:
        """The quadratic is deliberate and the claim is that it's cheap: 500 reports
        is 125,000 pairs of set intersections. Pinned because an inverted index would
        be faster and would also be the third thing to go wrong in a feature whose
        job is to be a hint."""
        import time

        project = ProjectFactory(owner="apache", repo="spark")
        reports = [
            SecurityReportFactory(
                project=project,
                title=f"Finding number {n} in module {n % 40}",
                raw_text=f"Issue at src/main/java/pkg{n % 40}/File{n}.java with detail {n}.",
            )
            for n in range(500)
        ]

        started = time.monotonic()
        detect_across_backlog(reports, _config())
        elapsed = time.monotonic() - started

        assert elapsed < 20  # generous for CI; locally this is around a second


@pytest.mark.django_db
class TestTriageIntegration:
    def test_a_stale_link_is_cleared_when_a_retriage_finds_nothing(self) -> None:
        """A stale link presented as this run's answer is worse than no link, because
        the operator has no way to tell it's stale."""
        from franktheunicorn.config.models import OperatorConfig
        from franktheunicorn.security.triage import _check_duplicates

        project = ProjectFactory(owner="apache", repo="spark")
        other = SecurityReportFactory(project=project, title="alpha", raw_text="alpha alpha")
        subject = SecurityReportFactory(
            project=project,
            title="beta",
            raw_text="beta beta",
            duplicate_of=other,
            duplicate_confidence=0.9,
            duplicate_reason="an earlier run thought so",
        )

        _check_duplicates(subject, OperatorConfig())

        subject.refresh_from_db()
        assert subject.duplicate_of_id is None
        assert subject.duplicate_confidence is None

    def test_a_link_the_operator_set_by_hand_is_not_revoked(self) -> None:
        """A null confidence marks a link as somebody's decision rather than ours,
        and detection only ever overwrites its own work."""
        from franktheunicorn.config.models import OperatorConfig
        from franktheunicorn.security.triage import _check_duplicates

        project = ProjectFactory(owner="apache", repo="spark")
        other = SecurityReportFactory(project=project, title="alpha", raw_text="alpha alpha")
        subject = SecurityReportFactory(
            project=project,
            title="beta",
            raw_text="beta beta",
            duplicate_of=other,
            duplicate_confidence=None,
        )

        _check_duplicates(subject, OperatorConfig())

        subject.refresh_from_db()
        assert subject.duplicate_of_id == other.pk

    def test_it_is_on_by_default(self) -> None:
        from franktheunicorn.config.models import OperatorConfig

        assert OperatorConfig().security_triage.duplicates.enabled is True
