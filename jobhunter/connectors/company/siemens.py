"""
Siemens Careers connector.

Siemens uses the SAP SuccessFactors ATS at jobs.siemens.com.
The careers site exposes a public JSON search API (no auth required).

Source method: SAP SuccessFactors public job search API.
Fallback: JSON-LD from HTML search pages.

Compliance: public pages, no login required, 10-second rate limit.
Location filter: Hungary (Budapest / Debrecen / Pécs / Győr).

Known limitations:
- API response structure may change with SAP updates.
- Only roles explicitly posted for Hungary are returned.
"""
from __future__ import annotations

import json
import re
from datetime import datetime
from typing import Any, Optional

import structlog
from bs4 import BeautifulSoup

from jobhunter.connectors.base import BaseConnector, RawJob

logger = structlog.get_logger(__name__)

CAREERS_BASE = "https://jobs.siemens.com"
SEARCH_API = f"{CAREERS_BASE}/jobs/search"

HU_LOCATIONS = [
    "Budapest", "Debrecen", "Pécs", "Győr", "Temesvár",
    "Hungary", "Magyarország", "Remote",
]


class SiemensCareersConnector(BaseConnector):
    """Fetches Siemens internship postings for Hungary."""

    name = "siemens_careers"
    description = "Siemens Careers — SAP SuccessFactors public search"
    requires_browser = False

    async def _fetch_jobs(self) -> list[RawJob]:
        jobs: list[RawJob] = []
        seen_ids: set[str] = set()

        search_terms = [
            "intern", "internship", "working student", "student",
            "trainee", "praktikum", "mechatronics", "automation",
        ]

        for term in search_terms:
            try:
                batch = await self._search(term, seen_ids)
                jobs.extend(batch)
                logger.info("siemens.search_done", term=term, found=len(batch))
            except Exception as exc:
                logger.warning("siemens.search_error", term=term, error=str(exc))

        return jobs

    async def _search(self, keyword: str, seen_ids: set[str]) -> list[RawJob]:
        try:
            return await self._search_via_api(keyword, seen_ids)
        except Exception as exc:
            logger.debug("siemens.api_failed", error=str(exc))
            return await self._search_via_html(keyword, seen_ids)

    async def _search_via_api(self, keyword: str, seen_ids: set[str]) -> list[RawJob]:
        """
        SAP SuccessFactors job search API used by Siemens.

        Parameters derived from network inspection of jobs.siemens.com.
        """
        params = {
            "q": keyword,
            "country": "Hungary",
            "rows": 50,
            "start": 0,
        }
        response = await self._get(SEARCH_API, params=params)
        if response.status_code != 200:
            raise RuntimeError(f"API {response.status_code}")
        return self._parse_api_response(response.json(), seen_ids)

    async def _search_via_html(self, keyword: str, seen_ids: set[str]) -> list[RawJob]:
        """HTML fallback with JSON-LD."""
        from urllib.parse import urlencode
        params = {"q": keyword, "country": "Hungary"}
        response = await self._get(
            f"{CAREERS_BASE}/jobs?{urlencode(params)}"
        )
        return self._extract_jsonld(response.text, seen_ids)

    def _parse_api_response(self, data: dict[str, Any], seen_ids: set[str]) -> list[RawJob]:
        jobs: list[RawJob] = []
        items = (
            data.get("results", [])
            or data.get("jobs", [])
            or data.get("data", {}).get("jobs", [])
            or []
        )

        for item in items:
            try:
                job_id = str(
                    item.get("jobId") or item.get("id") or item.get("requisitionId", "")
                )
                if job_id in seen_ids:
                    continue

                title = item.get("title") or item.get("jobTitle", "")
                if not title:
                    continue

                location_parts = []
                for field in ("city", "country", "location"):
                    val = item.get(field) or ""
                    if val:
                        location_parts.append(val)
                location = ", ".join(location_parts) or None

                if location and not self._is_hungarian(location):
                    continue

                path = item.get("detailUrl") or item.get("url") or f"/jobs/{job_id}"
                app_url = (
                    f"{CAREERS_BASE}{path}" if path.startswith("/") else path
                ) if path else None

                if job_id:
                    seen_ids.add(job_id)

                jobs.append(RawJob(
                    source=self.name,
                    source_job_id=job_id or None,
                    title=title,
                    company="Siemens",
                    location=location,
                    application_url=app_url,
                    source_url=app_url,
                    posted_at=self._parse_date(
                        item.get("postedDate") or item.get("publishedAt") or ""
                    ),
                    job_type_raw=self._classify_type(title),
                    work_mode_raw="remote" if item.get("remote") else None,
                ))
            except Exception as exc:
                logger.debug("siemens.item_error", error=str(exc))

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
                try:
                    loc = item.get("jobLocation") or {}
                    addr = loc.get("address") or {}
                    location = ", ".join(filter(None, [
                        addr.get("addressLocality", ""),
                        addr.get("addressCountry", ""),
                    ]))
                    if location and not self._is_hungarian(location):
                        continue

                    title = item.get("title") or item.get("name", "")
                    if not title:
                        continue

                    url = item.get("url")
                    ident = item.get("identifier") or {}
                    job_id = str(ident.get("value", ""))

                    if job_id in seen_ids:
                        continue
                    if job_id:
                        seen_ids.add(job_id)

                    raw_desc = item.get("description", "")
                    description = BeautifulSoup(raw_desc, "lxml").get_text(" ", strip=True)

                    jobs.append(RawJob(
                        source=self.name,
                        source_job_id=job_id or None,
                        title=title,
                        company="Siemens",
                        location=location or None,
                        description=description,
                        application_url=url,
                        source_url=url,
                        posted_at=self._parse_date(item.get("datePosted", "")),
                        deadline=self._parse_date(item.get("validThrough", "")),
                        job_type_raw=self._classify_type(title),
                    ))
                except Exception as exc:
                    logger.debug("siemens.jsonld_item_error", error=str(exc))

        return jobs

    @staticmethod
    def _is_hungarian(location: str) -> bool:
        return any(h.lower() in location.lower() for h in HU_LOCATIONS)

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
