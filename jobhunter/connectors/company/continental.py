"""
Continental Careers connector.

Continental uses the SAP SuccessFactors ATS at jobs.continental.com.
The public search is accessible without authentication.

Source method: SAP SuccessFactors public API + JSON-LD HTML fallback.
Location filter: Hungary (Budapest, Debrecen, Nyíregyháza, Győr, Veszprém).

Known limitations:
- API endpoint paths are observed from network traffic, not officially documented.
- Some Continental subsidiaries (ContiTech, Vitesco) post on separate portals;
  this connector targets the main jobs.continental.com only.
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

CAREERS_BASE = "https://jobs.continental.com"
SEARCH_API = f"{CAREERS_BASE}/jobs"

HU_LOCATIONS = [
    "Budapest", "Debrecen", "Nyíregyháza", "Győr", "Veszprém",
    "Makó", "Hungary", "Magyarország", "Remote",
]


class ContinentalCareersConnector(BaseConnector):
    """Fetches Continental internship postings for Hungary."""

    name = "continental_careers"
    description = "Continental Careers — SAP SuccessFactors public search"
    requires_browser = False

    async def _fetch_jobs(self) -> list[RawJob]:
        jobs: list[RawJob] = []
        seen_ids: set[str] = set()

        search_terms = [
            "intern", "internship", "working student", "trainee",
            "student", "mechatronics", "automation", "embedded",
        ]

        for term in search_terms:
            try:
                batch = await self._search(term, seen_ids)
                jobs.extend(batch)
                logger.info("continental.search_done", term=term, found=len(batch))
            except Exception as exc:
                logger.warning("continental.search_error", term=term, error=str(exc))

        return jobs

    async def _search(self, keyword: str, seen_ids: set[str]) -> list[RawJob]:
        try:
            return await self._search_via_html_jsonld(keyword, seen_ids)
        except Exception as exc:
            logger.debug("continental.search_failed", error=str(exc))
            return []

    async def _search_via_html_jsonld(self, keyword: str, seen_ids: set[str]) -> list[RawJob]:
        """
        Continental's public job search page with JSON-LD structured data.

        Continental embeds Schema.org JobPosting JSON-LD in each result,
        making this the most reliable extraction method.
        """
        params = {
            "query": keyword,
            "location": "Hungary",
            "employment_type": "Internship",
        }
        response = await self._get(SEARCH_API, params=params)
        return self._extract_from_html(response.text, seen_ids)

    def _extract_from_html(self, html: str, seen_ids: set[str]) -> list[RawJob]:
        """Extract jobs from JSON-LD and also structured HTML cards."""
        soup = BeautifulSoup(html, "lxml")
        jobs: list[RawJob] = []

        # Try JSON-LD first
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

        # Fallback: HTML job cards
        if not jobs:
            cards = soup.select(
                "article.job-card, div.job-item, li[data-job-id], .job-listing-item"
            )
            for card in cards:
                try:
                    job = self._parse_html_card(card)
                    if job and job.source_job_id not in seen_ids:
                        if job.source_job_id:
                            seen_ids.add(job.source_job_id)
                        jobs.append(job)
                except Exception as exc:
                    logger.debug("continental.card_error", error=str(exc))

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

        org = item.get("hiringOrganization") or {}
        company = org.get("name", "Continental")
        url = item.get("url") or item.get("sameAs")
        ident = item.get("identifier") or {}
        job_id = str(ident.get("value", ""))

        raw_desc = item.get("description", "")
        description = BeautifulSoup(raw_desc, "lxml").get_text(" ", strip=True)
        emp_type = item.get("employmentType") or ""

        return RawJob(
            source=self.name,
            source_job_id=job_id or None,
            title=title,
            company=company,
            location=location,
            description=description,
            application_url=url,
            source_url=url,
            posted_at=self._parse_date(item.get("datePosted", "")),
            deadline=self._parse_date(item.get("validThrough", "")),
            job_type_raw=self._classify_type(
                emp_type if isinstance(emp_type, str) else " ".join(emp_type)
            ),
        )

    def _parse_html_card(self, card: Any) -> Optional[RawJob]:
        job_id = (
            card.get("data-job-id") or card.get("data-id") or card.get("id", "")
        )
        title_el = card.select_one(
            "h2, h3, .job-title, .position-title, a.job-link"
        )
        if not title_el:
            return None
        title = title_el.get_text(strip=True)

        company_el = card.select_one(".company-name, .employer")
        company = company_el.get_text(strip=True) if company_el else "Continental"

        loc_el = card.select_one(".location, .city")
        location = loc_el.get_text(strip=True) if loc_el else None
        if location and not any(h.lower() in location.lower() for h in HU_LOCATIONS):
            return None

        link_el = card.select_one("a[href]")
        href = link_el.get("href", "") if link_el else ""
        app_url = f"{CAREERS_BASE}{href}" if href.startswith("/") else href or None

        return RawJob(
            source=self.name,
            source_job_id=str(job_id) if job_id else None,
            title=title,
            company=company,
            location=location,
            application_url=app_url,
            source_url=app_url,
        )

    @staticmethod
    def _classify_type(text: str) -> Optional[str]:
        t = text.lower()
        if any(k in t for k in ("intern", "internship", "INTERN")):
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
