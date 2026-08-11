"""Tests for the application state machine."""
from __future__ import annotations

import pytest
from sqlalchemy.orm import Session

from jobhunter.models.job import Job, JobStatus, JobStatusHistory
from jobhunter.state_machine import InvalidTransitionError, StateMachine, VALID_TRANSITIONS


class TestStateMachine:

    def test_valid_transition_discovered_to_scored(self, db: Session, make_job) -> None:
        job = make_job(status=JobStatus.DISCOVERED)
        sm = StateMachine(db)
        sm.transition(job, JobStatus.SCORED, changed_by="pipeline")
        db.commit()
        assert job.status == JobStatus.SCORED

    def test_valid_transition_scored_to_saved(self, db: Session, make_job) -> None:
        job = make_job(status=JobStatus.SCORED)
        sm = StateMachine(db)
        sm.transition(job, JobStatus.SAVED, changed_by="user")
        assert job.status == JobStatus.SAVED

    def test_invalid_transition_raises(self, db: Session, make_job) -> None:
        job = make_job(status=JobStatus.DISCOVERED)
        sm = StateMachine(db)
        with pytest.raises(InvalidTransitionError) as exc_info:
            sm.transition(job, JobStatus.MANUALLY_SUBMITTED)
        assert "discovered" in str(exc_info.value).lower()

    def test_transition_logs_history(self, db: Session, make_job) -> None:
        job = make_job(status=JobStatus.DISCOVERED)
        sm = StateMachine(db)
        sm.transition(job, JobStatus.SCORED, changed_by="pipeline", note="auto-scored")
        db.commit()

        history = (
            db.query(JobStatusHistory)
            .filter(JobStatusHistory.job_id == job.id)
            .all()
        )
        assert len(history) == 1
        assert history[0].from_status == JobStatus.DISCOVERED
        assert history[0].to_status == JobStatus.SCORED
        assert history[0].changed_by == "pipeline"
        assert history[0].note == "auto-scored"

    def test_noop_transition_no_history(self, db: Session, make_job) -> None:
        """Transitioning to the current state should be a no-op."""
        job = make_job(status=JobStatus.SCORED)
        sm = StateMachine(db)
        sm.transition(job, JobStatus.SCORED)
        db.commit()

        count = db.query(JobStatusHistory).filter(JobStatusHistory.job_id == job.id).count()
        assert count == 0

    def test_can_transition_to_expired_from_any_state(self, db: Session, make_job) -> None:
        """Expiry should be reachable from any state (force-allowed target)."""
        for status in [JobStatus.DISCOVERED, JobStatus.SCORED, JobStatus.SAVED]:
            job = make_job(status=status)
            sm = StateMachine(db)
            sm.transition(job, JobStatus.EXPIRED)
            assert job.status == JobStatus.EXPIRED

    def test_can_transition_to_withdrawn_from_any_active_state(self, db: Session, make_job) -> None:
        for status in [JobStatus.SAVED, JobStatus.APPLICATION_STARTED, JobStatus.FORM_PARTIALLY_COMPLETED]:
            job = make_job(status=status)
            sm = StateMachine(db)
            sm.transition(job, JobStatus.WITHDRAWN)
            assert job.status == JobStatus.WITHDRAWN

    def test_can_transition_returns_bool(self, db: Session, make_job) -> None:
        job = make_job(status=JobStatus.SCORED)
        sm = StateMachine(db)
        assert sm.can_transition(job, JobStatus.SAVED) is True
        assert sm.can_transition(job, JobStatus.INTERVIEW) is False

    def test_available_transitions_returns_list(self, db: Session, make_job) -> None:
        job = make_job(status=JobStatus.SCORED)
        sm = StateMachine(db)
        available = sm.available_transitions(job)
        assert isinstance(available, list)
        assert JobStatus.SAVED in available

    def test_terminal_states_have_no_outgoing(self, db: Session, make_job) -> None:
        for terminal in [JobStatus.WITHDRAWN, JobStatus.REJECTED]:
            job = make_job(status=terminal)
            sm = StateMachine(db)
            # Expired/withdrawn are force-allowed so skip those
            with pytest.raises(InvalidTransitionError):
                sm.transition(job, JobStatus.SCORED)

    def test_force_flag_bypasses_validation(self, db: Session, make_job) -> None:
        job = make_job(status=JobStatus.WITHDRAWN)
        sm = StateMachine(db)
        # Normally illegal, but force=True allows data repair
        sm.transition(job, JobStatus.SCORED, force=True, note="data repair")
        assert job.status == JobStatus.SCORED

    def test_full_happy_path(self, db: Session, make_job) -> None:
        """Walk through the full normal application lifecycle."""
        job = make_job(status=JobStatus.DISCOVERED)
        sm = StateMachine(db)

        path = [
            JobStatus.SCORED,
            JobStatus.SAVED,
            JobStatus.APPLICATION_STARTED,
            JobStatus.FORM_PARTIALLY_COMPLETED,
            JobStatus.READY_FOR_FINAL_REVIEW,
            JobStatus.MANUALLY_SUBMITTED,
            JobStatus.INTERVIEW,
            JobStatus.OFFER,
        ]
        for target in path:
            sm.transition(job, target, changed_by="user")
            assert job.status == target

        db.commit()
        history_count = db.query(JobStatusHistory).filter(
            JobStatusHistory.job_id == job.id
        ).count()
        assert history_count == len(path)

    def test_all_valid_transitions_actually_work(self, db: Session, make_job) -> None:
        """Smoke-test that every declared transition in VALID_TRANSITIONS can execute."""
        sm_count = 0
        for from_status, targets in VALID_TRANSITIONS.items():
            for to_status in targets:
                job = make_job(status=from_status)
                sm = StateMachine(db)
                sm.transition(job, to_status, force=True)
                assert job.status == to_status
                sm_count += 1
        assert sm_count > 10  # ensure we actually tested something
