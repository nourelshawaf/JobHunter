"""Tests for the deduplication engine."""

from __future__ import annotations

import pytest

from jobhunter.deduplication.engine import (
    DeduplicationEngine,
    make_fingerprint,
    _normalise_for_hash,
)
from jobhunter.models.job import Job


def make_job(
    title: str = "Engineering Intern",
    company: str = "Bosch",
    company_normalized: str = "Bosch",
    location: str = "Budapest",
    source: str = "bosch_careers",
    source_job_id: str = "12345",
) -> Job:
    """Create a minimal Job for deduplication testing."""
    job = Job()
    job.title = title
    job.company = company
    job.company_normalized = company_normalized
    job.location = location
    job.source = source
    job.source_job_id = source_job_id
    job.is_primary_listing = True
    return job


class TestFingerprint:
    """Unit tests for make_fingerprint()."""

    def test_same_job_same_fingerprint(self) -> None:
        job1 = make_job()
        job2 = make_job()
        assert make_fingerprint(job1) == make_fingerprint(job2)

    def test_different_company_different_fingerprint(self) -> None:
        job1 = make_job(company="Bosch", company_normalized="Bosch")
        job2 = make_job(company="Siemens", company_normalized="Siemens")
        assert make_fingerprint(job1) != make_fingerprint(job2)

    def test_different_title_different_fingerprint(self) -> None:
        job1 = make_job(title="Robotics Intern")
        job2 = make_job(title="Automation Intern")
        assert make_fingerprint(job1) != make_fingerprint(job2)

    def test_normalisation_strips_legal_suffixes(self) -> None:
        """GmbH and Kft. variations should produce the same fingerprint."""
        job1 = make_job(company="Bosch GmbH", company_normalized="Bosch")
        job2 = make_job(company="Bosch Kft.", company_normalized="Bosch")
        assert make_fingerprint(job1) == make_fingerprint(job2)


class TestNormaliseForHash:
    """Unit tests for _normalise_for_hash()."""

    def test_strips_punctuation(self) -> None:
        assert "engineering intern" in _normalise_for_hash("Engineering Intern!")

    def test_lowercases(self) -> None:
        result = _normalise_for_hash("BOSCH")
        assert result == result.lower()

    def test_removes_stopwords(self) -> None:
        result = _normalise_for_hash("the engineering intern")
        assert "the" not in result.split()

    def test_empty_string(self) -> None:
        assert _normalise_for_hash("") == ""


class TestDeduplicationEngineUnit:
    """Tests that don't require a real database session."""

    def test_fingerprint_is_set_on_new_job(self, mock_db: object) -> None:
        """A new job should get a fingerprint and a new group_id."""
        engine = DeduplicationEngine(mock_db)  # type: ignore[arg-type]
        job = make_job()
        result = engine.process(job)
        assert result.fingerprint is not None
        assert result.duplicate_group_id is not None
        assert result.is_primary_listing is True

    def test_same_fingerprint_reuses_group(self, mock_db: object) -> None:
        """Two jobs with the same fingerprint get the same group_id."""
        engine = DeduplicationEngine(mock_db)  # type: ignore[arg-type]

        job1 = make_job(source="profession_hu", source_job_id="1")
        job2 = make_job(source="eures", source_job_id="2")

        engine.process(job1)
        engine.process(job2)

        assert job1.duplicate_group_id == job2.duplicate_group_id


@pytest.fixture
def mock_db() -> object:
    """
    Minimal mock database session for unit tests.

    Replaces SQLAlchemy Session — only supports the query interface
    used by DeduplicationEngine in unit-test mode (no real DB needed).
    """

    class MockQuery:
        def filter(self, *args: object, **kwargs: object) -> "MockQuery":
            return self

        def all(self) -> list:
            return []

        def first(self) -> None:
            return None

    class MockDB:
        def query(self, *args: object) -> MockQuery:
            return MockQuery()

    return MockDB()
