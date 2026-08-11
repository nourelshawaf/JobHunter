"""
Valeo, ZF, and ABB Careers connectors.

All three use publicly accessible career pages with JSON-LD structured data.

Valeo: uses Oracle Taleo ATS — valeo.com/careers
ZF:    uses SAP SuccessFactors — careers.zf.com
ABB:   uses Workday ATS — careers.abb.com

Each connector:
  - Tries a JSON API first (observed from network inspection)
  - Falls back to JSON-LD extraction from public HTML pages
  - Filters to Hungary-relevant locations
  - Is idempotent on source_job_id

Compliance: all sources are publicly accessible, no authentication required.
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

HU_SIGNALS = [
    "budapest", "debrecen", "győr", "pécs", "miskolc",
    "hungary", "magyarország", "remote", "hybrid",
]


def _is_hu(text: str) -> bool:
    return any(s in (text or "").lower() for s in HU_SIGNALS)


def _parse_dt(raw: str) -> Optional[datetime]:
    if not raw:
        return None
    for fmt in ("%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(raw[:19], fmt)
        except ValueError:
            continue
    return None


def _classify(text: str) -> Optional[str]:
    t = text.lower()
    if any(k in t for k in ("intern", "internship", "practice", "praktikum")):
        return "internship"
    if "trainee" in t:
        return "trainee"
    if any(k in t for k in ("student", "working student")):
        return "working_student"
    return None


def _jsonld_jobs(html: str, source_name: str, company: str, seen: set[str]) -> list[RawJob]:
    """Shared JSON-LD extractor used by all three connectors."""
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
                    addr.get("addressLocality", ""), addr.get("addressCountry", "")
                ])) or None
                if location and not _is_hu(location):
                    continue
                title = item.get("title") or item.get("name", "")
                if not title:
                    continue
                url = item.get("url") or item.get("sameAs")
                ident = item.get("identifier") or {}
                job_id = str(ident.get("value", "")) or (
                    __import__("hashlib").sha256((url or title).encode()).hexdigest()[:16]
                )
                if job_id in seen:
                    continue
                seen.add(job_id)
                desc = BeautifulSoup(item.get("description", ""), "lxml").get_text(" ", strip=True)
                jobs.append(RawJob(
                    source=source_name, source_job_id=job_id,
                    title=title, company=company, location=location,
                    description=desc, application_url=url, source_url=url,
                    posted_at=_parse_dt(item.get("datePosted", "")),
                    deadline=_parse_dt(item.get("validThrough", "")),
                    job_type_raw=_classify(title),
                ))
            except Exception as exc:
                logger.debug("jsonld.item_error", source=source_name, error=str(exc))
    return jobs


# ── Valeo ─────────────────────────────────────────────────────────────────

class ValeoCareersConnector(BaseConnector):
    """Fetches Valeo internship postings for Hungary via Oracle Taleo + JSON-LD."""

    name = "valeo_careers"
    description = "Valeo Careers — Oracle Taleo + JSON-LD fallback"
    requires_browser = False
    _BASE = "https://valeo.com/en/careers"
    _SEARCH = "https://jobs.valeo.com/search"

    async def _fetch_jobs(self) -> list[RawJob]:
        jobs: list[RawJob] = []
        seen: set[str] = set()
        for term in ["intern", "internship", "student", "trainee"]:
            try:
                batch = await self._search(term, seen)
                jobs.extend(batch)
                logger.info("valeo.done", term=term, found=len(batch))
            except Exception as exc:
                logger.warning("valeo.error", term=term, error=str(exc))
        return jobs

    async def _search(self, keyword: str, seen: set[str]) -> list[RawJob]:
        try:
            params = {"keyword": keyword, "location": "Hungary", "results": 50}
            response = await self._get(self._SEARCH, params=params)
            if response.status_code != 200:
                raise RuntimeError(str(response.status_code))
            return self._parse_taleo(response.json(), seen)
        except Exception as exc:
            logger.debug("valeo.api_fallback", error=str(exc))
            url = f"{self._BASE}?{urlencode({'q': keyword, 'country': 'Hungary'})}"
            resp = await self._get(url)
            return _jsonld_jobs(resp.text, self.name, "Valeo", seen)

    def _parse_taleo(self, data: dict[str, Any], seen: set[str]) -> list[RawJob]:
        jobs: list[RawJob] = []
        for item in data.get("requisitionList", data.get("jobs", [])):
            try:
                job_id = str(item.get("requisitionId") or item.get("id") or "")
                if job_id in seen:
                    continue
                title = item.get("externalTitle") or item.get("title") or ""
                if not title:
                    continue
                location = item.get("locationDescription") or item.get("location") or ""
                if location and not _is_hu(location):
                    continue
                url = item.get("detailUrl") or f"https://jobs.valeo.com/jobs/{job_id}"
                if job_id:
                    seen.add(job_id)
                jobs.append(RawJob(
                    source=self.name, source_job_id=job_id or None,
                    title=title, company="Valeo", location=location or None,
                    application_url=url, source_url=url,
                    posted_at=_parse_dt(item.get("startDate") or ""),
                    job_type_raw=_classify(title),
                ))
            except Exception as exc:
                logger.debug("valeo.item_error", error=str(exc))
        return jobs

    async def _is_healthy(self) -> bool:
        try:
            r = await self._get(self._BASE)
            return r.status_code < 400
        except Exception:
            return False


# ── ZF ────────────────────────────────────────────────────────────────────

class ZFCareersConnector(BaseConnector):
    """Fetches ZF Group internship postings for Hungary via SAP SuccessFactors."""

    name = "zf_careers"
    description = "ZF Group Careers — SAP SuccessFactors + JSON-LD fallback"
    requires_browser = False
    _BASE = "https://careers.zf.com"
    _SEARCH = f"{_BASE}/jobs/search"

    async def _fetch_jobs(self) -> list[RawJob]:
        jobs: list[RawJob] = []
        seen: set[str] = set()
        for term in ["intern", "internship", "student", "trainee", "working student"]:
            try:
                batch = await self._search(term, seen)
                jobs.extend(batch)
                logger.info("zf.done", term=term, found=len(batch))
            except Exception as exc:
                logger.warning("zf.error", term=term, error=str(exc))
        return jobs

    async def _search(self, keyword: str, seen: set[str]) -> list[RawJob]:
        try:
            params = {"q": keyword, "country": "Hungary", "rows": 50}
            response = await self._get(self._SEARCH, params=params)
            if response.status_code != 200:
                raise RuntimeError(str(response.status_code))
            return self._parse_sap(response.json(), seen)
        except Exception as exc:
            logger.debug("zf.api_fallback", error=str(exc))
            url = f"{self._BASE}/jobs?{urlencode({'q': keyword, 'country': 'Hungary'})}"
            resp = await self._get(url)
            return _jsonld_jobs(resp.text, self.name, "ZF Group", seen)

    def _parse_sap(self, data: dict[str, Any], seen: set[str]) -> list[RawJob]:
        jobs: list[RawJob] = []
        for item in data.get("results", data.get("jobs", [])):
            try:
                job_id = str(item.get("jobId") or item.get("id") or "")
                if job_id in seen:
                    continue
                title = item.get("title") or item.get("jobTitle") or ""
                if not title:
                    continue
                location = ", ".join(filter(None, [
                    item.get("city", ""), item.get("country", "")
                ])) or None
                if location and not _is_hu(location):
                    continue
                path = item.get("detailUrl") or item.get("url") or f"/jobs/{job_id}"
                url = f"{self._BASE}{path}" if path.startswith("/") else path
                if job_id:
                    seen.add(job_id)
                jobs.append(RawJob(
                    source=self.name, source_job_id=job_id or None,
                    title=title, company="ZF Group", location=location,
                    application_url=url, source_url=url,
                    posted_at=_parse_dt(item.get("postedDate") or ""),
                    job_type_raw=_classify(title),
                ))
            except Exception as exc:
                logger.debug("zf.item_error", error=str(exc))
        return jobs

    async def _is_healthy(self) -> bool:
        try:
            r = await self._get(self._BASE)
            return r.status_code < 400
        except Exception:
            return False


# ── ABB ───────────────────────────────────────────────────────────────────

class ABBCareersConnector(BaseConnector):
    """Fetches ABB internship postings for Hungary via Workday + JSON-LD."""

    name = "abb_careers"
    description = "ABB Careers — Workday public search + JSON-LD fallback"
    requires_browser = False
    _BASE = "https://careers.abb.com"
    _SEARCH = f"{_BASE}/api/apply/v2/jobs"

    async def _fetch_jobs(self) -> list[RawJob]:
        jobs: list[RawJob] = []
        seen: set[str] = set()
        for term in ["intern", "internship", "student", "trainee", "automation"]:
            try:
                batch = await self._search(term, seen)
                jobs.extend(batch)
                logger.info("abb.done", term=term, found=len(batch))
            except Exception as exc:
                logger.warning("abb.error", term=term, error=str(exc))
        return jobs

    async def _search(self, keyword: str, seen: set[str]) -> list[RawJob]:
        try:
            params = {"q": keyword, "country": "HU", "limit": 50, "offset": 0}
            response = await self._get(self._SEARCH, params=params)
            if response.status_code != 200:
                raise RuntimeError(str(response.status_code))
            return self._parse_workday(response.json(), seen)
        except Exception as exc:
            logger.debug("abb.api_fallback", error=str(exc))
            url = f"{self._BASE}/jobs?{urlencode({'q': keyword, 'location': 'Hungary'})}"
            resp = await self._get(url)
            return _jsonld_jobs(resp.text, self.name, "ABB", seen)

    def _parse_workday(self, data: dict[str, Any], seen: set[str]) -> list[RawJob]:
        jobs: list[RawJob] = []
        for item in data.get("jobPostings", data.get("jobs", [])):
            try:
                job_id = str(item.get("externalId") or item.get("id") or "")
                if job_id in seen:
                    continue
                title = item.get("title") or item.get("name") or ""
                if not title:
                    continue
                location = item.get("locationsText") or item.get("location") or ""
                if location and not _is_hu(location):
                    continue
                path = item.get("externalPath") or item.get("url") or f"/jobs/{job_id}"
                url = f"{self._BASE}{path}" if path.startswith("/") else path
                if job_id:
                    seen.add(job_id)
                jobs.append(RawJob(
                    source=self.name, source_job_id=job_id or None,
                    title=title, company="ABB", location=location or None,
                    application_url=url, source_url=url,
                    posted_at=_parse_dt(item.get("postedOn") or ""),
                    job_type_raw=_classify(title),
                ))
            except Exception as exc:
                logger.debug("abb.item_error", error=str(exc))
        return jobs

    async def _is_healthy(self) -> bool:
        try:
            r = await self._get(self._BASE)
            return r.status_code < 400
        except Exception:
            return False
