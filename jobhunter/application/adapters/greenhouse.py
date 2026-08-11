"""
Greenhouse ATS adapter.

Greenhouse is used by many tech and scale-up companies.
URL pattern: boards.greenhouse.io/<company>/jobs/<id>

Greenhouse-specific behaviour:
- Public application pages require no login for basic fields
- Resume upload is standard (file input)
- Custom questions appear per-company
- Multi-step forms are less common than Workday — often single page
- Social links (LinkedIn, GitHub) are frequently requested

This adapter handles the standard Greenhouse form structure.
Company-specific custom questions are detected and flagged for manual input.
"""
from __future__ import annotations

from typing import Any, Optional
from pathlib import Path

import structlog

from jobhunter.application.base_adapter import (
    AdapterRegistry,
    ApplicationSession,
    BaseApplicationAdapter,
    FieldMapping,
)

logger = structlog.get_logger(__name__)

# Greenhouse standard field selectors (data-source and id patterns)
GH_FIELD_MAP = {
    "first_name": "#first_name",
    "last_name": "#last_name",
    "email": "#email",
    "phone": "#phone",
    "resume": "#resume",
    "cover_letter": "#cover_letter",
    "linkedin_profile": "#job_application_answers_linkedin_profile",
    "website": "#job_application_answers_website",
    "location": "#job_application_answers_location",
}


@AdapterRegistry.register
class GreenhouseAdapter(BaseApplicationAdapter):
    """Greenhouse ATS adapter — standard form field detection and fill."""

    name = "greenhouse"
    URL_PATTERNS = [
        "boards.greenhouse.io",
        "grnh.se",
        "greenhouse.io/jobs",
    ]

    def __init__(
        self,
        profile: dict[str, Any],
        cv_path: Optional[Path] = None,
        cover_letter_path: Optional[Path] = None,
        screenshot_dir: Optional[Path] = None,
    ) -> None:
        super().__init__(profile, screenshot_dir)
        self.cv_path = cv_path
        self.cover_letter_path = cover_letter_path

    async def _detect_fields(self) -> list[FieldMapping]:
        """Detect Greenhouse form fields."""
        if self._page is None:
            return self._mock_greenhouse_fields()

        fields: list[FieldMapping] = []
        try:
            inputs = await self._page.query_selector_all(
                "input:visible, textarea:visible, select:visible"
            )
            for inp in inputs:
                field_id = (
                    await inp.get_attribute("id") or
                    await inp.get_attribute("name") or ""
                )
                field_type = await inp.get_attribute("type") or "text"
                if field_type == "hidden":
                    continue

                label = await self._find_label(inp, field_id)
                is_required = (
                    await inp.get_attribute("required") is not None or
                    await inp.get_attribute("aria-required") == "true"
                )

                fields.append(FieldMapping(
                    field_id=field_id,
                    label=label,
                    field_type=field_type,
                    proposed_value=None,
                    is_required=is_required,
                    is_sensitive=False,
                    evidence=None,
                    needs_user_input=False,
                ))
        except Exception as exc:
            logger.warning("greenhouse.detect_fields_error", error=str(exc))

        # Detect custom questions (Greenhouse custom fields start with
        # job_application_answers_)
        for f in fields:
            if "answers" in f.field_id and f.field_id not in GH_FIELD_MAP.values():
                f.needs_user_input = True  # custom question — pause for user

        return fields

    async def _fill_fields(self, session: ApplicationSession) -> None:
        """Fill standard Greenhouse fields."""
        if self._page is None:
            for f in session.fields:
                if not f.is_sensitive and not f.needs_user_input and f.proposed_value:
                    session.log(f"[sim] Fill '{f.label}' = '{f.proposed_value}'")
            return

        for f in session.fields:
            if f.is_sensitive or f.needs_user_input or f.proposed_value is None:
                continue
            selector = GH_FIELD_MAP.get(f.field_id) or f"#{f.field_id}"
            try:
                el = await self._page.query_selector(selector)
                if el:
                    await el.triple_click()
                    await el.type(f.proposed_value, delay=40)
                    session.log(f"Filled '{f.label}'")
            except Exception as exc:
                session.log(f"Fill failed '{f.label}': {exc}")

        # Upload CV
        if self.cv_path and self.cv_path.exists():
            try:
                resume_el = await self._page.query_selector("#resume")
                if resume_el:
                    await resume_el.set_input_files(str(self.cv_path))
                    session.log(f"Uploaded CV: {self.cv_path.name}")
            except Exception as exc:
                session.log(f"CV upload failed: {exc}")

    @staticmethod
    def _mock_greenhouse_fields() -> list[FieldMapping]:
        return [
            FieldMapping("first_name", "First Name", "text", None, True, False, None, False),
            FieldMapping("last_name", "Last Name", "text", None, True, False, None, False),
            FieldMapping("email", "Email", "email", None, True, False, None, False),
            FieldMapping("phone", "Phone", "tel", None, False, False, None, False),
            FieldMapping("linkedin_profile", "LinkedIn Profile", "url", None, False, False, None, False),
            FieldMapping("website", "Personal Website / GitHub", "url", None, False, False, None, False),
            # Custom questions — always need user input
            FieldMapping(
                "job_application_answers_work_auth",
                "Are you legally authorised to work in Hungary?",
                "select", None, True, True, None, True,
            ),
            FieldMapping(
                "job_application_answers_salary",
                "What are your salary expectations?",
                "text", None, False, True, None, True,
            ),
        ]
