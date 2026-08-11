"""
Tests for the notification system.

Covers:
- Duplicate notification prevention
- Deadline approaching logic
- Daily digest rendering
- Email formatting
- Notifier with mocked SMTP
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import pytest
from sqlalchemy.orm import Session

from jobhunter.models.job import Job, JobStatus, JobType, WorkMode
from jobhunter.models.notification import Notification
from jobhunter.notifications.notifier import Notifier


# ── Helpers ───────────────────────────────────────────────────────────────

def make_job_obj(**kwargs: Any) -> Job:
    """Create a Job instance without DB persistence (for rendering tests)."""
    defaults = {
        "title": "Robotics Intern",
        "company": "Bosch",
        "location": "Budapest, Hungary",
        "job_type": JobType.INTERNSHIP,
        "work_mode": WorkMode.HYBRID,
        "relevance_score": 82,
        "status": JobStatus.SCORED,
        "source": "bosch_careers",
        "source_url": "https://careers.bosch.com/jobs/123",
        "application_url": "https://careers.bosch.com/jobs/123",
        "score_explanation": "82/100: robotics match, Budapest, English",
        "is_primary_listing": True,
        "hungarian_mandatory": False,
        "student_friendly": True,
    }
    defaults.update(kwargs)
    job = Job(**defaults)
    return job


# ── Deduplication tests ───────────────────────────────────────────────────

class TestNotificationDedup:

    def test_duplicate_notification_not_sent_twice(self, db: Session, make_job) -> None:
        """The same event for the same job should not generate two Notification records."""
        job = make_job(relevance_score=90)
        db.commit()

        notifier = Notifier(db)

        # Manually insert a notification record as if it was already sent
        existing = Notification(
            job_id=job.id,
            channel="email",
            event_type="new_high_score",
            subject="Test",
            success=True,
            sent_at=datetime.now(timezone.utc),
        )
        db.add(existing)
        db.commit()

        # Now check — should detect the duplicate
        assert notifier._already_notified(job.id, "new_high_score") is True

    def test_different_event_not_considered_duplicate(self, db: Session, make_job) -> None:
        job = make_job(relevance_score=90)
        db.commit()

        notifier = Notifier(db)

        existing = Notification(
            job_id=job.id,
            channel="email",
            event_type="new_high_score",
            success=True,
            sent_at=datetime.now(timezone.utc),
        )
        db.add(existing)
        db.commit()

        # Different event type — should NOT be considered duplicate
        assert notifier._already_notified(job.id, "deadline_3d") is False

    def test_old_notification_not_considered_duplicate(self, db: Session, make_job) -> None:
        """Notifications older than 24h should allow re-notification."""
        job = make_job(relevance_score=90)
        db.commit()

        notifier = Notifier(db)

        old_sent_at = datetime.now(timezone.utc) - timedelta(hours=25)
        old = Notification(
            job_id=job.id,
            channel="email",
            event_type="new_high_score",
            success=True,
            sent_at=old_sent_at,
        )
        db.add(old)
        db.commit()

        # Old enough — should NOT be considered duplicate
        assert notifier._already_notified(job.id, "new_high_score") is False

    def test_failed_notification_not_considered_duplicate(self, db: Session, make_job) -> None:
        """A failed notification should allow re-sending."""
        job = make_job(relevance_score=90)
        db.commit()

        notifier = Notifier(db)

        failed = Notification(
            job_id=job.id,
            channel="email",
            event_type="new_high_score",
            success=False,  # failed send
            sent_at=datetime.now(timezone.utc),
        )
        db.add(failed)
        db.commit()

        assert notifier._already_notified(job.id, "new_high_score") is False

    def test_none_job_id_never_duplicate(self, db: Session) -> None:
        """Daily digest (job_id=None) should never be flagged as duplicate."""
        notifier = Notifier(db)
        assert notifier._already_notified(None, "daily_summary") is False


# ── Deadline check tests ──────────────────────────────────────────────────

class TestDeadlineChecks:

    def test_deadline_3_days_triggers_warning(self, db: Session, make_job) -> None:
        deadline = datetime.now(timezone.utc) + timedelta(days=2, hours=12)
        job = make_job(status=JobStatus.SAVED, deadline=deadline)
        db.commit()

        notified_jobs = []

        class MockNotifier(Notifier):
            def notify_deadline_approaching(self, j: Job, days: int) -> None:
                notified_jobs.append((j.id, days))

        notifier = MockNotifier(db)
        notifier.check_and_notify_deadlines([job])

        assert len(notified_jobs) == 1
        assert notified_jobs[0][1] == 3  # 3-day warning

    def test_deadline_7_days_triggers_warning(self, db: Session, make_job) -> None:
        deadline = datetime.now(timezone.utc) + timedelta(days=6)
        job = make_job(status=JobStatus.SAVED, deadline=deadline)
        db.commit()

        notified_jobs = []

        class MockNotifier(Notifier):
            def notify_deadline_approaching(self, j: Job, days: int) -> None:
                notified_jobs.append((j.id, days))

        notifier = MockNotifier(db)
        notifier.check_and_notify_deadlines([job])

        assert len(notified_jobs) == 1
        assert notified_jobs[0][1] == 7

    def test_submitted_jobs_not_checked(self, db: Session, make_job) -> None:
        deadline = datetime.now(timezone.utc) + timedelta(days=1)
        job = make_job(status=JobStatus.MANUALLY_SUBMITTED, deadline=deadline)
        db.commit()

        notified = []

        class MockNotifier(Notifier):
            def notify_deadline_approaching(self, j: Job, days: int) -> None:
                notified.append(j.id)

        notifier = MockNotifier(db)
        notifier.check_and_notify_deadlines([job])
        assert len(notified) == 0

    def test_no_deadline_not_notified(self, db: Session, make_job) -> None:
        job = make_job(status=JobStatus.SAVED, deadline=None)
        db.commit()

        notified = []

        class MockNotifier(Notifier):
            def notify_deadline_approaching(self, j: Job, days: int) -> None:
                notified.append(j.id)

        notifier = MockNotifier(db)
        notifier.check_and_notify_deadlines([job])
        assert len(notified) == 0

    def test_future_deadline_beyond_7_days_not_notified(self, db: Session, make_job) -> None:
        deadline = datetime.now(timezone.utc) + timedelta(days=30)
        job = make_job(status=JobStatus.SAVED, deadline=deadline)
        db.commit()

        notified = []

        class MockNotifier(Notifier):
            def notify_deadline_approaching(self, j: Job, days: int) -> None:
                notified.append(j.id)

        notifier = MockNotifier(db)
        notifier.check_and_notify_deadlines([job])
        assert len(notified) == 0


# ── Rendering tests ───────────────────────────────────────────────────────

class TestNotifierRendering:

    def test_job_alert_body_contains_title(self) -> None:
        job = make_job_obj()
        job.id = "test-id-1"
        body = Notifier._render_job_alert(job)
        assert "Robotics Intern" in body
        assert "Bosch" in body

    def test_job_alert_body_contains_score(self) -> None:
        job = make_job_obj()
        job.id = "test-id-2"
        body = Notifier._render_job_alert(job)
        assert "82" in body

    def test_job_alert_body_contains_url(self) -> None:
        job = make_job_obj()
        job.id = "test-id-3"
        body = Notifier._render_job_alert(job)
        assert "careers.bosch.com" in body

    def test_daily_digest_with_jobs(self) -> None:
        jobs = [make_job_obj() for _ in range(3)]
        for i, j in enumerate(jobs):
            j.id = f"test-{i}"
        body = Notifier._render_daily_digest(jobs, jobs)
        assert "HIGH-MATCH" in body
        assert "Robotics Intern" in body

    def test_daily_digest_empty(self) -> None:
        body = Notifier._render_daily_digest([], [])
        assert "Digest" in body

    def test_send_logs_to_db_without_smtp(self, db: Session, make_job) -> None:
        """Notifier._send() should log to DB even when email is not configured."""
        job = make_job(relevance_score=85)
        db.commit()

        notifier = Notifier(db)
        # No SMTP configured — should not raise, just log failure
        notifier._send(
            event_type="test_event",
            subject="Test",
            body="Test body",
            job_id=job.id,
        )
        db.commit()

        record = (
            db.query(Notification)
            .filter(Notification.job_id == job.id)
            .first()
        )
        assert record is not None
        assert record.event_type == "test_event"


# ── Integration: notify_new_high_score_job ────────────────────────────────

class TestNotifyHighScoreJob:

    def test_notify_creates_notification_record(self, db: Session, make_job) -> None:
        job = make_job(relevance_score=90)
        db.commit()

        notifier = Notifier(db)
        notifier.notify_new_high_score_job(job)
        db.commit()

        record = (
            db.query(Notification)
            .filter(
                Notification.job_id == job.id,
                Notification.event_type == "new_high_score",
            )
            .first()
        )
        assert record is not None

    def test_notify_not_sent_twice(self, db: Session, make_job) -> None:
        job = make_job(relevance_score=90)
        db.commit()

        notifier = Notifier(db)
        notifier.notify_new_high_score_job(job)
        db.commit()
        notifier.notify_new_high_score_job(job)  # second call
        db.commit()

        count = (
            db.query(Notification)
            .filter(
                Notification.job_id == job.id,
                Notification.event_type == "new_high_score",
            )
            .count()
        )
        assert count == 1  # only one record created

    def test_send_daily_digest_empty_list_no_op(self, db: Session) -> None:
        notifier = Notifier(db)
        notifier.send_daily_digest([])  # should not raise
