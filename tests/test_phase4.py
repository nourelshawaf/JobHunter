"""
Phase 4 tests.

Covers:
- Database engine factory (SQLite + pool config)
- RSS connectors (generic, Jooble, Graduateland)
- New company connectors (Knorr-Bremse, Valeo, ZF, ABB)
- SAP SuccessFactors adapter (submit guard)
- Export module (CSV generation)
- CV tailoring keyword extraction
- All new connectors in pipeline registry
"""
from __future__ import annotations

import asyncio
import csv
import io
import json
import os

import pytest

os.environ.setdefault("JOBHUNTER_TESTING", "1")


# ── Database engine factory ───────────────────────────────────────────────

class TestDatabaseEngine:

    def test_sqlite_engine_creates(self) -> None:
        from jobhunter.database import _build_engine
        engine = _build_engine("sqlite:///./data/test_temp.db", is_sqlite=True)
        assert engine is not None
        engine.dispose()

    def test_inmemory_sqlite_works(self) -> None:
        # Import models so Base.metadata has all tables registered
        from jobhunter.models import application, job, notification, profile  # noqa: F401
        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker
        from sqlalchemy.pool import StaticPool
        from jobhunter.database import Base

        # StaticPool keeps the same in-memory DB alive across connections
        engine = create_engine(
            "sqlite:///:memory:",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(engine)
        Session = sessionmaker(bind=engine)
        db = Session()
        from jobhunter.models.job import Job
        count = db.query(Job).count()
        assert count == 0
        db.close()
        engine.dispose()

    def test_check_connection_returns_bool(self) -> None:
        from jobhunter.database import check_connection
        result = check_connection()
        assert isinstance(result, bool)

    def test_init_db_is_idempotent(self) -> None:
        from jobhunter.database import init_db
        init_db()
        init_db()  # should not raise on second call

    def test_session_local_yields_session(self) -> None:
        from jobhunter.database import SessionLocal
        from sqlalchemy.orm import Session
        db = SessionLocal()
        assert isinstance(db, Session)
        db.close()

    def test_pool_settings_in_config(self) -> None:
        from jobhunter.config import get_settings
        s = get_settings()
        assert s.db_pool_size >= 1
        assert s.db_max_overflow >= 0
        assert s.db_pool_recycle >= 60


# ── RSS connectors ────────────────────────────────────────────────────────

JOOBLE_RSS_XML = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
  <channel>
    <title>Jooble Jobs</title>
    <item>
      <title>Mechatronics Engineering Intern</title>
      <link>https://jooble.org/jdp/123456/intern+budapest</link>
      <description>Budapest, Hungary. Great internship at Bosch.</description>
      <author>Bosch Hungary</author>
      <pubDate>Wed, 15 May 2024 10:00:00 +0000</pubDate>
    </item>
    <item>
      <title>Senior Software Engineer</title>
      <link>https://jooble.org/jdp/654321/senior+berlin</link>
      <description>Berlin, Germany. Senior role at Deutsche Telekom.</description>
      <author>Deutsche Telekom</author>
      <pubDate>Wed, 15 May 2024 10:00:00 +0000</pubDate>
    </item>
  </channel>
</rss>"""

GRADUATELAND_API_RESPONSE = {
    "results": [
        {
            "id": "GL-001",
            "title": "Embedded Systems Intern",
            "company": {"name": "Continental"},
            "locations": [{"city": "Debrecen", "country": "Hungary"}],
            "url": "https://graduateland.com/jobs/GL-001",
            "publishedAt": "2024-05-10T00:00:00Z",
        },
        {
            "id": "GL-002",
            "title": "Data Analyst",
            "company": {"name": "SomeCorp"},
            "locations": [{"city": "Paris", "country": "France"}],
            "url": "https://graduateland.com/jobs/GL-002",
        },
    ]
}


class TestJoobleRSSConnector:

    def test_parses_rss_entries(self) -> None:
        import feedparser
        from jobhunter.connectors.boards.rss import JoobleRSSConnector, _parse_rss_date
        feed = feedparser.parse(JOOBLE_RSS_XML)
        assert len(feed.entries) == 2

    def test_connector_has_name(self) -> None:
        from jobhunter.connectors.boards.rss import JoobleRSSConnector
        assert JoobleRSSConnector.name == "jooble_rss"

    def test_connector_has_search_combos(self) -> None:
        from jobhunter.connectors.boards.rss import JoobleRSSConnector
        assert len(JoobleRSSConnector.SEARCH_COMBOS) >= 4

    def test_is_hungarian_function(self) -> None:
        from jobhunter.connectors.boards.rss import _is_hungarian
        assert _is_hungarian("Budapest internship")
        assert not _is_hungarian("Berlin Germany office")
        assert _is_hungarian("Remote position Hungary")


class TestGraduatelandConnector:

    def test_parses_api_response(self) -> None:
        from jobhunter.connectors.boards.rss import GraduatelandConnector
        c = GraduatelandConnector()
        jobs = c._parse_api_response(GRADUATELAND_API_RESPONSE, set())
        assert len(jobs) == 1  # Paris filtered out
        assert jobs[0].title == "Embedded Systems Intern"

    def test_filters_non_hungarian(self) -> None:
        from jobhunter.connectors.boards.rss import GraduatelandConnector
        c = GraduatelandConnector()
        jobs = c._parse_api_response(GRADUATELAND_API_RESPONSE, set())
        assert all("France" not in (j.location or "") for j in jobs)

    def test_extracts_company_from_nested_obj(self) -> None:
        from jobhunter.connectors.boards.rss import GraduatelandConnector
        c = GraduatelandConnector()
        jobs = c._parse_api_response(GRADUATELAND_API_RESPONSE, set())
        assert jobs[0].company == "Continental"

    def test_deduplication(self) -> None:
        from jobhunter.connectors.boards.rss import GraduatelandConnector
        c = GraduatelandConnector()
        seen = set()
        j1 = c._parse_api_response(GRADUATELAND_API_RESPONSE, seen)
        j2 = c._parse_api_response(GRADUATELAND_API_RESPONSE, seen)
        assert len(j2) == 0

    def test_source_set(self) -> None:
        from jobhunter.connectors.boards.rss import GraduatelandConnector
        c = GraduatelandConnector()
        jobs = c._parse_api_response(GRADUATELAND_API_RESPONSE, set())
        assert all(j.source == "graduateland" for j in jobs)


# ── Knorr-Bremse, Valeo, ZF, ABB connectors ──────────────────────────────

KB_HTML = """<html><head>
<script type="application/ld+json">
[{"@type":"JobPosting","title":"Mechatronics Intern","identifier":{"value":"KB-001"},
  "hiringOrganization":{"name":"Knorr-Bremse"},
  "jobLocation":{"address":{"addressLocality":"Budapest","addressCountry":"Hungary"}},
  "url":"https://jobs.knorr-bremse.com/jobs/KB-001","datePosted":"2024-05-01"}]
</script></head><body></body></html>"""

VALEO_API_RESPONSE = {
    "requisitionList": [
        {
            "requisitionId": "VAL-001",
            "externalTitle": "Automation Engineering Intern",
            "locationDescription": "Budapest, Hungary",
            "detailUrl": "https://jobs.valeo.com/jobs/VAL-001",
            "startDate": "2024-05-01T00:00:00Z",
        }
    ]
}

ZF_API_RESPONSE = {
    "results": [
        {
            "jobId": "ZF-001",
            "title": "Controls Engineering Intern",
            "city": "Debrecen",
            "country": "Hungary",
            "detailUrl": "/jobs/ZF-001",
        }
    ]
}

ABB_WORKDAY_RESPONSE = {
    "jobPostings": [
        {
            "externalId": "ABB-001",
            "title": "Electrical Engineering Intern",
            "locationsText": "Budapest, Hungary",
            "externalPath": "/en-US/jobs/ABB-001",
            "postedOn": "2024-05-05T00:00:00Z",
        }
    ]
}


class TestKnorrBremsConnector:

    def test_extracts_from_jsonld(self) -> None:
        from jobhunter.connectors.company.knorr_bremse import KnorrBremseCareersConnector
        c = KnorrBremseCareersConnector()
        jobs = c._extract_jsonld(KB_HTML, set())
        assert len(jobs) == 1
        assert jobs[0].title == "Mechatronics Intern"
        assert jobs[0].company == "Knorr-Bremse"

    def test_location_filter(self) -> None:
        from jobhunter.connectors.company.knorr_bremse import KnorrBremseCareersConnector
        c = KnorrBremseCareersConnector()
        jobs = c._extract_jsonld(KB_HTML, set())
        assert "Budapest" in (jobs[0].location or "")

    def test_source_name(self) -> None:
        from jobhunter.connectors.company.knorr_bremse import KnorrBremseCareersConnector
        assert KnorrBremseCareersConnector.name == "knorr_bremse_careers"


class TestValeoCareersConnector:

    def test_parses_taleo_response(self) -> None:
        from jobhunter.connectors.company.valeo_zf_abb import ValeoCareersConnector
        c = ValeoCareersConnector()
        jobs = c._parse_taleo(VALEO_API_RESPONSE, set())
        assert len(jobs) == 1
        assert jobs[0].title == "Automation Engineering Intern"

    def test_source_name(self) -> None:
        from jobhunter.connectors.company.valeo_zf_abb import ValeoCareersConnector
        assert ValeoCareersConnector.name == "valeo_careers"


class TestZFCareersConnector:

    def test_parses_sap_response(self) -> None:
        from jobhunter.connectors.company.valeo_zf_abb import ZFCareersConnector
        c = ZFCareersConnector()
        jobs = c._parse_sap(ZF_API_RESPONSE, set())
        assert len(jobs) == 1
        assert "Controls" in jobs[0].title

    def test_builds_url_from_path(self) -> None:
        from jobhunter.connectors.company.valeo_zf_abb import ZFCareersConnector
        c = ZFCareersConnector()
        jobs = c._parse_sap(ZF_API_RESPONSE, set())
        assert jobs[0].application_url and "careers.zf.com" in jobs[0].application_url


class TestABBCareersConnector:

    def test_parses_workday_response(self) -> None:
        from jobhunter.connectors.company.valeo_zf_abb import ABBCareersConnector
        c = ABBCareersConnector()
        jobs = c._parse_workday(ABB_WORKDAY_RESPONSE, set())
        assert len(jobs) == 1
        assert jobs[0].title == "Electrical Engineering Intern"

    def test_source_name(self) -> None:
        from jobhunter.connectors.company.valeo_zf_abb import ABBCareersConnector
        assert ABBCareersConnector.name == "abb_careers"


# ── Full registry test ────────────────────────────────────────────────────

class TestFullConnectorRegistry:

    def test_all_14_connectors_registered(self) -> None:
        from jobhunter.pipeline import CONNECTOR_REGISTRY
        expected = [
            "profession_hu", "eures", "bosch_careers", "bmw_careers",
            "baker_hughes_careers", "siemens_careers", "continental_careers",
            "email_alerts", "knorr_bremse_careers", "valeo_careers",
            "zf_careers", "abb_careers", "jooble_rss", "graduateland",
        ]
        for name in expected:
            assert name in CONNECTOR_REGISTRY, f"'{name}' missing from registry"

    def test_registry_length(self) -> None:
        from jobhunter.pipeline import CONNECTOR_REGISTRY
        assert len(CONNECTOR_REGISTRY) >= 14

    def test_all_extend_base_connector(self) -> None:
        from jobhunter.connectors.base import BaseConnector
        from jobhunter.pipeline import CONNECTOR_REGISTRY
        for name, cls in CONNECTOR_REGISTRY.items():
            assert issubclass(cls, BaseConnector), f"{name} does not extend BaseConnector"


# ── SAP SuccessFactors adapter ────────────────────────────────────────────

class TestSAPAdapter:

    @pytest.fixture
    def sap_adapter(self) -> object:
        from jobhunter.application.adapters.sap_successfactors import SAPSuccessFactorsAdapter
        return SAPSuccessFactorsAdapter(profile={
            "full_name": "Noureldeen Elshawaf",
            "email": "nour@example.com",
            "city": "Budapest",
        })

    def test_sap_is_registered(self) -> None:
        # Import the adapter module to trigger @AdapterRegistry.register decorator
        import jobhunter.application.adapters.sap_successfactors  # noqa: F401
        import jobhunter.application.adapters.workday  # noqa: F401
        import jobhunter.application.adapters.greenhouse  # noqa: F401
        from jobhunter.application.base_adapter import AdapterRegistry
        assert "sap_successfactors" in AdapterRegistry.all_names()

    def test_detect_from_successfactors_url(self) -> None:
        import jobhunter.application.adapters.sap_successfactors  # noqa: F401
        from jobhunter.application.base_adapter import AdapterRegistry
        from jobhunter.application.adapters.sap_successfactors import SAPSuccessFactorsAdapter
        # successfactors.com is in the URL_PATTERNS list
        url = "https://www.successfactors.com/en_US/company/jobs/123"
        cls = AdapterRegistry.detect(url)
        assert cls is SAPSuccessFactorsAdapter

    def test_run_returns_session(self, sap_adapter) -> None:
        from jobhunter.application.base_adapter import ApplicationSession
        session = asyncio.run(sap_adapter.run("sap-001", "https://successfactors.com/jobs/1"))
        assert isinstance(session, ApplicationSession)

    def test_never_submits(self, sap_adapter) -> None:
        session = asyncio.run(sap_adapter.run("sap-002", "https://successfactors.com/jobs/2"))
        assert session.status not in {"submitted", "completed", "applied"}

    def test_submit_guard_applies(self, sap_adapter) -> None:
        from jobhunter.application.base_adapter import SubmitGuardError
        with pytest.raises(SubmitGuardError):
            asyncio.run(sap_adapter._safe_click(None, label="Submit Application"))

    def test_sensitive_fields_marked(self, sap_adapter) -> None:
        session = asyncio.run(sap_adapter.run("sap-003", "https://successfactors.com/jobs/3"))
        summary = session.to_summary()
        sensitive = [f["label"] for f in summary["fields_needing_user_input"]]
        assert any("authoris" in s.lower() or "sponsor" in s.lower() or "salary" in s.lower()
                   for s in sensitive)

    def test_mock_fields_include_sensitive(self) -> None:
        from jobhunter.application.adapters.sap_successfactors import SAPSuccessFactorsAdapter
        fields = SAPSuccessFactorsAdapter._mock_sap_fields()
        sensitive = [f for f in fields if f.is_sensitive]
        assert len(sensitive) >= 4


# ── Export module ─────────────────────────────────────────────────────────

class TestExporter:

    def test_csv_string_returns_string(self, db) -> None:
        from jobhunter.export import Exporter
        exporter = Exporter(db)
        result = exporter.to_csv_string()
        assert isinstance(result, str)

    def test_csv_has_header_row(self, db) -> None:
        from jobhunter.export import Exporter, EXPORT_COLUMNS
        exporter = Exporter(db)
        csv_text = exporter.to_csv_string()
        reader = csv.reader(io.StringIO(csv_text))
        header = next(reader)
        assert header == EXPORT_COLUMNS

    def test_csv_with_jobs(self, db, make_job) -> None:
        from jobhunter.export import Exporter
        make_job(title="Test Intern", company="TestCo", relevance_score=80)
        db.commit()
        exporter = Exporter(db)
        csv_text = exporter.to_csv_string()
        assert "Test Intern" in csv_text
        assert "TestCo" in csv_text

    def test_to_csv_writes_file(self, db, tmp_path) -> None:
        from jobhunter.export import Exporter
        exporter = Exporter(db)
        output = tmp_path / "test_export.csv"
        path = exporter.to_csv(path=output)
        assert path.exists()

    def test_summary_stats_returns_dict(self, db) -> None:
        from jobhunter.export import Exporter
        stats = Exporter(db).summary_stats()
        assert "total" in stats
        assert "by_status" in stats
        assert "avg_score" in stats
        assert "top_jobs" in stats

    def test_min_score_filter(self, db, make_job) -> None:
        from jobhunter.export import Exporter
        make_job(title="Low Score Job", relevance_score=20)
        make_job(title="High Score Job", relevance_score=90)
        db.commit()
        exporter = Exporter(db)
        csv_text = exporter.to_csv_string(min_score=75)
        assert "High Score Job" in csv_text
        assert "Low Score Job" not in csv_text


# ── CV tailor keyword extraction ──────────────────────────────────────────

class TestCVTailor:

    @pytest.fixture
    def tailor(self) -> object:
        from jobhunter.cv_tailor import CVTailor
        return CVTailor()

    def test_extract_keywords_python(self) -> None:
        from jobhunter.cv_tailor import extract_keywords
        kws = extract_keywords("Proficient in Python and C++")
        assert "python" in kws
        assert "c++" in kws

    def test_extract_keywords_ros2(self) -> None:
        from jobhunter.cv_tailor import extract_keywords
        kws = extract_keywords("Experience with ROS 2 and OpenCV")
        assert any("ros" in k for k in kws)  # matches "ros", "ros2", or "ros 2"
        assert "opencv" in kws

    def test_extract_keywords_deduplicated(self) -> None:
        from jobhunter.cv_tailor import extract_keywords
        kws = extract_keywords("python python python")
        assert kws.count("python") == 1

    def test_extract_empty_returns_empty(self) -> None:
        from jobhunter.cv_tailor import extract_keywords
        assert extract_keywords("") == []

    def test_tailor_finds_missing_keywords(self, tailor) -> None:
        cv = "Experienced with Python and Arduino"
        jd = "Looking for Python, C++, ROS 2, and OpenCV experience"
        result = tailor.tailor(cv, jd, use_ai=False)
        assert "c++" in result.keywords_missing or "ros" in " ".join(result.keywords_missing).lower()

    def test_tailor_finds_matching_keywords(self, tailor) -> None:
        cv = "Proficient in Python and C++"
        jd = "Requires Python, C++, and embedded systems"
        result = tailor.tailor(cv, jd, use_ai=False)
        assert "python" in result.keywords_found
        assert "c++" in result.keywords_found  # now captured by updated regex

    def test_ats_score_good_when_high_overlap(self, tailor) -> None:
        cv = "Python C++ ROS 2 OpenCV embedded systems Arduino"
        jd = "Requires Python C++ ROS 2"
        result = tailor.tailor(cv, jd, use_ai=False)
        assert result.ats_score_estimate == "good"

    def test_ats_score_poor_when_low_overlap(self, tailor) -> None:
        cv = "Microsoft Word Excel PowerPoint"
        jd = "Python C++ ROS 2 embedded systems OpenCV mechatronics"
        result = tailor.tailor(cv, jd, use_ai=False)
        assert result.ats_score_estimate in ("poor", "fair")

    def test_summary_string_not_empty(self, tailor) -> None:
        result = tailor.tailor("Python developer", "Python ROS C++", use_ai=False)
        assert len(result.summary) > 0

    def test_no_ai_returns_gaps_not_suggestions(self, tailor) -> None:
        cv = "Python developer"
        jd = "Requires C++ and ROS 2"
        result = tailor.tailor(cv, jd, use_ai=False)
        # Without AI, missing keywords become gaps
        assert len(result.suggestions) == 0
        assert len(result.gaps) > 0

    def test_tailor_result_has_all_fields(self, tailor) -> None:
        from jobhunter.cv_tailor import TailoredCV
        result = tailor.tailor("Python", "Python C++", use_ai=False)
        assert isinstance(result, TailoredCV)
        assert isinstance(result.keywords_found, list)
        assert isinstance(result.keywords_missing, list)
        assert isinstance(result.gaps, list)
        assert isinstance(result.suggestions, list)


# ── CLI smoke tests for new Phase 4 commands ──────────────────────────────

class TestPhase4CLI:

    def test_export_csv_command_registered(self) -> None:
        from jobhunter.cli import app
        assert "export-csv" in app.commands

    def test_export_sheets_command_registered(self) -> None:
        from jobhunter.cli import app
        assert "export-sheets" in app.commands

    def test_all_7_cli_commands_present(self) -> None:
        from jobhunter.cli import app
        expected = [
            "migrate", "ingest", "serve", "status", "test-email",
            "scheduler", "security-check", "export-csv", "export-sheets",
        ]
        for cmd in expected:
            assert cmd in app.commands, f"CLI command '{cmd}' missing"
