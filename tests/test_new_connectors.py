"""
Tests for Phase 3 company connectors: BMW, Baker Hughes, Siemens, Continental.

All tests use realistic saved fixtures (HTML/JSON) — no real network calls.
"""
from __future__ import annotations

import json

import pytest

from jobhunter.connectors.company.baker_hughes import BakerHughesCareersConnector
from jobhunter.connectors.company.bmw import BMWCareersConnector
from jobhunter.connectors.company.continental import ContinentalCareersConnector
from jobhunter.connectors.company.siemens import SiemensCareersConnector


# ── Fixtures ──────────────────────────────────────────────────────────────

BMW_API_RESPONSE = {
    "jobs": [
        {
            "id": "BMW-001",
            "name": "Mechatronics Engineering Intern",
            "company": "BMW Group Hungary",
            "location": {"city": "Debrecen", "country": "Hungary"},
            "applyUrl": "https://www.bmwgroup.jobs/de/en/jobfinder/job-description.BMW-001.html",
            "publishedAt": "2024-05-01T00:00:00Z",
            "type": "INTERN",
        },
        {
            "id": "BMW-002",
            "name": "Working Student Software Engineering",
            "company": "BMW Group",
            "location": {"city": "Munich", "country": "Germany"},  # filtered out
            "applyUrl": "https://www.bmwgroup.jobs/de/en/jobfinder/job-description.BMW-002.html",
            "type": "WORKING_STUDENT",
        },
    ]
}

BAKER_HUGHES_WORKDAY_RESPONSE = {
    "jobPostings": [
        {
            "externalId": "R166163",
            "title": "Electrical Engineering Intern",
            "locationsText": "Budapest, Hungary",
            "externalPath": "/en-US/jobs/R166163",
            "postedOn": "2024-05-10T00:00:00Z",
        },
        {
            "externalId": "R166164",
            "title": "Mechanical Engineering Intern",
            "locationsText": "Fót, Hungary",
            "externalPath": "/en-US/jobs/R166164",
        },
        {
            "externalId": "R166165",
            "title": "Senior Software Engineer",  # not an internship
            "locationsText": "Houston, TX, US",  # not Hungary
            "externalPath": "/en-US/jobs/R166165",
        },
    ]
}

SIEMENS_API_RESPONSE = {
    "results": [
        {
            "jobId": "SIE-001",
            "title": "Automation Engineering Intern",
            "city": "Budapest",
            "country": "Hungary",
            "detailUrl": "/jobs/SIE-001",
            "postedDate": "2024-05-05T00:00:00Z",
            "remote": False,
        },
        {
            "jobId": "SIE-002",
            "title": "Controls Engineering Student",
            "city": "Debrecen",
            "country": "Hungary",
            "detailUrl": "/jobs/SIE-002",
        },
    ]
}

CONTINENTAL_JSONLD_HTML = """
<html><head>
<script type="application/ld+json">
[
  {
    "@type": "JobPosting",
    "title": "Embedded Systems Intern",
    "identifier": {"value": "CON-001"},
    "hiringOrganization": {"name": "Continental"},
    "jobLocation": {"address": {"addressLocality": "Debrecen", "addressCountry": "Hungary"}},
    "datePosted": "2024-05-08",
    "validThrough": "2024-07-08",
    "url": "https://jobs.continental.com/en/detail/123/embedded-systems-intern",
    "employmentType": "INTERN",
    "description": "<p>Great embedded systems internship.</p>"
  },
  {
    "@type": "JobPosting",
    "title": "Software Engineer",
    "identifier": {"value": "CON-002"},
    "hiringOrganization": {"name": "Continental"},
    "jobLocation": {"address": {"addressLocality": "Frankfurt", "addressCountry": "Germany"}},
    "url": "https://jobs.continental.com/en/detail/456/software-engineer"
  }
]
</script>
</head><body></body></html>
"""


# ── BMW tests ─────────────────────────────────────────────────────────────

class TestBMWConnector:

    def test_parses_api_response(self) -> None:
        c = BMWCareersConnector()
        jobs = c._parse_api_response(BMW_API_RESPONSE, set())
        assert len(jobs) == 1  # Germany job filtered out
        assert jobs[0].title == "Mechatronics Engineering Intern"

    def test_filters_non_hungarian_locations(self) -> None:
        c = BMWCareersConnector()
        jobs = c._parse_api_response(BMW_API_RESPONSE, set())
        assert not any("Germany" in (j.location or "") for j in jobs)

    def test_extracts_job_id(self) -> None:
        c = BMWCareersConnector()
        jobs = c._parse_api_response(BMW_API_RESPONSE, set())
        assert jobs[0].source_job_id == "BMW-001"

    def test_extracts_application_url(self) -> None:
        c = BMWCareersConnector()
        jobs = c._parse_api_response(BMW_API_RESPONSE, set())
        assert "bmwgroup.jobs" in (jobs[0].application_url or "")

    def test_classifies_intern_correctly(self) -> None:
        assert BMWCareersConnector._classify_job_type("Mechatronics Engineering Intern") == "internship"

    def test_classifies_working_student(self) -> None:
        assert BMWCareersConnector._classify_job_type("Working Student Software Engineering") == "working_student"

    def test_is_hungarian_location_debrecen(self) -> None:
        assert BMWCareersConnector._is_hungarian_location("Debrecen, Hungary")

    def test_is_hungarian_location_munich_false(self) -> None:
        assert not BMWCareersConnector._is_hungarian_location("Munich, Germany")

    def test_deduplication_in_batch(self) -> None:
        c = BMWCareersConnector()
        seen = set()
        jobs1 = c._parse_api_response(BMW_API_RESPONSE, seen)
        jobs2 = c._parse_api_response(BMW_API_RESPONSE, seen)
        assert len(jobs2) == 0  # all IDs already seen

    def test_source_set_to_bmw_careers(self) -> None:
        c = BMWCareersConnector()
        jobs = c._parse_api_response(BMW_API_RESPONSE, set())
        assert all(j.source == "bmw_careers" for j in jobs)


# ── Baker Hughes tests ────────────────────────────────────────────────────

class TestBakerHughesConnector:

    def test_parses_workday_response(self) -> None:
        c = BakerHughesCareersConnector()
        jobs = c._parse_workday_response(BAKER_HUGHES_WORKDAY_RESPONSE, set())
        # Houston + senior role filtered out
        assert len(jobs) == 2

    def test_filters_non_hungarian(self) -> None:
        c = BakerHughesCareersConnector()
        jobs = c._parse_workday_response(BAKER_HUGHES_WORKDAY_RESPONSE, set())
        for job in jobs:
            assert any(h.lower() in (job.location or "").lower()
                      for h in ["Budapest", "Fót", "Hungary"])

    def test_extracts_requisition_id(self) -> None:
        c = BakerHughesCareersConnector()
        jobs = c._parse_workday_response(BAKER_HUGHES_WORKDAY_RESPONSE, set())
        ids = [j.source_job_id for j in jobs]
        assert "R166163" in ids

    def test_builds_application_url(self) -> None:
        c = BakerHughesCareersConnector()
        jobs = c._parse_workday_response(BAKER_HUGHES_WORKDAY_RESPONSE, set())
        for job in jobs:
            assert job.application_url and "bakerhughes.com" in job.application_url

    def test_classifies_intern(self) -> None:
        assert BakerHughesCareersConnector._classify_type("Electrical Engineering Intern") == "internship"

    def test_parses_posted_date(self) -> None:
        result = BakerHughesCareersConnector._parse_date("2024-05-10T00:00:00Z")
        assert result is not None
        assert result.year == 2024

    def test_source_is_baker_hughes_careers(self) -> None:
        c = BakerHughesCareersConnector()
        jobs = c._parse_workday_response(BAKER_HUGHES_WORKDAY_RESPONSE, set())
        assert all(j.source == "baker_hughes_careers" for j in jobs)


# ── Siemens tests ─────────────────────────────────────────────────────────

class TestSiemensConnector:

    def test_parses_api_response(self) -> None:
        c = SiemensCareersConnector()
        jobs = c._parse_api_response(SIEMENS_API_RESPONSE, set())
        assert len(jobs) == 2

    def test_extracts_location(self) -> None:
        c = SiemensCareersConnector()
        jobs = c._parse_api_response(SIEMENS_API_RESPONSE, set())
        locations = [j.location or "" for j in jobs]
        assert any("Budapest" in loc for loc in locations)
        assert any("Debrecen" in loc for loc in locations)

    def test_builds_url_from_path(self) -> None:
        c = SiemensCareersConnector()
        jobs = c._parse_api_response(SIEMENS_API_RESPONSE, set())
        for job in jobs:
            assert job.application_url and "siemens.com" in job.application_url

    def test_is_hungarian_budapest(self) -> None:
        assert SiemensCareersConnector._is_hungarian("Budapest, Hungary")

    def test_is_hungarian_berlin_false(self) -> None:
        assert not SiemensCareersConnector._is_hungarian("Berlin, Germany")

    def test_classify_intern(self) -> None:
        assert SiemensCareersConnector._classify_type("Automation Engineering Intern") == "internship"

    def test_source_is_siemens_careers(self) -> None:
        c = SiemensCareersConnector()
        jobs = c._parse_api_response(SIEMENS_API_RESPONSE, set())
        assert all(j.source == "siemens_careers" for j in jobs)


# ── Continental tests ─────────────────────────────────────────────────────

class TestContinentalConnector:

    def test_extracts_jobs_from_jsonld(self) -> None:
        c = ContinentalCareersConnector()
        jobs = c._extract_from_html(CONTINENTAL_JSONLD_HTML, set())
        assert len(jobs) == 1  # Frankfurt job filtered out

    def test_filters_non_hungarian(self) -> None:
        c = ContinentalCareersConnector()
        jobs = c._extract_from_html(CONTINENTAL_JSONLD_HTML, set())
        assert not any("Frankfurt" in (j.location or "") for j in jobs)

    def test_extracts_title(self) -> None:
        c = ContinentalCareersConnector()
        jobs = c._extract_from_html(CONTINENTAL_JSONLD_HTML, set())
        assert jobs[0].title == "Embedded Systems Intern"

    def test_strips_html_from_description(self) -> None:
        c = ContinentalCareersConnector()
        jobs = c._extract_from_html(CONTINENTAL_JSONLD_HTML, set())
        assert "<p>" not in (jobs[0].description or "")

    def test_extracts_deadline(self) -> None:
        c = ContinentalCareersConnector()
        jobs = c._extract_from_html(CONTINENTAL_JSONLD_HTML, set())
        assert jobs[0].deadline is not None
        assert jobs[0].deadline.year == 2024

    def test_source_is_continental_careers(self) -> None:
        c = ContinentalCareersConnector()
        jobs = c._extract_from_html(CONTINENTAL_JSONLD_HTML, set())
        assert all(j.source == "continental_careers" for j in jobs)

    def test_deduplication(self) -> None:
        c = ContinentalCareersConnector()
        seen = set()
        jobs1 = c._extract_from_html(CONTINENTAL_JSONLD_HTML, seen)
        jobs2 = c._extract_from_html(CONTINENTAL_JSONLD_HTML, seen)
        assert len(jobs2) == 0  # already seen


# ── Pipeline registry test ────────────────────────────────────────────────

class TestConnectorRegistry:

    def test_all_new_connectors_registered(self) -> None:
        from jobhunter.pipeline import CONNECTOR_REGISTRY
        expected = [
            "bmw_careers",
            "baker_hughes_careers",
            "siemens_careers",
            "continental_careers",
        ]
        for name in expected:
            assert name in CONNECTOR_REGISTRY, f"'{name}' not in CONNECTOR_REGISTRY"

    def test_registry_has_eight_connectors(self) -> None:
        from jobhunter.pipeline import CONNECTOR_REGISTRY
        assert len(CONNECTOR_REGISTRY) >= 8

    def test_all_connectors_extend_base(self) -> None:
        from jobhunter.connectors.base import BaseConnector
        from jobhunter.pipeline import CONNECTOR_REGISTRY
        for name, cls in CONNECTOR_REGISTRY.items():
            assert issubclass(cls, BaseConnector), f"{name} does not extend BaseConnector"
