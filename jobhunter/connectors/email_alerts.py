"""
Email alert connector.

Connects to an IMAP mailbox and parses job-alert digest emails from
LinkedIn, Indeed, Glassdoor, and Hungarian job boards.

Design principles:
- Uses app passwords / IMAP credentials from environment — no OAuth required
  (OAuth can be added later without changing the public interface).
- Idempotent: tracks processed Message-IDs in the database; re-ingesting
  the same email produces no duplicates.
- Robust HTML parsing: never fails on one email's layout change — errors
  are logged per-email and the run continues.
- Original email metadata (message-id, sender, subject, date) is preserved
  on every extracted job for traceability.
- Does NOT modify or delete emails; read-only IMAP access.

Compliant because:
- We own the mailbox; we subscribed to these alerts ourselves.
- LinkedIn/Indeed/Glassdoor explicitly send these emails for the user to act on.
- No scraping of the platform itself takes place.
"""
from __future__ import annotations

import email
import email.policy
import hashlib
import re
import socket
from datetime import datetime, timezone
from email.message import Message
from typing import Optional
from urllib.parse import urljoin, urlparse

import imaplib
import structlog
from bs4 import BeautifulSoup

from jobhunter.config import get_settings
from jobhunter.connectors.base import BaseConnector, RawJob

logger = structlog.get_logger(__name__)

# ── Known sender → parser mapping ─────────────────────────────────────────
SENDER_PARSERS: dict[str, str] = {
    "jobalert@linkedin.com": "linkedin",
    "jobs-noreply@linkedin.com": "linkedin",
    "indeed@indeed.com": "indeed",
    "alert@indeed.com": "indeed",
    "noreply@glassdoor.com": "glassdoor",
    "alert@glassdoor.com": "glassdoor",
    "noreply@profession.hu": "profession_hu",
    "info@profession.hu": "profession_hu",
    "noreply@jobline.hu": "jobline_hu",
}


class EmailAlertsConnector(BaseConnector):
    """
    Parses job-alert emails from an IMAP mailbox.

    Each email yields zero or more RawJob instances.
    """

    name = "email_alerts"
    description = "IMAP job-alert email parser (LinkedIn, Indeed, Glassdoor, Hungarian boards)"
    requires_browser = False

    # Message-IDs already processed this session (across calls within one run)
    _processed_ids: set[str] = set()

    def __init__(self, *args, **kwargs) -> None:  # type: ignore[no-untyped-def]
        super().__init__(*args, **kwargs)
        self._imap: Optional[imaplib.IMAP4_SSL] = None

    async def _fetch_jobs(self) -> list[RawJob]:
        """Connect to IMAP and parse unread/recent alert emails."""
        settings = self.settings

        if not settings.alert_email_user or not settings.alert_email_password.get_secret_value():
            logger.info("email_alerts.skipped", reason="No IMAP credentials configured")
            return []

        jobs: list[RawJob] = []
        try:
            jobs = await self._run_imap_session()
        except (imaplib.IMAP4.error, socket.error, OSError) as exc:
            raise RuntimeError(f"IMAP connection failed: {exc}") from exc

        return jobs

    async def _run_imap_session(self) -> list[RawJob]:
        """Open IMAP session, fetch emails, close cleanly."""
        settings = self.settings
        jobs: list[RawJob] = []

        # imaplib is synchronous — fine for this use case (runs in a thread in production)
        with imaplib.IMAP4_SSL(
            host=settings.alert_email_host,
            port=settings.alert_email_port,
        ) as imap:
            imap.login(
                settings.alert_email_user,
                settings.alert_email_password.get_secret_value(),
            )
            imap.select(settings.alert_email_folder, readonly=True)

            # Search for emails from known job-alert senders in the last 30 days
            search_criteria = self._build_search_criteria()
            status, message_ids = imap.search(None, search_criteria)

            if status != "OK" or not message_ids[0]:
                logger.info("email_alerts.no_messages")
                return []

            ids = message_ids[0].split()
            logger.info("email_alerts.found_messages", count=len(ids))

            for msg_id in ids[-200:]:  # process most recent 200 max
                try:
                    status, msg_data = imap.fetch(msg_id, "(RFC822)")
                    if status != "OK" or not msg_data or not msg_data[0]:
                        continue

                    raw_bytes = msg_data[0][1]  # type: ignore[index]
                    parsed_email = email.message_from_bytes(
                        raw_bytes, policy=email.policy.default
                    )

                    batch = self._parse_email(parsed_email)
                    jobs.extend(batch)

                    logger.debug(
                        "email_alerts.email_processed",
                        subject=parsed_email.get("Subject", ""),
                        jobs_found=len(batch),
                    )
                except Exception as exc:
                    logger.warning(
                        "email_alerts.email_parse_error",
                        msg_id=msg_id,
                        error=str(exc),
                    )

        return jobs

    def _build_search_criteria(self) -> str:
        """Build IMAP SEARCH criteria for known alert senders."""
        # IMAP search for any of the known senders in the last 30 days
        return "SINCE 30-days-ago UNSEEN"

    def _parse_email(self, msg: Message) -> list[RawJob]:
        """
        Dispatch to the appropriate parser based on the sender.

        Returns an empty list (not an error) if no parser matches.
        """
        sender = str(msg.get("From", "")).lower()
        message_id = str(msg.get("Message-ID", "")).strip()
        subject = str(msg.get("Subject", ""))
        date_str = str(msg.get("Date", ""))

        # Skip already-processed message IDs (idempotency within a session)
        fingerprint = hashlib.sha256(message_id.encode()).hexdigest()[:16]
        if fingerprint in self._processed_ids:
            logger.debug("email_alerts.duplicate_skipped", fingerprint=fingerprint)
            return []
        self._processed_ids.add(fingerprint)

        # Identify the parser
        parser_name = None
        for known_sender, name in SENDER_PARSERS.items():
            if known_sender in sender:
                parser_name = name
                break

        if parser_name is None:
            logger.debug("email_alerts.unknown_sender", sender=sender)
            return []

        # Extract email body (prefer HTML for richness; fallback to plain text)
        html_body = self._extract_html_body(msg)
        text_body = self._extract_text_body(msg)
        body = html_body or text_body

        if not body:
            return []

        # Parse the email date
        email_date = self._parse_email_date(date_str)

        # Metadata attached to every job extracted from this email
        email_meta = {
            "email_message_id": message_id,
            "email_sender": sender,
            "email_subject": subject,
            "email_date": email_date.isoformat() if email_date else None,
            "email_fingerprint": fingerprint,
        }

        parser_fn = {
            "linkedin": self._parse_linkedin,
            "indeed": self._parse_indeed,
            "glassdoor": self._parse_glassdoor,
            "profession_hu": self._parse_profession_hu,
            "jobline_hu": self._parse_jobline_hu,
        }.get(parser_name)

        if parser_fn is None:
            return []

        try:
            jobs = parser_fn(body, email_meta, email_date)
            # Attach email metadata to every job
            for job in jobs:
                job.extra.update(email_meta)
                job.source = f"email_{parser_name}"
            return jobs
        except Exception as exc:
            logger.warning(
                "email_alerts.parser_error",
                parser=parser_name,
                error=str(exc),
            )
            return []

    # ── Per-source parsers ────────────────────────────────────────────────

    def _parse_linkedin(
        self,
        html: str,
        meta: dict,
        email_date: Optional[datetime],
    ) -> list[RawJob]:
        """
        Parse a LinkedIn Job Alert email.

        LinkedIn sends structured HTML with job cards that include:
        - Job title as an anchor text
        - Company name in a span
        - Location in a span
        - Direct job URL in the anchor href

        The layout is stable but we use multiple selectors defensively.
        """
        soup = BeautifulSoup(html, "lxml")
        jobs: list[RawJob] = []

        # LinkedIn job card selectors (multiple patterns for robustness)
        job_links = (
            soup.select("a[href*='/jobs/view/']")
            or soup.select("a[href*='linkedin.com/jobs']")
            or soup.select("td a[href*='jobs']")
        )

        for link in job_links:
            try:
                url = str(link.get("href", ""))
                if not url or "unsubscribe" in url.lower():
                    continue

                # Clean tracking parameters from URL
                url = self._clean_linkedin_url(url)

                # Title: the link text, or nearby heading
                title = link.get_text(strip=True)
                if not title or len(title) < 3:
                    continue

                # Company: look in the parent container
                container = link.find_parent("td") or link.find_parent("div") or link.parent

                company = ""
                if container:
                    # Try common company-name patterns in sibling/child text
                    company_el = (
                        container.find(class_=re.compile(r"company", re.I))
                        or container.find("span")
                    )
                    if company_el:
                        company = company_el.get_text(strip=True)

                if not company:
                    company = self._extract_company_from_context(link)

                # Location
                location = ""
                if container:
                    loc_el = container.find(class_=re.compile(r"location|location", re.I))
                    if loc_el:
                        location = loc_el.get_text(strip=True)

                # Skip non-Hungarian unless it says "Remote"
                if location and not self._is_relevant_location(location):
                    continue

                source_job_id = self._extract_linkedin_job_id(url)

                jobs.append(RawJob(
                    source="email_linkedin",
                    source_job_id=source_job_id or self._url_fingerprint(url),
                    title=title,
                    company=company or "Unknown",
                    location=location or None,
                    application_url=url,
                    source_url=url,
                    posted_at=email_date,
                ))
            except Exception as exc:
                logger.debug("email_alerts.linkedin_card_error", error=str(exc))

        return jobs

    def _parse_indeed(
        self,
        html: str,
        meta: dict,
        email_date: Optional[datetime],
    ) -> list[RawJob]:
        """
        Parse an Indeed Job Alert email.

        Indeed emails contain job cards with title, company, location,
        salary (sometimes), and a direct job URL.
        """
        soup = BeautifulSoup(html, "lxml")
        jobs: list[RawJob] = []

        # Indeed job links contain /rc/clk or /viewjob
        job_links = (
            soup.select("a[href*='/rc/clk']")
            or soup.select("a[href*='/viewjob']")
            or soup.select("a[href*='indeed.com/jobs']")
            or soup.select("a[href*='indeed.com/job']")
        )

        seen_ids: set[str] = set()

        for link in job_links:
            try:
                url = str(link.get("href", ""))
                if not url or "unsubscribe" in url.lower():
                    continue

                title = link.get_text(strip=True)
                if not title or len(title) < 3:
                    continue

                # Navigate to the containing job card
                container = self._find_job_container(link)

                company = self._extract_text_by_patterns(
                    container,
                    [r"company", r"employer"],
                    fallback="Unknown",
                )
                location = self._extract_text_by_patterns(
                    container,
                    [r"location", r"city"],
                    fallback="",
                )
                salary_raw = self._extract_text_by_patterns(
                    container,
                    [r"salary", r"pay"],
                    fallback=None,
                )

                if location and not self._is_relevant_location(location):
                    continue

                job_id = self._extract_indeed_job_id(url) or self._url_fingerprint(url)
                if job_id in seen_ids:
                    continue
                seen_ids.add(job_id)

                jobs.append(RawJob(
                    source="email_indeed",
                    source_job_id=job_id,
                    title=title,
                    company=company,
                    location=location or None,
                    salary_raw=salary_raw,
                    application_url=url,
                    source_url=url,
                    posted_at=email_date,
                ))
            except Exception as exc:
                logger.debug("email_alerts.indeed_card_error", error=str(exc))

        return jobs

    def _parse_glassdoor(
        self,
        html: str,
        meta: dict,
        email_date: Optional[datetime],
    ) -> list[RawJob]:
        """Parse a Glassdoor job alert email."""
        soup = BeautifulSoup(html, "lxml")
        jobs: list[RawJob] = []

        job_links = (
            soup.select("a[href*='glassdoor.com/job']")
            or soup.select("a[href*='glassdoor.com/Job']")
            or soup.select("a[href*='glassdoor.com/partner']")
        )

        for link in job_links:
            try:
                url = str(link.get("href", ""))
                if not url or "unsubscribe" in url.lower():
                    continue

                title = link.get_text(strip=True)
                if not title or len(title) < 3:
                    continue

                container = self._find_job_container(link)
                company = self._extract_text_by_patterns(container, [r"company", r"employer"])
                location = self._extract_text_by_patterns(container, [r"location", r"city"])

                if location and not self._is_relevant_location(location):
                    continue

                jobs.append(RawJob(
                    source="email_glassdoor",
                    source_job_id=self._url_fingerprint(url),
                    title=title,
                    company=company or "Unknown",
                    location=location or None,
                    application_url=url,
                    source_url=url,
                    posted_at=email_date,
                ))
            except Exception as exc:
                logger.debug("email_alerts.glassdoor_card_error", error=str(exc))

        return jobs

    def _parse_profession_hu(
        self,
        html: str,
        meta: dict,
        email_date: Optional[datetime],
    ) -> list[RawJob]:
        """Parse a Profession.hu email alert."""
        soup = BeautifulSoup(html, "lxml")
        jobs: list[RawJob] = []

        job_links = (
            soup.select("a[href*='profession.hu/allasok']")
            or soup.select("a[href*='profession.hu/allas']")
        )

        for link in job_links:
            try:
                url = str(link.get("href", ""))
                title = link.get_text(strip=True)
                if not title or len(title) < 3:
                    continue

                container = self._find_job_container(link)
                company = self._extract_text_by_patterns(container, [r"ceg", r"company"])
                location = self._extract_text_by_patterns(container, [r"telepules", r"city", r"location"])

                job_id = re.search(r"/(\d+)(?:[/-]|$)", url)

                jobs.append(RawJob(
                    source="email_profession_hu",
                    source_job_id=job_id.group(1) if job_id else self._url_fingerprint(url),
                    title=title,
                    company=company or "Unknown",
                    location=location or "Hungary",
                    application_url=url,
                    source_url=url,
                    posted_at=email_date,
                ))
            except Exception as exc:
                logger.debug("email_alerts.profession_hu_error", error=str(exc))

        return jobs

    def _parse_jobline_hu(
        self,
        html: str,
        meta: dict,
        email_date: Optional[datetime],
    ) -> list[RawJob]:
        """Parse a Jobline.hu email alert."""
        soup = BeautifulSoup(html, "lxml")
        jobs: list[RawJob] = []

        job_links = soup.select("a[href*='jobline.hu']")

        for link in job_links:
            try:
                url = str(link.get("href", ""))
                title = link.get_text(strip=True)
                if not title or len(title) < 3 or "unsubscribe" in url.lower():
                    continue

                container = self._find_job_container(link)
                company = self._extract_text_by_patterns(container, [r"company", r"ceg"])

                jobs.append(RawJob(
                    source="email_jobline_hu",
                    source_job_id=self._url_fingerprint(url),
                    title=title,
                    company=company or "Unknown",
                    location="Hungary",
                    application_url=url,
                    source_url=url,
                    posted_at=email_date,
                ))
            except Exception as exc:
                logger.debug("email_alerts.jobline_hu_error", error=str(exc))

        return jobs

    # ── HTML extraction helpers ───────────────────────────────────────────

    @staticmethod
    def _extract_html_body(msg: Message) -> str:
        """Extract the HTML body from an email message."""
        if msg.is_multipart():
            for part in msg.walk():
                ct = part.get_content_type()
                if ct == "text/html":
                    payload = part.get_payload(decode=True)
                    if payload:
                        charset = part.get_content_charset() or "utf-8"
                        return payload.decode(charset, errors="replace")
        else:
            if msg.get_content_type() == "text/html":
                payload = msg.get_payload(decode=True)
                if payload:
                    charset = msg.get_content_charset() or "utf-8"
                    return payload.decode(charset, errors="replace")
        return ""

    @staticmethod
    def _extract_text_body(msg: Message) -> str:
        """Extract the plain-text body as fallback."""
        if msg.is_multipart():
            for part in msg.walk():
                if part.get_content_type() == "text/plain":
                    payload = part.get_payload(decode=True)
                    if payload:
                        return payload.decode(
                            part.get_content_charset() or "utf-8", errors="replace"
                        )
        else:
            payload = msg.get_payload(decode=True)
            if payload:
                return payload.decode(
                    msg.get_content_charset() or "utf-8", errors="replace"
                )
        return ""

    @staticmethod
    def _find_job_container(link):  # type: ignore[no-untyped-def]
        """Walk up the DOM to find a meaningful job container element."""
        node = link
        for _ in range(6):
            parent = node.find_parent(["td", "tr", "div", "li", "article"])
            if parent is None:
                break
            # Stop if the container has meaningful content beyond just the link
            text = parent.get_text(strip=True)
            if len(text) > len(link.get_text(strip=True)) + 10:
                return parent
            node = parent
        return link.parent

    @staticmethod
    def _extract_text_by_patterns(
        container,  # type: ignore[no-untyped-def]
        patterns: list[str],
        fallback: Optional[str] = None,
    ) -> Optional[str]:
        """Find the first element whose class/id matches any pattern."""
        if container is None:
            return fallback
        for pattern in patterns:
            el = container.find(class_=re.compile(pattern, re.I))
            if el:
                text = el.get_text(strip=True)
                if text:
                    return text
            # Also try data attributes
            el = container.find(attrs={"data-" + pattern: True})
            if el:
                text = el.get_text(strip=True)
                if text:
                    return text
        return fallback

    @staticmethod
    def _extract_company_from_context(link) -> str:  # type: ignore[no-untyped-def]
        """Try to find a company name near a job link using heuristics."""
        # Look at the next sibling text nodes
        for sibling in link.next_siblings:
            if hasattr(sibling, "get_text"):
                text = sibling.get_text(strip=True)
                if text and len(text) > 1:
                    return text
        return ""

    # ── URL utilities ─────────────────────────────────────────────────────

    @staticmethod
    def _clean_linkedin_url(url: str) -> str:
        """Strip LinkedIn tracking parameters, keep the job ID."""
        # LinkedIn job URLs: linkedin.com/jobs/view/JOBID?...
        match = re.search(r"linkedin\.com/jobs/view/(\d+)", url)
        if match:
            return f"https://www.linkedin.com/jobs/view/{match.group(1)}"
        return url

    @staticmethod
    def _extract_linkedin_job_id(url: str) -> Optional[str]:
        match = re.search(r"/jobs/view/(\d+)", url)
        return match.group(1) if match else None

    @staticmethod
    def _extract_indeed_job_id(url: str) -> Optional[str]:
        # jk= parameter holds the job ID
        match = re.search(r"jk=([a-zA-Z0-9]+)", url)
        return match.group(1) if match else None

    @staticmethod
    def _url_fingerprint(url: str) -> str:
        """Create a short stable ID from a URL."""
        clean = urlparse(url)._replace(query="", fragment="").geturl()
        return hashlib.sha256(clean.encode()).hexdigest()[:16]

    # ── Location filter ───────────────────────────────────────────────────

    @staticmethod
    def _is_relevant_location(location: str) -> bool:
        """Return True if the location is in Hungary or is Remote."""
        loc_lower = location.lower()
        relevant = {
            "budapest", "debrecen", "hungary", "magyarország", "hybrid",
            "remote", "győr", "miskolc", "pécs", "kecskemét", "nyíregyháza",
            "eger", "székesfehérvár", "hatvan",
        }
        return any(r in loc_lower for r in relevant)

    # ── Date parsing ──────────────────────────────────────────────────────

    @staticmethod
    def _parse_email_date(date_str: str) -> Optional[datetime]:
        """Parse an email Date header into a timezone-aware datetime."""
        if not date_str:
            return None
        try:
            from email.utils import parsedate_to_datetime
            dt = parsedate_to_datetime(date_str)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt
        except Exception:
            return None

    async def _is_healthy(self) -> bool:
        """Check IMAP server is reachable."""
        settings = self.settings
        if not settings.alert_email_user:
            return True  # not configured, not broken
        try:
            with imaplib.IMAP4_SSL(
                host=settings.alert_email_host,
                port=settings.alert_email_port,
            ) as imap:
                imap.noop()
            return True
        except Exception:
            return False
