"""Tests for the relevance scoring engine."""

from __future__ import annotations

import pytest

from jobhunter.models.job import Job, JobStatus, JobType, WorkMode
from jobhunter.scoring.rule_engine import RuleEngine, ScoreResult, apply_score


def make_job(**kwargs: object) -> Job:
    """Create a minimal Job instance for testing."""
    defaults = {
        "title": "Engineering Intern",
        "company": "Test Corp",
        "company_normalized": "Test Corp",
        "location": "Budapest, Hungary",
        "description": "An internship position.",
        "requirements": "",
        "job_type": JobType.INTERNSHIP,
        "work_mode": WorkMode.ONSITE,
        "student_friendly": True,
        "hungarian_mandatory": False,
        "relevance_score": None,
        "status": JobStatus.DISCOVERED,
    }
    defaults.update(kwargs)
    job = Job()
    for k, v in defaults.items():
        setattr(job, k, v)
    return job


class TestRuleEngine:
    """Unit tests for RuleEngine.score()."""

    engine = RuleEngine()

    def test_high_score_robotics_intern_budapest(self) -> None:
        """A robotics internship in Budapest should score highly."""
        job = make_job(
            title="Robotics Software Intern",
            description="C++ ROS 2 Python embedded systems computer vision",
            location="Budapest, Hungary",
            job_type=JobType.INTERNSHIP,
            student_friendly=True,
        )
        result = self.engine.score(job)
        assert result.score >= 60
        assert not result.auto_reject

    def test_auto_reject_senior_role(self) -> None:
        """A senior role should be auto-rejected regardless of other signals."""
        job = make_job(
            title="Senior Robotics Engineer",
            description="10+ years required, robotics automation python",
        )
        result = self.engine.score(job)
        assert result.auto_reject is True
        assert result.score == 0

    def test_auto_reject_manager(self) -> None:
        job = make_job(title="Engineering Manager", description="Lead a team of 10")
        result = self.engine.score(job)
        assert result.auto_reject is True

    def test_hungarian_mandatory_penalty(self) -> None:
        """Mandatory Hungarian should subtract from the score."""
        job_no_hu = make_job(hungarian_mandatory=False)
        job_hu = make_job(hungarian_mandatory=True)

        score_no_hu = self.engine.score(job_no_hu).score
        score_hu = self.engine.score(job_hu).score

        assert score_hu < score_no_hu

    def test_domain_keywords_boost_score(self) -> None:
        """Domain-matching keywords should boost the score."""
        job_generic = make_job(
            title="Office Intern",
            description="Filing documents and answering phones",
            job_type=JobType.INTERNSHIP,
        )
        job_robotics = make_job(
            title="Robotics Intern",
            description="ROS 2 mechatronics embedded systems C++",
            job_type=JobType.INTERNSHIP,
        )
        assert self.engine.score(job_robotics).score > self.engine.score(job_generic).score

    def test_primary_location_boost(self) -> None:
        """Budapest should score higher than a generic location."""
        job_budapest = make_job(location="Budapest, Hungary")
        job_germany = make_job(location="Munich, Germany")

        assert self.engine.score(job_budapest).score > self.engine.score(job_germany).score

    def test_expired_job_auto_rejected(self) -> None:
        """An expired job should be auto-rejected."""
        from datetime import datetime, timedelta
        past_date = datetime.utcnow() - timedelta(days=30)
        past_date = past_date.replace(tzinfo=__import__("datetime").timezone.utc)

        job = make_job()
        job.deadline = past_date

        result = self.engine.score(job)
        assert result.auto_reject is True

    def test_score_clamped_to_100(self) -> None:
        """Score should never exceed 100."""
        job = make_job(
            title="Robotics Mechatronics Automation ROS Intern",
            description=(
                "Python C++ ROS 2 OpenCV embedded computer vision "
                "machine learning autonomous systems BSc student Budapest "
                "electrical engineering mechatronics"
            ),
            location="Budapest, Hungary",
            job_type=JobType.INTERNSHIP,
            work_mode=WorkMode.HYBRID,
            student_friendly=True,
            hungarian_mandatory=False,
        )
        result = self.engine.score(job)
        assert 0 <= result.score <= 100

    def test_score_never_negative(self) -> None:
        """Score should never go below 0."""
        job = make_job(
            title="Intern",
            description="fluent hungarian required, 3+ years experience mandatory",
            hungarian_mandatory=True,
            job_type=JobType.UNKNOWN,
        )
        result = self.engine.score(job)
        assert result.score >= 0

    def test_explanation_contains_score(self) -> None:
        """Explanation should start with the score."""
        job = make_job()
        result = self.engine.score(job)
        assert str(result.score) in result.explanation

    def test_apply_score_mutates_job(self) -> None:
        """apply_score() should set relevance_score on the job."""
        job = make_job()
        returned = apply_score(job)
        assert returned.relevance_score is not None
        assert returned.relevance_score == job.relevance_score

    def test_working_student_scores_well(self) -> None:
        """Working student roles should score comparably to internships."""
        job = make_job(
            title="Working Student Automation Engineer",
            job_type=JobType.WORKING_STUDENT,
            description="Python automation PLC Budapest",
        )
        result = self.engine.score(job)
        assert result.score >= 40

    def test_remote_work_boost(self) -> None:
        """Remote work availability should add to the score."""
        job_onsite = make_job(work_mode=WorkMode.ONSITE)
        job_remote = make_job(work_mode=WorkMode.REMOTE)
        assert self.engine.score(job_remote).score > self.engine.score(job_onsite).score
