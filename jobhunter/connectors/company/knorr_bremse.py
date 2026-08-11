"""
Knorr-Bremse Careers connector.

Knorr-Bremse uses SAP SuccessFactors (same ATS as Siemens).
Public search accessible via the careers page at jobs.knorr-bremse.com.

Source: SAP SuccessFactors public API + JSON-LD HTML fallback.
Hungary locations: Budapest (HQ for the KB Systems Hungary operation).
"""
from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Optional
from urllib.parse import urlencode

import structlog
from bs4 import BeautifulSoup

from jobhunter.connectors.base import BaseConnector, RawJob

logger = structlog.get_logger(__name__)

CAREERS_BASE = "https://jobs.knorr-bremse.com"
SEARCH_API = f"{CAREERS_BASE}/jobs/search"
HU_LOCATIONS = ["Budapest", "Hungary", "Magyarország", "Remote", "Hybrid"]


class KnorrBremseCareersConnector(BaseConnector):
    """Fetches Knorr-Bremse internship postings for Hungary."""

    name = "knorr_bremse_careers"
    description = "Knorr-Bremse Careers — SAP SuccessFactors + JSON-LD"
    requires_browser = False

    async def _fetch_jobs(self) -> list[RawJob]:
        jobs: list[RawJob] = []
        seen: set[str] = set()
        for term in ["intern", "internship", "working student", "trainee", "student"]:
            try:
                batch = await self._search(term, seen)
                jobs.extend(batch)
                logger.info("knorr_bremse.done", term=term, found=len(batch))
            except Exception as exc:
                logger.warning("knorr_bremse.error", term=term, error=str(exc))
        return jobs

    async def _search(self, keyword: str, seen: set[str]) -> list[RawJob]:
        try:
            return await self._search_api(keyword, seen)
        except Exception as exc:
            logger.debug("knorr_bremse.api_fallback", error=str(exc))
            return await self._search_html(keyword, seen)

    async def _search_api(self, keyword: str, seen: set[str]) -> list[RawJob]:
        params = {"query": keyword, "country": "Hungary", "rows": 50}
        response = await self._get(SEARCH_API, params=params)
        if response.status_code != 200:
            raise RuntimeError(f"API {response.status_code}")
        return self._parse_response(response.json(), seen)

    async def _search_html(self, keyword: str, seen: set[str]) -> list[RawJob]:
        url = f"{CAREERS_BASE}/jobs?{urlencode({'q': keyword, 'location': 'Hungary'})}"
        response = await self._get(url)
        return self._extract_jsonld(response.text, seen)

    def _parse_response(self, data: dict[str, Any], seen: set[str]) -> list[RawJob]:
        jobs: list[RawJob] = []
        for item in data.get("results", data.get("jobs", [])):
            try:
                job_id = str(item.get("jobId") or item.get("id") or "")
                if job_id in seen:
                    continue
                title = item.get("title") or item.get("jobTitle", "")
                if not title:
                    continue
                location = ", ".join(filter(None, [
                    item.get("city", ""), item.get("country", "")
                ])) or None
                if location and not any(h.lower() in location.lower() for h in HU_LOCATIONS):
                    continue
                path = item.get("detailUrl") or item.get("url") or f"/jobs/{job_id}"
                app_url = f"{CAREERS_BASE}{path}" if path.startswith("/") else path
                if job_id:
                    seen.add(job_id)
                jobs.append(RawJob(
                    source=self.name, source_job_id=job_id or None,
                    title=title, company="Knorr-Bremse", location=location,
                    application_url=app_url, source_url=app_url,
                    posted_at=self._parse_date(item.get("postedDate") or ""),
                    job_type_raw=self._classify(title),
                ))
            except Exception as exc:
                logger.debug("knorr_bremse.item_error", error=str(exc))
        return jobs

    def _extract_jsonld(self, html: str, seen: set[str]) -> list[RawJob]:
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
                loc = item.get("jobLocation") or {}
                addr = loc.get("address") or {}
                location = ", ".join(filter(None, [
                    addr.get("addressLocality", ""), addr.get("addressCountry", "")
                ])) or None
                if location and not any(h.lower() in location.lower() for h in HU_LOCATIONS):
                    continue
                title = item.get("title") or item.get("name", "")
                if not title:
                    continue
                url = item.get("url")
                ident = item.get("identifier") or {}
                job_id = str(ident.get("value", ""))
                if job_id in seen:
                    continue
                if job_id:
                    seen.add(job_id)
                desc = BeautifulSoup(item.get("description", ""), "lxml").get_text(" ", strip=True)
                jobs.append(RawJob(
                    source=self.name, source_job_id=job_id or None,
                    title=title, company="Knorr-Bremse", location=location,
                    description=desc, application_url=url, source_url=url,
                    posted_at=self._parse_date(item.get("datePosted", "")),
                    deadline=self._parse_date(item.get("validThrough", "")),
                    job_type_raw=self._classify(title),
                ))
        return jobs

    @staticmethod
    def _classify(text: str) -> Optional[str]:
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
