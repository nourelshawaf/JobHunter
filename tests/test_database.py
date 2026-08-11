"""
Database model and integrity tests.

Tests:
- Job creation and retrieval
- Unique constraint on (source, source_job_id)
- Status history cascade delete
- Notification FK integrity
- Profile CRUD
"""
from __future__ import annotations

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from jobhunter.models.job import Job, JobStatus, JobStatusHistory
from jobhunter.models.notification import Notification
from jobhunter.models.profile import CandidateProfile, Document


class TestJobModel:

    def test_create_and_retrieve_job(self, db: Session, make_job) -> None:
        job = make_job(title="Test Intern", company="ACME")
        db.commit()

        fetched = db.query(Job).filter(Job.id == job.id).one()
        assert fetched.title == "Test Intern"
        assert fetched.company == "ACME"

    def test_unique_constraint_source_and_id(self, db: Session, make_job) -> None:
        """Two jobs with the same source+source_job_id should violate the unique constraint."""
        make_job(source="test_source", source_job_id="JOB-001")
        db.commit()

        from jobhunter.models.job import Job as _Job
        dup = _Job(title="Dup", company="X", source="test_source", source_job_id="JOB-001")
        db.add(dup)
        with pytest.raises(IntegrityError):
            db.commit()

    def test_null_source_job_id_allowed(self, db: Session, make_job) -> None:
        """source_job_id=None is allowed (some connectors can't extract IDs)."""
        make_job(source="rss", source_job_id=None)
        make_job(source="rss", source_job_id=None)  # second None is fine
        db.commit()  # no error

    def test_status_default_is_discovered(self, db: Session) -> None:
        job = Job(title="X", company="Y", source="test")
        db.add(job)
        db.flush()
        assert job.status == JobStatus.DISCOVERED

    def test_is_primary_listing_default_true(self, db: Session) -> None:
        job = Job(title="X", company="Y", source="test")
        db.add(job)
        db.flush()
        assert job.is_primary_listing is True

    def test_status_history_cascade_delete(self, db: Session, make_job) -> None:
        """Deleting a job should cascade-delete its status history."""
        job = make_job()
        history = JobStatusHistory(
            job_id=job.id,
            from_status="discovered",
            to_status="scored",
            changed_by="test",
        )
        db.add(history)
        db.commit()

        db.delete(job)
        db.commit()

        remaining = db.query(JobStatusHistory).filter(
            JobStatusHistory.job_id == job.id
        ).count()
        assert remaining == 0

    def test_days_since_posted_none_when_no_date(self, db: Session, make_job) -> None:
        job = make_job(posted_at=None)
        assert job.days_since_posted is None

    def test_days_since_posted_recent(self, db: Session, make_job) -> None:
        from datetime import datetime, timedelta, timezone
        recent = datetime.now(timezone.utc) - timedelta(days=3)
        job = make_job(posted_at=recent)
        assert job.days_since_posted == 3

    def test_is_expired_false_no_deadline(self, db: Session, make_job) -> None:
        job = make_job(deadline=None)
        assert job.is_expired is False

    def test_is_expired_true_past_deadline(self, db: Session, make_job) -> None:
        from datetime import datetime, timedelta, timezone
        past = datetime.now(timezone.utc) - timedelta(days=10)
        job = make_job(deadline=past)
        assert job.is_expired is True

    def test_is_expired_false_future_deadline(self, db: Session, make_job) -> None:
        from datetime import datetime, timedelta, timezone
        future = datetime.now(timezone.utc) + timedelta(days=10)
        job = make_job(deadline=future)
        assert job.is_expired is False


class TestNotificationModel:

    def test_create_notification(self, db: Session, make_job) -> None:
        job = make_job()
        db.commit()

        notif = Notification(
            job_id=job.id,
            channel="email",
            event_type="new_high_score",
            subject="Test notification",
            success=True,
        )
        db.add(notif)
        db.commit()

        fetched = db.query(Notification).filter(Notification.job_id == job.id).one()
        assert fetched.event_type == "new_high_score"
        assert fetched.success is True

    def test_notification_without_job(self, db: Session) -> None:
        """Daily digest notifications have no job_id."""
        notif = Notification(
            job_id=None,
            channel="email",
            event_type="daily_summary",
            success=True,
        )
        db.add(notif)
        db.commit()
        assert notif.id is not None


class TestProfileModel:

    def test_create_profile(self, db: Session) -> None:
        profile = CandidateProfile(
            full_name="Noureldeen Elshawaf",
            email="test@example.com",
            university="University of Debrecen",
            degree="Mechatronics Engineering BSc",
            current_year=3,
        )
        db.add(profile)
        db.commit()

        fetched = db.query(CandidateProfile).filter(
            CandidateProfile.email == "test@example.com"
        ).one()
        assert fetched.full_name == "Noureldeen Elshawaf"
        assert fetched.current_year == 3

    def test_create_document(self, db: Session) -> None:
        doc = Document(
            name="CV_Robotics_2024.docx",
            doc_type="cv",
            variant="cv_robotics",
            file_path="data/documents/CV_Robotics_2024.docx",
        )
        db.add(doc)
        db.commit()
        assert doc.id is not None
        assert doc.is_active is True


class TestIdempotentIngest:
    """Verify that re-ingesting the same job does not create duplicates."""

    def test_same_source_job_id_no_duplicate(self, db: Session) -> None:
        """
        Simulates the pipeline upsert: second add of same source+ID should
        raise IntegrityError (caught by pipeline) rather than silently adding.
        """
        job1 = Job(
            title="Robotics Intern",
            company="Bosch",
            source="bosch_careers",
            source_job_id="BOSCH-1234",
        )
        db.add(job1)
        db.commit()

        job2 = Job(
            title="Robotics Intern",  # same posting
            company="Bosch",
            source="bosch_careers",
            source_job_id="BOSCH-1234",
        )
        db.add(job2)
        with pytest.raises(IntegrityError):
            db.commit()

        db.rollback()
        count = db.query(Job).filter(
            Job.source == "bosch_careers",
            Job.source_job_id == "BOSCH-1234",
        ).count()
        assert count == 1
