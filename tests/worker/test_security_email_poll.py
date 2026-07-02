"""Tests for the worker's read-only security-email poll.

Verifies the transparency + gating guarantees: every examined message gets
an EmailScanRecord, security ones become SecurityReport drafts, duplicates
are skipped, and auto-triage only drafts (it never sends).
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from franktheunicorn.config.models import (
    OperatorConfig,
    SecurityEmailConfig,
    SecurityTriageConfig,
)
from franktheunicorn.core.models import EmailScanRecord, SecurityReport
from franktheunicorn.data_access.email_inbox.types import EmailFetchResult, InboxMessage


def _operator_config(auto_triage: bool = False) -> OperatorConfig:
    return OperatorConfig(
        github_username="holdenk",
        security_triage=SecurityTriageConfig(
            enabled=True,
            auto_triage=auto_triage,
            email=SecurityEmailConfig(
                enabled=True,
                imap_host="imap.gmail.com",
                imap_user="holden@gmail.com",
                imap_pass="app-password",
                folder="INBOX",
            ),
        ),
    )


def _reset_poll_clock() -> None:
    # -inf (not 0.0) so the interval gate `now - last < interval` always
    # passes regardless of the absolute value of time.monotonic() — on a
    # freshly-booted CI runner monotonic() can be < the poll interval, which
    # made `now - 0.0 < interval` true and skipped the poll.
    import franktheunicorn.worker.runner as runner

    runner._last_security_email_poll = float("-inf")


@pytest.mark.django_db
class TestPollSecurityEmails:
    def test_records_every_examined_message_and_ingests_security(self) -> None:
        from franktheunicorn.worker.runner import _poll_security_emails

        _reset_poll_clock()
        fetch = EmailFetchResult(
            examined=[
                InboxMessage(
                    message_id="<sec-1>",
                    subject="[SECURITY] Path traversal",
                    from_name="Ryan Hughes",
                    from_email="security@apache.org",
                    body="path traversal vulnerability with an exploit",
                    is_security_report=True,
                    is_forwarded=True,
                    matched_keywords=("exploit", "path traversal", "vulnerability"),
                ),
                InboxMessage(
                    message_id="<chatter-1>",
                    subject="Lunch?",
                    from_email="friend@example.com",
                    body="want to grab lunch",
                    is_security_report=False,
                ),
            ]
        )

        with patch(
            "franktheunicorn.data_access.email_inbox.fetcher.fetch_security_emails",
            return_value=fetch,
        ):
            _poll_security_emails(_operator_config(auto_triage=False))

        # One audit row per examined message.
        assert EmailScanRecord.objects.count() == 2
        ingested = EmailScanRecord.objects.get(message_id="<sec-1>")
        assert ingested.action == "ingested"
        assert ingested.is_forwarded is True
        assert "path traversal" in ingested.matched_keywords
        assert ingested.security_report is not None

        skipped = EmailScanRecord.objects.get(message_id="<chatter-1>")
        assert skipped.action == "skipped_not_security"
        assert skipped.security_report is None

        # Exactly one report, from the unwrapped reporter/subject.
        report = SecurityReport.objects.get()
        assert report.title == "[SECURITY] Path traversal"
        assert report.reporter_name == "Ryan Hughes"
        assert report.source == "email"

    def test_duplicate_report_is_recorded_but_not_reingested(self) -> None:
        from franktheunicorn.worker.runner import _poll_security_emails

        # A report with this message-id already exists.
        SecurityReport.objects.create(
            raw_text="old", title="old", source="email", email_message_id="<sec-dup>"
        )
        _reset_poll_clock()
        fetch = EmailFetchResult(
            examined=[
                InboxMessage(
                    message_id="<sec-dup>",
                    subject="[SECURITY] dup",
                    body="vulnerability exploit",
                    is_security_report=True,
                )
            ]
        )
        with patch(
            "franktheunicorn.data_access.email_inbox.fetcher.fetch_security_emails",
            return_value=fetch,
        ):
            _poll_security_emails(_operator_config())

        assert SecurityReport.objects.count() == 1  # not re-ingested
        rec = EmailScanRecord.objects.get(message_id="<sec-dup>")
        assert rec.action == "skipped_duplicate"

    def test_auto_triage_drafts_only_never_sends(self) -> None:
        from franktheunicorn.worker.runner import _poll_security_emails

        _reset_poll_clock()
        fetch = EmailFetchResult(
            examined=[
                InboxMessage(
                    message_id="<sec-2>",
                    subject="[SECURITY] rce",
                    body="remote code execution vulnerability",
                    is_security_report=True,
                )
            ]
        )
        with (
            patch(
                "franktheunicorn.data_access.email_inbox.fetcher.fetch_security_emails",
                return_value=fetch,
            ),
            patch("franktheunicorn.security.triage.triage_report") as mock_triage,
        ):
            _poll_security_emails(_operator_config(auto_triage=True))

        # Triage runs on the drafted report; there is no send path invoked.
        assert mock_triage.call_count == 1

    def test_disabled_does_nothing(self) -> None:
        from franktheunicorn.worker.runner import _poll_security_emails

        _reset_poll_clock()
        cfg = OperatorConfig(security_triage=SecurityTriageConfig(enabled=False))
        with patch(
            "franktheunicorn.data_access.email_inbox.fetcher.fetch_security_emails"
        ) as mock_fetch:
            _poll_security_emails(cfg)
        mock_fetch.assert_not_called()
        assert EmailScanRecord.objects.count() == 0
