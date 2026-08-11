"""
Connector smoke tests.

Tests the connector interfaces and parsing logic without making real HTTP requests.
Uses monkeypatching and mock HTML responses.
"""
from __future__ import annotations

import pytest

from jobhunter.connectors.base import BaseConnector, RawJob
from jobhunter.connectors.boards.bosch import BoschCareersConnector
from jobhunter.connectors.boards.eures import EURESConnector
from jobhunter.connectors.boards.profession_hu import ProfessionHuConnector


# ── Base connector tests ──────────────────────────────────────────────────
class TestBaseConnector:

    def test_raw_job_defaults(self) -> None:
        job = RawJob(source="test", source_job_id="1", title="Intern", company="Corp")
        assert job.location is None
        assert job.description is None
        assert job.extra == {}

    def test_abstract_connector_cannot_be_instantiated(self) -> None:
        with pytest.raises(TypeError):
            BaseConnector()  # type: ignore[abstract]


# ── Profession.hu HTML parser ─────────────────────────────────────────────
class TestProfessionHuParser:
    """Test the HTML parser with synthetic job-listing pages."""

    SAMPLE_HTML = """
    <html><body>
    <article class="job-card" data-job-id="123456">
      <h2 class="job-title"><a href="/allasok/123456/mechatronikai-intern">Mechatronikai Intern</a></h2>
      <span class="company-name">Bosch Kft.</span>
      <span class="location">Budapest</span>
      <span class="posted-date">2 napja</span>
    </article>
    <article class="job-card" data-job-id="234567">
      <h2 class="job-title"><a href="/allasok/234567/robotika-intern">Robotika Intern</a></h2>
      <span class="company-name">Continental</span>
      <span class="location">Debrecen</span>
      <span class="posted-date">ma</span>
    </article>
    </body></html>
    """

    def test_parses_multiple_jobs(self) -> None:
        connector = ProfessionHuConnector()
        seen: set[str] = set()
        jobs = connector._parse_listing_page(self.SAMPLE_HTML, seen)
        assert len(jobs) == 2

    def test_extracts_title(self) -> None:
        connector = ProfessionHuConnector()
        seen: set[str] = set()
        jobs = connector._parse_listing_page(self.SAMPLE_HTML, seen)
        titles = [j.title for j in jobs]
        assert "Mechatronikai Intern" in titles

    def test_extracts_company(self) -> None:
        connector = ProfessionHuConnector()
        seen: set[str] = set()
        jobs = connector._parse_listing_page(self.SAMPLE_HTML, seen)
        companies = [j.company for j in jobs]
        assert "Bosch Kft." in companies or "Bosch" in companies

    def test_extracts_job_id(self) -> None:
        connector = ProfessionHuConnector()
        seen: set[str] = set()
        jobs = connector._parse_listing_page(self.SAMPLE_HTML, seen)
        ids = [j.source_job_id for j in jobs]
        assert "123456" in ids

    def test_deduplicates_within_batch(self) -> None:
        """Same job appearing twice in a page should only be extracted once."""
        doubled = self.SAMPLE_HTML + self.SAMPLE_HTML  # same HTML pasted twice
        connector = ProfessionHuConnector()
        seen: set[str] = set()
        jobs = connector._parse_listing_page(doubled, seen)
        ids = [j.source_job_id for j in jobs]
        assert len(ids) == len(set(ids))

    def test_parses_hungarian_date_napja(self) -> None:
        from datetime import datetime
        result = ProfessionHuConnector._parse_hungarian_date("3 napja")
        assert result is not None
        diff = datetime.utcnow() - result
        assert 2 <= diff.days <= 4

    def test_parses_hungarian_date_ma(self) -> None:
        from datetime import datetime
        result = ProfessionHuConnector._parse_hungarian_date("ma")
        assert result is not None
        assert result.date() == datetime.utcnow().date()

    def test_parses_hungarian_date_absolute(self) -> None:
        result = ProfessionHuConnector._parse_hungarian_date("2024.05.15")
        assert result is not None
        assert result.year == 2024
        assert result.month == 5
        assert result.day == 15

    def test_empty_html_returns_no_jobs(self) -> None:
        connector = ProfessionHuConnector()
        jobs = connector._parse_listing_page("<html><body></body></html>", set())
        assert jobs == []


# ── EURES API response parser ─────────────────────────────────────────────
class TestEURESParser:

    SAMPLE_RESPONSE = {
        "data": {
            "items": [
                {
                    "id": "EURES-001",
                    "title": "Mechatronics Engineer Intern",
                    "employer": {"name": "Siemens Hungary"},
                    "location": {"city": "Budapest", "country": "Hungary"},
                    "description": "Exciting internship for engineering students.",
                    "applicationUrl": "https://siemens.com/jobs/EURES-001",
                    "publicationStartDate": "2024-05-01T00:00:00Z",
                    "publicationEndDate": "2024-06-30T00:00:00Z",
                    "remoteWork": False,
                },
                {
                    "id": "EURES-002",
                    "title": "Automation Intern",
                    "employer": {"name": "ABB"},
                    "location": {"city": "Debrecen", "country": "Hungary"},
                    "applicationUrl": "https://abb.com/jobs/EURES-002",
                    "publicationStartDate": "2024-05-10T00:00:00Z",
                    "remoteWork": True,
                },
            ]
        }
    }

    def test_parses_jobs(self) -> None:
        connector = EURESConnector()
        jobs = connector._parse_api_response(self.SAMPLE_RESPONSE, set())
        assert len(jobs) == 2

    def test_extracts_title_and_company(self) -> None:
        connector = EURESConnector()
        jobs = connector._parse_api_response(self.SAMPLE_RESPONSE, set())
        assert any(j.title == "Mechatronics Engineer Intern" for j in jobs)
        assert any(j.company == "Siemens Hungary" for j in jobs)

    def test_extracts_location(self) -> None:
        connector = EURESConnector()
        jobs = connector._parse_api_response(self.SAMPLE_RESPONSE, set())
        locations = [j.location or "" for j in jobs]
        assert any("Budapest" in loc for loc in locations)

    def test_detects_remote_work(self) -> None:
        connector = EURESConnector()
        jobs = connector._parse_api_response(self.SAMPLE_RESPONSE, set())
        remote_jobs = [j for j in jobs if j.work_mode_raw == "remote"]
        assert len(remote_jobs) == 1

    def test_deduplicates_by_id(self) -> None:
        connector = EURESConnector()
        seen = {"EURES-001"}  # pre-seen
        jobs = connector._parse_api_response(self.SAMPLE_RESPONSE, seen)
        assert not any(j.source_job_id == "EURES-001" for j in jobs)

    def test_empty_response_returns_no_jobs(self) -> None:
        connector = EURESConnector()
        jobs = connector._parse_api_response({"data": {"items": []}}, set())
        assert jobs == []

    def test_parses_iso_date(self) -> None:
        result = EURESConnector._parse_iso_date("2024-05-01T00:00:00Z")
        assert result is not None
        assert result.year == 2024

    def test_parse_iso_date_invalid_returns_none(self) -> None:
        assert EURESConnector._parse_iso_date("not-a-date") is None
        assert EURESConnector._parse_iso_date(None) is None  # type: ignore[arg-type]


# ── Bosch JSON-LD parser ──────────────────────────────────────────────────
class TestBoschParser:

    SAMPLE_JSONLD_PAGE = """
    <html><head>
    <script type="application/ld+json">
    {
      "@type": "JobPosting",
      "title": "Robotics Intern",
      "identifier": {"value": "BOSCH-REQ-12345"},
      "hiringOrganization": {"name": "Robert Bosch GmbH"},
      "jobLocation": {
        "address": {
          "addressLocality": "Budapest",
          "addressCountry": "Hungary"
        }
      },
      "datePosted": "2024-05-01",
      "validThrough": "2024-07-01",
      "url": "https://careers.bosch.com/en/jobs/12345",
      "employmentType": "INTERN",
      "description": "<p>Exciting robotics internship.</p>"
    }
    </script>
    </head><body></body></html>
    """

    NON_HUNGARIAN_JSONLD = """
    <html><head>
    <script type="application/ld+json">
    {
      "@type": "JobPosting",
      "title": "Software Engineer",
      "hiringOrganization": {"name": "Bosch"},
      "jobLocation": {"address": {"addressLocality": "Stuttgart", "addressCountry": "Germany"}},
      "datePosted": "2024-05-01",
      "url": "https://careers.bosch.com/en/jobs/99999"
    }
    </script>
    </head><body></body></html>
    """

    def test_extracts_job_from_jsonld(self) -> None:
        connector = BoschCareersConnector()
        jobs = connector._extract_from_html(self.SAMPLE_JSONLD_PAGE, set())
        assert len(jobs) == 1
        assert jobs[0].title == "Robotics Intern"

    def test_extracts_company_name(self) -> None:
        connector = BoschCareersConnector()
        jobs = connector._extract_from_html(self.SAMPLE_JSONLD_PAGE, set())
        assert "Bosch" in jobs[0].company

    def test_extracts_location(self) -> None:
        connector = BoschCareersConnector()
        jobs = connector._extract_from_html(self.SAMPLE_JSONLD_PAGE, set())
        assert "Budapest" in (jobs[0].location or "")

    def test_filters_non_hungarian_locations(self) -> None:
        connector = BoschCareersConnector()
        jobs = connector._extract_from_html(self.NON_HUNGARIAN_JSONLD, set())
        assert len(jobs) == 0

    def test_extracts_job_type(self) -> None:
        connector = BoschCareersConnector()
        jobs = connector._extract_from_html(self.SAMPLE_JSONLD_PAGE, set())
        assert jobs[0].job_type_raw == "internship"

    def test_strips_html_from_description(self) -> None:
        connector = BoschCareersConnector()
        jobs = connector._extract_from_html(self.SAMPLE_JSONLD_PAGE, set())
        assert "<p>" not in (jobs[0].description or "")
        assert "robotics" in (jobs[0].description or "").lower()

    def test_is_hungarian_location_budapest(self) -> None:
        assert BoschCareersConnector._is_hungarian_location("Budapest, Hungary")

    def test_is_hungarian_location_germany_false(self) -> None:
        assert not BoschCareersConnector._is_hungarian_location("Stuttgart, Germany")

    def test_normalise_job_type_intern(self) -> None:
        assert BoschCareersConnector._normalise_job_type("INTERN") == "internship"

    def test_normalise_job_type_working_student(self) -> None:
        assert BoschCareersConnector._normalise_job_type("WORKING STUDENT") == "working_student"
