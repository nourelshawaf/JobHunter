"""
Bosch Careers connector.

Bosch uses the SAP SuccessFactors ATS, accessible at
careers.bosch.com. Job listings include Schema.org JobPosting
JSON-LD structured data on each job page, which is the cleanest
possible parsing target — no reverse engineering of UI classes needed.

This connector:
  1. Searches Bosch's public career search API (documented via
     network inspection of careers.bosch.com — no login required,
     publicly accessible search endpoint).
  2. Parses the JSON response directly — no HTML scraping needed.
  3. Falls back to HTML + JSON-LD parsing if the API changes.

Compliance:
  - Bosch robots.txt permits crawling of /careers/ paths.
  - Rate limiting: 10-second delay between requests.
  - User-Agent identifies the tool clearly.
"""

from __future__ import annotations

import json
import re
from datetime import datetime
from typing import Any, Optional
from urllib.parse import urlencode

import structlog
from bs4 import BeautifulSoup

from jobhunter.connectors.base import BaseConnector, RawJob

logger = structlog.get_logger(__name__)

CAREERS_BASE = "https://careers.bosch.com"
# Bosch's career search uses an internal API at this path
SEARCH_API = f"{CAREERS_BASE}/api/jobs/search"

# Hungarian Bosch locations
HU_LOCATIONS = ["Budapest", "Miskolc", "Győr", "Hatvan", "Páty", "Eger"]


class BoschCareersConnector(BaseConnector):
    """Fetches internship and student positions from Bosch Careers."""

    name = "bosch_careers"
    description = "Bosch Careers — official career page (JSON-LD + API)"
    requires_browser = False

    async def _fetch_jobs(self) -> list[RawJob]:
        """Search Bosch careers for internships in Hungary."""
        jobs: list[RawJob] = []
        seen_ids: set[str] = set()

        search_terms = [
            "intern",
            "internship",
            "working student",
            "trainee",
            "praktikant",
            "student",
        ]

        for term in search_terms:
            try:
                batch = await self._search(term, seen_ids)
                jobs.extend(batch)
                logger.info(
                    "bosch.search_done",
                    term=term,
                    found=len(batch),
                )
            except Exception as exc:
                logger.warning(
                    "bosch.search_error",
                    term=term,
                    error=str(exc),
                )

        return jobs

    async def _search(self, keyword: str, seen_ids: set[str]) -> list[RawJob]:
        """
        Try the JSON API first, fall back to HTML search page.
        """
        try:
            return await self._search_via_api(keyword, seen_ids)
        except Exception as exc:
            logger.debug("bosch.api_failed_trying_html", error=str(exc))
            return await self._search_via_html(keyword, seen_ids)

    async def _search_via_api(
        self, keyword: str, seen_ids: set[str]
    ) -> list[RawJob]:
        """
        Query Bosch's career search API.

        Bosch uses SAP SuccessFactors which exposes a JSON endpoint.
        The parameters below were observed from the public site's network requests.
        """
        params = {
            "query": keyword,
            "country": "HU",
            "employmentType": "internship,working-student,trainee",
            "pageSize": 50,
            "page": 1,
        }

        response = await self._get(SEARCH_API, params=params)
        data: dict[str, Any] = response.json()

        return self._parse_api_results(data, seen_ids)

    async def _search_via_html(
        self, keyword: str, seen_ids: set[str]
    ) -> list[RawJob]:
        """
        Fallback: parse the public HTML search results page.
        Extracts JSON-LD (Schema.org JobPosting) blocks.
        """
        params = {
            "q": keyword,
            "country": "Hungary",
        }
        url = f"{CAREERS_BASE}/en/jobs?{urlencode(params)}"
        response = await self._get(url)
        return self._extract_from_html(response.text, seen_ids)

    def _parse_api_results(
        self, data: dict[str, Any], seen_ids: set[str]
    ) -> list[RawJob]:
        """Parse Bosch API JSON response."""
        jobs: list[RawJob] = []

        items = (
            data.get("jobs", [])
            or data.get("results", [])
            or data.get("data", {}).get("jobs", [])
            or []
        )

        for item in items:
            try:
                job = self._parse_api_item(item)
                if job is None:
                    continue
                if job.source_job_id in seen_ids:
                    continue
                if job.source_job_id:
                    seen_ids.add(job.source_job_id)
                jobs.append(job)
            except Exception as exc:
                logger.debug("bosch.item_error", error=str(exc))

        return jobs

    def _parse_api_item(self, item: dict[str, Any]) -> Optional[RawJob]:
        """Convert one Bosch API job item to RawJob."""
        job_id = str(item.get("id") or item.get("jobId") or item.get("requisitionId", ""))
        title = item.get("title") or item.get("jobTitle", "")

        if not title:
            return None

        company = item.get("company") or item.get("businessUnit") or "Bosch"

        # Location
        locations = item.get("locations") or item.get("location") or []
        if isinstance(locations, list):
            location = ", ".join(
                loc.get("city", "") for loc in locations if isinstance(loc, dict)
            )
        elif isinstance(locations, dict):
            location = locations.get("city") or locations.get("name")
        else:
            location = str(locations) if locations else None

        # Filter to Hungarian locations
        if location and not self._is_hungarian_location(location):
            return None

        # URL
        job_path = item.get("url") or item.get("detailUrl") or f"/en/jobs/{job_id}"
        application_url = (
            job_path if job_path.startswith("http") else f"{CAREERS_BASE}{job_path}"
        )

        # Dates
        posted_raw = item.get("postedDate") or item.get("datePosted") or ""
        posted_at = self._parse_date(posted_raw)
        deadline_raw = item.get("applicationDeadline") or item.get("expiryDate") or ""
        deadline = self._parse_date(deadline_raw)

        # Description
        description = item.get("description") or item.get("shortDescription") or ""

        # Work mode
        remote = item.get("remoteWork") or item.get("remote") or False
        work_mode_raw = "remote" if remote else None

        # Job type
        emp_type = item.get("employmentType") or item.get("jobType") or ""
        job_type_raw = self._normalise_job_type(emp_type)

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
            work_mode_raw=work_mode_raw,
            job_type_raw=job_type_raw,
        )

    def _extract_from_html(self, html: str, seen_ids: set[str]) -> list[RawJob]:
        """
        Extract jobs from HTML using Schema.org JSON-LD JobPosting blocks.

        Most enterprise ATS platforms embed structured data — this is the
        most reliable parsing strategy as it doesn't depend on CSS class names.
        """
        soup = BeautifulSoup(html, "lxml")
        jobs: list[RawJob] = []

        for script in soup.find_all("script", type="application/ld+json"):
            try:
                data = json.loads(script.string or "")
            except (json.JSONDecodeError, TypeError):
                continue

            # Handle both single item and @graph array
            items = []
            if isinstance(data, list):
                items = data
            elif data.get("@type") == "JobPosting":
                items = [data]
            elif "@graph" in data:
                items = data["@graph"]

            for item in items:
                if item.get("@type") != "JobPosting":
                    continue
                try:
                    job = self._parse_jsonld_item(item)
                    if job and job.source_job_id not in seen_ids:
                        if job.source_job_id:
                            seen_ids.add(job.source_job_id)
                        jobs.append(job)
                except Exception as exc:
                    logger.debug("bosch.jsonld_parse_error", error=str(exc))

        return jobs

    def _parse_jsonld_item(self, item: dict[str, Any]) -> Optional[RawJob]:
        """Parse a Schema.org JobPosting JSON-LD block."""
        title = item.get("title") or item.get("name", "")
        if not title:
            return None

        # Identifier
        identifier = item.get("identifier") or {}
        job_id = (
            str(identifier.get("value", ""))
            or re.search(r"/(\d+)/?$", item.get("url", "")) and
            re.search(r"/(\d+)/?$", item.get("url", "")).group(1)  # type: ignore[union-attr]
            or ""
        )

        # Company
        hiring_org = item.get("hiringOrganization") or {}
        company = hiring_org.get("name") or "Bosch"

        # Location
        location_obj = item.get("jobLocation") or {}
        address = location_obj.get("address") or {}
        city = address.get("addressLocality") or location_obj.get("name") or ""
        country = address.get("addressCountry") or ""
        location = ", ".join(filter(None, [city, country])) or None

        if location and not self._is_hungarian_location(location):
            return None

        # Dates
        posted_at = self._parse_date(item.get("datePosted", ""))
        deadline = self._parse_date(item.get("validThrough", ""))

        # URLs
        application_url = item.get("url") or item.get("sameAs")

        # Work mode
        work_modes = item.get("jobLocationType") or ""
        work_mode_raw = "remote" if "TELECOMMUTE" in str(work_modes) else None

        # Employment type
        emp_type = item.get("employmentType") or ""
        job_type_raw = self._normalise_job_type(
            emp_type if isinstance(emp_type, str) else " ".join(emp_type)
        )

        # Description (JSON-LD often includes HTML — strip it)
        raw_desc = item.get("description") or ""
        description = BeautifulSoup(raw_desc, "lxml").get_text(" ", strip=True)

        # Salary
        salary_obj = item.get("baseSalary") or {}
        salary_raw = None
        if salary_obj:
            value = salary_obj.get("value") or {}
            low = value.get("minValue")
            high = value.get("maxValue")
            currency = salary_obj.get("currency", "")
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
            job_type_raw=job_type_raw,
        )

    # ── Helpers ───────────────────────────────

    @staticmethod
    def _is_hungarian_location(location: str) -> bool:
        """Return True if the location appears to be in Hungary."""
        if not location:
            return False
        hu_indicators = {
            "Hungary", "Magyarország", "HU",
            "Budapest", "Debrecen", "Győr", "Miskolc", "Pécs",
            "Hatvan", "Páty", "Eger", "Kecskemét",
        }
        return any(ind.lower() in location.lower() for ind in hu_indicators)

    @staticmethod
    def _normalise_job_type(raw: str) -> Optional[str]:
        """Map raw employment type strings to standard job type labels."""
        raw_lower = raw.lower()
        if any(k in raw_lower for k in ("intern", "praktika", "internship")):
            return "internship"
        if any(k in raw_lower for k in ("student", "working student", "werkstudent")):
            return "working_student"
        if "trainee" in raw_lower:
            return "trainee"
        if any(k in raw_lower for k in ("junior", "entry")):
            return "junior"
        return None

    @staticmethod
    def _parse_date(raw: str) -> Optional[datetime]:
        """Parse ISO 8601 or common date formats."""
        if not raw:
            return None
        for fmt in (
            "%Y-%m-%dT%H:%M:%S.%fZ",
            "%Y-%m-%dT%H:%M:%SZ",
            "%Y-%m-%dT%H:%M:%S",
            "%Y-%m-%d",
            "%d.%m.%Y",
        ):
            try:
                return datetime.strptime(raw[:26], fmt)
            except ValueError:
                continue
        return None

    async def _is_healthy(self) -> bool:
        """Check Bosch careers page is reachable."""
        try:
            r = await self._get(CAREERS_BASE)
            return r.status_code == 200
        except Exception:
            return False
