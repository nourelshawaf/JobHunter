"""
Tests for the AI provider interface.

No real API calls — NullAIProvider and mocked structured outputs tested.
"""
from __future__ import annotations

import json
import os

import pytest

os.environ["JOBHUNTER_TESTING"] = "1"

from jobhunter.ai_provider import (
    DraftAnswer,
    JobAnalysis,
    NullAIProvider,
    _parse_draft_answers,
    _parse_job_analysis,
    get_ai_provider,
)


SAMPLE_PROFILE = {
    "full_name": "Noureldeen Elshawaf",
    "university": "University of Debrecen",
    "degree": "Mechatronics Engineering BSc",
    "skills": ["Python", "C++", "ROS 2", "embedded systems"],
}

SAMPLE_JD = """
We are looking for a Mechatronics Engineering Intern.
Requirements: Python, C++, embedded systems experience.
The position is in Budapest, Hungary.
English required.
"""

SAMPLE_ANALYSIS_JSON = {
    "summary": "An internship for mechatronics engineers.",
    "key_requirements": ["Python", "C++", "embedded systems"],
    "preferred_qualifications": ["ROS 2"],
    "mandatory_languages": ["English"],
    "experience_years_required": None,
    "is_student_friendly": True,
    "match_explanation": "Strong match: Python, C++, embedded systems all present.",
    "skill_gaps": ["Simulink"],
    "cv_keywords": ["embedded", "firmware", "sensor"],
    "evidence_warnings": [],
}


class TestNullAIProvider:

    @pytest.fixture
    def provider(self) -> NullAIProvider:
        return NullAIProvider()

    def test_analyse_job_returns_job_analysis(self, provider: NullAIProvider) -> None:
        result = provider.analyse_job(SAMPLE_JD, SAMPLE_PROFILE)
        assert isinstance(result, JobAnalysis)

    def test_analyse_job_summary_not_empty(self, provider: NullAIProvider) -> None:
        result = provider.analyse_job(SAMPLE_JD, SAMPLE_PROFILE)
        assert len(result.summary) > 0

    def test_draft_answers_returns_list(self, provider: NullAIProvider) -> None:
        questions = ["Why do you want to work here?", "What is your experience?"]
        answers = provider.draft_answers(questions, SAMPLE_JD, SAMPLE_PROFILE)
        assert len(answers) == 2
        assert all(isinstance(a, DraftAnswer) for a in answers)

    def test_draft_answers_one_per_question(self, provider: NullAIProvider) -> None:
        questions = ["Q1", "Q2", "Q3"]
        answers = provider.draft_answers(questions, SAMPLE_JD, SAMPLE_PROFILE)
        assert len(answers) == len(questions)

    def test_draft_cover_letter_returns_string(self, provider: NullAIProvider) -> None:
        result = provider.draft_cover_letter(
            "Intern", "Bosch", SAMPLE_JD, SAMPLE_PROFILE
        )
        assert isinstance(result, str)
        assert len(result) > 0

    def test_null_provider_does_not_raise(self, provider: NullAIProvider) -> None:
        """NullAIProvider must not raise under any input."""
        provider.analyse_job("", {})
        provider.draft_answers([], "", {})
        provider.draft_cover_letter("", "", "", {})


class TestJobAnalysisSchema:

    def test_valid_json_parses_correctly(self) -> None:
        result = _parse_job_analysis(json.dumps(SAMPLE_ANALYSIS_JSON))
        assert isinstance(result, JobAnalysis)
        assert result.summary == "An internship for mechatronics engineers."
        assert "Python" in result.key_requirements
        assert result.is_student_friendly is True

    def test_invalid_json_returns_fallback(self) -> None:
        result = _parse_job_analysis("not valid json at all {{{")
        assert isinstance(result, JobAnalysis)
        assert "Parse error" in result.summary or len(result.evidence_warnings) > 0

    def test_missing_fields_handled_gracefully(self) -> None:
        minimal = {"summary": "Short summary", "match_explanation": "Good fit"}
        result = _parse_job_analysis(json.dumps(minimal))
        # Should not raise — missing fields get defaults
        assert isinstance(result, JobAnalysis)

    def test_markdown_fenced_json_parsed(self) -> None:
        fenced = f"```json\n{json.dumps(SAMPLE_ANALYSIS_JSON)}\n```"
        result = _parse_job_analysis(fenced)
        assert isinstance(result, JobAnalysis)


class TestDraftAnswerSchema:

    def test_parses_valid_answer_list(self) -> None:
        data = [
            {
                "question": "Why Bosch?",
                "answer": "I am interested in automotive engineering.",
                "evidence_sources": ["work_experience"],
                "confidence": "high",
                "warning": None,
            }
        ]
        result = _parse_draft_answers(json.dumps(data), ["Why Bosch?"])
        assert len(result) == 1
        assert result[0].question == "Why Bosch?"
        assert result[0].confidence == "high"

    def test_invalid_json_returns_fallback_per_question(self) -> None:
        questions = ["Q1", "Q2"]
        result = _parse_draft_answers("broken {{}", questions)
        assert len(result) == 2
        assert all(a.confidence == "low" for a in result)
        assert all(a.warning is not None for a in result)


class TestGetAIProvider:

    def test_returns_null_when_ai_provider_none(self) -> None:
        provider = get_ai_provider()
        # With AI_PROVIDER=none (default in tests), should return NullAIProvider
        assert isinstance(provider, NullAIProvider)

    def test_null_provider_is_functional(self) -> None:
        provider = get_ai_provider()
        result = provider.analyse_job(SAMPLE_JD, SAMPLE_PROFILE)
        assert isinstance(result, JobAnalysis)
