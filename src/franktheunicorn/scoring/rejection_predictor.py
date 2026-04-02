"""Bayesian rejection predictor (v1.75 — Tier 2 learning).

Lightweight sklearn model trained on operator accept/reject history.
Predicts P(rejection) for candidate findings before showing them.

Features:
  - Structured: governance, category, severity, file_ext, is_test_file,
    is_new_contributor, is_ai_pr, diff_size_bucket, approval_rate_bucket
  - Text: bag-of-words from comment_body and code_context

Thresholds:
  - P(rejection) > 0.8 → auto-suppress (hidden by default, visible in dashboard)
  - 0.5 < P(rejection) ≤ 0.8 → flagged as "likely low-value"
"""

from __future__ import annotations

import fcntl
import hashlib
import hmac
import logging
import pickle
import re
from pathlib import Path
from typing import TYPE_CHECKING, Any

from sklearn.feature_extraction import DictVectorizer  # type: ignore[import-untyped]
from sklearn.naive_bayes import MultinomialNB  # type: ignore[import-untyped]
from sklearn.pipeline import Pipeline  # type: ignore[import-untyped]

if TYPE_CHECKING:
    from franktheunicorn.core.models import ReviewDraft

logger = logging.getLogger(__name__)

# Thresholds for auto-suppress and low-value flagging.
SUPPRESS_THRESHOLD = 0.8
LOW_VALUE_THRESHOLD = 0.5

# Minimum operator actions required to train the model.
MIN_ACTIONS_TO_TRAIN = 50

# Retrain every N new actions after the last training.
RETRAIN_INTERVAL = 50

# Maximum number of words to extract from text fields for bag-of-words.
_MAX_BOW_WORDS = 200

# Simple tokenizer: split on non-alphanumeric, lowercase, skip short tokens.
_TOKEN_RE = re.compile(r"[a-zA-Z_][a-zA-Z0-9_]{2,}")


def _tokenize(text: str) -> list[str]:
    """Extract lowercase tokens from text for bag-of-words features."""
    return [t.lower() for t in _TOKEN_RE.findall(text)][:_MAX_BOW_WORDS]


def _diff_size_bucket(additions: int, deletions: int) -> str:
    """Bucketize diff size into small/medium/large/huge."""
    total = additions + deletions
    if total < 100:
        return "small"
    if total < 500:
        return "medium"
    if total < 1000:
        return "large"
    return "huge"


def _approval_rate_bucket(rate: float) -> str:
    """Bucketize approval rate into high/medium/low."""
    if rate >= 0.7:
        return "high"
    if rate >= 0.4:
        return "medium"
    return "low"


class RejectionPredictor:
    """Bayesian model predicting P(rejection) for review findings.

    Uses sklearn MultinomialNB with DictVectorizer. Features combine
    structured metadata with bag-of-words from comment text and code context.
    """

    def __init__(self) -> None:
        self.model: Pipeline = Pipeline(
            [
                ("vectorizer", DictVectorizer(sparse=True)),
                ("classifier", MultinomialNB(alpha=1.0)),
            ]
        )
        self._approval_rates: dict[tuple[int, str], float] = {}
        self._trained = False
        self._last_action_count = 0

    def extract_features(
        self,
        *,
        category: str = "other",
        severity: str = "nit",
        file_path: str = "",
        comment_body: str = "",
        code_context: str = "",
        governance: str = "standard",
        is_new_contributor: bool = False,
        is_ai_pr: bool = False,
        additions: int = 0,
        deletions: int = 0,
        project_id: int | None = None,
    ) -> dict[str, Any]:
        """Extract feature dict from finding attributes.

        All feature values are strings, bools, or ints — compatible with
        DictVectorizer which one-hot-encodes string values.
        """
        file_ext = file_path.rsplit(".", 1)[-1] if "." in file_path else ""
        is_test_file = "test" in file_path.lower()

        # Approval rate for this (project, category) pair.
        rate_key = (project_id or 0, category)
        rate = self._approval_rates.get(rate_key, 0.5)

        features: dict[str, Any] = {
            "governance": governance,
            "category": category,
            "severity": severity,
            "file_ext": file_ext,
            "is_test_file": is_test_file,
            "is_new_contributor": is_new_contributor,
            "is_ai_pr": is_ai_pr,
            "diff_size": _diff_size_bucket(additions, deletions),
            "approval_rate": _approval_rate_bucket(rate),
        }

        # Bag-of-words from comment body.
        for token in _tokenize(comment_body):
            features[f"comment_{token}"] = features.get(f"comment_{token}", 0) + 1

        # Bag-of-words from code context.
        for token in _tokenize(code_context):
            features[f"code_{token}"] = features.get(f"code_{token}", 0) + 1

        return features

    def _features_from_draft(
        self,
        draft: ReviewDraft,
        governance: str = "standard",
    ) -> dict[str, Any]:
        """Extract features from a persisted ReviewDraft instance."""
        pr = draft.pull_request
        return self.extract_features(
            category=draft.category,
            severity=draft.severity,
            file_path=draft.file_path,
            comment_body=draft.comment_body,
            code_context=draft.code_context,
            governance=governance,
            is_new_contributor=pr.is_new_contributor,
            is_ai_pr=pr.likely_ai_generated,
            additions=pr.additions,
            deletions=pr.deletions,
            project_id=pr.project_id,
        )

    @staticmethod
    def _compute_approval_rates(
        project_id: int | None = None,
    ) -> dict[tuple[int, str], float]:
        """Compute historical approval rate per (project_id, category).

        Returns a dict mapping (project_id, category) to approval rate [0, 1].
        """
        from django.db.models import Case, Count, F, FloatField, Value, When

        from franktheunicorn.core.models import OperatorAction

        qs = OperatorAction.objects.filter(
            action_type__in=["accept_draft", "reject_draft", "edit_draft"],
            review_draft__isnull=False,
        )
        if project_id is not None:
            qs = qs.filter(review_draft__pull_request__project_id=project_id)

        rates = (
            qs.values(
                pid=F("review_draft__pull_request__project_id"),
                cat=F("review_draft__category"),
            )
            .annotate(
                total=Count("id"),
                approved=Count(
                    Case(
                        When(action_type__in=["accept_draft", "edit_draft"], then=Value(1)),
                        output_field=FloatField(),
                    )
                ),
            )
            .filter(total__gt=0)
        )

        result: dict[tuple[int, str], float] = {}
        for row in rates:
            key = (row["pid"], row["cat"])
            result[key] = row["approved"] / row["total"]
        return result

    def train(self, project_id: int | None = None, *, force: bool = False) -> bool:
        """Train the model from operator action history.

        Returns True if training succeeded, False if insufficient data.
        Requires at least MIN_ACTIONS_TO_TRAIN actions unless force=True.
        """
        from franktheunicorn.core.models import OperatorAction

        qs = OperatorAction.objects.filter(
            action_type__in=["accept_draft", "reject_draft", "edit_draft"],
            review_draft__isnull=False,
        ).select_related(
            "review_draft",
            "review_draft__pull_request",
            "review_draft__pull_request__project",
        )
        if project_id is not None:
            qs = qs.filter(review_draft__pull_request__project_id=project_id)

        actions = list(qs)
        if len(actions) < MIN_ACTIONS_TO_TRAIN and not force:
            logger.info(
                "Not enough actions to train rejection model (%d < %d).",
                len(actions),
                MIN_ACTIONS_TO_TRAIN,
            )
            return False

        if not actions:
            return False

        # Compute approval rates before extracting features.
        self._approval_rates = self._compute_approval_rates(project_id)

        # Look up governance for each project.
        governance_map = self._load_governance_map()

        features_list: list[dict[str, Any]] = []
        labels: list[int] = []

        for action in actions:
            draft = action.review_draft
            if draft is None:
                continue
            proj = draft.pull_request.project
            gov = governance_map.get(proj.full_name, "standard")
            features_list.append(self._features_from_draft(draft, governance=gov))
            labels.append(1 if action.action_type == "reject_draft" else 0)

        if not features_list:
            return False

        self.model.fit(features_list, labels)
        self._trained = True
        self._last_action_count = len(actions)
        logger.info(
            "Trained rejection model with %d examples (project_id=%s).",
            len(actions),
            project_id,
        )
        return True

    def predict_rejection(self, features: dict[str, Any]) -> float:
        """Predict P(rejection) for a finding. Returns 0.5 if model not trained."""
        if not self._trained:
            return 0.5

        proba = self.model.predict_proba([features])
        # Class 1 = rejected. If only one class was seen, handle gracefully.
        classes = list(self.model.classes_)
        if 1 in classes:
            return float(proba[0][classes.index(1)])
        return 0.0

    @staticmethod
    def _load_governance_map() -> dict[str, str]:
        """Load governance settings from project configs."""
        try:
            from django.conf import settings

            from franktheunicorn.config.loader import load_project_configs

            configs = load_project_configs(settings.FRANK_PROJECTS_DIR)
            return {c.full_name: c.governance for c in configs}
        except Exception:
            return {}

    def save(self, path: Path) -> None:
        """Serialize the predictor to a pickle file with HMAC signature."""
        path.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "model": self.model,
            "approval_rates": self._approval_rates,
            "trained": self._trained,
            "last_action_count": self._last_action_count,
        }
        payload = pickle.dumps(data)
        sig = _compute_hmac(payload)
        with open(path, "wb") as f:
            f.write(payload)
        sig_path = path.with_suffix(".sig")
        sig_path.write_text(sig)

    @classmethod
    def load(cls, path: Path) -> RejectionPredictor:
        """Load a predictor from a signed pickle file.

        Raises ValueError if the HMAC signature is missing or invalid.
        """
        sig_path = path.with_suffix(".sig")
        if not sig_path.exists():
            raise ValueError(f"Missing signature file for model at {path}")
        payload = path.read_bytes()
        expected_sig = sig_path.read_text().strip()
        actual_sig = _compute_hmac(payload)
        if not hmac.compare_digest(expected_sig, actual_sig):
            raise ValueError(f"HMAC signature mismatch for model at {path}")
        data = pickle.loads(payload)  # verified by HMAC above
        predictor = cls()
        predictor.model = data["model"]
        predictor._approval_rates = data["approval_rates"]
        predictor._trained = data["trained"]
        predictor._last_action_count = data.get("last_action_count", 0)
        return predictor


def _compute_hmac(payload: bytes) -> str:
    """Compute HMAC-SHA256 of *payload* using the Django SECRET_KEY."""
    from django.conf import settings

    key = settings.SECRET_KEY.encode()
    return hmac.new(key, payload, hashlib.sha256).hexdigest()


def model_path_for_project(project_owner: str, project_repo: str) -> Path:
    """Return the path where the rejection model pickle is stored."""
    from django.conf import settings

    data_dir = Path(getattr(settings, "DATA_DIR", Path.home() / ".review-agent"))
    return data_dir / "models" / f"{project_owner}-{project_repo}" / "rejection_model.pkl"


def _lock_path_for_model(model_path: Path) -> Path:
    """Return the lock file path for a model pickle."""
    return model_path.with_suffix(".lock")


def load_predictor_for_project(
    project_owner: str,
    project_repo: str,
) -> RejectionPredictor | None:
    """Load a trained predictor for a project, or None if not available."""
    path = model_path_for_project(project_owner, project_repo)
    if not path.exists():
        return None
    try:
        return RejectionPredictor.load(path)
    except Exception:
        logger.warning("Failed to load rejection model from %s", path, exc_info=True)
        return None


def maybe_retrain(
    project_id: int,
    project_owner: str,
    project_repo: str,
) -> bool:
    """Retrain the model if enough new actions have accumulated.

    Acquires a file lock to prevent concurrent retrains. If the lock
    is held by another process, skips silently.

    Returns True if retrained, False otherwise.
    """
    from franktheunicorn.core.models import OperatorAction

    action_count = OperatorAction.objects.filter(
        action_type__in=["accept_draft", "reject_draft", "edit_draft"],
        review_draft__pull_request__project_id=project_id,
    ).count()

    if action_count < MIN_ACTIONS_TO_TRAIN:
        return False

    model_path = model_path_for_project(project_owner, project_repo)
    lock_path = _lock_path_for_model(model_path)

    # Check if retrain is needed.
    if model_path.exists():
        try:
            existing = RejectionPredictor.load(model_path)
            if action_count - existing._last_action_count < RETRAIN_INTERVAL:
                return False
        except Exception:
            pass  # Corrupt model — retrain.

    # Try to acquire file lock (non-blocking).
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with open(lock_path, "w") as lock_fd:
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                logger.debug("Another process is retraining the rejection model, skipping.")
                return False

            try:
                predictor = RejectionPredictor()
                if predictor.train(project_id):
                    predictor.save(model_path)
                    logger.info("Retrained rejection model for %s/%s.", project_owner, project_repo)
                    return True
                return False
            finally:
                fcntl.flock(lock_fd, fcntl.LOCK_UN)
    except OSError:
        logger.debug("Could not open lock file for rejection model retrain.")
        return False
