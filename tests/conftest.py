"""
Shared pytest fixtures.

- In-memory SQLite database (isolated per test)
- Pre-built Job factory
- Realistic HTML email bodies for email-parsing tests
"""
from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session, sessionmaker

from jobhunter.database import Base
from jobhunter.models.job import Job, JobStatus, JobType, WorkMode


# ── Ensure config.yaml exists for tests ──────────────────────────────────
def pytest_configure(config: object) -> None:
    root = Path(__file__).parent.parent
    yaml = root / "config.yaml"
    example = root / "config.example.yaml"
    if not yaml.exists() and example.exists():
        shutil.copy(example, yaml)


# ── In-memory database per test ───────────────────────────────────────────
@pytest.fixture
def db() -> Session:
    """Isolated in-memory SQLite session — rolls back after each test."""
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
    )

    @event.listens_for(engine, "connect")
    def _fk(conn, _):  # type: ignore[no-untyped-def]
        conn.execute("PRAGMA foreign_keys=ON")

    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autocommit=False, autoflush=False)
    session = factory()
    try:
        yield session
    finally:
        session.close()
        Base.metadata.drop_all(engine)


# ── Job factory ───────────────────────────────────────────────────────────
@pytest.fixture
def make_job(db: Session):  # type: ignore[no-untyped-def]
    """Return a callable that creates and persists a Job in the test DB."""

    def _factory(**kwargs: object) -> Job:
        defaults: dict = {
            "title": "Engineering Intern",
            "company": "Test Corp",
            "company_normalized": "Test Corp",
            "source": "test",
            "source_job_id": None,
            "location": "Budapest, Hungary",
            "description": "A great internship position.",
            "requirements": "",
            "job_type": JobType.INTERNSHIP,
            "work_mode": WorkMode.ONSITE,
            "student_friendly": True,
            "hungarian_mandatory": False,
            "relevance_score": 70,
            "status": JobStatus.SCORED,
            "is_primary_listing": True,
        }
        defaults.update(kwargs)
        job = Job(**defaults)
        db.add(job)
        db.flush()
        return job

    return _factory


# ── Realistic email fixtures ──────────────────────────────────────────────

LINKEDIN_ALERT_HTML = """\
<!DOCTYPE html>
<html>
<body>
<table>
  <tr>
    <td>
      <a href="https://www.linkedin.com/jobs/view/3856789012?trk=jalt">
        Robotics Software Engineer Intern
      </a>
      <span class="company-name">Bosch Hungary</span>
      <span class="location">Budapest, Hungary</span>
    </td>
  </tr>
  <tr>
    <td>
      <a href="https://www.linkedin.com/jobs/view/3856789013?trk=jalt">
        Embedded Systems Intern
      </a>
      <span class="company-name">Continental</span>
      <span class="location">Debrecen, Hungary</span>
    </td>
  </tr>
  <tr>
    <td>
      <a href="https://www.linkedin.com/jobs/view/9999999999?trk=jalt">
        Senior Java Developer
      </a>
      <span class="company-name">SomeCorp</span>
      <span class="location">Berlin, Germany</span>
    </td>
  </tr>
  <tr>
    <td>
      <a href="https://www.linkedin.com/unsubscribe?token=abc">Unsubscribe</a>
    </td>
  </tr>
</table>
</body>
</html>
"""

INDEED_ALERT_HTML = """\
<!DOCTYPE html>
<html>
<body>
<table>
  <tr>
    <td>
      <a href="https://www.indeed.com/rc/clk?jk=abc123def456&fccid=x">
        Automation Engineer Intern
      </a>
      <div class="company">Siemens Hungary</div>
      <div class="location">Budapest, HU</div>
      <div class="salary">350,000 – 450,000 HUF/month</div>
    </td>
  </tr>
  <tr>
    <td>
      <a href="https://www.indeed.com/rc/clk?jk=xyz789ghi012&fccid=y">
        Mechatronics Trainee
      </a>
      <div class="company">ABB</div>
      <div class="location">Győr, Hungary</div>
    </td>
  </tr>
  <tr>
    <td>
      <a href="https://www.indeed.com/unsubscribe">Manage alerts</a>
    </td>
  </tr>
</table>
</body>
</html>
"""

GLASSDOOR_ALERT_HTML = """\
<!DOCTYPE html>
<html>
<body>
<div>
  <a href="https://www.glassdoor.com/job/robotics-intern-budapest-JV_IC2960267_KO0,15_KE16,24.htm">
    Robotics Intern
  </a>
  <div class="company">Knorr-Bremse</div>
  <div class="location">Budapest, Hungary</div>
</div>
</body>
</html>
"""

PROFESSION_HU_ALERT_HTML = """\
<!DOCTYPE html>
<html>
<body>
<table>
  <tr>
    <td>
      <a href="https://www.profession.hu/allasok/123456/mechatronikai-gyakornok">
        Mechatronikai Gyakornok
      </a>
      <span class="ceg">Bosch Kft.</span>
      <span class="telepules">Budapest</span>
    </td>
  </tr>
</table>
</body>
</html>
"""


@pytest.fixture
def linkedin_email_html() -> str:
    return LINKEDIN_ALERT_HTML


@pytest.fixture
def indeed_email_html() -> str:
    return INDEED_ALERT_HTML


@pytest.fixture
def glassdoor_email_html() -> str:
    return GLASSDOOR_ALERT_HTML


@pytest.fixture
def profession_hu_email_html() -> str:
    return PROFESSION_HU_ALERT_HTML
