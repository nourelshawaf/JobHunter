"""
Profession.hu connector.

Profession.hu is Hungary's largest job board.
This connector fetches public search result pages using httpx + BeautifulSoup.
It respects robots.txt (no restricted paths accessed) and rate limits.

Profession.hu does not provide an RSS feed or public API,
so HTML parsing is used on the permitted public search pages.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Optional
from urllib.parse import urlencode, urljoin

import structlog
from bs4 import BeautifulSoup, Tag

from jobhunter.config import get_search_config
from jobhunter.connectors.base import BaseConnector, RawJob

logger = structlog.get_logger(__name__)

BASE_URL = "https://www.profession.hu"
SEARCH_PATH = "/allasok"


class ProfessionHuConnector(BaseConnector):
    """Fetches internship and student job listings from Profession.hu."""

    name = "profession_hu"
    description = "Profession.hu — Hungary's largest job board (public search pages)"
    requires_browser = False

    async def _fetch_jobs(self) -> list[RawJob]:
        """Search Profession.hu for all configured keyword groups."""
        search_config = get_search_config()
        jobs: list[RawJob] = []
        seen_ids: set[str] = set()

        keyword_groups = [
            "mérnök gyakornok",
            "mechatronika gyakorlat",
            "automatizálás gyakornok",
            "engineering intern",
            "robotics intern",
            "embedded intern",
            "automation intern",
        ]

        for keyword in keyword_groups:
            try:
                batch = await self._search_keyword(keyword, seen_ids)
                jobs.extend(batch)
                logger.info(
                    "profession_hu.keyword_done",
                    keyword=keyword,
                    found=len(batch),
                )
            except Exception as exc:
                logger.warning(
                    "profession_hu.keyword_error",
                    keyword=keyword,
                    error=str(exc),
                )

        return jobs

    async def _search_keyword(
        self, keyword: str, seen_ids: set[str]
    ) -> list[RawJob]:
        """Fetch one page of results for a keyword."""
        params = {
            "mit": keyword,
            "hol": "Budapest,Debrecen",
        }
        url = f"{BASE_URL}{SEARCH_PATH}?{urlencode(params)}"

        response = await self._get(url)
        return self._parse_listing_page(response.text, seen_ids)

    def _parse_listing_page(self, html: str, seen_ids: set[str]) -> list[RawJob]:
        """Parse a Profession.hu search results page."""
        soup = BeautifulSoup(html, "lxml")
        jobs: list[RawJob] = []

        # Profession.hu job cards: <article class="job-card ...">
        cards = soup.select("article.job-card, div.job-item, li.position-item")

        if not cards:
            # Try alternative selectors for layout variants
            cards = soup.select("[data-job-id], .jobs-list-item")

        for card in cards:
            try:
                job = self._parse_card(card)
                if job is None:
                    continue
                if job.source_job_id in seen_ids:
                    continue
                if job.source_job_id:
                    seen_ids.add(job.source_job_id)
                jobs.append(job)
            except Exception as exc:
                logger.debug("profession_hu.card_parse_error", error=str(exc))

        return jobs

    def _parse_card(self, card: Tag) -> Optional[RawJob]:
        """Extract a RawJob from a single job card element."""
        # Job ID
        job_id = (
            card.get("data-job-id")
            or card.get("data-id")
            or card.get("id", "").replace("job-", "")
        )

        # Title
        title_el = card.select_one(
            "h2.job-title, h3.job-title, .position-title, a.job-link, .title"
        )
        if not title_el:
            return None
        title = title_el.get_text(strip=True)
        if not title:
            return None

        # Company
        company_el = card.select_one(
            ".company-name, .employer-name, .ceg-nev, [data-company]"
        )
        company = company_el.get_text(strip=True) if company_el else "Unknown"

        # Location
        location_el = card.select_one(".location, .city, .telepules")
        location = location_el.get_text(strip=True) if location_el else None

        # Application URL
        link_el = card.select_one("a[href]")
        application_url: Optional[str] = None
        if link_el:
            href = str(link_el.get("href", ""))
            application_url = urljoin(BASE_URL, href) if href else None

        # Posted date (often relative: "2 napja" = 2 days ago)
        date_el = card.select_one(".posted-date, .date, .datum, .feladva")
        posted_at = self._parse_hungarian_date(
            date_el.get_text(strip=True) if date_el else ""
        )

        # Salary
        salary_el = card.select_one(".salary, .ber, .juttatás")
        salary_raw = salary_el.get_text(strip=True) if salary_el else None

        return RawJob(
            source=self.name,
            source_job_id=str(job_id) if job_id else None,
            title=title,
            company=company,
            location=location,
            application_url=application_url,
            source_url=application_url,
            posted_at=posted_at,
            salary_raw=salary_raw,
        )

    @staticmethod
    def _parse_hungarian_date(raw: str) -> Optional[datetime]:
        """
        Parse Hungarian relative and absolute date strings.

        Examples:
          "2 napja" → 2 days ago
          "ma" → today
          "2024.05.15" → exact date
        """
        if not raw:
            return None

        raw = raw.strip().lower()
        now = datetime.utcnow()

        if raw in ("ma", "today"):
            return now.replace(hour=0, minute=0, second=0, microsecond=0)

        if raw in ("tegnap", "yesterday"):
            from datetime import timedelta
            return (now - timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)

        # "X napja" (X days ago)
        match = re.match(r"(\d+)\s*napja", raw)
        if match:
            from datetime import timedelta
            days = int(match.group(1))
            return (now - timedelta(days=days)).replace(
                hour=0, minute=0, second=0, microsecond=0
            )

        # "X hete" (X weeks ago)
        match = re.match(r"(\d+)\s*hete", raw)
        if match:
            from datetime import timedelta
            weeks = int(match.group(1))
            return (now - timedelta(weeks=weeks)).replace(
                hour=0, minute=0, second=0, microsecond=0
            )

        # Absolute formats: "2024.05.15" or "2024-05-15"
        for fmt in ("%Y.%m.%d", "%Y-%m-%d", "%d.%m.%Y"):
            try:
                return datetime.strptime(raw, fmt)
            except ValueError:
                continue

        return None

    async def _is_healthy(self) -> bool:
        """Check that Profession.hu is reachable."""
        try:
            response = await self._get(BASE_URL)
            return response.status_code == 200
        except Exception:
            return False
