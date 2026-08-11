"""
AI-powered CV tailoring.

Takes a CV as plain text (or extracted from .docx) and a job description,
then uses the AI provider to:

  1. Identify high-value keywords in the JD that are absent from the CV
  2. Suggest specific bullet-point rewrites to incorporate them naturally
  3. Rank suggestions by ATS impact (exact match > synonym > related)
  4. Flag any suggestion that would require inventing experience

Rules (same as AI provider):
  - Never invent skills, experience, or credentials.
  - Every suggestion is grounded in the existing CV text.
  - Keyword insertions must be semantically accurate, not stuffed.
  - If there is no honest way to include a keyword, it is flagged as a gap,
    not forced into the output.

Output is a TailoredCV object — a structured diff of suggested changes,
not a rewritten CV. The user applies the suggestions manually.

Usage::

    from jobhunter.cv_tailor import CVTailor
    tailor = CVTailor()
    result = tailor.tailor(cv_text, job_description, job_title="Robotics Intern")
    print(result.summary)
    for s in result.suggestions:
        print(s.section, s.original, "→", s.rewrite)
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Optional

import structlog

logger = structlog.get_logger(__name__)


@dataclass
class KeywordSuggestion:
    """One suggested CV edit to incorporate a JD keyword."""

    keyword: str
    section: str               # "Skills", "Experience — Schaeffler", etc.
    original: str              # existing bullet or phrase
    rewrite: str               # suggested replacement
    impact: str                # "high" | "medium" | "low"
    evidence: str              # which part of the existing CV supports this change
    is_gap: bool = False       # True if the keyword cannot be added honestly


@dataclass
class TailoredCV:
    """Result of CV tailoring analysis for one job."""

    job_title: str
    company: str
    keywords_found: list[str]       # keywords already in CV
    keywords_missing: list[str]     # JD keywords absent from CV
    suggestions: list[KeywordSuggestion]
    gaps: list[str]                 # keywords that cannot be added honestly
    summary: str
    ats_score_estimate: str         # "good" | "fair" | "poor"


# ── Keyword extraction (deterministic, no AI needed) ─────────────────────

_TECH_KEYWORD_PATTERN = re.compile(
    r"(?<!\w)("
    r"python|c\+\+|c#|java(?:script)?|matlab|simulink|ros2|ros 2|ros|"
    r"opencv|tensorflow|pytorch|keras|scikit|sklearn|"
    r"arduino|esp32|raspberry pi|stm32|fpga|vhdl|verilog|"
    r"autocad|solidworks|fusion 360|catia|nx|ansys|"
    r"plc|scada|labview|ladder logic|"
    r"docker|kubernetes|git(?:hub|lab)?|ci/cd|jenkins|"
    r"sql|postgresql|mysql|mongodb|redis|"
    r"agile|scrum|jira|confluence|"
    r"embedded|firmware|rtos|freertos|"
    r"machine learning|deep learning|computer vision|nlp|"
    r"control systems|pid|kalman|slam|"
    r"can bus|modbus|profibus|opc.ua|mqtt|i2c|spi|uart|"
    r"iso 9001|iatf 16949|aspice|misra|"
    r"digital twin|simulation|webots|gazebo|"
    r"power bi|tableau|excel|vba"
    r")(?!\w)",
    re.IGNORECASE,
)

_SOFT_KEYWORD_PATTERN = re.compile(
    r"\b("
    r"teamwork|collaboration|communication|leadership|"
    r"problem.solving|analytical|critical thinking|"
    r"project management|time management|prioriti[sz]ation|"
    r"cross.functional|stakeholder|presentation|"
    r"attention to detail|documentation|reporting"
    r")\b",
    re.IGNORECASE,
)


def extract_keywords(text: str) -> list[str]:
    """Extract tech and soft keywords from text, deduplicated."""
    tech = _TECH_KEYWORD_PATTERN.findall(text.lower())
    soft = _SOFT_KEYWORD_PATTERN.findall(text.lower())
    seen: set[str] = set()
    result = []
    for kw in tech + soft:
        kw_clean = kw.strip().lower()
        if kw_clean not in seen:
            seen.add(kw_clean)
            result.append(kw_clean)
    return result


# ── CV Tailor ─────────────────────────────────────────────────────────────

class CVTailor:
    """
    Produces targeted CV keyword suggestions for a specific job.

    Works in two modes:
      - deterministic: keyword diff only, no AI needed (fast, always available)
      - ai-enhanced:   also rewrites bullets to incorporate missing keywords
                       (requires AI_PROVIDER to be configured)
    """

    def tailor(
        self,
        cv_text: str,
        job_description: str,
        job_title: str = "",
        company: str = "",
        use_ai: bool = True,
    ) -> TailoredCV:
        """
        Analyse a CV against a job description and return tailoring suggestions.

        Args:
            cv_text:         Plain text content of the CV.
            job_description: Full job description text.
            job_title:       Used for context in AI prompts.
            company:         Used for context in AI prompts.
            use_ai:          If True and AI is configured, generate rewrites.
                             If False or AI is unavailable, returns keyword diff only.

        Returns:
            TailoredCV with suggestions and gap analysis.
        """
        cv_keywords = set(extract_keywords(cv_text))
        jd_keywords = set(extract_keywords(job_description))

        keywords_found = sorted(cv_keywords & jd_keywords)
        keywords_missing = sorted(jd_keywords - cv_keywords)

        # Basic ATS score estimate
        if len(jd_keywords) == 0:
            ats_score = "unknown"
        else:
            ratio = len(keywords_found) / len(jd_keywords)
            ats_score = "good" if ratio >= 0.7 else "fair" if ratio >= 0.4 else "poor"

        suggestions: list[KeywordSuggestion] = []
        gaps: list[str] = []

        if use_ai and keywords_missing:
            try:
                suggestions, gaps = self._ai_suggestions(
                    cv_text, job_description, keywords_missing,
                    job_title, company,
                )
            except Exception as exc:
                logger.warning("cv_tailor.ai_failed", error=str(exc))
                # Fall through to deterministic gaps
                gaps = keywords_missing

        if not suggestions:
            # Deterministic fallback: mark all missing keywords as gaps
            # that need to be addressed if the candidate genuinely has that skill
            gaps = keywords_missing

        summary = (
            f"Found {len(keywords_found)}/{len(jd_keywords)} JD keywords in CV "
            f"(ATS estimate: {ats_score}). "
            f"{len(keywords_missing)} keywords missing. "
            f"{len(suggestions)} suggested rewrites. "
            f"{len(gaps)} genuine gaps."
        )

        return TailoredCV(
            job_title=job_title,
            company=company,
            keywords_found=keywords_found,
            keywords_missing=keywords_missing,
            suggestions=suggestions,
            gaps=gaps,
            summary=summary,
            ats_score_estimate=ats_score,
        )

    def _ai_suggestions(
        self,
        cv_text: str,
        job_description: str,
        missing_keywords: list[str],
        job_title: str,
        company: str,
    ) -> tuple[list[KeywordSuggestion], list[str]]:
        """
        Use the AI provider to generate bullet-point rewrites.

        Returns (suggestions, genuine_gaps).
        """
        from jobhunter.ai_provider import get_ai_provider
        provider = get_ai_provider()

        # Build a targeted prompt
        prompt_question = (
            f"The candidate is applying for: {job_title} at {company}.\n"
            f"These JD keywords are MISSING from their CV: {', '.join(missing_keywords[:20])}.\n\n"
            f"CV (first 3000 chars):\n{cv_text[:3000]}\n\n"
            f"For each missing keyword:\n"
            f"1. Check if the candidate's existing experience GENUINELY supports adding it.\n"
            f"2. If yes: suggest a specific bullet rewrite (section + original line + rewrite).\n"
            f"3. If no: flag it as a genuine gap.\n"
            f"Return ONLY valid JSON: a list of objects with fields: "
            f"keyword, section, original, rewrite, impact (high/medium/low), "
            f"evidence, is_gap (bool).\n"
            f"No preamble, no markdown."
        )

        raw = provider.draft_answers(
            [prompt_question],
            job_description[:2000],
            {"cv_text": cv_text[:1500]},
        )

        if not raw or not raw[0].answer:
            return [], missing_keywords

        try:
            answer = raw[0].answer.strip()
            if answer.startswith("```"):
                answer = answer.split("\n", 1)[1].rsplit("```", 1)[0]
            items = json.loads(answer)
            if not isinstance(items, list):
                return [], missing_keywords

            suggestions = []
            gaps = []
            for item in items:
                if item.get("is_gap"):
                    gaps.append(item.get("keyword", ""))
                else:
                    suggestions.append(KeywordSuggestion(
                        keyword=item.get("keyword", ""),
                        section=item.get("section", ""),
                        original=item.get("original", ""),
                        rewrite=item.get("rewrite", ""),
                        impact=item.get("impact", "medium"),
                        evidence=item.get("evidence", ""),
                        is_gap=False,
                    ))
            return suggestions, gaps

        except Exception as exc:
            logger.warning("cv_tailor.parse_error", error=str(exc))
            return [], missing_keywords
