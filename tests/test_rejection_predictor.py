"""Tests for the Bayesian rejection predictor (v1.75)."""

from __future__ import annotations

import fcntl
from pathlib import Path
from unittest.mock import patch

import pytest

from franktheunicorn.core.models import OperatorAction, Project
from franktheunicorn.scoring.rejection_predictor import (
    LOW_VALUE_THRESHOLD,
    MIN_ACTIONS_TO_TRAIN,
    SUPPRESS_THRESHOLD,
    RejectionPredictor,
    _approval_rate_bucket,
    _diff_size_bucket,
    _tokenize,
    load_predictor_for_project,
    maybe_retrain,
)
from tests.factories import (
    OperatorActionFactory,
    PullRequestFactory,
    ReviewDraftFactory,
)


class TestTokenize:
    def test_basic(self) -> None:
        tokens = _tokenize("Consider adding a test for this method")
        assert "consider" in tokens
        assert "adding" in tokens
        assert "test" in tokens

    def test_skips_short_tokens(self) -> None:
        tokens = _tokenize("do it or fix a b c")
        # Only tokens with 3+ chars starting with a letter
        assert "fix" in tokens
        assert "a" not in tokens
        assert "b" not in tokens

    def test_code_tokens(self) -> None:
        tokens = _tokenize("def calculate_total(items: list) -> int:")
        assert "def" in tokens
        assert "calculate_total" in tokens
        assert "items" in tokens

    def test_max_tokens(self) -> None:
        long_text = " ".join(f"word{i}" for i in range(500))
        tokens = _tokenize(long_text)
        assert len(tokens) <= 200


class TestDiffSizeBucket:
    def test_small(self) -> None:
        assert _diff_size_bucket(30, 20) == "small"

    def test_medium(self) -> None:
        assert _diff_size_bucket(200, 100) == "medium"

    def test_large(self) -> None:
        assert _diff_size_bucket(500, 200) == "large"

    def test_huge(self) -> None:
        assert _diff_size_bucket(800, 300) == "huge"

    def test_boundary_small(self) -> None:
        assert _diff_size_bucket(99, 0) == "small"
        assert _diff_size_bucket(100, 0) == "medium"


class TestApprovalRateBucket:
    def test_high(self) -> None:
        assert _approval_rate_bucket(0.8) == "high"

    def test_medium(self) -> None:
        assert _approval_rate_bucket(0.5) == "medium"

    def test_low(self) -> None:
        assert _approval_rate_bucket(0.2) == "low"


class TestFeatureExtraction:
    def test_basic_features(self) -> None:
        predictor = RejectionPredictor()
        features = predictor.extract_features(
            category="style",
            severity="nit",
            file_path="src/utils/helper.py",
            comment_body="Consider using a list comprehension here",
            governance="asf",
            is_new_contributor=True,
            additions=50,
            deletions=10,
        )
        assert features["category"] == "style"
        assert features["severity"] == "nit"
        assert features["file_ext"] == "py"
        assert features["is_test_file"] is False
        assert features["is_new_contributor"] is True
        assert features["governance"] == "asf"
        assert features["diff_size"] == "small"

    def test_test_file_detection(self) -> None:
        predictor = RejectionPredictor()
        features = predictor.extract_features(file_path="tests/test_utils.py")
        assert features["is_test_file"] is True

    def test_no_extension(self) -> None:
        predictor = RejectionPredictor()
        features = predictor.extract_features(file_path="Makefile")
        assert features["file_ext"] == ""

    def test_comment_bow(self) -> None:
        predictor = RejectionPredictor()
        features = predictor.extract_features(comment_body="nit: fix the spacing in this function")
        assert features.get("comment_nit", 0) >= 1
        assert features.get("comment_fix", 0) >= 1
        assert features.get("comment_spacing", 0) >= 1

    def test_code_context_bow(self) -> None:
        predictor = RejectionPredictor()
        features = predictor.extract_features(
            code_context="def calculate_total(items):\n    return sum(items)"
        )
        assert features.get("code_calculate_total", 0) >= 1
        assert features.get("code_return", 0) >= 1

    def test_approval_rate_uses_cache(self) -> None:
        predictor = RejectionPredictor()
        predictor._approval_rates = {(1, "style"): 0.9}
        features = predictor.extract_features(category="style", project_id=1)
        assert features["approval_rate"] == "high"

    def test_approval_rate_defaults_to_medium(self) -> None:
        predictor = RejectionPredictor()
        features = predictor.extract_features(category="style", project_id=99)
        assert features["approval_rate"] == "medium"


@pytest.mark.django_db
class TestRejectionPredictorTraining:
    def _create_actions(
        self,
        project: Project,
        n_accept: int = 30,
        n_reject: int = 20,
    ) -> list[OperatorAction]:
        """Helper to create operator actions for training."""
        pr = PullRequestFactory(
            project=project,
            additions=50,
            deletions=10,
            is_new_contributor=False,
        )
        actions: list[OperatorAction] = []
        for i in range(n_accept):
            draft = ReviewDraftFactory(
                pull_request=pr,
                category="correctness",
                severity="important",
                file_path=f"src/module_{i}.py",
                comment_body=f"Good catch on the bug in module {i}",
            )
            actions.append(
                OperatorActionFactory(
                    action_type="accept_draft",
                    review_draft=draft,
                    pull_request=pr,
                )
            )
        for i in range(n_reject):
            draft = ReviewDraftFactory(
                pull_request=pr,
                category="style",
                severity="nit",
                file_path=f"tests/test_module_{i}.py",
                comment_body=f"nit: fix spacing in test {i}",
            )
            actions.append(
                OperatorActionFactory(
                    action_type="reject_draft",
                    review_draft=draft,
                    pull_request=pr,
                )
            )
        return actions

    def test_train_insufficient_data(self, db_project: Project) -> None:
        predictor = RejectionPredictor()
        self._create_actions(db_project, n_accept=5, n_reject=5)
        result = predictor.train(db_project.pk)
        assert result is False
        assert predictor._trained is False

    def test_train_sufficient_data(self, db_project: Project) -> None:
        predictor = RejectionPredictor()
        self._create_actions(db_project, n_accept=30, n_reject=25)
        result = predictor.train(db_project.pk)
        assert result is True
        assert predictor._trained is True
        assert predictor._last_action_count == 55

    def test_train_force_with_few_actions(self, db_project: Project) -> None:
        predictor = RejectionPredictor()
        self._create_actions(db_project, n_accept=5, n_reject=5)
        result = predictor.train(db_project.pk, force=True)
        assert result is True
        assert predictor._trained is True

    def test_train_no_actions_force(self, db_project: Project) -> None:
        predictor = RejectionPredictor()
        result = predictor.train(db_project.pk, force=True)
        assert result is False

    def test_predict_untrained(self) -> None:
        predictor = RejectionPredictor()
        features = predictor.extract_features(category="style")
        prob = predictor.predict_rejection(features)
        assert prob == 0.5

    def test_predict_trained(self, db_project: Project) -> None:
        predictor = RejectionPredictor()
        self._create_actions(db_project, n_accept=30, n_reject=25)
        predictor.train(db_project.pk)

        features = predictor.extract_features(
            category="style",
            severity="nit",
            file_path="tests/test_foo.py",
            comment_body="nit: fix spacing",
        )
        prob = predictor.predict_rejection(features)
        assert 0.0 <= prob <= 1.0

    def test_predict_returns_float(self, db_project: Project) -> None:
        predictor = RejectionPredictor()
        self._create_actions(db_project, n_accept=30, n_reject=25)
        predictor.train(db_project.pk)

        features = predictor.extract_features(category="correctness", severity="critical")
        prob = predictor.predict_rejection(features)
        assert isinstance(prob, float)

    def test_approval_rates_computed(self, db_project: Project) -> None:
        self._create_actions(db_project, n_accept=30, n_reject=25)
        predictor = RejectionPredictor()
        predictor.train(db_project.pk)
        # Should have entries for (project_id, category) pairs.
        assert len(predictor._approval_rates) > 0


@pytest.mark.django_db
class TestRejectionPredictorPersistence:
    def _create_actions(self, project: Project) -> None:
        pr = PullRequestFactory(project=project, additions=50, deletions=10)
        for i in range(30):
            draft = ReviewDraftFactory(
                pull_request=pr, category="correctness", file_path=f"src/m{i}.py"
            )
            OperatorActionFactory(action_type="accept_draft", review_draft=draft, pull_request=pr)
        for i in range(25):
            draft = ReviewDraftFactory(
                pull_request=pr, category="style", file_path=f"tests/t{i}.py"
            )
            OperatorActionFactory(action_type="reject_draft", review_draft=draft, pull_request=pr)

    def test_save_load_roundtrip(self, db_project: Project, tmp_path: Path) -> None:
        self._create_actions(db_project)
        predictor = RejectionPredictor()
        predictor.train(db_project.pk)

        model_path = tmp_path / "test_model.pkl"
        predictor.save(model_path)
        assert model_path.exists()

        loaded = RejectionPredictor.load(model_path)
        assert loaded._trained is True
        assert loaded._last_action_count == predictor._last_action_count
        assert loaded._approval_rates == predictor._approval_rates

        # Predictions should be identical.
        features = predictor.extract_features(category="style", severity="nit")
        assert loaded.predict_rejection(features) == predictor.predict_rejection(features)

    def test_save_creates_directories(self, tmp_path: Path) -> None:
        predictor = RejectionPredictor()
        model_path = tmp_path / "nested" / "dir" / "model.pkl"
        predictor.save(model_path)
        assert model_path.exists()

    def test_load_nonexistent_raises(self, tmp_path: Path) -> None:
        with pytest.raises(FileNotFoundError):
            RejectionPredictor.load(tmp_path / "nonexistent.pkl")


@pytest.mark.django_db
class TestLoadPredictorForProject:
    def test_returns_none_when_no_model(self) -> None:
        result = load_predictor_for_project("apache", "spark")
        assert result is None

    def test_loads_existing_model(self, db_project: Project, tmp_path: Path) -> None:
        # Create and save a dummy model.
        predictor = RejectionPredictor()
        predictor._trained = True
        model_path = tmp_path / "models" / "apache-spark" / "rejection_model.pkl"
        predictor.save(model_path)

        with patch(
            "franktheunicorn.scoring.rejection_predictor._model_path_for_project",
            return_value=model_path,
        ):
            loaded = load_predictor_for_project("apache", "spark")
            assert loaded is not None
            assert loaded._trained is True


@pytest.mark.django_db
class TestMaybeRetrain:
    def _create_actions(self, project: Project, count: int) -> None:
        pr = PullRequestFactory(project=project, additions=50, deletions=10)
        for i in range(count):
            draft = ReviewDraftFactory(
                pull_request=pr,
                category="style" if i % 2 == 0 else "correctness",
                file_path=f"src/m{i}.py",
            )
            OperatorActionFactory(
                action_type="accept_draft" if i % 3 != 0 else "reject_draft",
                review_draft=draft,
                pull_request=pr,
            )

    def test_skips_below_threshold(self, db_project: Project, tmp_path: Path) -> None:
        self._create_actions(db_project, 10)
        with patch(
            "franktheunicorn.scoring.rejection_predictor._model_path_for_project",
            return_value=tmp_path / "model.pkl",
        ):
            result = maybe_retrain(db_project.pk, "apache", "spark")
        assert result is False

    def test_trains_when_enough_actions(self, db_project: Project, tmp_path: Path) -> None:
        self._create_actions(db_project, 55)
        model_path = tmp_path / "model.pkl"
        with patch(
            "franktheunicorn.scoring.rejection_predictor._model_path_for_project",
            return_value=model_path,
        ):
            result = maybe_retrain(db_project.pk, "apache", "spark")
        assert result is True
        assert model_path.exists()

    def test_skips_when_recently_retrained(self, db_project: Project, tmp_path: Path) -> None:
        self._create_actions(db_project, 55)
        model_path = tmp_path / "model.pkl"

        # First retrain should succeed.
        with patch(
            "franktheunicorn.scoring.rejection_predictor._model_path_for_project",
            return_value=model_path,
        ):
            assert maybe_retrain(db_project.pk, "apache", "spark") is True
            # Second retrain should skip (not enough new actions).
            assert maybe_retrain(db_project.pk, "apache", "spark") is False

    def test_file_lock_prevents_concurrent_retrain(
        self, db_project: Project, tmp_path: Path
    ) -> None:
        self._create_actions(db_project, 55)
        model_path = tmp_path / "model.pkl"
        lock_path = model_path.with_suffix(".lock")
        lock_path.parent.mkdir(parents=True, exist_ok=True)

        # Hold the lock to simulate another process retraining.
        with open(lock_path, "w") as lock_fd:
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            try:
                with patch(
                    "franktheunicorn.scoring.rejection_predictor._model_path_for_project",
                    return_value=model_path,
                ):
                    result = maybe_retrain(db_project.pk, "apache", "spark")
                assert result is False
            finally:
                fcntl.flock(lock_fd, fcntl.LOCK_UN)


class TestThresholdConstants:
    def test_suppress_threshold(self) -> None:
        assert SUPPRESS_THRESHOLD == 0.8

    def test_low_value_threshold(self) -> None:
        assert LOW_VALUE_THRESHOLD == 0.5

    def test_min_actions(self) -> None:
        assert MIN_ACTIONS_TO_TRAIN == 50
