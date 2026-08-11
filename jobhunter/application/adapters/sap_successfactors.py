"""
SAP SuccessFactors ATS adapter.

SAP SuccessFactors is used by:
  Siemens, Continental, ZF, Knorr-Bremse, BASF, Bayer, Bosch (some divisions),
  and dozens of other large manufacturers.

URL pattern: jobs.<company>.com (typically) or <company>.jobs2web.com
             or careers.sap.com (for SAP itself)

SAP form structure (observed across multiple tenants):
  - Multi-step wizard with "Continue" buttons
  - Standard personal info section (name, email, phone)
  - Work authorisation section (sensitive — always pause)
  - CV upload (file input with data-automation-id or id="resume")
  - Cover letter upload (optional)
  - Custom questions per company (detected from label text)

This adapter is registered as "sap_successfactors" and is auto-detected
from URL patterns. Subclasses can override URL_PATTERNS to specialise.
"""
from __future__ import annotations

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

# SAP SuccessFactors common field selectors (observed across multiple tenants)
SAP_FIELD_SELECTORS: dict[str, str] = {
    "first_name": '[id*="firstName"], [name*="firstName"], [placeholder*="First"]',
    "last_name": '[id*="lastName"], [name*="lastName"], [placeholder*="Last"]',
    "email": '[type="email"], [id*="email"], [name*="email"]',
    "phone": '[id*="phone"], [id*="tel"], [type="tel"]',
    "address": '[id*="address"], [name*="address"]',
    "city": '[id*="city"], [name*="city"]',
    "country": '[id*="country"], [name*="country"]',
    "linkedin": '[id*="linkedin"], [placeholder*="LinkedIn"]',
    "resume_upload": '[id="resume"], input[type="file"]',
}

SAP_NEXT_SELECTORS = [
    '[data-automation-id="bottom-navigation-next-button"]',
    'button[id*="next"]',
    'button[id*="continue"]',
    'button:has-text("Continue")',
    'button:has-text("Next")',
    'button:has-text("Weiter")',   # German
    'button:has-text("Tovább")',   # Hungarian
]


@AdapterRegistry.register
class SAPSuccessFactorsAdapter(BaseApplicationAdapter):
    """
    Generic SAP SuccessFactors ATS adapter.

    Handles the standard SAP form structure shared across most tenants.
    Company-specific customisations appear as extra questions that are
    flagged for manual input.
    """

    name = "sap_successfactors"
    URL_PATTERNS = [
        "jobs2web.com",
        "successfactors.com",
        "sap-successfactors.com",
        "careers.sap.com",
    ]

    def __init__(
        self,
        profile: dict[str, Any],
        cv_path: Optional[Path] = None,
        screenshot_dir: Optional[Path] = None,
    ) -> None:
        super().__init__(profile, screenshot_dir)
        self.cv_path = cv_path

    async def _detect_fields(self) -> list[FieldMapping]:
        if self._page is None:
            return self._mock_sap_fields()

        fields: list[FieldMapping] = []
        try:
            inputs = await self._page.query_selector_all(
                "input:visible, textarea:visible, select:visible"
            )
            for inp in inputs:
                field_id = (
                    await inp.get_attribute("id") or
                    await inp.get_attribute("name") or
                    await inp.get_attribute("data-automation-id") or ""
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
            logger.warning("sap.detect_fields_error", error=str(exc))
        return fields

    async def _fill_fields(self, session: ApplicationSession) -> None:
        if self._page is None:
            for f in session.fields:
                if not f.is_sensitive and not f.needs_user_input and f.proposed_value:
                    session.log(f"[sim] Fill '{f.label}' = '{f.proposed_value}' (from {f.evidence})")
            return

        for f in session.fields:
            if f.is_sensitive or f.needs_user_input or f.proposed_value is None:
                continue

            # Try SAP-specific selectors first, then generic by ID
            selector = SAP_FIELD_SELECTORS.get(f.field_id) or f"[id='{f.field_id}']"
            try:
                el = await self._page.query_selector(selector)
                if el:
                    await el.triple_click()
                    await el.type(f.proposed_value, delay=40)
                    session.log(f"Filled '{f.label}'")
            except Exception as exc:
                session.log(f"Failed '{f.label}': {exc}")

        if self.cv_path and self.cv_path.exists():
            try:
                upload = await self._page.query_selector(SAP_FIELD_SELECTORS["resume_upload"])
                if upload:
                    await upload.set_input_files(str(self.cv_path))
                    session.log(f"Uploaded CV: {self.cv_path.name}")
            except Exception as exc:
                session.log(f"CV upload failed: {exc}")

    async def advance_step(self, session: ApplicationSession) -> bool:
        """Click the SAP wizard's Next/Continue button (safe — not a submit)."""
        if self._page is None:
            session.log("[sim] Would click Continue")
            return True

        for sel in SAP_NEXT_SELECTORS:
            try:
                btn = await self._page.query_selector(sel)
                if btn:
                    label = (await btn.inner_text()).strip()
                    await self._safe_click(btn, label)
                    await self._page.wait_for_load_state("networkidle", timeout=10000)
                    session.log(f"Advanced step: clicked '{label}'")
                    return True
            except SubmitGuardError:
                raise
            except Exception:
                continue
        return False

    @staticmethod
    def _mock_sap_fields() -> list[FieldMapping]:
        return [
            FieldMapping("first_name", "First Name", "text", None, True, False, None, False),
            FieldMapping("last_name", "Last Name", "text", None, True, False, None, False),
            FieldMapping("email", "Email Address", "email", None, True, False, None, False),
            FieldMapping("phone", "Phone Number", "tel", None, False, False, None, False),
            FieldMapping("city", "City", "text", None, False, False, None, False),
            FieldMapping("linkedin", "LinkedIn Profile URL", "url", None, False, False, None, False),
            # Sensitive SAP fields — always pause
            FieldMapping("work_authorization", "Work Authorisation", "select", None, True, True, None, True),
            FieldMapping("visa_sponsorship", "Do you require visa sponsorship?", "select", None, True, True, None, True),
            FieldMapping("salary_expectation", "Expected Salary", "text", None, False, True, None, True),
            FieldMapping("disability_status", "Disability Status", "select", None, False, True, None, True),
            FieldMapping("gender", "Gender", "select", None, False, True, None, True),
        ]
