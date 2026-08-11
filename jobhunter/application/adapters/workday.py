"""
Workday application adapter.

Workday is used by: Bosch, BMW, Baker Hughes, Siemens, Continental,
GE Vernova, Honeywell, and many other target companies. One adapter
covers a large portion of the target company list.

Workday URL pattern: wd3.myworkdayjobs.com or myworkdayjobs.com

Workday-specific behaviour:
- Multi-step wizard with "Next" buttons between sections
- "Review" page before the final submit — we stop here
- Account creation / SSO login is common — we pause and wait
- CAPTCHA rarely appears but we detect and pause for it
- File upload field for CV — we use only the user-approved CV path

This adapter is designed to be tested with mock pages (Playwright is
optional — if no browser is available, it runs in simulation mode
and returns a fully populated ApplicationSession with mock data).
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Optional

import structlog

from jobhunter.application.base_adapter import (
    AdapterRegistry,
    ApplicationSession,
    BaseApplicationAdapter,
    FieldMapping,
    SubmitGuardError,
)

logger = structlog.get_logger(__name__)

# Workday "Next" / "Save" buttons are safe — they advance steps without submitting
WORKDAY_SAFE_BUTTON_PATTERNS = {
    "next", "continue", "save", "save and continue", "back",
    "add", "add another", "upload", "browse",
}

# Workday form section selectors (observed from Workday wd5/wd3 versions)
WORKDAY_FIELD_SELECTORS = {
    "first_name": '[data-automation-id="legalNameSection_firstName"]',
    "last_name": '[data-automation-id="legalNameSection_lastName"]',
    "email": '[data-automation-id="email"]',
    "phone": '[data-automation-id="phone"]',
    "address_line1": '[data-automation-id="addressSection_addressLine1"]',
    "city": '[data-automation-id="addressSection_city"]',
    "linkedin": '[data-automation-id="linkedIn"]',
    "portfolio": '[data-automation-id="portfolioURL"]',
}

WORKDAY_FILE_UPLOAD = '[data-automation-id="file-upload-input-ref"]'
WORKDAY_REVIEW_INDICATORS = [
    "review", "preview", "summary", "confirm your application",
    "review application", "check your application",
]


@AdapterRegistry.register
class WorkdayAdapter(BaseApplicationAdapter):
    """
    Workday ATS adapter.

    URL_PATTERNS identifies pages this adapter handles.
    """

    name = "workday"
    URL_PATTERNS = [
        "myworkdayjobs.com",
        "wd3.myworkdayjobs.com",
        "wd5.myworkdayjobs.com",
        "workday.com/apply",
    ]

    def __init__(
        self,
        profile: dict[str, Any],
        cv_path: Optional[Path] = None,
        screenshot_dir: Optional[Path] = None,
    ) -> None:
        super().__init__(profile, screenshot_dir)
        self.cv_path = cv_path  # must be user-approved before being set

    # ── Core abstract method implementations ──────────────────────────────

    async def _detect_fields(self) -> list[FieldMapping]:
        """
        Detect visible form fields on the current Workday page.

        Uses Workday's data-automation-id attributes for reliable selection.
        Falls back to label-text matching for non-standard Workday deployments.
        """
        if self._page is None:
            return self._mock_workday_fields()

        fields: list[FieldMapping] = []

        # Use Playwright to inspect the page
        try:
            inputs = await self._page.query_selector_all(
                "input:visible, textarea:visible, select:visible"
            )
            for inp in inputs:
                field_id = (await inp.get_attribute("data-automation-id") or
                            await inp.get_attribute("id") or
                            await inp.get_attribute("name") or "")
                field_type = await inp.get_attribute("type") or "text"
                if field_type == "hidden":
                    continue

                # Find the associated label
                label = await self._find_label(inp, field_id)
                is_required = await inp.get_attribute("aria-required") == "true"

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
            logger.warning("workday.detect_fields_error", error=str(exc))

        return fields

    async def _fill_fields(self, session: ApplicationSession) -> None:
        """
        Fill safe fields using Workday's data-automation-id selectors.

        For each safe field with a proposed value:
        1. Locate the element by selector or ID
        2. Clear any existing content
        3. Type the proposed value
        4. Log the action in the session audit

        Sensitive fields and file uploads are skipped — user handles those.
        """
        if self._page is None:
            # Simulation mode — just log
            for f in session.fields:
                if not f.is_sensitive and f.proposed_value is not None:
                    session.log(f"[sim] Would fill '{f.label}' = '{f.proposed_value}' (from {f.evidence})")
            return

        # Map field IDs to Workday selectors
        selector_map = WORKDAY_FIELD_SELECTORS

        for f in session.fields:
            if f.is_sensitive or f.needs_user_input or f.proposed_value is None:
                continue

            selector = selector_map.get(f.field_id) or f"[id='{f.field_id}']"

            try:
                element = await self._page.query_selector(selector)
                if element is None:
                    session.log(f"Field not found: {f.label} (selector: {selector})")
                    continue

                await element.triple_click()
                await element.type(f.proposed_value, delay=50)
                session.log(f"Filled '{f.label}' = '{f.proposed_value[:30]}...' (from {f.evidence})")
            except Exception as exc:
                session.log(f"Failed to fill '{f.label}': {exc}")

        # Handle CV upload if approved
        if self.cv_path and self.cv_path.exists():
            await self._upload_cv(session)

    # ── Workday-specific methods ───────────────────────────────────────────

    async def _upload_cv(self, session: ApplicationSession) -> None:
        """Upload the user-approved CV to Workday's file upload field."""
        if self._page is None:
            session.log(f"[sim] Would upload CV: {self.cv_path}")
            return

        try:
            upload = await self._page.query_selector(WORKDAY_FILE_UPLOAD)
            if upload:
                await upload.set_input_files(str(self.cv_path))
                session.log(f"Uploaded CV: {self.cv_path.name}")
            else:
                session.log("CV upload field not found on this page")
        except Exception as exc:
            session.log(f"CV upload failed: {exc}")

    async def _detect_workday_review_page(self) -> bool:
        """Return True if the current page appears to be Workday's review step."""
        if self._page is None:
            return False
        try:
            content = (await self._page.content()).lower()
            return any(indicator in content for indicator in WORKDAY_REVIEW_INDICATORS)
        except Exception:
            return False

    async def advance_step(self, session: ApplicationSession) -> bool:
        """
        Click the 'Next' button to advance to the next Workday wizard step.

        This is safe — Next does not submit the application.
        Returns True if a next button was found and clicked.
        """
        if self._page is None:
            session.log("[sim] Would click Next")
            return True

        next_selectors = [
            'button[data-automation-id="bottom-navigation-next-button"]',
            'button[data-automation-id="next"]',
            'button:has-text("Next")',
            'button:has-text("Continue")',
        ]
        for sel in next_selectors:
            try:
                btn = await self._page.query_selector(sel)
                if btn:
                    label = (await btn.inner_text()).strip()
                    # Double-check: never click submit-like buttons
                    await self._safe_click(btn, label)
                    session.log(f"Clicked '{label}'")
                    await self._page.wait_for_load_state("networkidle", timeout=10000)
                    return True
            except SubmitGuardError:
                raise
            except Exception:
                continue
        return False

    async def _find_label(self, element: Any, field_id: str) -> str:
        """Find the label text for a form input element."""
        try:
            # Try aria-label first
            aria = await element.get_attribute("aria-label")
            if aria:
                return aria

            # Try associated <label> by for= attribute
            if self._page and field_id:
                label_el = await self._page.query_selector(f'label[for="{field_id}"]')
                if label_el:
                    return await label_el.inner_text()

            # Try placeholder
            placeholder = await element.get_attribute("placeholder")
            if placeholder:
                return placeholder

        except Exception:
            pass
        return field_id

    # ── Simulation / test helpers ──────────────────────────────────────────

    @staticmethod
    def _mock_workday_fields() -> list[FieldMapping]:
        """Return a realistic set of Workday fields for testing without a browser."""
        return [
            FieldMapping("first_name", "First Name", "text", None, True, False, None, False),
            FieldMapping("last_name", "Last Name", "text", None, True, False, None, False),
            FieldMapping("email", "Email Address", "email", None, True, False, None, False),
            FieldMapping("phone", "Phone Number", "tel", None, False, False, None, False),
            FieldMapping("linkedin", "LinkedIn Profile URL", "url", None, False, False, None, False),
            FieldMapping("city", "City", "text", None, False, False, None, False),
            FieldMapping("university", "University / School", "text", None, False, False, None, False),
            # Sensitive fields
            FieldMapping("salary_expectation", "Salary Expectation", "text", None, False, True, None, True),
            FieldMapping("work_authorization", "Are you authorised to work?", "select", None, True, True, None, True),
            FieldMapping("sponsorship", "Do you require visa sponsorship?", "select", None, True, True, None, True),
            FieldMapping("disability", "Do you have a disability?", "select", None, False, True, None, True),
            FieldMapping("gender", "Gender", "select", None, False, True, None, True),
        ]
