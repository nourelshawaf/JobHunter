"""
Notification dispatcher.

Sends email (and optionally Telegram) alerts when:
- A new job scores above the threshold
- An application deadline is approaching
- A daily digest is ready

All sent notifications are logged to the database to prevent duplicates.
"""
from __future__ import annotations

import smtplib
import textwrap
from datetime import datetime, timedelta, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Any, Optional

import structlog
from sqlalchemy.orm import Session

from jobhunter.config import get_search_config, get_settings
from jobhunter.models.job import Job, JobStatus
from jobhunter.models.notification import Notification

logger = structlog.get_logger(__name__)


class Notifier:
    """Dispatches notifications across configured channels."""

    def __init__(self, db: Session) -> None:
        self.db = db
        self.settings = get_settings()
        self.config = get_search_config()

    # ── Public API ────────────────────────────────────────────────────────

    def notify_new_high_score_job(self, job: Job) -> None:
        """Send an alert for a newly discovered high-scoring job."""
        if self._already_notified(job.id, "new_high_score"):
            return

        subject = f"[JobHunter] New match: {job.title} @ {job.company} ({job.relevance_score}/100)"
        body = self._render_job_alert(job)

        self._send(
            job_id=job.id,
            event_type="new_high_score",
            subject=subject,
            body=body,
            job_obj=job,
        )

    def notify_deadline_approaching(self, job: Job, days_remaining: int) -> None:
        """Warn about an upcoming application deadline."""
        event = f"deadline_{days_remaining}d"
        if self._already_notified(job.id, event):
            return

        subject = (
            f"[JobHunter] ⏰ Deadline in {days_remaining} day(s): "
            f"{job.title} @ {job.company}"
        )
        body = textwrap.dedent(f"""
            Application deadline approaching!

            Job:      {job.title}
            Company:  {job.company}
            Location: {job.location or 'N/A'}
            Score:    {job.relevance_score}/100
            Deadline: {job.deadline}
            Status:   {job.status}

            Apply here: {job.application_url or 'N/A'}
        """).strip()

        self._send(job_id=job.id, event_type=event, subject=subject, body=body)

    def send_daily_digest(self, jobs: list[Job]) -> None:
        """Send the daily summary of new and high-priority jobs."""
        if not jobs:
            return

        high_score = [j for j in jobs if (j.relevance_score or 0) >= self.config.min_score_to_notify]
        all_new = [j for j in jobs if j.status == JobStatus.SCORED]

        subject = (
            f"[JobHunter] Daily digest — {len(high_score)} high-match, "
            f"{len(all_new)} new jobs"
        )
        body = self._render_daily_digest(high_score, all_new)

        self._send(job_id=None, event_type="daily_summary", subject=subject, body=body)

    def check_and_notify_deadlines(self, all_jobs: list[Job]) -> None:
        """Check all saved jobs for approaching deadlines and notify."""
        now = datetime.now(timezone.utc)
        warning_days = [3, 7]

        for job in all_jobs:
            if not job.deadline or job.status in (
                JobStatus.MANUALLY_SUBMITTED, JobStatus.WITHDRAWN,
                JobStatus.REJECTED, JobStatus.EXPIRED
            ):
                continue
            for days in warning_days:
                threshold = now + timedelta(days=days)
                # Normalise deadline to UTC-aware for comparison
                deadline = job.deadline
                if deadline.tzinfo is None:
                    deadline = deadline.replace(tzinfo=timezone.utc)
                if deadline <= threshold:
                    self.notify_deadline_approaching(job, days)
                    break  # only notify for the smallest bracket

    # ── Rendering ─────────────────────────────────────────────────────────

    @staticmethod
    def _render_job_alert(job: Job) -> str:
        return textwrap.dedent(f"""
            New high-match job discovered!

            Title:    {job.title}
            Company:  {job.company}
            Location: {job.location or 'N/A'}
            Score:    {job.relevance_score}/100
            Type:     {job.job_type}
            Mode:     {job.work_mode}
            Posted:   {job.posted_at.date() if job.posted_at else 'Unknown'}
            Deadline: {job.deadline.date() if job.deadline else 'Not specified'}

            Why it matched:
            {job.score_explanation or 'No explanation available'}

            Apply here: {job.application_url or 'N/A'}
            Source:     {job.source_url or 'N/A'}
        """).strip()

    @staticmethod
    def _render_daily_digest(high_score: list[Job], all_new: list[Job]) -> str:
        lines = ["=" * 60, "JobHunter Daily Digest", "=" * 60, ""]

        if high_score:
            lines.append(f"🌟 HIGH-MATCH JOBS ({len(high_score)})")
            lines.append("-" * 40)
            for job in sorted(high_score, key=lambda j: j.relevance_score or 0, reverse=True)[:10]:
                lines.append(
                    f"  [{job.relevance_score}/100] {job.title} @ {job.company} "
                    f"({job.location or 'N/A'})"
                )
                lines.append(f"    → {job.application_url or 'N/A'}")
            lines.append("")

        if all_new:
            lines.append(f"📋 ALL NEW JOBS ({len(all_new)})")
            lines.append("-" * 40)
            for job in all_new[:20]:
                lines.append(
                    f"  [{job.relevance_score or '?'}/100] {job.title} @ {job.company}"
                )
            if len(all_new) > 20:
                lines.append(f"  ... and {len(all_new) - 20} more")

        lines.append("")
        lines.append(f"Generated: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")
        return "\n".join(lines)

    # ── Transport ─────────────────────────────────────────────────────────

    def _send(
        self,
        *,
        event_type: str,
        subject: str,
        body: str,
        job_id: Optional[str] = None,
        job_obj: Optional[Any] = None,
    ) -> None:
        """Dispatch to all enabled channels."""
        channels = self.config.notification_channels
        errors: list[str] = []

        if "email" in channels:
            try:
                self._send_email(subject, body)
            except Exception as exc:
                errors.append(f"email: {exc}")
                logger.error("notifier.email_failed", error=str(exc))

        if "telegram" in channels:
            try:
                from jobhunter.notifications.telegram import get_telegram_notifier
                tg = get_telegram_notifier()
                if tg:
                    if job_obj and event_type == "new_high_score":
                        tg.send_new_job(job_obj)
                    elif job_obj and event_type.startswith("deadline_"):
                        days = int(event_type.split("_")[1].rstrip("d"))
                        tg.send_deadline_warning(job_obj, days)
                    else:
                        tg.send_plain(f"[JobHunter] {subject}\n\n{body[:500]}")
            except Exception as exc:
                errors.append(f"telegram: {exc}")
                logger.error("notifier.telegram_failed", error=str(exc))

        # Log regardless of send success
        record = Notification(
            job_id=job_id,
            channel="email" if "email" in channels else "none",
            event_type=event_type,
            subject=subject[:500],
            body_preview=body[:500],
            sent_at=datetime.now(timezone.utc),
            success=len(errors) == 0,
            error="; ".join(errors) if errors else None,
        )
        self.db.add(record)
        self.db.flush()

        logger.info(
            "notifier.sent",
            event_type=event_type,
            job_id=job_id,
            channels=channels,
            success=len(errors) == 0,
        )

    def _send_email(self, subject: str, body: str) -> None:
        """Send a plain-text email via SMTP."""
        settings = self.settings

        if not settings.notify_to_email or not settings.smtp_user:
            logger.debug("notifier.email_not_configured")
            return

        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"] = settings.smtp_user
        msg["To"] = settings.notify_to_email
        msg.attach(MIMEText(body, "plain", "utf-8"))

        with smtplib.SMTP(settings.smtp_host, settings.smtp_port) as smtp:
            smtp.ehlo()
            smtp.starttls()
            smtp.login(settings.smtp_user, settings.smtp_password.get_secret_value())
            smtp.sendmail(settings.smtp_user, settings.notify_to_email, msg.as_string())

    def _already_notified(self, job_id: Optional[str], event_type: str) -> bool:
        """Return True if this event was already notified in the last 24h."""
        if job_id is None:
            return False
        cutoff = datetime.now(timezone.utc) - timedelta(hours=24)
        existing = (
            self.db.query(Notification)
            .filter(
                Notification.job_id == job_id,
                Notification.event_type == event_type,
                Notification.sent_at >= cutoff,
                Notification.success.is_(True),
            )
            .first()
        )
        return existing is not None
