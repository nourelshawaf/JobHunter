"""
Optional AI analysis module.

Disabled by default (AI_PROVIDER=none in .env).
When enabled, provides:
  - Requirement extraction from job descriptions
  - Role summary
  - Match explanation vs candidate profile
  - Skill-gap analysis
  - CV keyword suggestions
  - Draft application answers
  - Cover letter drafting

Design rules:
  1. No invented experience. Every generated claim links to profile evidence
     or job description evidence. If evidence is missing, a warning is emitted.
  2. Structured outputs validated with Pydantic — no free-form JSON parsing.
  3. Provider timeout and retry via tenacity.
  4. Tests use mocked responses — no real API calls.
  5. Graceful degradation: if AI is unavailable, scoring and pipeline continue.
"""
from __future__ import annotations

import json
import os
from typing import Any, Optional

import structlog
from pydantic import BaseModel, Field
from tenacity import retry, stop_after_attempt, wait_exponential

logger = structlog.get_logger(__name__)

_TESTING = os.environ.get("JOBHUNTER_TESTING", "0") == "1"


# ── Output schemas ────────────────────────────────────────────────────────

class JobAnalysis(BaseModel):
    """AI-structured analysis of a job posting."""

    summary: str = Field(description="2-3 sentence role summary")
    key_requirements: list[str] = Field(description="Mandatory requirements extracted from JD")
    preferred_qualifications: list[str] = Field(description="Nice-to-have qualifications")
    mandatory_languages: list[str] = Field(description="Languages explicitly required")
    experience_years_required: Optional[int] = Field(default=None)
    is_student_friendly: bool = Field(description="True if current students can apply")
    match_explanation: str = Field(description="Why this role matches the candidate profile")
    skill_gaps: list[str] = Field(description="Skills in JD not found in candidate profile")
    cv_keywords: list[str] = Field(description="Keywords to add/emphasise in the CV")
    evidence_warnings: list[str] = Field(
        default_factory=list,
        description="Claims that could not be grounded in profile evidence"
    )


class DraftAnswer(BaseModel):
    """A drafted answer to an application question."""

    question: str
    answer: str
    evidence_sources: list[str] = Field(
        description="Profile fields or JD sections used as evidence"
    )
    confidence: str = Field(description="high | medium | low")
    warning: Optional[str] = Field(
        default=None,
        description="Set if answer relies on weak or missing evidence"
    )


# ── Base provider ─────────────────────────────────────────────────────────

class BaseAIProvider:
    """Abstract base for AI providers."""

    name: str = ""

    def analyse_job(self, job_description: str, profile: dict[str, Any]) -> JobAnalysis:
        raise NotImplementedError

    def draft_answers(
        self,
        questions: list[str],
        job_description: str,
        profile: dict[str, Any],
    ) -> list[DraftAnswer]:
        raise NotImplementedError

    def draft_cover_letter(
        self,
        job_title: str,
        company: str,
        job_description: str,
        profile: dict[str, Any],
    ) -> str:
        raise NotImplementedError


# ── Anthropic provider ────────────────────────────────────────────────────

class AnthropicProvider(BaseAIProvider):
    """Uses the Anthropic Messages API (Claude models)."""

    name = "anthropic"

    def __init__(self, api_key: str, model: str = "claude-sonnet-4-6") -> None:
        import anthropic
        self._client = anthropic.Anthropic(api_key=api_key)
        self.model = model

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=2, max=30))
    def _call(self, system: str, user: str, max_tokens: int = 1500) -> str:
        """Make a completion call with retry."""
        if _TESTING:
            raise RuntimeError("AI calls disabled in test mode")

        response = self._client.messages.create(
            model=self.model,
            max_tokens=max_tokens,
            system=system,
            messages=[{"role": "user", "content": user}],
        )
        return response.content[0].text

    def analyse_job(self, job_description: str, profile: dict[str, Any]) -> JobAnalysis:
        system = _build_analysis_system_prompt(profile)
        user = f"Analyse this job description:\n\n{job_description[:4000]}"
        raw = self._call(system, user)
        return _parse_job_analysis(raw)

    def draft_answers(
        self,
        questions: list[str],
        job_description: str,
        profile: dict[str, Any],
    ) -> list[DraftAnswer]:
        system = _build_answer_system_prompt(profile)
        user = (
            f"Job description:\n{job_description[:2000]}\n\n"
            f"Questions to answer:\n" +
            "\n".join(f"{i+1}. {q}" for i, q in enumerate(questions))
        )
        raw = self._call(system, user)
        return _parse_draft_answers(raw, questions)

    def draft_cover_letter(
        self, job_title: str, company: str, job_description: str, profile: dict[str, Any]
    ) -> str:
        system = _build_cover_letter_system_prompt(profile)
        user = (
            f"Write a cover letter for:\nRole: {job_title}\nCompany: {company}\n\n"
            f"Job description:\n{job_description[:2000]}"
        )
        return self._call(system, user, max_tokens=800)


# ── OpenAI provider ───────────────────────────────────────────────────────

class OpenAIProvider(BaseAIProvider):
    """Uses the OpenAI Chat Completions API (also compatible with local endpoints)."""

    name = "openai"

    def __init__(
        self, api_key: str, model: str = "gpt-4o-mini", base_url: Optional[str] = None
    ) -> None:
        from openai import OpenAI
        self._client = OpenAI(api_key=api_key, base_url=base_url)
        self.model = model

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=2, max=30))
    def _call(self, system: str, user: str, max_tokens: int = 1500) -> str:
        if _TESTING:
            raise RuntimeError("AI calls disabled in test mode")

        response = self._client.chat.completions.create(
            model=self.model,
            max_tokens=max_tokens,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        )
        return response.choices[0].message.content or ""

    def analyse_job(self, job_description: str, profile: dict[str, Any]) -> JobAnalysis:
        raw = self._call(_build_analysis_system_prompt(profile),
                         f"Analyse this job:\n\n{job_description[:4000]}")
        return _parse_job_analysis(raw)

    def draft_answers(
        self, questions: list[str], job_description: str, profile: dict[str, Any]
    ) -> list[DraftAnswer]:
        raw = self._call(
            _build_answer_system_prompt(profile),
            f"JD:\n{job_description[:2000]}\n\nQuestions:\n" +
            "\n".join(f"{i+1}. {q}" for i, q in enumerate(questions))
        )
        return _parse_draft_answers(raw, questions)

    def draft_cover_letter(
        self, job_title: str, company: str, job_description: str, profile: dict[str, Any]
    ) -> str:
        return self._call(
            _build_cover_letter_system_prompt(profile),
            f"Role: {job_title}\nCompany: {company}\n\nJD:\n{job_description[:2000]}"
        )


# ── Null provider (default) ───────────────────────────────────────────────

class NullAIProvider(BaseAIProvider):
    """No-op provider used when AI is disabled (AI_PROVIDER=none)."""

    name = "none"

    def analyse_job(self, job_description: str, profile: dict[str, Any]) -> JobAnalysis:
        return JobAnalysis(
            summary="AI analysis disabled.",
            key_requirements=[],
            preferred_qualifications=[],
            mandatory_languages=[],
            is_student_friendly=False,
            match_explanation="Enable AI_PROVIDER in .env for analysis.",
            skill_gaps=[],
            cv_keywords=[],
        )

    def draft_answers(self, questions, job_description, profile) -> list[DraftAnswer]:
        return [
            DraftAnswer(
                question=q,
                answer="AI drafting disabled. Please answer manually.",
                evidence_sources=[],
                confidence="low",
                warning="AI_PROVIDER=none",
            )
            for q in questions
        ]

    def draft_cover_letter(self, job_title, company, job_description, profile) -> str:
        return "AI cover letter drafting disabled. Set AI_PROVIDER in .env."


# ── Factory ───────────────────────────────────────────────────────────────

def get_ai_provider() -> BaseAIProvider:
    """
    Return the configured AI provider.

    Reads AI_PROVIDER from settings. Returns NullAIProvider if
    not configured or if provider credentials are missing.
    """
    from jobhunter.config import get_settings
    settings = get_settings()
    provider_name = settings.ai_provider

    if provider_name == "anthropic":
        key = settings.anthropic_api_key.get_secret_value()
        if not key:
            logger.warning("ai.anthropic_key_missing")
            return NullAIProvider()
        return AnthropicProvider(api_key=key, model=settings.ai_model)

    if provider_name == "openai":
        key = settings.openai_api_key.get_secret_value()
        if not key:
            logger.warning("ai.openai_key_missing")
            return NullAIProvider()
        return OpenAIProvider(api_key=key, model=settings.ai_model)

    return NullAIProvider()


# ── Prompt builders ───────────────────────────────────────────────────────

def _build_analysis_system_prompt(profile: dict[str, Any]) -> str:
    return f"""You are a career assistant helping a Mechatronics Engineering student
find suitable internships. Analyse job descriptions against this candidate profile:

{json.dumps(profile, indent=2)[:1500]}

Rules you MUST follow:
- Only claim the candidate matches a requirement if evidence exists in the profile above.
- If you cannot find evidence for a match claim, add it to evidence_warnings.
- Never invent skills, experience, or qualifications not in the profile.
- Return ONLY valid JSON matching this schema:
  summary, key_requirements (list), preferred_qualifications (list),
  mandatory_languages (list), experience_years_required (int or null),
  is_student_friendly (bool), match_explanation (str), skill_gaps (list),
  cv_keywords (list), evidence_warnings (list)
- No markdown, no explanation, just the JSON object."""


def _build_answer_system_prompt(profile: dict[str, Any]) -> str:
    return f"""You are a career assistant drafting application answers for a candidate.
Only use information from this profile:

{json.dumps(profile, indent=2)[:1500]}

Rules:
- Never invent experience, credentials, or qualifications not in the profile.
- If the profile lacks evidence for an answer, set confidence to "low" and add a warning.
- Return ONLY valid JSON: a list of objects with fields:
  question, answer, evidence_sources (list), confidence (high/medium/low), warning (str or null)
- No markdown, no preamble."""


def _build_cover_letter_system_prompt(profile: dict[str, Any]) -> str:
    return f"""You are a career assistant writing a cover letter.
Only use information from this candidate profile:

{json.dumps(profile, indent=2)[:1500]}

Rules:
- Write 3-4 paragraphs, professional tone, no invented claims.
- Opening: enthusiasm + brief intro.
- Middle: 2-3 relevant skills/projects from profile matched to the role.
- Closing: availability and call to action.
- Return plain text only, no markdown."""


# ── Parsers ───────────────────────────────────────────────────────────────

def _parse_job_analysis(raw: str) -> JobAnalysis:
    """Parse and validate AI job analysis output."""
    try:
        raw = raw.strip()
        if raw.startswith("```"):
            raw = raw.split("\n", 1)[1].rsplit("```", 1)[0]
        data = json.loads(raw)
        return JobAnalysis(**data)
    except Exception as exc:
        logger.error("ai.parse_job_analysis_error", error=str(exc))
        return JobAnalysis(
            summary="Parse error",
            key_requirements=[],
            preferred_qualifications=[],
            mandatory_languages=[],
            is_student_friendly=False,
            match_explanation="Could not parse AI response.",
            skill_gaps=[],
            cv_keywords=[],
            evidence_warnings=[f"Parse error: {exc}"],
        )


def _parse_draft_answers(raw: str, questions: list[str]) -> list[DraftAnswer]:
    """Parse and validate AI draft answers."""
    try:
        raw = raw.strip()
        if raw.startswith("```"):
            raw = raw.split("\n", 1)[1].rsplit("```", 1)[0]
        data = json.loads(raw)
        if isinstance(data, list):
            return [DraftAnswer(**item) for item in data]
    except Exception as exc:
        logger.error("ai.parse_answers_error", error=str(exc))

    # Fallback: one error answer per question
    return [
        DraftAnswer(
            question=q,
            answer="Could not parse AI response. Please answer manually.",
            evidence_sources=[],
            confidence="low",
            warning="Parse error",
        )
        for q in questions
    ]
