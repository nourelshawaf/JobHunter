"""
Job model.

Stores every discovered job posting with full metadata,
scoring, deduplication info, and application status.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import Enum
from typing import Optional

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from jobhunter.database import Base


class JobStatus(str, Enum):
    """Application state machine states."""

    DISCOVERED = "discovered"
    SCORED = "scored"
    REJECTED_BY_FILTER = "rejected_by_filter"
    SAVED = "saved"
    APPLICATION_STARTED = "application_started"
    AWAITING_USER_INFO = "awaiting_user_info"
    AWAITING_LOGIN = "awaiting_login"
    AWAITING_CAPTCHA = "awaiting_captcha"
    FORM_PARTIALLY_COMPLETED = "form_partially_completed"
    READY_FOR_FINAL_REVIEW = "ready_for_final_review"
    MANUALLY_SUBMITTED = "manually_submitted"
    WITHDRAWN = "withdrawn"
    REJECTED = "rejected"
    INTERVIEW = "interview"
    OFFER = "offer"
    EXPIRED = "expired"


class WorkMode(str, Enum):
    REMOTE = "remote"
    HYBRID = "hybrid"
    ONSITE = "onsite"
    UNKNOWN = "unknown"


class JobType(str, Enum):
    INTERNSHIP = "internship"
    WORKING_STUDENT = "working_student"
    TRAINEE = "trainee"
    JUNIOR = "junior"
    GRADUATE = "graduate"
    PART_TIME = "part_time"
    UNKNOWN = "unknown"


class Job(Base):
    """A discovered job posting."""

    __tablename__ = "jobs"
    __table_args__ = (
        UniqueConstraint("source", "source_job_id", name="uq_source_job"),
        Index("ix_jobs_status", "status"),
        Index("ix_jobs_score", "relevance_score"),
        Index("ix_jobs_company", "company"),
        Index("ix_jobs_posted_at", "posted_at"),
    )

    # ── Identity ──────────────────────────────
    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    source: Mapped[str] = mapped_column(String(64), nullable=False)
    source_job_id: Mapped[Optional[str]] = mapped_column(String(256))

    # ── Core fields ───────────────────────────
    title: Mapped[str] = mapped_column(String(512), nullable=False)
    company: Mapped[str] = mapped_column(String(256), nullable=False)
    company_normalized: Mapped[Optional[str]] = mapped_column(String(256))
    location: Mapped[Optional[str]] = mapped_column(String(256))
    work_mode: Mapped[str] = mapped_column(String(16), default=WorkMode.UNKNOWN)
    job_type: Mapped[str] = mapped_column(String(32), default=JobType.UNKNOWN)

    # ── Content ───────────────────────────────
    description: Mapped[Optional[str]] = mapped_column(Text)
    requirements: Mapped[Optional[str]] = mapped_column(Text)
    preferred_qualifications: Mapped[Optional[str]] = mapped_column(Text)

    # ── Language & experience ─────────────────
    language_requirements: Mapped[Optional[str]] = mapped_column(String(512))
    hungarian_mandatory: Mapped[bool] = mapped_column(Boolean, default=False)
    required_experience_years: Mapped[Optional[int]] = mapped_column(Integer)
    student_friendly: Mapped[bool] = mapped_column(Boolean, default=False)

    # ── Dates ─────────────────────────────────
    posted_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    discovered_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    last_checked_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
    deadline: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))

    # ── URLs ──────────────────────────────────
    application_url: Mapped[Optional[str]] = mapped_column(Text)
    source_url: Mapped[Optional[str]] = mapped_column(Text)
    canonical_url: Mapped[Optional[str]] = mapped_column(Text)

    # ── Compensation ──────────────────────────
    salary_raw: Mapped[Optional[str]] = mapped_column(String(256))
    salary_min: Mapped[Optional[float]] = mapped_column(Float)
    salary_max: Mapped[Optional[float]] = mapped_column(Float)
    salary_currency: Mapped[Optional[str]] = mapped_column(String(8))

    # ── Work authorization ────────────────────
    visa_info: Mapped[Optional[str]] = mapped_column(String(512))
    sponsorship_available: Mapped[Optional[bool]] = mapped_column(Boolean)

    # ── Scoring ───────────────────────────────
    relevance_score: Mapped[Optional[int]] = mapped_column(Integer)
    score_explanation: Mapped[Optional[str]] = mapped_column(Text)
    ai_summary: Mapped[Optional[str]] = mapped_column(Text)
    ai_gap_analysis: Mapped[Optional[str]] = mapped_column(Text)
    suggested_cv_keywords: Mapped[Optional[str]] = mapped_column(Text)

    # ── Deduplication ─────────────────────────
    fingerprint: Mapped[Optional[str]] = mapped_column(String(64))
    duplicate_group_id: Mapped[Optional[str]] = mapped_column(String(36))
    is_primary_listing: Mapped[bool] = mapped_column(Boolean, default=True)

    # ── Status & notes ────────────────────────
    status: Mapped[str] = mapped_column(String(32), default=JobStatus.DISCOVERED)
    notes: Mapped[Optional[str]] = mapped_column(Text)
    archived: Mapped[bool] = mapped_column(Boolean, default=False)

    # ── Relationships ─────────────────────────
    status_history: Mapped[list["JobStatusHistory"]] = relationship(
        "JobStatusHistory", back_populates="job", cascade="all, delete-orphan"
    )
    # Note: Notification relationship intentionally omitted here to avoid
    # cross-module mapper init ordering issues. Access via:
    #   db.query(Notification).filter(Notification.job_id == job.id)

    def __repr__(self) -> str:
        return f"<Job {self.id[:8]} {self.company!r} — {self.title!r} [{self.status}]>"

    @property
    def is_expired(self) -> bool:
        """True if the deadline has passed."""
        if self.deadline is None:
            return False
        return datetime.utcnow() > self.deadline.replace(tzinfo=None)

    @property
    def days_since_posted(self) -> Optional[int]:
        """Days since the job was posted, or None if unknown."""
        if self.posted_at is None:
            return None
        delta = datetime.utcnow() - self.posted_at.replace(tzinfo=None)
        return delta.days


class JobStatusHistory(Base):
    """Audit log of every status change for a job."""

    __tablename__ = "job_status_history"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    job_id: Mapped[str] = mapped_column(String(36), ForeignKey("jobs.id", ondelete="CASCADE"), nullable=False)
    from_status: Mapped[Optional[str]] = mapped_column(String(32))
    to_status: Mapped[str] = mapped_column(String(32), nullable=False)
    changed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    changed_by: Mapped[str] = mapped_column(String(64), default="system")
    note: Mapped[Optional[str]] = mapped_column(Text)

    job: Mapped[Job] = relationship("Job", back_populates="status_history")

    def __repr__(self) -> str:
        return f"<StatusHistory {self.job_id[:8]} {self.from_status}→{self.to_status}>"
