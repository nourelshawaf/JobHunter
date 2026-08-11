"""
BMW Group Careers connector.

BMW uses an Oracle/Taleo-based ATS at bmwgroup.jobs (global) and
careers.bmwgroup.com. Job listings are publicly accessible.

Source method: BMW's public career search JSON API discovered by
inspecting network requests on bmwgroup.jobs. The endpoint accepts
keyword + location parameters and returns paginated JSON.

Compliance: public pages, no login required, respectful rate limiting.
Location filter: Hungary (Budapest / Debrecen) + remote.

Known limitations:
- The API endpoint may change without notice (no official documentation).
- If the API returns 4xx, we fall back to JSON-LD extraction from the
  HTML search results page.
- Only jobs explicitly listed for Hungary are returned.
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

CAREERS_BASE = "https://www.bmwgroup.jobs"
# BMW uses SmartRecruiters internally — public API
SEARCH_API = f"{CAREERS_BASE}/search"

# HTML search page fallback
SEARCH_PAGE = f"{CAREERS_BASE}/global/en/search-results.html"

# Hungary-specific BMW locations
HU_LOCATIONS = [
    "Budapest", "Debrecen", "Hungary", "Magyarország",
    "Remote", "Home Office",
]


class BMWCareersConnector(BaseConnector):
    """Fetches BMW Group internship and student postings for Hungary."""

    name = "bmw_careers"
    description = "BMW Group Careers — official career page (JSON API + JSON-LD fallback)"
    requires_browser = False

    async def _fetch_jobs(self) -> list[RawJob]:
        jobs: list[RawJob] = []
        seen_ids: set[str] = set()

        search_terms = [
            "intern", "internship", "working student", "student",
            "trainee", "praktikant", "gyakorlat",
        ]

        for term in search_terms:
            try:
                batch = await self._search(term, seen_ids)
                jobs.extend(batch)
                logger.info("bmw.search_done", term=term, found=len(batch))
            except Exception as exc:
                logger.warning("bmw.search_error", term=term, error=str(exc))

        return jobs

    async def _search(self, keyword: str, seen_ids: set[str]) -> list[RawJob]:
        """Try JSON API first, fall back to HTML+JSON-LD."""
        try:
            return await self._search_via_api(keyword, seen_ids)
        except Exception as exc:
            logger.debug("bmw.api_failed_html_fallback", error=str(exc))
            return await self._search_via_html(keyword, seen_ids)

    async def _search_via_api(self, keyword: str, seen_ids: set[str]) -> list[RawJob]:
        """
        BMW SmartRecruiters-based JSON search API.

        Observed endpoint: POST /search with JSON body.
        Falls through to HTML if status != 200.
        """
        payload = {
            "keyword": keyword,
            "location": {"country": "HU"},
            "jobTypes": ["INTERN", "PART_TIME"],
            "pageSize": 50,
            "pageNumber": 1,
        }
        response = await self._post(SEARCH_API, json=payload)
        if response.status_code != 200:
            raise RuntimeError(f"API returned {response.status_code}")

        data = response.json()
        return self._parse_api_response(data, seen_ids)

    async def _search_via_html(self, keyword: str, seen_ids: set[str]) -> list[RawJob]:
        """HTML fallback: extract JSON-LD from BMW search results page."""
        params = {
            "keywords": keyword,
            "location": "Hungary",
        }
        url = f"{SEARCH_PAGE}?{urlencode(params)}"
        response = await self._get(url)
        return self._extract_jsonld(response.text, seen_ids)

    def _parse_api_response(self, data: dict[str, Any], seen_ids: set[str]) -> list[RawJob]:
        """Parse BMW SmartRecruiters API response."""
        jobs: list[RawJob] = []
        items = data.get("jobs", data.get("content", data.get("results", [])))

        for item in items:
            try:
                job = self._parse_api_item(item)
                if job and job.source_job_id not in seen_ids:
                    if job.source_job_id:
                        seen_ids.add(job.source_job_id)
                    jobs.append(job)
            except Exception as exc:
                logger.debug("bmw.api_item_error", error=str(exc))

        return jobs

    def _parse_api_item(self, item: dict[str, Any]) -> Optional[RawJob]:
        job_id = str(item.get("id") or item.get("jobId") or item.get("refNumber", ""))
        title = item.get("name") or item.get("title") or item.get("jobTitle", "")
        if not title:
            return None

        # Location check
        location_obj = item.get("location") or {}
        city = location_obj.get("city") or location_obj.get("municipality") or ""
        country = location_obj.get("country") or location_obj.get("countryCode") or ""
        location = ", ".join(filter(None, [city, country]))

        if not self._is_hungarian_location(location):
            return None

        company = item.get("company") or "BMW Group"
        app_url = item.get("applyUrl") or item.get("url")
        if job_id and not app_url:
            app_url = f"{CAREERS_BASE}/job/{job_id}"

        posted_at = self._parse_date(item.get("publishedAt") or item.get("postedDate") or "")
        deadline = self._parse_date(item.get("applicationEndDate") or "")

        emp_type = item.get("type") or item.get("employmentType") or ""
        job_type_raw = self._classify_job_type(emp_type + " " + title)

        return RawJob(
            source=self.name,
            source_job_id=job_id or None,
            title=title,
            company=company,
            location=location,
            application_url=app_url,
            source_url=app_url,
            posted_at=posted_at,
            deadline=deadline,
            job_type_raw=job_type_raw,
        )

    def _extract_jsonld(self, html: str, seen_ids: set[str]) -> list[RawJob]:
        """Extract Schema.org JobPosting from HTML."""
        soup = BeautifulSoup(html, "lxml")
        jobs: list[RawJob] = []

        for script in soup.find_all("script", type="application/ld+json"):
            try:
                data = json.loads(script.string or "")
            except (json.JSONDecodeError, TypeError):
                continue

            items = data if isinstance(data, list) else [data]
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
                    logger.debug("bmw.jsonld_error", error=str(exc))

        return jobs

    def _parse_jsonld_item(self, item: dict[str, Any]) -> Optional[RawJob]:
        title = item.get("title") or item.get("name", "")
        if not title:
            return None

        org = item.get("hiringOrganization") or {}
        company = org.get("name", "BMW Group")

        loc = item.get("jobLocation") or {}
        addr = loc.get("address") or {}
        city = addr.get("addressLocality") or ""
        country = addr.get("addressCountry") or ""
        location = ", ".join(filter(None, [city, country]))

        if not self._is_hungarian_location(location):
            return None

        url = item.get("url") or item.get("sameAs")
        ident = item.get("identifier") or {}
        job_id = str(ident.get("value", "")) or re.search(r"/(\d+)/?$", url or "")
        if hasattr(job_id, "group"):
            job_id = job_id.group(1)

        raw_desc = item.get("description") or ""
        description = BeautifulSoup(raw_desc, "lxml").get_text(" ", strip=True)

        emp_type = item.get("employmentType") or ""
        job_type_raw = self._classify_job_type(
            emp_type if isinstance(emp_type, str) else " ".join(emp_type)
        )

        return RawJob(
            source=self.name,
            source_job_id=str(job_id) if job_id else None,
            title=title,
            company=company,
            location=location,
            description=description,
            application_url=url,
            source_url=url,
            posted_at=self._parse_date(item.get("datePosted", "")),
            deadline=self._parse_date(item.get("validThrough", "")),
            job_type_raw=job_type_raw,
        )

    @staticmethod
    def _is_hungarian_location(location: str) -> bool:
        if not location:
            return False
        return any(h.lower() in location.lower() for h in HU_LOCATIONS)

    @staticmethod
    def _classify_job_type(text: str) -> Optional[str]:
        t = text.lower()
        if any(k in t for k in ("intern", "internship", "praktika", "gyakorlat")):
            return "internship"
        if any(k in t for k in ("working student", "werkstudent")):
            return "working_student"
        if "trainee" in t:
            return "trainee"
        return None

    @staticmethod
    def _parse_date(raw: str) -> Optional[datetime]:
        if not raw:
            return None
        for fmt in ("%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
            try:
                return datetime.strptime(raw[:19], fmt)
            except ValueError:
                continue
        return None

    async def _is_healthy(self) -> bool:
        try:
            r = await self._get(CAREERS_BASE)
            return r.status_code < 400
        except Exception:
            return False
