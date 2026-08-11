"""
Job normaliser.

Converts RawJob (connector output) into a normalised Job ORM instance.
Handles field mapping, work-mode detection, job-type classification,
company name normalisation, and URL canonicalisation.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Optional
from urllib.parse import urlparse, urlunparse

import structlog

from jobhunter.connectors.base import RawJob
from jobhunter.models.job import Job, JobStatus, JobType, WorkMode

logger = structlog.get_logger(__name__)

# Keywords that suggest a role is student/intern-friendly
STUDENT_FRIENDLY_SIGNALS = [
    "intern", "internship", "working student", "werkstudent",
    "student assistant", "junior", "trainee", "praktikant",
    "practice", "placement", "co-op", "bsc student", "msc student",
    "graduate", "entry level", "entry-level", "no experience",
    "fresh graduate", "university student", "current student",
    "gyakorlat", "gyakornok", "hallgató",
]

# Keywords that suggest fluent Hungarian is mandatory
HU_MANDATORY_SIGNALS = [
    "fluent hungarian", "native hungarian", "hungarian native",
    "anyanyelvi szintű magyar", "folyékony magyar",
    "b2 hungarian required", "c1 hungarian",
    "hungarian is mandatory", "hungarian required",
    "hungarian language required",
    "követelmény: magyar",
]

# Remote / hybrid signals
REMOTE_SIGNALS = ["remote", "home office", "work from home", "otthoni munka", "fully remote"]
HYBRID_SIGNALS = ["hybrid", "hibrid", "mixed", "flexible location"]


class Normaliser:
    """Converts RawJob instances to Job ORM objects."""

    def normalise(self, raw: RawJob) -> Job:
        """
        Map a RawJob to a Job ORM model instance.

        Does not commit to the database — caller is responsible for the session.
        """
        job = Job()

        # ── Source ────────────────────────────
        job.source = raw.source
        job.source_job_id = raw.source_job_id

        # ── Core ──────────────────────────────
        job.title = self._clean_text(raw.title)
        job.company = self._clean_text(raw.company)
        job.company_normalized = self._normalise_company(raw.company)
        job.location = self._clean_text(raw.location or "")

        # ── Classification ────────────────────
        combined_text = " ".join(filter(None, [
            raw.title, raw.description, raw.requirements,
            raw.job_type_raw, raw.language_requirements,
        ])).lower()

        job.work_mode = self._detect_work_mode(raw.work_mode_raw, combined_text)
        job.job_type = self._detect_job_type(raw.job_type_raw, combined_text)
        job.student_friendly = self._is_student_friendly(combined_text)
        job.hungarian_mandatory = self._is_hungarian_mandatory(combined_text)

        # ── Content ───────────────────────────
        job.description = raw.description
        job.requirements = raw.requirements
        job.language_requirements = raw.language_requirements

        # ── Dates ─────────────────────────────
        job.posted_at = raw.posted_at
        job.deadline = raw.deadline
        job.discovered_at = datetime.utcnow()

        # ── URLs ──────────────────────────────
        job.application_url = raw.application_url
        job.source_url = raw.source_url
        job.canonical_url = self._canonicalise_url(
            raw.application_url or raw.source_url or ""
        )

        # ── Salary ────────────────────────────
        job.salary_raw = raw.salary_raw
        if raw.salary_raw:
            min_s, max_s, currency = self._parse_salary(raw.salary_raw)
            job.salary_min = min_s
            job.salary_max = max_s
            job.salary_currency = currency

        # ── Status ────────────────────────────
        job.status = JobStatus.DISCOVERED

        return job

    # ── Detection helpers ─────────────────────

    @staticmethod
    def _detect_work_mode(raw: Optional[str], text: str) -> str:
        raw_lower = (raw or "").lower()
        if any(s in raw_lower for s in REMOTE_SIGNALS) or any(s in text for s in REMOTE_SIGNALS):
            return WorkMode.REMOTE
        if any(s in raw_lower for s in HYBRID_SIGNALS) or any(s in text for s in HYBRID_SIGNALS):
            return WorkMode.HYBRID
        if "onsite" in raw_lower or "on-site" in raw_lower or "on site" in raw_lower:
            return WorkMode.ONSITE
        return WorkMode.UNKNOWN

    @staticmethod
    def _detect_job_type(raw: Optional[str], text: str) -> str:
        raw_lower = (raw or "").lower()
        combined = raw_lower + " " + text

        if any(k in combined for k in ("internship", "intern ", "gyakorlat", "praktikant")):
            return JobType.INTERNSHIP
        if any(k in combined for k in ("working student", "werkstudent", "student worker")):
            return JobType.WORKING_STUDENT
        if "trainee" in combined:
            return JobType.TRAINEE
        if any(k in combined for k in ("junior", "entry level", "entry-level")):
            return JobType.JUNIOR
        if any(k in combined for k in ("graduate programme", "graduate program")):
            return JobType.GRADUATE
        return JobType.UNKNOWN

    @staticmethod
    def _is_student_friendly(text: str) -> bool:
        return any(signal in text for signal in STUDENT_FRIENDLY_SIGNALS)

    @staticmethod
    def _is_hungarian_mandatory(text: str) -> bool:
        return any(signal in text for signal in HU_MANDATORY_SIGNALS)

    # ── Text cleaning ─────────────────────────

    @staticmethod
    def _clean_text(text: str) -> str:
        """Strip excess whitespace and control characters."""
        if not text:
            return ""
        text = re.sub(r"\s+", " ", text)
        return text.strip()

    @staticmethod
    def _normalise_company(name: str) -> str:
        """
        Produce a canonical company name for deduplication.

        "Bosch Group", "Robert Bosch GmbH", "Bosch Kft." → "Bosch"
        """
        if not name:
            return ""
        # Strip legal suffixes
        name = re.sub(
            r"\b(GmbH|Kft\.?|Zrt\.?|Bt\.?|Ltd\.?|Inc\.?|Corp\.?|Group|SE|AG|plc|"
            r"LLC|BV|NV|SRL|SAS|SA|AB|Oy|AS|A/S)\b",
            "",
            name,
            flags=re.IGNORECASE,
        )
        # Strip "Robert" prefix from Bosch
        name = re.sub(r"^Robert\s+", "", name, flags=re.IGNORECASE)
        return re.sub(r"\s+", " ", name).strip()

    @staticmethod
    def _canonicalise_url(url: str) -> str:
        """Strip tracking parameters and normalise URL."""
        if not url:
            return ""
        try:
            parsed = urlparse(url)
            # Remove common tracking params
            from urllib.parse import parse_qs, urlencode
            params = parse_qs(parsed.query)
            clean_params = {
                k: v for k, v in params.items()
                if k.lower() not in (
                    "utm_source", "utm_medium", "utm_campaign", "utm_content",
                    "utm_term", "ref", "referral", "source", "sid",
                )
            }
            clean_query = urlencode(clean_params, doseq=True)
            return urlunparse(parsed._replace(query=clean_query, fragment=""))
        except Exception:
            return url

    @staticmethod
    def _parse_salary(
        raw: str,
    ) -> tuple[Optional[float], Optional[float], Optional[str]]:
        """
        Extract min, max, currency from a raw salary string.

        Examples:
          "435,000 HUF/month" → (435000, None, "HUF")
          "400,000–500,000 HUF" → (400000, 500000, "HUF")
          "€1,500–€2,000 per month" → (1500, 2000, "EUR")
        """
        if not raw:
            return None, None, None

        # Detect currency
        currency = None
        if "HUF" in raw or "Ft" in raw:
            currency = "HUF"
        elif "EUR" in raw or "€" in raw:
            currency = "EUR"
        elif "USD" in raw or "$" in raw:
            currency = "USD"
        elif "GBP" in raw or "£" in raw:
            currency = "GBP"

        # Extract numbers
        numbers = re.findall(r"[\d,. ]+", raw)
        parsed: list[float] = []
        for n in numbers:
            n_clean = n.replace(",", "").replace(" ", "").strip()
            try:
                val = float(n_clean)
                if val > 100:  # ignore percentages or tiny values
                    parsed.append(val)
            except ValueError:
                continue

        if len(parsed) == 0:
            return None, None, currency
        elif len(parsed) == 1:
            return parsed[0], None, currency
        else:
            return min(parsed), max(parsed), currency
