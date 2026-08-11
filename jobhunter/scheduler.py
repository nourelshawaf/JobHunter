"""
APScheduler-based background scheduler.

Responsibilities:
- Run job ingestion on a configurable interval
- Send daily digest at a configured time
- Check application deadlines periodically
- Prevent overlapping ingestion runs (MaxInstancesError → skip)
- Log every execution with timing and outcome
- Support daemon mode (runs forever) and one-shot mode (runs once, exits)
- Disabled automatically during pytest (JOBHUNTER_TESTING env var)

Usage::

    from jobhunter.scheduler import Scheduler
    s = Scheduler()
    s.start()          # daemon: blocks until KeyboardInterrupt
    s.run_once()       # one-shot: runs all jobs immediately, then returns
    s.dry_run()        # prints schedule without executing
"""
from __future__ import annotations

import asyncio
import os
import signal
import sys
from datetime import datetime, timezone
from typing import Optional

import structlog
from apscheduler.events import EVENT_JOB_ERROR, EVENT_JOB_EXECUTED, EVENT_JOB_MISSED
from apscheduler.executors.pool import ThreadPoolExecutor
from apscheduler.job import Job as APSJob
from apscheduler.jobstores.memory import MemoryJobStore
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from jobhunter.config import get_search_config, get_settings

logger = structlog.get_logger(__name__)

# Set by conftest.py to disable real scheduling during tests
_TESTING = os.environ.get("JOBHUNTER_TESTING", "0") == "1"


class Scheduler:
    """
    Wraps APScheduler with jobhunter-specific jobs and lifecycle management.
    """

    def __init__(self) -> None:
        self.settings = get_settings()
        self.config = get_search_config()
        self._scheduler: Optional[BackgroundScheduler] = None
        self._running = False

    # ── Public API ────────────────────────────────────────────────────────

    def build(self) -> BackgroundScheduler:
        """Create and configure the APScheduler instance."""
        jobstores = {"default": MemoryJobStore()}
        executors = {"default": ThreadPoolExecutor(max_workers=2)}
        job_defaults = {
            "coalesce": True,      # merge missed runs into one
            "max_instances": 1,    # prevent overlapping ingestion
            "misfire_grace_time": 300,
        }

        scheduler = BackgroundScheduler(
            jobstores=jobstores,
            executors=executors,
            job_defaults=job_defaults,
            timezone="UTC",
        )

        # ── Ingestion job ─────────────────────────────────────────────
        interval_hours = self.settings.search_interval_hours
        scheduler.add_job(
            func=self._run_ingestion,
            trigger=IntervalTrigger(hours=interval_hours),
            id="ingestion",
            name=f"Job ingestion (every {interval_hours}h)",
            replace_existing=True,
        )

        # ── Daily digest ──────────────────────────────────────────────
        hh, mm = self.config.daily_summary_time.split(":")
        scheduler.add_job(
            func=self._run_daily_digest,
            trigger=CronTrigger(hour=int(hh), minute=int(mm)),
            id="daily_digest",
            name="Daily summary email",
            replace_existing=True,
        )

        # ── Deadline checker (every 6 hours) ──────────────────────────
        scheduler.add_job(
            func=self._run_deadline_check,
            trigger=IntervalTrigger(hours=6),
            id="deadline_check",
            name="Deadline checker",
            replace_existing=True,
        )

        # ── Event listeners ───────────────────────────────────────────
        scheduler.add_listener(self._on_job_executed, EVENT_JOB_EXECUTED)
        scheduler.add_listener(self._on_job_error, EVENT_JOB_ERROR)
        scheduler.add_listener(self._on_job_missed, EVENT_JOB_MISSED)

        self._scheduler = scheduler
        return scheduler

    def start(self) -> None:
        """Start the scheduler daemon. Blocks until SIGINT/SIGTERM."""
        if _TESTING:
            logger.warning("scheduler.disabled_in_testing")
            return

        scheduler = self.build()
        scheduler.start()
        self._running = True

        logger.info(
            "scheduler.started",
            jobs=[j.name for j in scheduler.get_jobs()],
            ingestion_interval_hours=self.settings.search_interval_hours,
            daily_digest_time=self.config.daily_summary_time,
        )

        # Print next run times
        for job in scheduler.get_jobs():
            logger.info(
                "scheduler.job_registered",
                job_id=job.id,
                name=job.name,
                trigger=str(job.trigger),
            )

        # Block on SIGINT/SIGTERM
        def _shutdown(sig: int, _frame: object) -> None:
            logger.info("scheduler.shutdown_signal", signal=sig)
            scheduler.shutdown(wait=False)
            self._running = False
            sys.exit(0)

        signal.signal(signal.SIGINT, _shutdown)
        signal.signal(signal.SIGTERM, _shutdown)

        try:
            import time
            while self._running:
                time.sleep(1)
        except (KeyboardInterrupt, SystemExit):
            scheduler.shutdown(wait=False)

    def run_once(self, connector_names: Optional[list[str]] = None) -> None:
        """Run all jobs immediately once, then return (one-shot mode)."""
        logger.info("scheduler.run_once")

        self._run_ingestion(connector_names=connector_names)
        self._run_deadline_check()

        logger.info("scheduler.run_once.complete")

    def dry_run(self) -> None:
        """Print the scheduled jobs and their next run times without executing."""
        scheduler = self.build()
        print("\nScheduled jobs (dry-run — nothing will execute):\n")
        for job in scheduler.get_jobs():
            print(f"  [{job.id}] {job.name}")
            trigger_str = str(job.trigger) if job.trigger else "N/A"
            print(f"    Trigger: {trigger_str}")
            print()
        print("Dry-run complete. Scheduler not started.\n")

    # ── Job implementations ───────────────────────────────────────────────

    def _run_ingestion(
        self, connector_names: Optional[list[str]] = None
    ) -> None:
        """Execute the discovery pipeline."""
        from jobhunter.pipeline import Pipeline

        started = datetime.now(timezone.utc)
        logger.info("scheduler.ingestion.start", connectors=connector_names)

        try:
            pipeline = Pipeline()
            if connector_names:
                pipeline.config._data.setdefault("connectors", {})["enabled"] = connector_names
            result = asyncio.run(pipeline.run())

            logger.info(
                "scheduler.ingestion.complete",
                new_jobs=result.new_jobs,
                updated=result.updated_jobs,
                rejected=result.rejected,
                errors=len(result.errors),
                duration_s=result.duration_seconds,
            )

            # Trigger notifications for high-score new jobs
            if result.new_jobs > 0:
                self._notify_new_jobs()

        except Exception as exc:
            logger.error("scheduler.ingestion.error", error=str(exc))

    def _run_daily_digest(self) -> None:
        """Send the daily summary notification."""
        from jobhunter.database import SessionLocal
        from jobhunter.models.job import Job, JobStatus
        from jobhunter.notifications.notifier import Notifier

        db = SessionLocal()
        try:
            from datetime import timedelta
            yesterday = datetime.now(timezone.utc) - timedelta(hours=24)
            new_jobs = (
                db.query(Job)
                .filter(Job.discovered_at >= yesterday)
                .order_by(Job.relevance_score.desc())
                .limit(50)
                .all()
            )
            notifier = Notifier(db)
            notifier.send_daily_digest(new_jobs)
            db.commit()
            logger.info("scheduler.daily_digest.sent", job_count=len(new_jobs))
        except Exception as exc:
            logger.error("scheduler.daily_digest.error", error=str(exc))
        finally:
            db.close()

    def _run_deadline_check(self) -> None:
        """Check for approaching deadlines and notify."""
        from jobhunter.database import SessionLocal
        from jobhunter.models.job import Job, JobStatus
        from jobhunter.notifications.notifier import Notifier

        db = SessionLocal()
        try:
            saved_jobs = (
                db.query(Job)
                .filter(
                    Job.status == JobStatus.SAVED,
                    Job.deadline.isnot(None),
                )
                .all()
            )
            notifier = Notifier(db)
            notifier.check_and_notify_deadlines(saved_jobs)
            db.commit()
            logger.info("scheduler.deadline_check.done", checked=len(saved_jobs))
        except Exception as exc:
            logger.error("scheduler.deadline_check.error", error=str(exc))
        finally:
            db.close()

    def _notify_new_jobs(self) -> None:
        """Notify about newly discovered high-score jobs."""
        from jobhunter.database import SessionLocal
        from jobhunter.models.job import Job, JobStatus
        from jobhunter.notifications.notifier import Notifier

        db = SessionLocal()
        try:
            threshold = self.config.min_score_to_notify
            from datetime import timedelta
            recent = datetime.now(timezone.utc) - timedelta(hours=self.settings.search_interval_hours + 1)
            high_score_jobs = (
                db.query(Job)
                .filter(
                    Job.relevance_score >= threshold,
                    Job.discovered_at >= recent,
                    Job.status == JobStatus.SCORED,
                )
                .all()
            )
            notifier = Notifier(db)
            for job in high_score_jobs:
                notifier.notify_new_high_score_job(job)
            db.commit()
        except Exception as exc:
            logger.error("scheduler.notify_new_jobs.error", error=str(exc))
        finally:
            db.close()

    # ── APScheduler event listeners ───────────────────────────────────────

    @staticmethod
    def _on_job_executed(event: object) -> None:
        logger.info(
            "scheduler.job_executed",
            job_id=getattr(event, "job_id", "?"),
            run_time=str(getattr(event, "scheduled_run_time", "?")),
        )

    @staticmethod
    def _on_job_error(event: object) -> None:
        logger.error(
            "scheduler.job_error",
            job_id=getattr(event, "job_id", "?"),
            exception=str(getattr(event, "exception", "?")),
        )

    @staticmethod
    def _on_job_missed(event: object) -> None:
        logger.warning(
            "scheduler.job_missed",
            job_id=getattr(event, "job_id", "?"),
            run_time=str(getattr(event, "scheduled_run_time", "?")),
        )
