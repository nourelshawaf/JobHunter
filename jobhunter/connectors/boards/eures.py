"""
EURES connector.

EURES (European Employment Services) is the EU employment portal.
It provides a public JSON API that does not require authentication
for job searches — fully compliant, no scraping needed.

API reference: https://eures.europa.eu/api-documentation
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

import structlog

from jobhunter.connectors.base import BaseConnector, RawJob

logger = structlog.get_logger(__name__)

# EURES public search API
API_BASE = "https://eures.europa.eu/api"
SEARCH_ENDPOINT = f"{API_BASE}/jv-search"

# ISCO occupation codes relevant to mechatronics/engineering interns
ENGINEERING_ISCO_CODES = [
    "2144",  # Mechanical engineers
    "2151",  # Electrical engineers
    "2152",  # Electronics engineers
    "2166",  # Graphic and multimedia designers (for computer vision)
    "2153",  # Telecommunications engineers
    "2143",  # Environmental engineers
    "7412",  # Electrical mechanics and fitters
]

# Hungarian NUTS code
HUNGARY_NUTS = "HU"
BUDAPEST_NUTS = "HU110"
DEBRECEN_NUTS = "HU321"


class EURESConnector(BaseConnector):
    """Fetches engineering internship listings from the EURES EU jobs portal."""

    name = "eures"
    description = "EURES — European Employment Services public API"
    requires_browser = False

    async def _fetch_jobs(self) -> list[RawJob]:
        """Query EURES API for engineering roles in Hungary."""
        jobs: list[RawJob] = []
        seen_ids: set[str] = set()

        search_queries = [
            {"keywords": "mechatronics intern", "country": HUNGARY_NUTS},
            {"keywords": "robotics intern", "country": HUNGARY_NUTS},
            {"keywords": "automation intern", "country": HUNGARY_NUTS},
            {"keywords": "embedded systems intern", "country": HUNGARY_NUTS},
            {"keywords": "engineering student", "country": HUNGARY_NUTS},
            {"keywords": "electrical engineering intern", "country": HUNGARY_NUTS},
            {"keywords": "manufacturing intern Hungary", "country": HUNGARY_NUTS},
        ]

        for query in search_queries:
            try:
                batch = await self._query_api(
                    keywords=query["keywords"],
                    country=query["country"],
                    seen_ids=seen_ids,
                )
                jobs.extend(batch)
                logger.info(
                    "eures.query_done",
                    keywords=query["keywords"],
                    found=len(batch),
                )
            except Exception as exc:
                logger.warning(
                    "eures.query_error",
                    keywords=query["keywords"],
                    error=str(exc),
                )

        return jobs

    async def _query_api(
        self,
        keywords: str,
        country: str,
        seen_ids: set[str],
        page_size: int = 50,
    ) -> list[RawJob]:
        """
        Call the EURES search API.

        The EURES API uses a POST request with a JSON body.
        Documented at: https://eures.europa.eu/api-documentation
        """
        payload = {
            "keywords": keywords,
            "countries": [country],
            "pageNumber": 0,
            "pageSize": page_size,
            "sortBy": "BEST_MATCH",
            "positionSchedule": [],  # no filter — includes part-time
        }

        try:
            response = await self._post(
                SEARCH_ENDPOINT,
                json=payload,
                headers={"Content-Type": "application/json"},
            )
            data = response.json()
        except Exception as exc:
            # Try alternate endpoint format
            logger.debug("eures.api_fallback", error=str(exc))
            return await self._query_api_v2(keywords, country, seen_ids, page_size)

        return self._parse_api_response(data, seen_ids)

    async def _query_api_v2(
        self,
        keywords: str,
        country: str,
        seen_ids: set[str],
        page_size: int = 50,
    ) -> list[RawJob]:
        """Fallback: GET-based EURES API variant."""
        params = {
            "keywords": keywords,
            "countries": country,
            "resultsPerPage": page_size,
            "page": 1,
        }
        response = await self._get(SEARCH_ENDPOINT, params=params)
        data = response.json()
        return self._parse_api_response(data, seen_ids)

    def _parse_api_response(
        self, data: dict[str, Any], seen_ids: set[str]
    ) -> list[RawJob]:
        """Parse EURES API JSON response into RawJob list."""
        jobs: list[RawJob] = []

        # EURES response structure: {"data": {"items": [...], "totalElements": N}}
        items = (
            data.get("data", {}).get("items", [])
            or data.get("jobVacancies", [])
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
                logger.debug("eures.item_parse_error", error=str(exc))

        return jobs

    def _parse_item(self, item: dict[str, Any]) -> Optional[RawJob]:
        """Convert one EURES API item to a RawJob."""
        # Extract ID
        job_id = str(
            item.get("id")
            or item.get("jobVacancyId")
            or item.get("handle", "")
        )

        # Title
        title = (
            item.get("title")
            or item.get("positionTitle")
            or item.get("jobTitle", "")
        )
        if not title:
            return None

        # Company / employer
        employer = item.get("employer", {})
        company = (
            employer.get("name")
            or employer.get("companyName")
            or item.get("companyName", "Unknown")
        )

        # Location
        location_data = item.get("location", {}) or item.get("jobLocation", {})
        location_parts = []
        if city := location_data.get("city") or location_data.get("municipality"):
            location_parts.append(city)
        if country := location_data.get("country") or location_data.get("countryCode"):
            location_parts.append(country)
        location = ", ".join(location_parts) if location_parts else None

        # Description
        description = item.get("description") or item.get("jobDescription")

        # URL
        application_url = (
            item.get("applicationUrl")
            or item.get("externalUrl")
            or item.get("url")
        )
        if job_id and not application_url:
            application_url = f"https://eures.europa.eu/job-details/{job_id}"

        # Dates
        posted_at = self._parse_iso_date(item.get("publicationStartDate") or item.get("postedAt"))
        deadline = self._parse_iso_date(item.get("publicationEndDate") or item.get("deadline"))

        # Work mode
        work_mode_raw = None
        if item.get("remote") or item.get("remoteWork"):
            work_mode_raw = "remote"
        elif item.get("hybrid"):
            work_mode_raw = "hybrid"

        # Salary
        salary_info = item.get("salary") or item.get("remuneration") or {}
        salary_raw = None
        if salary_info:
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
            extra={"eures_raw": item},
        )

    @staticmethod
    def _parse_iso_date(raw: Any) -> Optional[datetime]:
        """Parse an ISO 8601 date string from the API."""
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
        """Ping the EURES API."""
        try:
            response = await self._get("https://eures.europa.eu")
            return response.status_code < 400
        except Exception:
            return False
