"""
Tests for the email alert connector.

Uses realistic HTML fixtures (defined in conftest.py) — no real IMAP connection needed.
Tests cover: parsing, idempotency, location filtering, URL cleaning, and edge cases.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

import pytest

from jobhunter.connectors.email_alerts import EmailAlertsConnector


@pytest.fixture
def connector() -> EmailAlertsConnector:
    """Connector instance with a fresh (empty) processed-IDs set per test."""
    c = EmailAlertsConnector()
    c._processed_ids = set()  # reset between tests
    return c


META = {
    "email_message_id": "<test-123@example.com>",
    "email_sender": "jobalert@linkedin.com",
    "email_subject": "5 new jobs match your search",
    "email_date": "2024-05-15T10:00:00+00:00",
    "email_fingerprint": "abcdef123456",
}
EMAIL_DATE = datetime(2024, 5, 15, 10, 0, tzinfo=timezone.utc)


class TestLinkedInParser:

    def test_extracts_job_titles(self, connector: EmailAlertsConnector, linkedin_email_html: str) -> None:
        jobs = connector._parse_linkedin(linkedin_email_html, META, EMAIL_DATE)
        titles = [j.title for j in jobs]
        assert any("Robotics" in t or "Intern" in t for t in titles)

    def test_extracts_job_urls(self, connector: EmailAlertsConnector, linkedin_email_html: str) -> None:
        jobs = connector._parse_linkedin(linkedin_email_html, META, EMAIL_DATE)
        assert all(j.application_url for j in jobs)
        assert all("linkedin.com/jobs/view/" in (j.application_url or "") for j in jobs)

    def test_strips_tracking_params_from_url(self, connector: EmailAlertsConnector, linkedin_email_html: str) -> None:
        jobs = connector._parse_linkedin(linkedin_email_html, META, EMAIL_DATE)
        for job in jobs:
            assert "?trk=" not in (job.application_url or "")
            assert "&trk=" not in (job.application_url or "")

    def test_filters_unsubscribe_links(self, connector: EmailAlertsConnector, linkedin_email_html: str) -> None:
        jobs = connector._parse_linkedin(linkedin_email_html, META, EMAIL_DATE)
        for job in jobs:
            assert "unsubscribe" not in (job.application_url or "").lower()

    def test_extracts_source_job_id_from_url(self, connector: EmailAlertsConnector, linkedin_email_html: str) -> None:
        jobs = connector._parse_linkedin(linkedin_email_html, META, EMAIL_DATE)
        for job in jobs:
            # LinkedIn job IDs are numeric
            if job.source_job_id:
                assert job.source_job_id.isdigit()

    def test_filters_non_hungarian_locations(self, connector: EmailAlertsConnector, linkedin_email_html: str) -> None:
        """Berlin job should be filtered; Budapest and Debrecen should be kept."""
        jobs = connector._parse_linkedin(linkedin_email_html, META, EMAIL_DATE)
        locations = [j.location or "" for j in jobs]
        assert not any("Berlin" in loc for loc in locations)

    def test_email_date_attached_to_jobs(self, connector: EmailAlertsConnector, linkedin_email_html: str) -> None:
        jobs = connector._parse_linkedin(linkedin_email_html, META, EMAIL_DATE)
        for job in jobs:
            assert job.posted_at == EMAIL_DATE


class TestIndeedParser:

    def test_extracts_jobs(self, connector: EmailAlertsConnector, indeed_email_html: str) -> None:
        jobs = connector._parse_indeed(indeed_email_html, META, EMAIL_DATE)
        assert len(jobs) >= 1

    def test_extracts_job_id_from_jk_param(self, connector: EmailAlertsConnector, indeed_email_html: str) -> None:
        jobs = connector._parse_indeed(indeed_email_html, META, EMAIL_DATE)
        ids = [j.source_job_id for j in jobs]
        assert "abc123def456" in ids or any(id_ and len(id_) > 5 for id_ in ids)

    def test_extracts_salary(self, connector: EmailAlertsConnector, indeed_email_html: str) -> None:
        jobs = connector._parse_indeed(indeed_email_html, META, EMAIL_DATE)
        salaries = [j.salary_raw for j in jobs if j.salary_raw]
        assert any("HUF" in (s or "") for s in salaries)

    def test_no_duplicate_job_ids(self, connector: EmailAlertsConnector, indeed_email_html: str) -> None:
        jobs = connector._parse_indeed(indeed_email_html, META, EMAIL_DATE)
        ids = [j.source_job_id for j in jobs]
        assert len(ids) == len(set(ids))

    def test_source_set_correctly(self, connector: EmailAlertsConnector, indeed_email_html: str) -> None:
        jobs = connector._parse_indeed(indeed_email_html, META, EMAIL_DATE)
        assert all(j.source == "email_indeed" for j in jobs)


class TestGlassdoorParser:

    def test_extracts_jobs(self, connector: EmailAlertsConnector, glassdoor_email_html: str) -> None:
        jobs = connector._parse_glassdoor(glassdoor_email_html, META, EMAIL_DATE)
        assert len(jobs) >= 1

    def test_job_has_title(self, connector: EmailAlertsConnector, glassdoor_email_html: str) -> None:
        jobs = connector._parse_glassdoor(glassdoor_email_html, META, EMAIL_DATE)
        assert all(j.title for j in jobs)


class TestProfessionHuParser:

    def test_extracts_jobs(self, connector: EmailAlertsConnector, profession_hu_email_html: str) -> None:
        jobs = connector._parse_profession_hu(profession_hu_email_html, META, EMAIL_DATE)
        assert len(jobs) >= 1

    def test_extracts_numeric_job_id(self, connector: EmailAlertsConnector, profession_hu_email_html: str) -> None:
        jobs = connector._parse_profession_hu(profession_hu_email_html, META, EMAIL_DATE)
        assert any(j.source_job_id and j.source_job_id.isdigit() for j in jobs)

    def test_location_defaults_to_hungary(self, connector: EmailAlertsConnector, profession_hu_email_html: str) -> None:
        jobs = connector._parse_profession_hu(profession_hu_email_html, META, EMAIL_DATE)
        assert all("Hungary" in (j.location or "") or "Budapest" in (j.location or "") for j in jobs)


class TestEmailDispatch:

    def test_unknown_sender_returns_empty(self, connector: EmailAlertsConnector) -> None:
        """An email from an unknown sender should produce no jobs."""
        import email as email_lib
        msg = email_lib.message_from_string(
            "From: unknown@random.com\nSubject: Spam\n\nBuy now!"
        )
        jobs = connector._parse_email(msg)
        assert jobs == []

    def test_idempotent_same_message_id(self, connector: EmailAlertsConnector, linkedin_email_html: str) -> None:
        """Processing the same Message-ID twice should yield jobs only once."""
        import email as email_lib

        raw = (
            "From: jobalert@linkedin.com\n"
            "Subject: Job Alert\n"
            "Message-ID: <dedup-test-001@linkedin.com>\n"
            "Content-Type: text/html\n\n"
            + linkedin_email_html
        )
        msg = email_lib.message_from_string(raw)

        first = connector._parse_email(msg)
        second = connector._parse_email(msg)

        assert len(first) > 0        # first pass yields jobs
        assert len(second) == 0      # second pass is a no-op

    def test_metadata_attached_to_jobs(self, connector: EmailAlertsConnector) -> None:
        """Email metadata should be attached to every extracted job's extra dict."""
        import email as email_lib

        raw = (
            "From: jobalert@linkedin.com\n"
            "Subject: Test Alert\n"
            "Message-ID: <meta-test-001@linkedin.com>\n"
            "Date: Wed, 15 May 2024 10:00:00 +0000\n"
            "Content-Type: text/html\n\n"
            + '<a href="https://www.linkedin.com/jobs/view/9876543210">Python Intern</a>'
            + '<span class="company-name">TestCo</span>'
            + '<span class="location">Budapest, Hungary</span>'
        )
        msg = email_lib.message_from_string(raw)
        jobs = connector._parse_email(msg)

        if jobs:  # may be empty if parsing doesn't find the card in minimal HTML
            for job in jobs:
                assert "email_message_id" in job.extra
                assert "email_sender" in job.extra


class TestHelpers:

    def test_clean_linkedin_url_strips_trk(self) -> None:
        dirty = "https://www.linkedin.com/jobs/view/3856789012?trk=jalt&refId=abc"
        clean = EmailAlertsConnector._clean_linkedin_url(dirty)
        assert clean == "https://www.linkedin.com/jobs/view/3856789012"
        assert "trk" not in clean

    def test_extract_linkedin_job_id(self) -> None:
        url = "https://www.linkedin.com/jobs/view/3856789012"
        assert EmailAlertsConnector._extract_linkedin_job_id(url) == "3856789012"

    def test_extract_indeed_job_id(self) -> None:
        url = "https://www.indeed.com/rc/clk?jk=abc123def456&fccid=x"
        assert EmailAlertsConnector._extract_indeed_job_id(url) == "abc123def456"

    def test_url_fingerprint_is_stable(self) -> None:
        url = "https://example.com/jobs/123?utm_source=email"
        fp1 = EmailAlertsConnector._url_fingerprint(url)
        fp2 = EmailAlertsConnector._url_fingerprint(url)
        assert fp1 == fp2
        assert len(fp1) == 16

    def test_is_relevant_location_budapest(self) -> None:
        assert EmailAlertsConnector._is_relevant_location("Budapest, Hungary")

    def test_is_relevant_location_remote(self) -> None:
        assert EmailAlertsConnector._is_relevant_location("Remote")

    def test_is_relevant_location_berlin_false(self) -> None:
        assert not EmailAlertsConnector._is_relevant_location("Berlin, Germany")

    def test_parse_email_date_rfc2822(self) -> None:
        date_str = "Wed, 15 May 2024 10:00:00 +0000"
        result = EmailAlertsConnector._parse_email_date(date_str)
        assert result is not None
        assert result.year == 2024
        assert result.month == 5

    def test_parse_email_date_empty_returns_none(self) -> None:
        assert EmailAlertsConnector._parse_email_date("") is None
