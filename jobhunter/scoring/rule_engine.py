"""
Relevance scoring engine.

Scores every job 0–100 using a deterministic rule engine.
No AI required — fast, transparent, explainable.

Each rule contributes a signed integer to the score.
The final score is clamped to [0, 100].
A human-readable explanation is generated alongside the score.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional

import structlog

from jobhunter.config import get_search_config
from jobhunter.models.job import Job, JobType, WorkMode

logger = structlog.get_logger(__name__)


@dataclass
class ScoreResult:
    """Output of the scoring engine for one job."""

    score: int
    explanation: str
    reasons: list[str] = field(default_factory=list)
    penalties: list[str] = field(default_factory=list)
    auto_reject: bool = False


class RuleEngine:
    """
    Deterministic relevance scorer.

    Scores are reproducible — the same job always gets the same score
    unless the configuration changes.
    """

    # ── Domain keyword groups ─────────────────
    CORE_DOMAIN_KEYWORDS = {
        "mechatronics": 8,
        "robotics": 8,
        "automation": 7,
        "embedded": 7,
        "computer vision": 8,
        "machine learning": 6,
        "artificial intelligence": 6,
        "control systems": 7,
        "control engineering": 7,
        "autonomous": 7,
        "PLC": 6,
        "ROS": 8,
        "ROS 2": 8,
        "digital twin": 6,
        "manufacturing engineering": 5,
        "test engineering": 5,
        "quality engineering": 4,
        "electrical engineering": 6,
        "electronics": 5,
    }

    TECHNICAL_SKILL_KEYWORDS = {
        "python": 5,
        "c++": 5,
        "matlab": 4,
        "simulink": 4,
        "openCV": 5,
        "tensorflow": 4,
        "pytorch": 4,
        "arduino": 4,
        "esp32": 4,
        "raspberry pi": 4,
        "CAD": 3,
        "fusion 360": 3,
        "solidworks": 3,
        "autocad": 3,
        "labview": 3,
        "fpga": 5,
        "vhdl": 4,
        "verilog": 4,
        "linux": 3,
        "git": 2,
    }

    EXCLUSION_PATTERNS = [
        re.compile(r"\bsenior\b", re.IGNORECASE),
        re.compile(r"\bdirector\b", re.IGNORECASE),
        re.compile(r"\bmanager\b", re.IGNORECASE),
        re.compile(r"\bvice president\b", re.IGNORECASE),
        re.compile(r"\blead\b.*\bengineer\b", re.IGNORECASE),
        re.compile(r"\bhead of\b", re.IGNORECASE),
        re.compile(r"\bprincipal engineer\b", re.IGNORECASE),
        re.compile(r"\bstaff engineer\b", re.IGNORECASE),
        re.compile(r"\b10\+\s*years\b", re.IGNORECASE),
        re.compile(r"\b8\+\s*years\b", re.IGNORECASE),
        re.compile(r"\b7\+\s*years\b", re.IGNORECASE),
    ]

    EXPERIENCE_PATTERNS = [
        (re.compile(r"\b([3-6])\+?\s*years?\b", re.IGNORECASE), "3-6 years", -20),
        (re.compile(r"\b([2])\+?\s*years?\b", re.IGNORECASE), "2 years", -10),
        (re.compile(r"\b(1)\+?\s*year\b", re.IGNORECASE), "1 year", 0),
    ]

    def score(self, job: Job) -> ScoreResult:
        """
        Score a job and return a ScoreResult with explanation.

        Mutates nothing — returns a new ScoreResult each call.
        """
        config = get_search_config()
        combined_text = self._combined_text(job)
        title_lower = job.title.lower()
        reasons: list[str] = []
        penalties: list[str] = []
        total = 0

        # ── Auto-reject check ─────────────────
        for pattern in self.EXCLUSION_PATTERNS:
            if pattern.search(combined_text):
                return ScoreResult(
                    score=0,
                    explanation=f"Auto-rejected: matched exclusion pattern '{pattern.pattern}'",
                    auto_reject=True,
                )

        if job.is_expired:
            return ScoreResult(
                score=0,
                explanation="Auto-rejected: job deadline has passed",
                auto_reject=True,
            )

        # ── Core domain match (max 25) ────────
        domain_score = 0
        matched_domains: list[str] = []
        for keyword, points in self.CORE_DOMAIN_KEYWORDS.items():
            if keyword.lower() in combined_text:
                domain_score = min(domain_score + points, 25)
                matched_domains.append(keyword)

        if domain_score > 0:
            total += domain_score
            reasons.append(
                f"+{domain_score} domain match: {', '.join(matched_domains[:3])}"
            )

        # ── Job type suitability (max 15) ─────
        if job.job_type in (
            JobType.INTERNSHIP, JobType.WORKING_STUDENT, JobType.TRAINEE
        ):
            total += 15
            reasons.append(f"+15 internship/student role ({job.job_type})")
        elif job.job_type in (JobType.JUNIOR, JobType.GRADUATE):
            total += 8
            reasons.append(f"+8 junior/graduate role ({job.job_type})")
        elif job.student_friendly:
            total += 10
            reasons.append("+10 student-friendly signals in description")

        # ── Location (max 10) ─────────────────
        location_lower = (job.location or "").lower()
        if any(loc.lower() in location_lower for loc in ["budapest", "debrecen"]):
            total += 10
            reasons.append(f"+10 primary location: {job.location}")
        elif any(
            loc.lower() in location_lower
            for loc in ["hungary", "magyarország", "győr", "miskolc", "pécs"]
        ):
            total += 6
            reasons.append(f"+6 Hungary location: {job.location}")

        # ── Work mode ─────────────────────────
        if job.work_mode == WorkMode.REMOTE:
            total += 4
            reasons.append("+4 remote work available")
        elif job.work_mode == WorkMode.HYBRID:
            total += 4
            reasons.append("+4 hybrid work available")

        # ── English accessibility ─────────────
        lang_lower = (job.language_requirements or "").lower()
        if "english" in lang_lower and "hungarian" not in lang_lower:
            total += 10
            reasons.append("+10 English-only role")
        elif "english" in combined_text and not job.hungarian_mandatory:
            total += 6
            reasons.append("+6 English accessible (no mandatory Hungarian)")

        # ── Technical skills (max 15) ─────────
        skill_score = 0
        matched_skills: list[str] = []
        for skill, points in self.TECHNICAL_SKILL_KEYWORDS.items():
            if skill.lower() in combined_text:
                skill_score = min(skill_score + points, 15)
                matched_skills.append(skill)

        if skill_score > 0:
            total += skill_score
            reasons.append(
                f"+{skill_score} technical skills: {', '.join(matched_skills[:4])}"
            )

        # ── BSc student accepted ───────────────
        if job.student_friendly or any(
            k in combined_text
            for k in ["bsc", "bachelor", "university student", "hallgató"]
        ):
            total += 8
            reasons.append("+8 accepts university students")

        # ── Recency ───────────────────────────
        if job.days_since_posted is not None:
            if job.days_since_posted <= 7:
                total += 5
                reasons.append(f"+5 posted {job.days_since_posted} days ago (recent)")
            elif job.days_since_posted <= 21:
                total += 2
                reasons.append(f"+2 posted {job.days_since_posted} days ago")

        # ── Priority company ──────────────────
        company_norm = (job.company_normalized or job.company).lower()
        for priority_co in config.priority_companies:
            if priority_co.lower() in company_norm:
                total += 5
                reasons.append(f"+5 priority company: {priority_co}")
                break

        # ── Hungarian mandatory penalty ────────
        if job.hungarian_mandatory:
            penalty = config.hungarian_mandatory_penalty
            total -= penalty
            penalties.append(f"−{penalty} mandatory Hungarian language")

        # ── Experience requirements ────────────
        for pattern, label, delta in self.EXPERIENCE_PATTERNS:
            if pattern.search(combined_text):
                total += delta
                if delta < 0:
                    penalties.append(f"{delta} experience requirement: {label}")
                break

        # ── Clamp ─────────────────────────────
        final_score = max(0, min(100, total))

        # ── Explanation ───────────────────────
        explanation_parts: list[str] = [f"{final_score}/100"]
        if reasons:
            explanation_parts.append(". ".join(reasons))
        if penalties:
            explanation_parts.append(". ".join(penalties))

        return ScoreResult(
            score=final_score,
            explanation=". ".join(explanation_parts),
            reasons=reasons,
            penalties=penalties,
            auto_reject=False,
        )

    @staticmethod
    def _combined_text(job: Job) -> str:
        """Combine all text fields for keyword scanning."""
        return " ".join(
            filter(
                None,
                [
                    job.title,
                    job.description,
                    job.requirements,
                    job.preferred_qualifications,
                    job.language_requirements,
                    job.location,
                ],
            )
        ).lower()


def apply_score(job: Job, engine: Optional[RuleEngine] = None) -> Job:
    """
    Score a job in-place and return it.

    Convenience wrapper used in the pipeline.
    """
    if engine is None:
        engine = RuleEngine()

    result = engine.score(job)
    job.relevance_score = result.score
    job.score_explanation = result.explanation

    if result.auto_reject:
        job.status = "rejected_by_filter"
    else:
        job.status = "scored"

    return job
