"""Notification log model."""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from jobhunter.database import Base


class Notification(Base):
    """Record of every notification sent — prevents duplicate alerts."""

    __tablename__ = "notifications"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    job_id: Mapped[Optional[str]] = mapped_column(String(36), ForeignKey("jobs.id", ondelete="SET NULL"))
    channel: Mapped[str] = mapped_column(String(32), nullable=False)  # email|telegram|desktop
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    # e.g. new_high_score | deadline_approaching | daily_summary
    subject: Mapped[Optional[str]] = mapped_column(String(512))
    body_preview: Mapped[Optional[str]] = mapped_column(Text)
    sent_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    success: Mapped[bool] = mapped_column(Boolean, default=True)
    error: Mapped[Optional[str]] = mapped_column(Text)

    def __repr__(self) -> str:
        return f"<Notification {self.channel} {self.event_type} job={self.job_id}>"
