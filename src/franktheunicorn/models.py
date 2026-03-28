"""SQLAlchemy ORM models.

All state lives in SQLite.  No Postgres, no Redis, no cloud.
"""

from __future__ import annotations

import datetime

from sqlalchemy import DateTime, Float, ForeignKey, Integer, String, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    """Shared base class."""


def _utcnow() -> datetime.datetime:
    return datetime.datetime.now(datetime.UTC)


class Project(Base):
    """A monitored GitHub repository."""

    __tablename__ = "projects"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    slug: Mapped[str] = mapped_column(String(128), unique=True, nullable=False, index=True)
    repo: Mapped[str] = mapped_column(String(256), nullable=False)
    review_context: Mapped[str] = mapped_column(Text, default="")
    asf_project: Mapped[bool] = mapped_column(default=False)
    enabled: Mapped[bool] = mapped_column(default=True)
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )

    pull_requests: Mapped[list[PullRequest]] = relationship(
        "PullRequest", back_populates="project", cascade="all, delete-orphan"
    )

    def __repr__(self) -> str:
        return f"<Project slug={self.slug!r} repo={self.repo!r}>"


class PullRequest(Base):
    """An ingested pull request."""

    __tablename__ = "pull_requests"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), nullable=False, index=True)
    github_pr_number: Mapped[int] = mapped_column(Integer, nullable=False)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    author_login: Mapped[str] = mapped_column(String(128), nullable=False)
    state: Mapped[str] = mapped_column(String(32), default="open")
    html_url: Mapped[str] = mapped_column(Text, nullable=False)
    # JSON-serialised list of changed file paths.
    changed_files_json: Mapped[str] = mapped_column(Text, default="[]")
    # Computed interest score (0.0-1.0+).
    interest_score: Mapped[float] = mapped_column(Float, default=0.0)
    # Labels as comma-separated string.
    labels: Mapped[str] = mapped_column(Text, default="")
    # Whether operator is the author.
    operator_is_author: Mapped[bool] = mapped_column(default=False)
    # Whether operator was mentioned or review-requested.
    operator_mentioned: Mapped[bool] = mapped_column(default=False)
    # Heuristic flags.
    likely_ai_generated: Mapped[bool] = mapped_column(default=False)
    new_contributor: Mapped[bool] = mapped_column(default=False)
    body: Mapped[str] = mapped_column(Text, default="")
    github_created_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    github_updated_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    ingested_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    last_scored_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    project: Mapped[Project] = relationship("Project", back_populates="pull_requests")
    review_drafts: Mapped[list[ReviewDraft]] = relationship(
        "ReviewDraft", back_populates="pull_request", cascade="all, delete-orphan"
    )
    operator_actions: Mapped[list[OperatorAction]] = relationship(
        "OperatorAction", back_populates="pull_request", cascade="all, delete-orphan"
    )

    def __repr__(self) -> str:
        return f"<PullRequest #{self.github_pr_number} project_id={self.project_id}>"


class ReviewDraft(Base):
    """A draft review comment generated (or later, by LLM) for a PR."""

    __tablename__ = "review_drafts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    pull_request_id: Mapped[int] = mapped_column(
        ForeignKey("pull_requests.id"), nullable=False, index=True
    )
    # "stub", "llm", "human-edited"
    source: Mapped[str] = mapped_column(String(32), default="stub")
    body: Mapped[str] = mapped_column(Text, nullable=False)
    # "pending", "posted", "discarded"
    status: Mapped[str] = mapped_column(String(32), default="pending")
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    posted_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    pull_request: Mapped[PullRequest] = relationship("PullRequest", back_populates="review_drafts")

    def __repr__(self) -> str:
        return f"<ReviewDraft id={self.id} pr_id={self.pull_request_id} status={self.status!r}>"


class AntiPattern(Base):
    """A stored anti-pattern: a comment the operator did not like.

    Used by the Bayesian heuristic to suppress similar future suggestions.
    """

    __tablename__ = "anti_patterns"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    # Short label, e.g. "nitpick-style", "pedantic-formatting".
    label: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    # Representative phrase that characterises this anti-pattern.
    phrase: Mapped[str] = mapped_column(Text, nullable=False)
    # Observed count (incremented on each negative feedback).
    count: Mapped[int] = mapped_column(Integer, default=1)
    # Total times a comment matching this pattern was shown.
    total_shown: Mapped[int] = mapped_column(Integer, default=1)
    # Derived probability = count / total_shown (updated on write).
    probability: Mapped[float] = mapped_column(Float, default=1.0)
    project_slug: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )

    def __repr__(self) -> str:
        return f"<AntiPattern label={self.label!r} p={self.probability:.2f}>"


class OperatorAction(Base):
    """Records what the operator did with a draft or PR.

    This is the primary feedback loop for the system.
    """

    __tablename__ = "operator_actions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    pull_request_id: Mapped[int] = mapped_column(
        ForeignKey("pull_requests.id"), nullable=False, index=True
    )
    # "posted", "discarded", "edited", "snoozed", "flagged-anti-pattern"
    action: Mapped[str] = mapped_column(String(64), nullable=False)
    # Optional free-text note from operator.
    note: Mapped[str] = mapped_column(Text, default="")
    acted_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    pull_request: Mapped[PullRequest] = relationship(
        "PullRequest", back_populates="operator_actions"
    )

    def __repr__(self) -> str:
        return f"<OperatorAction action={self.action!r} pr_id={self.pull_request_id}>"
