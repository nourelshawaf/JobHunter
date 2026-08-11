"""
Deduplication engine.

Detects duplicate job postings across sources using a two-stage approach:

Stage 1 — Fingerprint: fast exact match on a normalised hash of
           (company_normalized, title_normalized, location_normalized).
           O(1) lookup, handles identical cross-postings instantly.

Stage 2 — Fuzzy: RapidFuzz similarity on title + company for near-duplicates
           (e.g. "Software Engineering Intern" vs "Intern - Software Engineering").
           Only runs when Stage 1 finds no match.

When duplicates are found, the official company career page listing
is preferred as the primary (is_primary_listing=True).
"""

from __future__ import annotations

import hashlib
import re
import uuid
from typing import Optional

import structlog
from rapidfuzz import fuzz
from sqlalchemy.orm import Session

from jobhunter.models.job import Job

logger = structlog.get_logger(__name__)

# Similarity threshold for fuzzy dedup (0–100)
FUZZY_TITLE_THRESHOLD = 85
FUZZY_COMPANY_THRESHOLD = 80

# Sources ranked by trustworthiness (higher = preferred as primary)
SOURCE_PRIORITY: dict[str, int] = {
    "bosch_careers": 100,
    "baker_hughes_careers": 100,
    "bmw_careers": 100,
    "siemens_careers": 100,
    "eures": 60,
    "profession_hu": 50,
    "jobline_hu": 50,
    "email_alerts": 40,
}


def make_fingerprint(job: Job) -> str:
    """
    Create a normalised fingerprint for fast duplicate detection.

    Two jobs with the same fingerprint are considered identical.
    """
    parts = [
        _normalise_for_hash(job.company_normalized or job.company),
        _normalise_for_hash(job.title),
        _normalise_for_hash(job.location or ""),
    ]
    key = "|".join(parts)
    return hashlib.sha256(key.encode()).hexdigest()[:32]


def _normalise_for_hash(text: str) -> str:
    """Aggressive normalisation for fingerprinting."""
    if not text:
        return ""
    text = text.lower().strip()
    # Remove punctuation and excess whitespace
    text = re.sub(r"[^\w\s]", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    # Remove common filler words
    stopwords = {"the", "a", "an", "for", "of", "in", "at", "and", "or"}
    words = [w for w in text.split() if w not in stopwords]
    return " ".join(words)


class DeduplicationEngine:
    """
    Checks incoming jobs against existing database records and marks duplicates.
    """

    def __init__(self, db: Session) -> None:
        self.db = db
        # In-memory fingerprint cache for the current batch
        self._fingerprint_cache: dict[str, str] = {}  # fingerprint → group_id

    def load_existing_fingerprints(self) -> None:
        """
        Load all fingerprints from the database into memory.

        Call this once before processing a batch for efficiency.
        """
        rows = (
            self.db.query(Job.fingerprint, Job.duplicate_group_id)
            .filter(Job.fingerprint.isnot(None))
            .all()
        )
        for fingerprint, group_id in rows:
            if fingerprint and group_id:
                self._fingerprint_cache[fingerprint] = group_id

        logger.info(
            "dedup.cache_loaded",
            fingerprints=len(self._fingerprint_cache),
        )

    def process(self, job: Job) -> Job:
        """
        Check a job for duplicates and set deduplication fields.

        Mutates and returns the job — does not commit to the database.
        """
        fingerprint = make_fingerprint(job)
        job.fingerprint = fingerprint

        # Stage 1: exact fingerprint match
        if fingerprint in self._fingerprint_cache:
            group_id = self._fingerprint_cache[fingerprint]
            job.duplicate_group_id = group_id
            job.is_primary_listing = self._should_be_primary(job, group_id)

            logger.debug(
                "dedup.exact_match",
                job_title=job.title,
                company=job.company,
                group_id=group_id,
            )
            return job

        # Stage 2: fuzzy match against recent jobs
        fuzzy_match = self._fuzzy_match(job)
        if fuzzy_match:
            group_id = fuzzy_match.duplicate_group_id or str(uuid.uuid4())
            if not fuzzy_match.duplicate_group_id:
                fuzzy_match.duplicate_group_id = group_id

            job.duplicate_group_id = group_id
            job.is_primary_listing = self._should_be_primary(job, group_id)
            self._fingerprint_cache[fingerprint] = group_id

            logger.debug(
                "dedup.fuzzy_match",
                job_title=job.title,
                matched_title=fuzzy_match.title,
                group_id=group_id,
            )
            return job

        # No duplicate found — this job is a new primary listing
        group_id = str(uuid.uuid4())
        job.duplicate_group_id = group_id
        job.is_primary_listing = True
        self._fingerprint_cache[fingerprint] = group_id

        return job

    def _fuzzy_match(self, incoming: Job) -> Optional[Job]:
        """
        Find an existing job that is likely a duplicate of the incoming one.

        Only queries the last 90 days to keep it fast.
        """
        from datetime import timedelta
        cutoff = __import__("datetime").datetime.utcnow() - timedelta(days=90)

        # Load candidate jobs from same approximate company
        company_key = _normalise_for_hash(incoming.company_normalized or incoming.company)

        candidates = (
            self.db.query(Job)
            .filter(
                Job.discovered_at >= cutoff,
                Job.is_primary_listing.is_(True),
            )
            .all()
        )

        for candidate in candidates:
            cand_company = _normalise_for_hash(
                candidate.company_normalized or candidate.company
            )

            # Quick company similarity check
            company_score = fuzz.token_sort_ratio(company_key, cand_company)
            if company_score < FUZZY_COMPANY_THRESHOLD:
                continue

            # Title similarity
            title_score = fuzz.token_sort_ratio(
                _normalise_for_hash(incoming.title),
                _normalise_for_hash(candidate.title),
            )
            if title_score >= FUZZY_TITLE_THRESHOLD:
                return candidate

        return None

    def _should_be_primary(self, incoming: Job, group_id: str) -> bool:
        """
        Decide if the incoming job should be the primary listing in its group.

        Prefers official company career pages over aggregators.
        """
        incoming_priority = SOURCE_PRIORITY.get(incoming.source, 0)

        existing_primary = (
            self.db.query(Job)
            .filter(
                Job.duplicate_group_id == group_id,
                Job.is_primary_listing.is_(True),
            )
            .first()
        )

        if not existing_primary:
            return True

        existing_priority = SOURCE_PRIORITY.get(existing_primary.source, 0)

        if incoming_priority > existing_priority:
            # Demote the existing primary
            existing_primary.is_primary_listing = False
            return True

        return False
