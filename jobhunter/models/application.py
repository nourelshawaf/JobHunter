"""Application session model — tracks browser-assisted application state."""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import Boolean, DateTime, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from jobhunter.database import Base


class ApplicationSession(Base):
    """
    One browser-assisted application attempt for a job.

    A job can have multiple sessions (e.g. resumed after a break).
    The most recent session with status != abandoned is the active one.
    """

    __tablename__ = "application_sessions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    job_id: Mapped[str] = mapped_column(String(36), nullable=False)
    adapter: Mapped[Optional[str]] = mapped_column(String(64))  # workday|greenhouse|generic
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    last_active_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
    status: Mapped[str] = mapped_column(String(32), default="started")
    # started | paused | ready_for_review | submitted | abandoned

    # Captured form state (JSON) — resumed on restart
    form_state_json: Mapped[Optional[str]] = mapped_column(Text)

    # Summary shown to user before manual submission
    prefill_summary: Mapped[Optional[str]] = mapped_column(Text)

    # Flags
    needs_login: Mapped[bool] = mapped_column(Boolean, default=False)
    needs_captcha: Mapped[bool] = mapped_column(Boolean, default=False)
    has_sensitive_fields: Mapped[bool] = mapped_column(Boolean, default=False)

    def __repr__(self) -> str:
        return f"<ApplicationSession job={self.job_id} [{self.status}]>"
