"""
EURES connector.

EURES (European Employment Services) is the EU employment portal.

API updated August 2026: endpoint moved from
  eures.europa.eu/api/jv-search  (old, now 404)
to
  europa.eu/eures/api/jv-searchengine/public/jv-search/search  (current)

The new API uses a POST body with a structured JSON schema.
Reference: https://github.com/rorar/EURES-API-Documentation
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

import structlog

from jobhunter.connectors.base import BaseConnector, RawJob

logger = structlog.get_logger(__name__)

# Updated endpoint (as of 2026)
SEARCH_ENDPOINT = (
    "https://europa.eu/eures/api/jv-searchengine/public/jv-search/search"
)

HUNGARY_NUTS = "hu"   # lowercase country code in new API


class EURESConnector(BaseConnector):
    """Fetches engineering internship listings from the EURES EU jobs portal."""

    name = "eures"
    description = "EURES — European Employment Services public API (2026 endpoint)"
    requires_browser = False

    async def _fetch_jobs(self) -> list[RawJob]:
        jobs: list[RawJob] = []
        seen_ids: set[str] = set()

        keywords = [
            "mechatronics intern",
            "robotics intern",
            "automation intern",
            "embedded systems intern",
            "electrical engineering intern",
            "engineering student",
            "manufacturing intern",
        ]

        for kw in keywords:
            try:
                batch = await self._query(kw, seen_ids)
                jobs.extend(batch)
                logger.info("eures.query_done", keywords=kw, found=len(batch))
            except Exception as exc:
                logger.warning("eures.query_error", keywords=kw, error=str(exc))

        return jobs

    async def _query(self, keyword: str, seen_ids: set[str]) -> list[RawJob]:
        """POST to the EURES search API with the 2026 request schema."""
        payload = {
            "resultsPerPage": 50,
            "page": 1,
            "sortSearch": "MOST_RECENT",
            "keywords": [
                {"keyword": keyword, "specificSearchCode": "EVERYWHERE"}
            ],
            "publicationPeriod": None,
            "occupationUris": [],
            "skillUris": [],
            "requiredExperienceCodes": [],
            "positionScheduleCodes": [],
            "sectorCodes": [],
            "educationAndQualificationLevelCodes": [],
            "positionOfferingCodes": [],
            "locationCodes": [HUNGARY_NUTS],
            "euresFlagCodes": [],
            "otherBenefitsCodes": [],
            "requiredLanguages": [],
            "minNumberPost": None,
            "sessionId": "jobhunter-session",
            "requestLanguage": "en",
        }

        response = await self._post(
            SEARCH_ENDPOINT,
            json=payload,
            headers={"Content-Type": "application/json"},
        )

        if response.status_code != 200:
            raise RuntimeError(f"EURES API returned {response.status_code}")

        return self._parse(response.json(), seen_ids)

    def _parse(self, data: dict[str, Any], seen_ids: set[str]) -> list[RawJob]:
        jobs: list[RawJob] = []

        # New API response: {"jobVacancies": [...], "total": N}
        items = (
            data.get("jobVacancies", [])
            or data.get("data", {}).get("items", [])
            or data.get("items", [])
            or []
        )

        for item in items:
            try:
                job = self._parse_item(item)
                if job is None:
                    continue
                if job.source_job_id and job.source_job_id in seen_ids:
                    continue
                if job.source_job_id:
                    seen_ids.add(job.source_job_id)
                jobs.append(job)
            except Exception as exc:
                logger.debug("eures.item_error", error=str(exc))

        return jobs

    def _parse_item(self, item: dict[str, Any]) -> Optional[RawJob]:
        # ID — new API uses "id" or "handle"
        job_id = str(
            item.get("id")
            or item.get("handle")
            or item.get("jobVacancyId", "")
        )

        # Title
        title = (
            item.get("title")
            or item.get("positionTitle")
            or item.get("jobTitle", "")
        )
        if not title:
            return None

        # Employer
        employer = item.get("employer") or item.get("company") or {}
        company = (
            employer.get("name") if isinstance(employer, dict) else str(employer)
        ) or "Unknown"

        # Location — new API nests location differently
        location_data = (
            item.get("jobLocation")
            or item.get("location")
            or item.get("place")
            or {}
        )
        city = (
            location_data.get("city")
            or location_data.get("municipality")
            or location_data.get("name", "")
        ) if isinstance(location_data, dict) else ""
        country = (
            location_data.get("country")
            or location_data.get("countryCode", "")
        ) if isinstance(location_data, dict) else ""
        location = ", ".join(filter(None, [city, country])) or "Hungary"

        # URL
        application_url = (
            item.get("applicationUrl")
            or item.get("externalUrl")
            or item.get("url")
        )
        if job_id and not application_url:
            application_url = f"https://europa.eu/eures/portal/jv-se/jv/{job_id}"

        # Dates
        posted_at = self._parse_date(
            item.get("publicationStartDate")
            or item.get("postedAt")
            or item.get("startDate", "")
        )
        deadline = self._parse_date(
            item.get("publicationEndDate")
            or item.get("deadline")
            or item.get("endDate", "")
        )

        # Description
        description = item.get("description") or item.get("jobDescription")

        # Work mode
        work_mode_raw = None
        if item.get("remote") or item.get("remoteWork"):
            work_mode_raw = "remote"

        # Salary
        salary_info = item.get("salary") or item.get("remuneration") or {}
        salary_raw = None
        if salary_info and isinstance(salary_info, dict):
            low = salary_info.get("from") or salary_info.get("minimum")
            high = salary_info.get("to") or salary_info.get("maximum")
            currency = salary_info.get("currency", "EUR")
            if low or high:
                salary_raw = f"{low or '?'}–{high or '?'} {currency}"

        return RawJob(
            source=self.name,
            source_job_id=job_id or None,
            title=title,
            company=company,
            location=location,
            description=description,
            application_url=application_url,
            source_url=application_url,
            posted_at=posted_at,
            deadline=deadline,
            salary_raw=salary_raw,
            work_mode_raw=work_mode_raw,
        )

    @staticmethod
    def _parse_date(raw: Any) -> Optional[datetime]:
        if not raw or not isinstance(raw, str):
            return None
        for fmt in (
            "%Y-%m-%dT%H:%M:%S.%fZ",
            "%Y-%m-%dT%H:%M:%SZ",
            "%Y-%m-%dT%H:%M:%S",
            "%Y-%m-%d",
        ):
            try:
                return datetime.strptime(raw[:26], fmt)
            except ValueError:
                continue
        return None

    async def _is_healthy(self) -> bool:
        try:
            r = await self._get("https://europa.eu/eures")
            return r.status_code < 400
        except Exception:
            return False
