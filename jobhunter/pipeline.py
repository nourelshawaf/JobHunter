"""
Job discovery pipeline.

Orchestrates the full flow:
  connector.run() → normalise → deduplicate → score → persist → notify

Each connector runs independently — a failure in one never blocks others.
Results are idempotently written to the database (upsert on source+source_job_id).
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Optional

import structlog
from sqlalchemy.orm import Session

from jobhunter.config import get_search_config, get_settings
from jobhunter.connectors.base import BaseConnector, ConnectorResult
from jobhunter.connectors.boards.bosch import BoschCareersConnector
from jobhunter.connectors.boards.eures import EURESConnector
from jobhunter.connectors.boards.profession_hu import ProfessionHuConnector
from jobhunter.connectors.company.baker_hughes import BakerHughesCareersConnector
from jobhunter.connectors.company.bmw import BMWCareersConnector
from jobhunter.connectors.company.continental import ContinentalCareersConnector
from jobhunter.connectors.company.knorr_bremse import KnorrBremseCareersConnector
from jobhunter.connectors.company.valeo_zf_abb import (
    ABBCareersConnector,
    ValeoCareersConnector,
    ZFCareersConnector,
)
from jobhunter.connectors.boards.rss import (
    GraduatelandConnector,
    JoobleRSSConnector,
)
from jobhunter.connectors.company.siemens import SiemensCareersConnector
from jobhunter.connectors.email_alerts import EmailAlertsConnector
from jobhunter.database import SessionLocal
from jobhunter.deduplication.engine import DeduplicationEngine
from jobhunter.models.job import Job, JobStatus
from jobhunter.normalisation.normaliser import Normaliser
from jobhunter.scoring.rule_engine import RuleEngine, apply_score

logger = structlog.get_logger(__name__)

# Registry of all available connectors
CONNECTOR_REGISTRY: dict[str, type[BaseConnector]] = {
    "profession_hu": ProfessionHuConnector,
    "eures": EURESConnector,
    "bosch_careers": BoschCareersConnector,
    "bmw_careers": BMWCareersConnector,
    "baker_hughes_careers": BakerHughesCareersConnector,
    "siemens_careers": SiemensCareersConnector,
    "continental_careers": ContinentalCareersConnector,
    "email_alerts": EmailAlertsConnector,
    "knorr_bremse_careers": KnorrBremseCareersConnector,
    "valeo_careers": ValeoCareersConnector,
    "zf_careers": ZFCareersConnector,
    "abb_careers": ABBCareersConnector,
    "jooble_rss": JoobleRSSConnector,
    "graduateland": GraduatelandConnector,
}


class Pipeline:
    """
    Main job discovery pipeline.

    Usage::

        pipeline = Pipeline()
        await pipeline.run()
    """

    def __init__(self, db: Optional[Session] = None) -> None:
        self.settings = get_settings()
        self.config = get_search_config()
        self.normaliser = Normaliser()
        self.rule_engine = RuleEngine()
        self._db = db

    async def run(self) -> PipelineResult:
        """
        Run all enabled connectors and process their results.

        Returns a summary of what was discovered, saved, and rejected.
        """
        started = datetime.utcnow()
        logger.info("pipeline.start")

        # Build connector instances for enabled connectors
        connectors = self._build_connectors()

        if not connectors:
            logger.warning("pipeline.no_connectors_enabled")
            return PipelineResult(started=started)

        # Run all connectors concurrently
        connector_results = await asyncio.gather(
            *[c.run() for c in connectors],
            return_exceptions=True,
        )

        # Process results
        result = PipelineResult(started=started)
        db = self._db or SessionLocal()
        should_close = self._db is None

        try:
            dedup_engine = DeduplicationEngine(db)
            dedup_engine.load_existing_fingerprints()

            for connector_result in connector_results:
                if isinstance(connector_result, Exception):
                    logger.error("pipeline.connector_exception", error=str(connector_result))
                    result.errors.append(str(connector_result))
                    continue

                batch_result = self._process_connector_result(
                    connector_result, db, dedup_engine
                )
                result.merge(batch_result)

            db.commit()
            result.finished_at = datetime.utcnow()
            logger.info(
                "pipeline.complete",
                new_jobs=result.new_jobs,
                updated=result.updated_jobs,
                duplicates=result.duplicates,
                rejected=result.rejected,
                duration_seconds=result.duration_seconds,
            )
        except Exception as exc:
            db.rollback()
            logger.error("pipeline.fatal_error", error=str(exc))
            result.errors.append(str(exc))
            raise
        finally:
            if should_close:
                db.close()

        return result

    def _process_connector_result(
        self,
        connector_result: ConnectorResult,
        db: Session,
        dedup_engine: DeduplicationEngine,
    ) -> "PipelineResult":
        """Process raw jobs from one connector."""
        batch = PipelineResult()

        for raw_job in connector_result.jobs:
            try:
                # 1. Normalise
                job = self.normaliser.normalise(raw_job)

                # 2. Check for existing record (idempotent upsert)
                existing = self._find_existing(db, job.source, job.source_job_id)
                if existing:
                    self._update_existing(existing, job)
                    batch.updated_jobs += 1
                    continue

                # 3. Deduplicate
                job = dedup_engine.process(job)
                if not job.is_primary_listing:
                    batch.duplicates += 1

                # 4. Score
                apply_score(job, self.rule_engine)

                # 5. Auto-reject low scores
                min_save = self.config.min_score_to_save
                if (
                    job.status != JobStatus.REJECTED_BY_FILTER
                    and job.relevance_score is not None
                    and job.relevance_score < self.config.auto_reject_below
                ):
                    job.status = JobStatus.REJECTED_BY_FILTER
                    batch.rejected += 1
                else:
                    batch.new_jobs += 1

                db.add(job)

                logger.debug(
                    "pipeline.job_added",
                    title=job.title,
                    company=job.company,
                    score=job.relevance_score,
                    status=job.status,
                )

            except Exception as exc:
                logger.warning(
                    "pipeline.job_processing_error",
                    source=connector_result.connector_name,
                    error=str(exc),
                )
                batch.errors.append(str(exc))

        return batch

    def _build_connectors(self) -> list[BaseConnector]:
        """Instantiate enabled connectors from registry."""
        connectors: list[BaseConnector] = []
        for name in self.config.enabled_connectors:
            cls = CONNECTOR_REGISTRY.get(name)
            if cls is None:
                logger.warning("pipeline.unknown_connector", name=name)
                continue
            connectors.append(cls(settings=self.settings))
        return connectors

    @staticmethod
    def _find_existing(
        db: Session, source: str, source_job_id: Optional[str]
    ) -> Optional[Job]:
        """Look up an existing job by source + source_job_id."""
        if not source_job_id:
            return None
        return (
            db.query(Job)
            .filter(Job.source == source, Job.source_job_id == source_job_id)
            .first()
        )

    @staticmethod
    def _update_existing(existing: Job, incoming: Job) -> None:
        """Update mutable fields on an existing job record."""
        existing.last_checked_at = datetime.utcnow()
        # Refresh description in case it changed
        if incoming.description:
            existing.description = incoming.description
        if incoming.deadline:
            existing.deadline = incoming.deadline


class PipelineResult:
    """Summary of a pipeline run."""

    def __init__(self, started: Optional[datetime] = None) -> None:
        self.started = started or datetime.utcnow()
        self.finished_at: Optional[datetime] = None
        self.new_jobs: int = 0
        self.updated_jobs: int = 0
        self.duplicates: int = 0
        self.rejected: int = 0
        self.errors: list[str] = []

    def merge(self, other: "PipelineResult") -> None:
        """Merge another result into this one."""
        self.new_jobs += other.new_jobs
        self.updated_jobs += other.updated_jobs
        self.duplicates += other.duplicates
        self.rejected += other.rejected
        self.errors.extend(other.errors)

    @property
    def duration_seconds(self) -> float:
        if self.finished_at is None:
            return 0.0
        return (self.finished_at - self.started).total_seconds()

    def __repr__(self) -> str:
        return (
            f"<PipelineResult new={self.new_jobs} updated={self.updated_jobs} "
            f"dupes={self.duplicates} rejected={self.rejected} "
            f"errors={len(self.errors)}>"
        )
