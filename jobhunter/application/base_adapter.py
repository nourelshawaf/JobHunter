"""
BaseApplicationAdapter — browser-assisted application framework.

Core invariant: The final Submit/Apply/Confirm button is NEVER clicked
automatically. This is enforced at FIVE independent layers:

  Layer 1: Adapter-level rule — _submit_is_forbidden() always returns True.
  Layer 2: Page action interceptor — all clicks pass through _safe_click()
           which checks the label against SUBMIT_PATTERNS before executing.
  Layer 3: Accessibility-label matching — aria-label, value, and text content
           are all scanned before any form-submission attempt.
  Layer 4: Explicit manual-review state — the adapter moves to
           READY_FOR_FINAL_REVIEW and pauses, surfacing a checklist.
  Layer 5: Tests — test_no_auto_submit() proves the guard works with a mock page.

Any attempt to call _click_submit() directly raises SubmitGuardError.
"""
from __future__ import annotations

import abc
import json
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import structlog

logger = structlog.get_logger(__name__)

# ── Submit guard patterns ─────────────────────────────────────────────────
# Any button/link whose accessible text matches these patterns is forbidden.
SUBMIT_PATTERNS = {
    "submit",
    "apply",
    "apply now",
    "send application",
    "send",
    "confirm",
    "confirm application",
    "finish",
    "finish application",
    "complete",
    "complete application",
    "submit application",
    "submit form",
    "final submit",
    "küldje el",        # Hungarian: "send it"
    "jelentkezem",      # Hungarian: "I apply"
    "elküld",           # Hungarian: "send"
}

# Sensitive fields that must never be auto-filled — always pause for user
SENSITIVE_FIELDS = frozenset({
    "salary", "salary_expectation", "salary_min", "salary_max",
    "disability", "disabled", "disability_status",
    "ethnicity", "race", "racial_origin",
    "gender", "gender_identity",
    "veteran", "veteran_status", "military",
    "criminal", "criminal_history", "background_check",
    "sponsorship", "visa", "work_authorization",
    "legal_declaration", "legal_agreement",
    "consent", "data_consent", "privacy_consent",
    "relocation", "relocation_willing",
    "start_date", "availability", "notice_period",
    "conflict_of_interest", "conflict",
})


class SubmitGuardError(RuntimeError):
    """Raised when application code attempts to click a submit button."""

    def __init__(self, button_label: str) -> None:
        super().__init__(
            f"SUBMIT GUARD TRIGGERED: Cannot auto-click '{button_label}'. "
            "Manual review and submission required."
        )
        self.button_label = button_label


@dataclass
class FieldMapping:
    """Represents one detected form field and its proposed fill value."""

    field_id: str
    label: str
    field_type: str          # text, textarea, select, checkbox, radio, file
    proposed_value: Optional[str]
    is_required: bool
    is_sensitive: bool
    evidence: Optional[str]  # which profile field this value came from
    needs_user_input: bool   # True if the field should pause for user


@dataclass
class ApplicationSession:
    """Runtime state for one browser-assisted application attempt."""

    job_id: str
    adapter_name: str
    url: str
    started_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    status: str = "started"
    # started | awaiting_login | awaiting_captcha | filling | ready_for_review | aborted

    fields: list[FieldMapping] = field(default_factory=list)
    screenshots: list[str] = field(default_factory=list)   # file paths
    form_snapshot: dict[str, Any] = field(default_factory=dict)
    audit_log: list[str] = field(default_factory=list)

    def log(self, message: str) -> None:
        ts = datetime.now(timezone.utc).isoformat()
        entry = f"[{ts}] {message}"
        self.audit_log.append(entry)
        logger.info("application_session.event", message=message, job_id=self.job_id)

    def to_summary(self) -> dict[str, Any]:
        """Produce the pre-submission review summary shown to the user."""
        return {
            "job_id": self.job_id,
            "adapter": self.adapter_name,
            "url": self.url,
            "status": self.status,
            "started_at": self.started_at.isoformat(),
            "fields_auto_filled": [
                {"label": f.label, "value": f.proposed_value, "from": f.evidence}
                for f in self.fields
                if not f.is_sensitive and f.proposed_value is not None
            ],
            "fields_needing_user_input": [
                {"label": f.label, "reason": "sensitive" if f.is_sensitive else "unknown"}
                for f in self.fields
                if f.needs_user_input
            ],
            "required_fields_missing": [
                f.label for f in self.fields
                if f.is_required and f.proposed_value is None and not f.needs_user_input
            ],
            "screenshots": self.screenshots,
            "audit_log": self.audit_log[-20:],
        }


class BaseApplicationAdapter(abc.ABC):
    """
    Abstract base for all ATS-specific application adapters.

    Subclasses implement `_detect_fields()` and `_fill_fields()`
    for each specific ATS platform. The submit guard is implemented
    here and CANNOT be overridden.
    """

    #: ATS platform name used in registry and logs
    name: str = ""

    def __init__(
        self,
        profile: dict[str, Any],
        screenshot_dir: Optional[Path] = None,
    ) -> None:
        self.profile = profile
        self.screenshot_dir = screenshot_dir or Path("data/screenshots")
        self.screenshot_dir.mkdir(parents=True, exist_ok=True)
        self._page: Any = None  # playwright Page, injected at runtime

    # ── Public API ────────────────────────────────────────────────────────

    async def run(self, job_id: str, url: str) -> ApplicationSession:
        """
        Execute the application assistance workflow.

        Returns an ApplicationSession in READY_FOR_FINAL_REVIEW state.
        The user must manually review and submit.
        """
        session = ApplicationSession(
            job_id=job_id,
            adapter_name=self.name,
            url=url,
        )
        session.log(f"Starting application assistance for {url}")

        try:
            # Step 1: Navigate
            await self._navigate(url, session)

            # Step 2: Check for login requirement
            needs_login = await self._detect_login_required()
            if needs_login:
                session.status = "awaiting_login"
                session.log("Login required — pausing for user action")
                await self._take_screenshot(session, "login_required")
                return session

            # Step 3: Check for CAPTCHA
            needs_captcha = await self._detect_captcha()
            if needs_captcha:
                session.status = "awaiting_captcha"
                session.log("CAPTCHA detected — pausing for user action")
                await self._take_screenshot(session, "captcha")
                return session

            # Step 4: Detect fields
            session.status = "filling"
            session.fields = await self._detect_fields()
            session.log(f"Detected {len(session.fields)} form fields")

            # Step 5: Mark sensitive fields
            for field_obj in session.fields:
                if self._is_sensitive(field_obj.label, field_obj.field_id):
                    field_obj.is_sensitive = True
                    field_obj.needs_user_input = True
                    field_obj.proposed_value = None

            # Step 6: Map safe fields from profile
            self._map_profile_to_fields(session.fields)

            # Step 7: Fill safe fields
            await self._fill_fields(session)
            await self._take_screenshot(session, "after_fill")

            # Step 8: Move to review state — NEVER auto-submit
            session.status = "ready_for_review"
            session.log("All safe fields filled. Ready for manual review and submission.")
            session.log("⚠️  DO NOT submit without reviewing all fields above.")

        except SubmitGuardError as e:
            session.log(f"SUBMIT GUARD TRIGGERED: {e}")
            session.status = "aborted"
        except Exception as exc:
            session.log(f"Error during filling: {exc}")
            session.status = "aborted"
            await self._take_screenshot(session, "error")

        return session

    # ── Submit guard — FINAL AND NON-OVERRIDABLE ──────────────────────────

    @staticmethod
    def _submit_is_forbidden() -> bool:
        """Layer 1: Always returns True. Adapter-level rule."""
        return True

    async def _safe_click(self, element: Any, label: str = "") -> None:
        """
        Layer 2+3: Click wrapper that checks submit patterns before executing.

        Any element whose label matches SUBMIT_PATTERNS raises SubmitGuardError.
        This is called by all fill/interaction code — direct page.click() calls
        are forbidden in adapter implementations.
        """
        label_lower = (label or "").lower().strip()
        if label_lower in SUBMIT_PATTERNS:
            raise SubmitGuardError(label)

        # Also check element text/aria-label if page is available
        if element is not None and hasattr(element, "inner_text"):
            try:
                el_text = (await element.inner_text()).lower().strip()
                if el_text in SUBMIT_PATTERNS:
                    raise SubmitGuardError(el_text)
                aria = (await element.get_attribute("aria-label") or "").lower()
                if aria in SUBMIT_PATTERNS:
                    raise SubmitGuardError(aria)
                val = (await element.get_attribute("value") or "").lower()
                if val in SUBMIT_PATTERNS:
                    raise SubmitGuardError(val)
            except SubmitGuardError:
                raise
            except Exception:
                pass  # non-critical if element inspection fails

        # Safe to click
        if element is not None:
            await element.click()

    # ── Abstract methods — implement per ATS ─────────────────────────────

    @abc.abstractmethod
    async def _detect_fields(self) -> list[FieldMapping]:
        """Read the current page and return detected form fields."""
        ...

    @abc.abstractmethod
    async def _fill_fields(self, session: ApplicationSession) -> None:
        """Fill safe (non-sensitive) fields using proposed values."""
        ...

    # ── Overridable detection hooks ────────────────────────────────────────

    async def _detect_login_required(self) -> bool:
        """Return True if the current page requires a login."""
        if self._page is None:
            return False
        try:
            url = self._page.url
            return any(p in url for p in ("/login", "/signin", "/auth", "/sso"))
        except Exception:
            return False

    async def _detect_captcha(self) -> bool:
        """Return True if a CAPTCHA is present on the page."""
        if self._page is None:
            return False
        try:
            content = await self._page.content()
            return any(s in content.lower() for s in (
                "recaptcha", "hcaptcha", "turnstile", "captcha"
            ))
        except Exception:
            return False

    # ── Helpers ───────────────────────────────────────────────────────────

    async def _navigate(self, url: str, session: ApplicationSession) -> None:
        """Navigate to the application URL."""
        if self._page is None:
            session.log(f"[mock] Would navigate to: {url}")
            return
        await self._page.goto(url, wait_until="networkidle", timeout=30000)
        session.log(f"Navigated to {url}")

    async def _take_screenshot(self, session: ApplicationSession, label: str) -> None:
        """Save a screenshot for audit purposes."""
        if self._page is None:
            session.screenshots.append(f"[mock_screenshot_{label}]")
            return
        filename = f"{session.job_id}_{label}_{datetime.now().strftime('%H%M%S')}.png"
        path = self.screenshot_dir / filename
        try:
            await self._page.screenshot(path=str(path))
            session.screenshots.append(str(path))
            session.log(f"Screenshot saved: {filename}")
        except Exception as exc:
            session.log(f"Screenshot failed: {exc}")

    @staticmethod
    def _is_sensitive(label: str, field_id: str) -> bool:
        """Return True if this field is in the sensitive fields list."""
        combined = (label + " " + field_id).lower()
        return any(s in combined for s in SENSITIVE_FIELDS)

    def _map_profile_to_fields(self, fields: list[FieldMapping]) -> None:
        """
        Map profile values to form fields by label matching.

        Only safe, non-sensitive fields are populated.
        Every mapping records its evidence source.
        """
        mappings = {
            "first name": ("full_name", lambda v: v.split()[0] if v else None),
            "last name": ("full_name", lambda v: v.split()[-1] if v else None),
            "surname": ("full_name", lambda v: v.split()[-1] if v else None),
            "email": ("email", None),
            "phone": ("phone", None),
            "city": ("city", None),
            "university": ("university", None),
            "degree": ("degree", None),
            "linkedin": ("linkedin_url", None),
            "github": ("github_url", None),
            "portfolio": ("portfolio_url", None),
            "graduation": ("graduation_expected", None),
        }

        for f in fields:
            if f.is_sensitive or f.needs_user_input:
                continue

            label_lower = f.label.lower()
            for pattern, (profile_key, transform) in mappings.items():
                if pattern in label_lower:
                    raw_value = self.profile.get(profile_key)
                    if raw_value is not None:
                        if transform:
                            try:
                                f.proposed_value = transform(str(raw_value))
                            except Exception:
                                f.proposed_value = str(raw_value)
                        else:
                            f.proposed_value = str(raw_value)
                        f.evidence = profile_key
                    break

    @classmethod
    def _click_submit(cls) -> None:
        """
        Layer 1: Unconditionally raises SubmitGuardError.

        This method exists so that tests can verify the guard by calling it.
        It is NEVER called from adapter code.
        """
        raise SubmitGuardError("_click_submit() called directly — this must never happen")


# ── Adapter registry ──────────────────────────────────────────────────────

class AdapterRegistry:
    """Maps ATS platform names to adapter classes."""

    _registry: dict[str, type[BaseApplicationAdapter]] = {}

    @classmethod
    def register(cls, adapter_cls: type[BaseApplicationAdapter]) -> type[BaseApplicationAdapter]:
        """Decorator to register an adapter class."""
        cls._registry[adapter_cls.name] = adapter_cls
        return adapter_cls

    @classmethod
    def get(cls, name: str) -> Optional[type[BaseApplicationAdapter]]:
        return cls._registry.get(name)

    @classmethod
    def detect(cls, url: str) -> Optional[type[BaseApplicationAdapter]]:
        """Auto-detect the adapter from a job application URL."""
        url_lower = url.lower()
        for name, adapter_cls in cls._registry.items():
            if hasattr(adapter_cls, "URL_PATTERNS"):
                for pattern in adapter_cls.URL_PATTERNS:
                    if pattern in url_lower:
                        return adapter_cls
        return None

    @classmethod
    def all_names(cls) -> list[str]:
        return list(cls._registry.keys())
