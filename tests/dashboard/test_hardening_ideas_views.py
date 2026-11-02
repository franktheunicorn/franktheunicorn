"""Dashboard views for the Hardening Ideas action-item queue."""

from __future__ import annotations

from unittest.mock import patch

import pytest
from django.test import Client
from django.utils import timezone

from franktheunicorn.config.models import OperatorConfig
from franktheunicorn.core.models import HardeningIdea, WorkerCommand
from tests.factories import HardeningIdeaFactory, ProjectFactory, SecurityReportFactory


def _config(*, enabled: bool = True) -> OperatorConfig:
    config = OperatorConfig()
    config.security_triage.hardening_ideas.enabled = enabled
    return config


@pytest.mark.django_db
def test_hardening_list_and_detail(client: Client) -> None:
    idea = HardeningIdeaFactory(title="Tighten bounds", still_exists="yes", status="confirmed")
    html = client.get("/hardening/").content.decode()
    assert "Hardening Ideas" in html
    assert "Tighten bounds" in html
    detail = client.get(f"/hardening/{idea.pk}/").content.decode()
    assert "Tighten bounds" in detail


@pytest.mark.django_db
def test_hardening_all_tab_shows_everything(client: Client) -> None:
    confirmed = HardeningIdeaFactory(title="Keep me", status="confirmed", still_exists="yes")
    dismissed = HardeningIdeaFactory(title="Dismissed idea", status="dismissed")
    all_html = client.get("/hardening/?status=all").content.decode()
    assert confirmed.title in all_html
    assert dismissed.title in all_html
    # The bare /hardening/ defaults to the worklist, and the All tab link
    # must carry status=all so it does not bounce back to that default.
    nav = client.get("/hardening/").content.decode()
    assert "status=all&amp;sort=" in nav or "status=all&sort=" in nav


@pytest.mark.django_db
def test_hardening_list_shows_button_on_security_page(client: Client) -> None:
    report = SecurityReportFactory(matched_cve_id="CVE-2024-1", status="valid")
    body = client.get(f"/security/{report.pk}/").content.decode()
    assert "Mine hardening ideas" in body


@pytest.mark.django_db
def test_hardening_status_buttons(client: Client) -> None:
    idea = HardeningIdeaFactory(status="new")
    assert client.post(f"/hardening/{idea.pk}/filed/").status_code == 302
    idea.refresh_from_db()
    assert idea.status == HardeningIdea.STATUS_FILED
    assert client.post(f"/hardening/{idea.pk}/fixed/").status_code == 302
    idea.refresh_from_db()
    assert idea.status == HardeningIdea.STATUS_FIXED
    idea2 = HardeningIdeaFactory(status="new")
    assert client.post(f"/hardening/{idea2.pk}/dismiss/").status_code == 302
    idea2.refresh_from_db()
    assert idea2.status == HardeningIdea.STATUS_DISMISSED


@pytest.mark.django_db
def test_bulk_mine_button_queues(client: Client) -> None:
    project = ProjectFactory()
    SecurityReportFactory(project=project, matched_cve_id="CVE-2024-1", status="valid")
    with patch(
        "franktheunicorn.config.loader.get_operator_config",
        return_value=_config(),
    ):
        resp = client.post("/security/mine-hardening-ideas/", {"agent": "all"}, follow=True)
    assert resp.status_code == 200
    assert WorkerCommand.objects.filter(command="mine_hardening_ideas").exists()


@pytest.mark.django_db
def test_per_report_mine_button(client: Client) -> None:
    report = SecurityReportFactory(matched_cve_id="CVE-2024-1", status="valid")
    with patch(
        "franktheunicorn.config.loader.get_operator_config",
        return_value=_config(),
    ):
        resp = client.post(f"/security/{report.pk}/mine-hardening/")
    assert resp.status_code == 200
    assert b"Hardening-ideas mine queued" in resp.content
    assert WorkerCommand.objects.filter(
        command="mine_report_hardening", security_report=report
    ).exists()


@pytest.mark.django_db
def test_already_scanned_detail_button_blocked(client: Client) -> None:
    report = SecurityReportFactory(
        matched_cve_id="CVE-2024-1",
        status="valid",
        hardening_scanned_at=timezone.now(),
        hardening_idea_count=2,
    )
    with patch(
        "franktheunicorn.config.loader.get_operator_config",
        return_value=_config(),
    ):
        resp = client.post(f"/security/{report.pk}/mine-hardening/")
    assert b"Already mined" in resp.content
    assert WorkerCommand.objects.filter(command="mine_report_hardening").count() == 0


@pytest.mark.django_db
def test_disabled_gate_names_the_setting(client: Client) -> None:
    report = SecurityReportFactory(matched_cve_id="CVE-2024-1", status="valid")
    with patch(
        "franktheunicorn.config.loader.get_operator_config",
        return_value=_config(enabled=False),
    ):
        resp = client.post(f"/security/{report.pk}/mine-hardening/")
    assert b"hardening_ideas.enabled" in resp.content


@pytest.mark.django_db
def test_confirm_queues(client: Client) -> None:
    idea = HardeningIdeaFactory(status="new")
    assert client.post(f"/hardening/{idea.pk}/confirm/").status_code == 302
    assert WorkerCommand.objects.filter(
        command="confirm_hardening_idea", kwargs={"idea_id": idea.pk}
    ).exists()
