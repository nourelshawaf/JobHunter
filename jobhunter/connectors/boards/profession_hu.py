"""
Profession.hu connector.

Profession.hu is Hungary's largest job board.

Updated August 2026 — the site now renders job listings as structured
anchor links in the format:
  ## [Job Title](https://www.profession.hu/allas/title-company-city-ID)

The connector uses two strategies:
  1. Category URL approach — directly hits the pre-filtered category
     pages for mechatronics, electrical engineering, and intern listings.
     These are stable URLs built from the category structure observed
     on the live site (/allasok/mechatronikai-mernok/1,28,0,0,429 etc.)
  2. Link extraction — parses all /allas/ hrefs from the page and
     extracts job metadata from surrounding context.

This avoids fragile CSS class-name matching and works with the
current Profession.hu layout.
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta
from typing import Optional
from urllib.parse import urljoin

import structlog
from bs4 import BeautifulSoup

from jobhunter.connectors.base import BaseConnector, RawJob

logger = structlog.get_logger(__name__)

BASE_URL = "https://www.profession.hu"

# Direct category page URLs — pre-filtered to engineering roles in Hungary.
# Category IDs observed from the live site navigation.
CATEGORY_URLS = [
    # Mechatronics engineer (429)
    f"{BASE_URL}/allasok/mechatronikai-mernok/budapest/1,28,23,0,429",
    f"{BASE_URL}/allasok/mechatronikai-mernok/debrecen/1,28,32,0,429",
    f"{BASE_URL}/allasok/mechatronikai-mernok/1,28,0,0,429",
    # Electrical engineer (63)
    f"{BASE_URL}/allasok/villamosmernok/budapest/1,28,23,0,63",
    f"{BASE_URL}/allasok/villamosmernok/debrecen/1,28,32,0,63",
    # Mechanical engineer (53)
    f"{BASE_URL}/allasok/gepeszmernok/budapest/1,28,23,0,53",
    f"{BASE_URL}/allasok/gepeszmernok/debrecen/1,28,32,0,53",
    # Internships/practice (Szakmai gyakorlat = type 7, Diákmunka = type 10)
    f"{BASE_URL}/allasok/mernok/budapest/1,28,23,0,0,0,0,0,0,0,0,0,0,7",
    f"{BASE_URL}/allasok/mernok/debrecen/1,28,32,0,0,0,0,0,0,0,0,0,0,7",
    f"{BASE_URL}/allasok/mernok/budapest/1,28,23,0,0,0,0,0,0,0,0,0,0,10",
    f"{BASE_URL}/allasok/mernok/debrecen/1,28,32,0,0,0,0,0,0,0,0,0,0,10",
]


class ProfessionHuConnector(BaseConnector):
    """Fetches engineering internship listings from Profession.hu."""

    name = "profession_hu"
    description = "Profession.hu — Hungary's largest job board (category pages)"
    requires_browser = False

    async def _fetch_jobs(self) -> list[RawJob]:
        jobs: list[RawJob] = []
        seen_ids: set[str] = set()

        for url in CATEGORY_URLS:
            try:
                response = await self._get(url)
                batch = self._parse_page(response.text, seen_ids)
                jobs.extend(batch)
                logger.info(
                    "profession_hu.page_done",
                    url=url.split("/")[-1],
                    found=len(batch),
                )
            except Exception as exc:
                logger.warning("profession_hu.page_error", url=url, error=str(exc))

        return jobs

    def _parse_page(self, html: str, seen_ids: set[str]) -> list[RawJob]:
        """Extract jobs from a Profession.hu listing page."""
        soup = BeautifulSoup(html, "lxml")
        jobs: list[RawJob] = []

        # Every job card has a link matching /allas/<slug>
        # The pattern is: ## [Title](url) then company and location nearby
        job_links = soup.find_all("a", href=re.compile(r"/allas/[^/\"]+$"))

        for link in job_links:
            try:
                href = str(link.get("href", ""))
                if not href or "belepes" in href or "regisztracio" in href:
                    continue

                full_url = urljoin(BASE_URL, href)

                # Extract the numeric job ID from the URL (last segment)
                job_id_match = re.search(r"-(\d+)(?:/pro)?$", href)
                job_id = job_id_match.group(1) if job_id_match else None

                if job_id and job_id in seen_ids:
                    continue
                if job_id:
                    seen_ids.add(job_id)

                title = link.get_text(strip=True)
                if not title or len(title) < 3:
                    continue

                # Walk up the DOM to find the job card container
                container = self._find_container(link)

                # Company — look for an <a> pointing to /allasok/<company>/
                company = self._extract_company(container)

                # Location — look for bold text or location indicators
                location = self._extract_location(container)

                # Date — look for "feladva" (posted) pattern
                posted_at = self._extract_date(container)

                # Work mode
                work_mode_raw = None
                if container:
                    text = container.get_text(" ", strip=True).lower()
                    if "hibrid" in text or "home office" in text:
                        work_mode_raw = "hybrid"
                    elif "távmunka" in text or "remote" in text:
                        work_mode_raw = "remote"

                # Job type
                job_type_raw = self._detect_job_type(container, title)

                jobs.append(RawJob(
                    source=self.name,
                    source_job_id=job_id,
                    title=title,
                    company=company or "Unknown",
                    location=location,
                    application_url=full_url,
                    source_url=full_url,
                    posted_at=posted_at,
                    work_mode_raw=work_mode_raw,
                    job_type_raw=job_type_raw,
                ))
            except Exception as exc:
                logger.debug("profession_hu.link_error", error=str(exc))

        return jobs

    @staticmethod
    def _find_container(link) -> Optional[object]:
        """Walk up the DOM to find a meaningful job card element."""
        node = link
        for _ in range(8):
            parent = node.find_parent(["li", "article", "div", "section"])
            if parent is None:
                break
            text = parent.get_text(strip=True)
            if len(text) > len(link.get_text(strip=True)) + 20:
                return parent
            node = parent
        return link.parent

    @staticmethod
    def _extract_company(container) -> Optional[str]:
        if container is None:
            return None
        # Company links point to /allasok/<company>/...
        company_link = container.find(
            "a", href=re.compile(r"/allasok/[^/]+/1,0,0")
        )
        if company_link:
            return company_link.get_text(strip=True)
        return None

    @staticmethod
    def _extract_location(container) -> Optional[str]:
        if container is None:
            return None
        text = container.get_text(" ", strip=True)

        # Profession.hu shows location in bold after company name
        # Look for known Hungarian cities
        cities = [
            "Budapest", "Debrecen", "Győr", "Miskolc", "Pécs",
            "Székesfehérvár", "Kecskemét", "Nyíregyháza", "Eger",
            "Tatabánya", "Kaposvár", "Veszprém", "Komárom",
        ]
        for city in cities:
            if city in text:
                return city + ", Hungary"

        # Check for county names
        counties = [
            "Budapest", "Pest megye", "Fejér megye", "Győr-Moson-Sopron",
            "Hajdú-Bihar", "Baranya", "Heves", "Borsod",
        ]
        for county in counties:
            if county in text:
                return county + ", Hungary"

        return "Hungary"

    @staticmethod
    def _extract_date(container) -> Optional[datetime]:
        if container is None:
            return None
        text = container.get_text(" ", strip=True)

        # "feladva: Tegnap" = posted yesterday
        if "tegnap" in text.lower():
            return (datetime.utcnow() - timedelta(days=1)).replace(
                hour=0, minute=0, second=0, microsecond=0
            )

        # "feladva: Ma" = today
        if re.search(r"feladva.*\bma\b", text, re.IGNORECASE):
            return datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)

        # Absolute date like "Augusztus 10." or "2024.08.10"
        month_hu = {
            "január": 1, "február": 2, "március": 3, "április": 4,
            "május": 5, "június": 6, "július": 7, "augusztus": 8,
            "szeptember": 9, "október": 10, "november": 11, "december": 12,
        }
        for name, num in month_hu.items():
            m = re.search(rf"{name}\s+(\d+)\.", text, re.IGNORECASE)
            if m:
                try:
                    day = int(m.group(1))
                    year = datetime.utcnow().year
                    return datetime(year, num, day)
                except ValueError:
                    pass

        return None

    @staticmethod
    def _detect_job_type(container, title: str) -> Optional[str]:
        text = ((container.get_text(" ", strip=True) if container else "") + " " + title).lower()
        if any(k in text for k in ("szakmai gyakorlat", "intern", "internship", "gyakorlat")):
            return "internship"
        if any(k in text for k in ("diákmunka", "student", "diák")):
            return "working_student"
        if "trainee" in text:
            return "trainee"
        return None

    async def _is_healthy(self) -> bool:
        try:
            r = await self._get(BASE_URL)
            return r.status_code == 200
        except Exception:
            return False
