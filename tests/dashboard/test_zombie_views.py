"""Tests for the Zombie Queue tab and its buttons."""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

import pytest
from django.test import Client

from franktheunicorn.config.models import JiraConfig, OperatorConfig, ProjectConfig
from franktheunicorn.core.models import (
    ZOMBIE_QUEUE,
    OperatorAction,
    Project,
    PullRequest,
    WorkerCommand,
    ZombieAssessment,
)
from tests.factories import (
    ProjectFactory,
    PullRequestFactory,
    ZombieAssessmentFactory,
    ZombieScanStateFactory,
)


def _zombie(project: Project, number: int, **kwargs: Any) -> ZombieAssessment:
    pr = PullRequestFactory(
        project=project,
        number=number,
        github_id=90_000 + number,
        title=f"[SPARK-{number}] zombie {number}",
        state="closed",
        queue=ZOMBIE_QUEUE,
    )
    return ZombieAssessmentFactory(pull_request=pr, **kwargs)


@pytest.fixture
def spark(db: Any) -> Project:
    return ProjectFactory(owner="apache", repo="spark")


@pytest.fixture
def jira_projects(spark: Project, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = ProjectConfig(
        owner="apache",
        repo="spark",
        jira=JiraConfig(
            enabled=True, server="https://issues.apache.org/jira", project_prefix="SPARK"
        ),
    )
    monkeypatch.setattr("franktheunicorn.review.zombie.zombie_projects", lambda: [(spark, cfg)])
    monkeypatch.setattr(
        "franktheunicorn.config.loader.get_project_config",
        lambda name: cfg if name.lower() == "apache/spark" else None,
    )


@pytest.mark.django_db
class TestZombieTab:
    def test_tab_is_in_the_bar_with_its_icon(self, client: Client) -> None:
        response = client.get("/")
        body = response.content.decode()
        assert "Zombie Queue" in body
        assert "zombie-duck.svg" in body

    def test_closed_zombies_show_on_their_tab_and_nowhere_else(
        self, client: Client, spark: Project
    ) -> None:
        _zombie(
            spark,
            5,
            verdict="revive-with-changes",
            summary="Worth a rebase.",
            needed_changes=["Rebase on master"],
        )
        PullRequestFactory(project=spark, number=6, title="Open one", state="open", queue="review")

        zombie_tab = client.get("/?queue=zombie").content.decode()
        review_tab = client.get("/?queue=review").content.decode()

        assert "zombie 5" in zombie_tab
        assert "Revive with changes" in zombie_tab
        assert "Worth a rebase." in zombie_tab
        assert "Rebase on master" in zombie_tab
        assert "browse/SPARK-5" in zombie_tab
        assert "Open one" not in zombie_tab
        assert "zombie 5" not in review_tab
        assert "Hunt for zombies" in zombie_tab

    def test_tab_counts_only_live_zombies(self, client: Client, spark: Project) -> None:
        _zombie(spark, 1)
        _zombie(spark, 2, disposition="dismissed")

        body = client.get("/?queue=zombie").content.decode()

        assert "zombie 1" in body
        assert "zombie 2" not in body
        assert "1 dismissed zombie" in body

    def test_unassessed_failure_is_visible(self, client: Client, spark: Project) -> None:
        _zombie(spark, 3, last_error="stub/ answered without a JSON object")
        body = client.get("/?queue=zombie").content.decode()
        assert "Assessment failed" in body
        assert "Not yet assessed" in body

    def test_empty_tab_offers_the_hunt(self, client: Client) -> None:
        body = client.get("/?queue=zombie").content.decode()
        assert "No zombies in the queue" in body
        assert "zombie-duck.svg" in body

    def test_scan_state_is_shown(self, client: Client, spark: Project, jira_projects: None) -> None:
        ZombieScanStateFactory(
            project=spark, prs_examined=500, candidates_found=12, lowest_number_seen=51234
        )
        body = client.get("/?queue=zombie").content.decode()
        assert "apache/spark" in body
        assert "500 closed PRs examined" in body
        assert "12 zombies found" in body
        assert "#51234" in body

    def test_in_flight_hunt_disables_the_button(
        self, client: Client, spark: Project, jira_projects: None
    ) -> None:
        WorkerCommand.objects.create(command="scan_zombie_prs", status="running")
        body = client.get("/?queue=zombie").content.decode()
        assert "already queued or running" in body
        assert "disabled" in body


@pytest.mark.django_db
class TestHuntButton:
    def test_queues_one_bulk_sweep(
        self, client: Client, spark: Project, jira_projects: None
    ) -> None:
        from franktheunicorn.security.queue import PRIORITY_BULK

        with (
            patch(
                "franktheunicorn.config.loader.get_operator_config", return_value=OperatorConfig()
            ),
            patch("franktheunicorn.review.zombie.resolve_backend", return_value=None),
        ):
            response = client.post("/zombies/scan/", follow=True)

        assert response.status_code == 200
        cmds = WorkerCommand.objects.filter(command="scan_zombie_prs")
        assert cmds.count() == 1
        assert cmds.get().priority == PRIORITY_BULK
        body = response.content.decode()
        assert "Queued the zombie hunt" in body
        # No backend: say so rather than queue silently.
        assert "not assessed" in body

    def test_second_press_does_not_queue_a_second(
        self, client: Client, spark: Project, jira_projects: None
    ) -> None:
        with patch(
            "franktheunicorn.config.loader.get_operator_config", return_value=OperatorConfig()
        ):
            client.post("/zombies/scan/", follow=True)
            response = client.post("/zombies/scan/", follow=True)

        assert WorkerCommand.objects.filter(command="scan_zombie_prs").count() == 1
        assert b"already queued or running" in response.content

    def test_no_jira_project_is_a_flash_not_a_command(
        self, client: Client, monkeypatch: pytest.MonkeyPatch, db: Any
    ) -> None:
        monkeypatch.setattr("franktheunicorn.review.zombie.zombie_projects", list)
        response = client.post("/zombies/scan/", follow=True)
        assert b"jira.enabled" in response.content
        assert not WorkerCommand.objects.filter(command="scan_zombie_prs").exists()


@pytest.mark.django_db
class TestDetailPanel:
    def test_panel_renders_the_assessment(self, client: Client, spark: Project) -> None:
        zombie = _zombie(
            spark,
            9,
            verdict="rethink",
            jira_makes_sense=True,
            jira_reasoning="Still a real gap.",
            fix_makes_sense=False,
            fix_reasoning="Wrong layer.",
            summary="Needs a different approach.",
            needed_changes=["Move the check into the analyzer"],
            closing_context="[bot 2026-01-02] closed for inactivity",
            backend_used="stub/x",
        )
        body = client.get(f"/pr/{zombie.pull_request.pk}/").content.decode()
        assert "JIRA valid, fix needs a new approach" in body
        assert "Still a real gap." in body
        assert "Wrong layer." in body
        assert "Move the check into the analyzer" in body
        assert "closed for inactivity" in body
        assert "Re-assess" in body
        assert "Dismiss" in body

    def test_no_panel_for_an_ordinary_pr(self, client: Client, db_pr: PullRequest) -> None:
        body = client.get(f"/pr/{db_pr.pk}/").content.decode()
        assert "Zombie:" not in body

    def test_dismiss_and_restore(self, client: Client, spark: Project) -> None:
        zombie = _zombie(spark, 10, verdict="drop")
        pr = zombie.pull_request

        response = client.post(f"/pr/{pr.pk}/zombie/dismiss/")

        assert response.status_code == 200
        zombie.refresh_from_db()
        assert zombie.disposition == "dismissed"
        assert b"Restore to Zombie Queue" in response.content
        assert OperatorAction.objects.filter(action_type="zombie_dismiss", pull_request=pr).exists()
        assert "zombie 10" not in client.get("/?queue=zombie").content.decode()

        response = client.post(f"/pr/{pr.pk}/zombie/dismiss/", {"restore": "1"})
        zombie.refresh_from_db()
        assert zombie.disposition == "pending"
        assert b"Dismiss" in response.content
        assert OperatorAction.objects.filter(action_type="zombie_restore").exists()

    def test_dismiss_404s_without_an_assessment(self, client: Client, db_pr: PullRequest) -> None:
        assert client.post(f"/pr/{db_pr.pk}/zombie/dismiss/").status_code == 404

    def test_reassess_queues_an_interactive_command_once(
        self, client: Client, spark: Project, jira_projects: None
    ) -> None:
        from franktheunicorn.security.queue import PRIORITY_INTERACTIVE

        zombie = _zombie(spark, 11)
        pr = zombie.pull_request

        first = client.post(f"/pr/{pr.pk}/zombie/reassess/")
        second = client.post(f"/pr/{pr.pk}/zombie/reassess/")

        cmds = WorkerCommand.objects.filter(command="assess_zombie_pr", pull_request=pr)
        assert cmds.count() == 1
        assert cmds.get().priority == PRIORITY_INTERACTIVE
        assert b"Assessment queued" in first.content
        assert b"already queued or running" in second.content

    def test_reassess_without_jira_says_why(
        self, client: Client, monkeypatch: pytest.MonkeyPatch, db: Any
    ) -> None:
        project = ProjectFactory(owner="holdenk", repo="thing")
        monkeypatch.setattr(
            "franktheunicorn.config.loader.get_project_config",
            lambda name: ProjectConfig(owner="holdenk", repo="thing"),
        )
        zombie = _zombie(project, 12)
        response = client.post(f"/pr/{zombie.pull_request.pk}/zombie/reassess/")
        assert b"jira.enabled" in response.content
        assert not WorkerCommand.objects.filter(command="assess_zombie_pr").exists()
