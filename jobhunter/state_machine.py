"""
Application state machine.

Enforces valid transitions between job application states,
logs every change to job_status_history, and raises on illegal moves.

States flow roughly:
  discovered → scored → saved → application_started → ... → manually_submitted
  Any state → rejected_by_filter | withdrawn | expired
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

import structlog
from sqlalchemy.orm import Session

from jobhunter.models.job import Job, JobStatus, JobStatusHistory

logger = structlog.get_logger(__name__)

# ── Valid transitions ──────────────────────────────────────────────────────
# Maps each state to the set of states it may transition into.
VALID_TRANSITIONS: dict[str, set[str]] = {
    JobStatus.DISCOVERED: {
        JobStatus.SCORED,
        JobStatus.REJECTED_BY_FILTER,
        JobStatus.EXPIRED,
    },
    JobStatus.SCORED: {
        JobStatus.SAVED,
        JobStatus.REJECTED_BY_FILTER,
        JobStatus.EXPIRED,
    },
    JobStatus.REJECTED_BY_FILTER: {
        JobStatus.SAVED,          # user overrides filter decision
    },
    JobStatus.SAVED: {
        JobStatus.APPLICATION_STARTED,
        JobStatus.WITHDRAWN,
        JobStatus.EXPIRED,
    },
    JobStatus.APPLICATION_STARTED: {
        JobStatus.AWAITING_USER_INFO,
        JobStatus.AWAITING_LOGIN,
        JobStatus.AWAITING_CAPTCHA,
        JobStatus.FORM_PARTIALLY_COMPLETED,
        JobStatus.WITHDRAWN,
    },
    JobStatus.AWAITING_USER_INFO: {
        JobStatus.FORM_PARTIALLY_COMPLETED,
        JobStatus.WITHDRAWN,
    },
    JobStatus.AWAITING_LOGIN: {
        JobStatus.APPLICATION_STARTED,
        JobStatus.WITHDRAWN,
    },
    JobStatus.AWAITING_CAPTCHA: {
        JobStatus.APPLICATION_STARTED,
        JobStatus.WITHDRAWN,
    },
    JobStatus.FORM_PARTIALLY_COMPLETED: {
        JobStatus.AWAITING_USER_INFO,
        JobStatus.READY_FOR_FINAL_REVIEW,
        JobStatus.WITHDRAWN,
    },
    JobStatus.READY_FOR_FINAL_REVIEW: {
        JobStatus.MANUALLY_SUBMITTED,
        JobStatus.FORM_PARTIALLY_COMPLETED,   # user wants to edit
        JobStatus.WITHDRAWN,
    },
    JobStatus.MANUALLY_SUBMITTED: {
        JobStatus.INTERVIEW,
        JobStatus.REJECTED,
        JobStatus.WITHDRAWN,
    },
    JobStatus.INTERVIEW: {
        JobStatus.OFFER,
        JobStatus.REJECTED,
        JobStatus.WITHDRAWN,
    },
    JobStatus.OFFER: {
        JobStatus.WITHDRAWN,   # declined offer
    },
    # Terminal states — no outgoing transitions
    JobStatus.WITHDRAWN: set(),
    JobStatus.REJECTED: set(),
    JobStatus.EXPIRED: set(),
}

# States from which any terminal state may be reached without validation
FORCE_ALLOWED_TARGETS = {
    JobStatus.EXPIRED,
    JobStatus.WITHDRAWN,
}


class InvalidTransitionError(ValueError):
    """Raised when an illegal state transition is attempted."""

    def __init__(self, from_state: str, to_state: str) -> None:
        super().__init__(
            f"Cannot transition from '{from_state}' to '{to_state}'. "
            f"Valid targets: {sorted(VALID_TRANSITIONS.get(from_state, set()))}"
        )
        self.from_state = from_state
        self.to_state = to_state


class StateMachine:
    """
    Manages job application state transitions.

    Usage::

        sm = StateMachine(db)
        sm.transition(job, JobStatus.SAVED, changed_by="user", note="Looks great")
        db.commit()
    """

    def __init__(self, db: Session) -> None:
        self.db = db

    def transition(
        self,
        job: Job,
        to_state: str,
        *,
        changed_by: str = "system",
        note: Optional[str] = None,
        force: bool = False,
    ) -> Job:
        """
        Move a job to a new state, validating the transition first.

        Args:
            job: The Job ORM instance to update.
            to_state: Target state string (use JobStatus constants).
            changed_by: Who initiated the change ("system", "user", "pipeline").
            note: Optional free-text explanation recorded in the audit log.
            force: Skip transition validation (use only for data-repair tasks).

        Returns:
            The mutated job instance (not committed).

        Raises:
            InvalidTransitionError: If the transition is not permitted and force=False.
        """
        from_state = job.status

        if from_state == to_state:
            return job  # no-op, not an error

        if not force:
            self._validate(from_state, to_state)

        # Record audit entry
        history = JobStatusHistory(
            job_id=job.id,
            from_status=from_state,
            to_status=to_state,
            changed_at=datetime.now(timezone.utc),
            changed_by=changed_by,
            note=note,
        )
        self.db.add(history)

        job.status = to_state

        logger.info(
            "state_machine.transition",
            job_id=job.id,
            from_state=from_state,
            to_state=to_state,
            changed_by=changed_by,
        )

        return job

    def can_transition(self, job: Job, to_state: str) -> bool:
        """Return True if the transition is currently allowed."""
        try:
            self._validate(job.status, to_state)
            return True
        except InvalidTransitionError:
            return False

    def available_transitions(self, job: Job) -> list[str]:
        """Return the list of states this job can move to right now."""
        return sorted(VALID_TRANSITIONS.get(job.status, set()))

    # ── Private ───────────────────────────────────────────────────────────

    @staticmethod
    def _validate(from_state: str, to_state: str) -> None:
        """Raise InvalidTransitionError if the move is illegal."""
        # Force-allowed targets (expired/withdrawn) are always reachable
        if to_state in FORCE_ALLOWED_TARGETS:
            return

        allowed = VALID_TRANSITIONS.get(from_state)
        if allowed is None:
            raise InvalidTransitionError(from_state, to_state)

        if to_state not in allowed:
            raise InvalidTransitionError(from_state, to_state)
