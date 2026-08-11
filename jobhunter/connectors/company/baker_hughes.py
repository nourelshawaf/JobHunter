"""
Baker Hughes Careers connector.

Baker Hughes uses the Workday ATS at careers.bakerhughes.com.
Public job listings are accessible without login; the search API
is discoverable via network inspection of the careers site.

Source method: Workday public job search JSON API.
Fallback: JSON-LD extraction from HTML search results.

Compliance: public pages, no login required, respectful rate limiting.
Location filter: Hungary (Budapest / Fót / East Gate Business Park).

Known limitations:
- Workday API endpoints are not officially documented for third-party use.
- If API structure changes, HTML+JSON-LD fallback activates automatically.
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

CAREERS_BASE = "https://careers.bakerhughes.com"
# Workday job search API pattern used by Baker Hughes
SEARCH_API = f"{CAREERS_BASE}/api/apply/v2/jobs"
SEARCH_PAGE = f"{CAREERS_BASE}/jobs"

HU_LOCATIONS = ["Budapest", "Fót", "Hungary", "East Gate", "Remote"]


class BakerHughesCareersConnector(BaseConnector):
    """Fetches Baker Hughes internship postings for Hungary."""

    name = "baker_hughes_careers"
    description = "Baker Hughes Careers — Workday ATS public search"
    requires_browser = False

    async def _fetch_jobs(self) -> list[RawJob]:
        jobs: list[RawJob] = []
        seen_ids: set[str] = set()

        search_terms = [
            "intern", "internship", "student", "graduate", "trainee",
            "electrical engineering intern", "mechanical engineering intern",
        ]

        for term in search_terms:
            try:
                batch = await self._search(term, seen_ids)
                jobs.extend(batch)
                logger.info("baker_hughes.search_done", term=term, found=len(batch))
            except Exception as exc:
                logger.warning("baker_hughes.search_error", term=term, error=str(exc))

        return jobs

    async def _search(self, keyword: str, seen_ids: set[str]) -> list[RawJob]:
        try:
            return await self._search_via_api(keyword, seen_ids)
        except Exception as exc:
            logger.debug("baker_hughes.api_failed", error=str(exc))
            return await self._search_via_html(keyword, seen_ids)

    async def _search_via_api(self, keyword: str, seen_ids: set[str]) -> list[RawJob]:
        """
        Workday public job search API.

        Baker Hughes Workday tenant ID is observed from the careers URL.
        Endpoint: GET /api/apply/v2/jobs?q=<keyword>&locations=<country_id>&limit=50
        """
        params = {
            "q": keyword,
            "locations": "a30a87ed25634629aa6c3958aa2b91ea",  # Hungary location ID
            "limit": 50,
            "offset": 0,
        }
        response = await self._get(SEARCH_API, params=params)
        if response.status_code != 200:
            raise RuntimeError(f"API {response.status_code}")
        return self._parse_workday_response(response.json(), seen_ids)

    async def _search_via_html(self, keyword: str, seen_ids: set[str]) -> list[RawJob]:
        """HTML fallback with JSON-LD extraction."""
        params = {"q": keyword, "location": "Hungary"}
        response = await self._get(SEARCH_PAGE, params=params)
        return self._extract_jsonld(response.text, seen_ids)

    def _parse_workday_response(self, data: dict[str, Any], seen_ids: set[str]) -> list[RawJob]:
        """Parse Workday job search API response."""
        jobs: list[RawJob] = []
        job_postings = data.get("jobPostings", data.get("jobs", []))

        for item in job_postings:
            try:
                job_id = str(item.get("externalId") or item.get("id") or "")
                if job_id in seen_ids:
                    continue

                title = item.get("title") or item.get("name", "")
                if not title:
                    continue

                location_parts = []
                for loc in item.get("locationsText", "").split(","):
                    loc = loc.strip()
                    if loc:
                        location_parts.append(loc)
                location = ", ".join(location_parts) or None

                if location and not any(h.lower() in location.lower() for h in HU_LOCATIONS):
                    continue

                path = item.get("externalPath") or item.get("url") or f"/jobs/{job_id}"
                app_url = f"{CAREERS_BASE}{path}" if path.startswith("/") else path

                posted_raw = item.get("postedOn") or item.get("startDate") or ""
                posted_at = self._parse_date(posted_raw)

                if job_id:
                    seen_ids.add(job_id)

                jobs.append(RawJob(
                    source=self.name,
                    source_job_id=job_id or None,
                    title=title,
                    company="Baker Hughes",
                    location=location,
                    application_url=app_url,
                    source_url=app_url,
                    posted_at=posted_at,
                    job_type_raw=self._classify_type(title),
                    extra={"workday_raw": item},
                ))
            except Exception as exc:
                logger.debug("baker_hughes.item_error", error=str(exc))

        return jobs

    def _extract_jsonld(self, html: str, seen_ids: set[str]) -> list[RawJob]:
        soup = BeautifulSoup(html, "lxml")
        jobs: list[RawJob] = []

        for script in soup.find_all("script", type="application/ld+json"):
            try:
                data = json.loads(script.string or "")
            except Exception:
                continue
            for item in (data if isinstance(data, list) else [data]):
                if item.get("@type") != "JobPosting":
                    continue
                job = self._parse_jsonld(item)
                if job and job.source_job_id not in seen_ids:
                    if job.source_job_id:
                        seen_ids.add(job.source_job_id)
                    jobs.append(job)
        return jobs

    def _parse_jsonld(self, item: dict[str, Any]) -> Optional[RawJob]:
        title = item.get("title") or item.get("name", "")
        if not title:
            return None

        loc = item.get("jobLocation") or {}
        addr = loc.get("address") or {}
        location = ", ".join(filter(None, [
            addr.get("addressLocality", ""),
            addr.get("addressCountry", ""),
        ])) or None

        if location and not any(h.lower() in location.lower() for h in HU_LOCATIONS):
            return None

        url = item.get("url")
        ident = item.get("identifier") or {}
        job_id = str(ident.get("value", ""))

        return RawJob(
            source=self.name,
            source_job_id=job_id or None,
            title=title,
            company="Baker Hughes",
            location=location,
            description=BeautifulSoup(item.get("description", ""), "lxml").get_text(" ", strip=True),
            application_url=url,
            source_url=url,
            posted_at=self._parse_date(item.get("datePosted", "")),
            deadline=self._parse_date(item.get("validThrough", "")),
            job_type_raw=self._classify_type(title),
        )

    @staticmethod
    def _classify_type(text: str) -> Optional[str]:
        t = text.lower()
        if any(k in t for k in ("intern", "internship")):
            return "internship"
        if "trainee" in t:
            return "trainee"
        if "student" in t:
            return "working_student"
        return None

    @staticmethod
    def _parse_date(raw: str) -> Optional[datetime]:
        if not raw:
            return None
        raw = raw.strip()[:19]
        for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
            try:
                return datetime.strptime(raw, fmt)
            except ValueError:
                continue
        return None

    async def _is_healthy(self) -> bool:
        try:
            r = await self._get(CAREERS_BASE)
            return r.status_code < 400
        except Exception:
            return False
