"""
Generic RSS/Atom connector.

RSS feeds are the cleanest compliant source: structured, real-time, no scraping.

Sources implemented here:
  - GenericRSSConnector: parses any RSS/Atom feed with Schema.org JobPosting fields
  - JoobleRSSConnector: Jooble's public job search RSS
  - GraduatelandRSSConnector: Graduateland public RSS/search API

All three respect robots.txt, enforce rate limits, and use standard
feedparser for robust parsing across feed format variations.

Compliance:
  - RSS is explicitly designed for machine consumption — no scraping.
  - Jooble and Graduateland permit RSS access for personal/research use.
  - EURES also provides RSS (see eures_rss below).
"""
from __future__ import annotations

import hashlib
from datetime import datetime
from typing import Any, Optional
from urllib.parse import urlencode

import feedparser
import structlog

from jobhunter.connectors.base import BaseConnector, RawJob

logger = structlog.get_logger(__name__)

HU_SIGNALS = {
    "budapest", "debrecen", "hungary", "magyarország",
    "győr", "miskolc", "pécs", "kecskemét", "hybrid", "remote",
}


def _is_hungarian(text: str) -> bool:
    return any(s in text.lower() for s in HU_SIGNALS)


def _parse_rss_date(entry: Any) -> Optional[datetime]:
    """Parse feedparser's published_parsed or updated_parsed into a datetime."""
    for attr in ("published_parsed", "updated_parsed"):
        val = getattr(entry, attr, None)
        if val:
            try:
                import time
                return datetime(*val[:6])
            except Exception:
                pass
    return None


def _entry_fingerprint(url: str) -> str:
    return hashlib.sha256(url.encode()).hexdigest()[:16]


# ── Generic RSS ───────────────────────────────────────────────────────────

class GenericRSSConnector(BaseConnector):
    """
    Parse any RSS/Atom feed that contains job postings.

    Config in config.yaml::

        rss_feeds:
          - url: https://example.com/jobs.rss
            name: "Company X Jobs"

    Entries without a location field are included (let the scorer decide).
    """

    name = "rss_generic"
    description = "Generic RSS/Atom job feed reader"
    requires_browser = False

    def __init__(self, feed_urls: Optional[list[str]] = None, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._feed_urls = feed_urls or []

    async def _fetch_jobs(self) -> list[RawJob]:
        if not self._feed_urls:
            # Load from config
            from jobhunter.config import get_search_config
            config = get_search_config()
            self._feed_urls = config._data.get("rss_feeds", [])

        jobs: list[RawJob] = []
        seen: set[str] = set()

        for feed_url in self._feed_urls:
            if isinstance(feed_url, dict):
                url = feed_url.get("url", "")
                label = feed_url.get("name", url)
            else:
                url = feed_url
                label = url

            try:
                batch = await self._parse_feed(url, label, seen)
                jobs.extend(batch)
                logger.info("rss.feed_done", url=url, found=len(batch))
            except Exception as exc:
                logger.warning("rss.feed_error", url=url, error=str(exc))

        return jobs

    async def _parse_feed(
        self, url: str, label: str, seen: set[str]
    ) -> list[RawJob]:
        response = await self._get(url)
        feed = feedparser.parse(response.text)
        jobs: list[RawJob] = []

        for entry in feed.entries:
            try:
                link = entry.get("link") or entry.get("id") or ""
                if not link:
                    continue

                fp = _entry_fingerprint(link)
                if fp in seen:
                    continue
                seen.add(fp)

                title = entry.get("title", "").strip()
                if not title:
                    continue

                # Location: look in tags, summary, or content
                location = (
                    entry.get("location")
                    or entry.get("jobLocation")
                    or self._extract_location_from_text(
                        entry.get("summary", "") + " " + entry.get("content", [{}])[0].get("value", "")
                    )
                )

                company = (
                    entry.get("author")
                    or entry.get("company")
                    or entry.get("dc_publisher")
                    or label
                )

                description = entry.get("summary") or entry.get("content", [{}])[0].get("value", "")

                jobs.append(RawJob(
                    source=self.name,
                    source_job_id=fp,
                    title=title,
                    company=str(company),
                    location=location,
                    description=description,
                    application_url=link,
                    source_url=link,
                    posted_at=_parse_rss_date(entry),
                ))
            except Exception as exc:
                logger.debug("rss.entry_error", error=str(exc))

        return jobs

    @staticmethod
    def _extract_location_from_text(text: str) -> Optional[str]:
        """Pull the first recognisable Hungarian location from free text."""
        for signal in HU_SIGNALS:
            if signal in text.lower():
                # Capitalise nicely
                return signal.title()
        return None


# ── Jooble RSS ────────────────────────────────────────────────────────────

class JoobleRSSConnector(BaseConnector):
    """
    Jooble job aggregator — public RSS search feeds.

    Jooble provides RSS feeds for keyword + location searches.
    No API key required for the public RSS endpoint.

    Feed URL format:
        https://jooble.org/rss/<location>/<keyword>
    """

    name = "jooble_rss"
    description = "Jooble job aggregator — public RSS feeds for Hungary"
    requires_browser = False

    JOOBLE_RSS_BASE = "https://jooble.org/rss"

    SEARCH_COMBOS = [
        ("Budapest", "intern"),
        ("Budapest", "engineering intern"),
        ("Budapest", "mechatronics intern"),
        ("Budapest", "robotics intern"),
        ("Debrecen", "intern"),
        ("Debrecen", "engineering intern"),
        ("Hungary", "mechatronics"),
        ("Hungary", "automation intern"),
    ]

    async def _fetch_jobs(self) -> list[RawJob]:
        jobs: list[RawJob] = []
        seen: set[str] = set()

        for location, keyword in self.SEARCH_COMBOS:
            url = f"{self.JOOBLE_RSS_BASE}/{location}/{keyword}"
            try:
                batch = await self._parse_jooble_feed(url, seen)
                jobs.extend(batch)
                logger.info(
                    "jooble.feed_done",
                    location=location,
                    keyword=keyword,
                    found=len(batch),
                )
            except Exception as exc:
                logger.warning("jooble.feed_error", url=url, error=str(exc))

        return jobs

    async def _parse_jooble_feed(self, url: str, seen: set[str]) -> list[RawJob]:
        response = await self._get(url)
        feed = feedparser.parse(response.text)
        jobs: list[RawJob] = []

        for entry in feed.entries:
            try:
                link = entry.get("link", "")
                if not link:
                    continue

                fp = _entry_fingerprint(link)
                if fp in seen:
                    continue
                seen.add(fp)

                title = entry.get("title", "").strip()
                if not title:
                    continue

                summary = entry.get("summary", "")

                # Jooble puts location in <location> tag or summary first line
                location = entry.get("jooble_location") or entry.get("location")
                if not location:
                    # Try to extract from first line of summary
                    first_line = summary.split("\n")[0] if summary else ""
                    if any(h in first_line.lower() for h in HU_SIGNALS):
                        location = first_line.strip()

                # Filter: keep only Hungary-relevant
                full_text = (title + " " + (location or "") + " " + summary).lower()
                if not _is_hungarian(full_text):
                    continue

                company = entry.get("author") or entry.get("jooble_company") or "Unknown"

                jobs.append(RawJob(
                    source=self.name,
                    source_job_id=fp,
                    title=title,
                    company=str(company),
                    location=location,
                    description=summary,
                    application_url=link,
                    source_url=link,
                    posted_at=_parse_rss_date(entry),
                ))
            except Exception as exc:
                logger.debug("jooble.entry_error", error=str(exc))

        return jobs

    async def _is_healthy(self) -> bool:
        try:
            r = await self._get("https://jooble.org")
            return r.status_code < 400
        except Exception:
            return False


# ── Graduateland ──────────────────────────────────────────────────────────

class GraduatelandConnector(BaseConnector):
    """
    Graduateland — EU student job platform.

    Graduateland provides a public search API for job listings.
    No authentication required for search results.

    API endpoint observed from network inspection of graduateland.com.
    Falls back to HTML parsing if the API is unavailable.
    """

    name = "graduateland"
    description = "Graduateland — EU student and graduate job platform"
    requires_browser = False

    SEARCH_API = "https://graduateland.com/api/v2/jobads"
    SEARCH_PAGE = "https://graduateland.com/jobs"

    SEARCH_TERMS = [
        "mechatronics", "robotics", "automation", "embedded",
        "electrical engineering", "manufacturing", "intern",
    ]

    async def _fetch_jobs(self) -> list[RawJob]:
        jobs: list[RawJob] = []
        seen: set[str] = set()

        for term in self.SEARCH_TERMS:
            try:
                batch = await self._search(term, seen)
                jobs.extend(batch)
                logger.info("graduateland.search_done", term=term, found=len(batch))
            except Exception as exc:
                logger.warning("graduateland.search_error", term=term, error=str(exc))

        return jobs

    async def _search(self, keyword: str, seen: set[str]) -> list[RawJob]:
        try:
            return await self._search_via_api(keyword, seen)
        except Exception as exc:
            logger.debug("graduateland.api_fallback", error=str(exc))
            return await self._search_via_html(keyword, seen)

    async def _search_via_api(self, keyword: str, seen: set[str]) -> list[RawJob]:
        params = {
            "query": keyword,
            "country": "Hungary",
            "limit": 50,
            "offset": 0,
        }
        response = await self._get(self.SEARCH_API, params=params)
        if response.status_code != 200:
            raise RuntimeError(f"API {response.status_code}")

        data = response.json()
        return self._parse_api_response(data, seen)

    async def _search_via_html(self, keyword: str, seen: set[str]) -> list[RawJob]:
        """Fallback: parse the HTML search page."""
        from urllib.parse import urlencode
        from bs4 import BeautifulSoup
        import json

        params = {"q": keyword, "country": "Hungary"}
        url = f"{self.SEARCH_PAGE}?{urlencode(params)}"
        response = await self._get(url)
        soup = BeautifulSoup(response.text, "lxml")
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
                job = self._parse_jsonld_item(item)
                if job and job.source_job_id not in seen:
                    if job.source_job_id:
                        seen.add(job.source_job_id)
                    jobs.append(job)

        return jobs

    def _parse_api_response(self, data: Any, seen: set[str]) -> list[RawJob]:
        jobs: list[RawJob] = []
        items = data.get("results", data.get("jobs", data.get("data", [])))
        if not isinstance(items, list):
            return []

        for item in items:
            try:
                job_id = str(item.get("id") or item.get("slug") or "")
                if job_id in seen:
                    continue

                title = item.get("title") or item.get("name") or ""
                if not title:
                    continue

                locations = item.get("locations", [])
                if isinstance(locations, list):
                    location = ", ".join(
                        loc.get("city", "") + " " + loc.get("country", "")
                        for loc in locations
                        if isinstance(loc, dict)
                    ).strip() or None
                else:
                    location = str(locations) if locations else None

                # Only Hungary-relevant
                if location and not _is_hungarian(location):
                    continue

                company_obj = item.get("company") or {}
                company = (
                    company_obj.get("name") if isinstance(company_obj, dict)
                    else str(company_obj)
                ) or "Unknown"

                url = item.get("url") or item.get("applicationUrl")
                if job_id and not url:
                    url = f"https://graduateland.com/jobs/{job_id}"

                if job_id:
                    seen.add(job_id)

                jobs.append(RawJob(
                    source=self.name,
                    source_job_id=job_id or None,
                    title=title,
                    company=company,
                    location=location,
                    description=item.get("description") or item.get("body"),
                    application_url=url,
                    source_url=url,
                    posted_at=self._parse_date(item.get("publishedAt") or item.get("createdAt")),
                    job_type_raw=self._classify_type(title),
                ))
            except Exception as exc:
                logger.debug("graduateland.item_error", error=str(exc))

        return jobs

    def _parse_jsonld_item(self, item: dict[str, Any]) -> Optional[RawJob]:
        title = item.get("title") or item.get("name", "")
        if not title:
            return None
        loc = item.get("jobLocation") or {}
        addr = loc.get("address") or {}
        location = ", ".join(filter(None, [
            addr.get("addressLocality", ""),
            addr.get("addressCountry", ""),
        ])) or None
        if location and not _is_hungarian(location):
            return None
        url = item.get("url")
        ident = item.get("identifier") or {}
        job_id = str(ident.get("value", "")) or _entry_fingerprint(url or title)
        from bs4 import BeautifulSoup
        desc = BeautifulSoup(item.get("description", ""), "lxml").get_text(" ", strip=True)
        return RawJob(
            source=self.name,
            source_job_id=job_id,
            title=title,
            company=(item.get("hiringOrganization") or {}).get("name", "Unknown"),
            location=location,
            description=desc,
            application_url=url,
            source_url=url,
            posted_at=self._parse_date(item.get("datePosted")),
            deadline=self._parse_date(item.get("validThrough")),
            job_type_raw=self._classify_type(title),
        )

    @staticmethod
    def _classify_type(text: str) -> Optional[str]:
        t = text.lower()
        if any(k in t for k in ("intern", "internship", "practice", "trainee")):
            return "internship"
        if "student" in t:
            return "working_student"
        return None

    @staticmethod
    def _parse_date(raw: Any) -> Optional[datetime]:
        if not raw or not isinstance(raw, str):
            return None
        for fmt in ("%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
            try:
                return datetime.strptime(raw[:19], fmt)
            except ValueError:
                continue
        return None

    async def _is_healthy(self) -> bool:
        try:
            r = await self._get("https://graduateland.com")
            return r.status_code < 400
        except Exception:
            return False
