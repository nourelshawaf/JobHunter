"""Tests for the job normaliser."""

from __future__ import annotations

from datetime import datetime

import pytest

from jobhunter.connectors.base import RawJob
from jobhunter.models.job import JobType, WorkMode
from jobhunter.normalisation.normaliser import Normaliser


@pytest.fixture
def normaliser() -> Normaliser:
    return Normaliser()


def make_raw(**kwargs: object) -> RawJob:
    defaults = {
        "source": "test_source",
        "source_job_id": "test-001",
        "title": "Robotics Intern",
        "company": "Test Corp GmbH",
        "location": "Budapest, Hungary",
    }
    defaults.update(kwargs)
    return RawJob(**defaults)  # type: ignore[arg-type]


class TestNormaliser:

    def test_basic_normalisation(self, normaliser: Normaliser) -> None:
        raw = make_raw()
        job = normaliser.normalise(raw)
        assert job.title == "Robotics Intern"
        assert job.source == "test_source"

    def test_company_normalisation_strips_gmbh(self, normaliser: Normaliser) -> None:
        raw = make_raw(company="Robert Bosch GmbH")
        job = normaliser.normalise(raw)
        assert "GmbH" not in (job.company_normalized or "")
        assert "Bosch" in (job.company_normalized or "")

    def test_company_normalisation_strips_kft(self, normaliser: Normaliser) -> None:
        raw = make_raw(company="Siemens Kft.")
        job = normaliser.normalise(raw)
        assert "Kft" not in (job.company_normalized or "")

    def test_work_mode_remote_detection(self, normaliser: Normaliser) -> None:
        raw = make_raw(description="This is a fully remote position with home office")
        job = normaliser.normalise(raw)
        assert job.work_mode == WorkMode.REMOTE

    def test_work_mode_hybrid_detection(self, normaliser: Normaliser) -> None:
        raw = make_raw(description="Hybrid work arrangement available")
        job = normaliser.normalise(raw)
        assert job.work_mode == WorkMode.HYBRID

    def test_student_friendly_detection(self, normaliser: Normaliser) -> None:
        raw = make_raw(description="We are looking for a university student or fresh graduate")
        job = normaliser.normalise(raw)
        assert job.student_friendly is True

    def test_hungarian_mandatory_detection(self, normaliser: Normaliser) -> None:
        raw = make_raw(description="Fluent Hungarian is required for this role")
        job = normaliser.normalise(raw)
        assert job.hungarian_mandatory is True

    def test_job_type_internship_from_title(self, normaliser: Normaliser) -> None:
        raw = make_raw(title="Software Engineering Internship")
        job = normaliser.normalise(raw)
        assert job.job_type == JobType.INTERNSHIP

    def test_job_type_working_student(self, normaliser: Normaliser) -> None:
        raw = make_raw(title="Working Student Automation")
        job = normaliser.normalise(raw)
        assert job.job_type == JobType.WORKING_STUDENT

    def test_salary_parsing_huf_range(self, normaliser: Normaliser) -> None:
        raw = make_raw(salary_raw="400,000–500,000 HUF/month")
        job = normaliser.normalise(raw)
        assert job.salary_min == 400000.0
        assert job.salary_max == 500000.0
        assert job.salary_currency == "HUF"

    def test_salary_parsing_single_value(self, normaliser: Normaliser) -> None:
        raw = make_raw(salary_raw="435,000 HUF")
        job = normaliser.normalise(raw)
        assert job.salary_min == 435000.0
        assert job.salary_max is None

    def test_canonical_url_strips_utm(self, normaliser: Normaliser) -> None:
        raw = make_raw(
            application_url="https://careers.bosch.com/jobs/123?utm_source=linkedin&utm_medium=cpc"
        )
        job = normaliser.normalise(raw)
        assert "utm_source" not in (job.canonical_url or "")
        assert "123" in (job.canonical_url or "")

    def test_discovered_at_set_on_normalise(self, normaliser: Normaliser) -> None:
        before = datetime.utcnow()
        raw = make_raw()
        job = normaliser.normalise(raw)
        assert job.discovered_at is not None
        assert job.discovered_at >= before
