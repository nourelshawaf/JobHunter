"""
Tests for the application assistance framework.

Critical tests:
- test_no_auto_submit: proves the submit guard cannot be bypassed
- test_sensitive_fields_never_filled: sensitive fields always block
- test_submit_guard_at_layer1-5: each guard layer independently verified

No real browser needed — all tests use simulation mode (page=None).
"""
from __future__ import annotations

import asyncio

import pytest

from jobhunter.application.base_adapter import (
    SENSITIVE_FIELDS,
    SUBMIT_PATTERNS,
    AdapterRegistry,
    ApplicationSession,
    BaseApplicationAdapter,
    FieldMapping,
    SubmitGuardError,
)
from jobhunter.application.adapters.workday import WorkdayAdapter


# ── Fixtures ──────────────────────────────────────────────────────────────

@pytest.fixture
def sample_profile() -> dict:
    return {
        "full_name": "Noureldeen Elshawaf",
        "email": "nour@example.com",
        "phone": "+36 70 123 4567",
        "city": "Budapest",
        "university": "University of Debrecen",
        "degree": "Mechatronics Engineering BSc",
        "linkedin_url": "https://linkedin.com/in/test",
        "github_url": "https://github.com/test",
        "graduation_expected": "2028",
    }


@pytest.fixture
def workday_adapter(sample_profile):
    return WorkdayAdapter(profile=sample_profile)


# ── Submit guard tests ────────────────────────────────────────────────────

class TestSubmitGuard:

    def test_layer1_submit_is_forbidden_always_true(self) -> None:
        """Layer 1: _submit_is_forbidden() is always True."""
        assert BaseApplicationAdapter._submit_is_forbidden() is True

    def test_layer1_click_submit_raises(self) -> None:
        """Layer 1: _click_submit() always raises SubmitGuardError."""
        with pytest.raises(SubmitGuardError) as exc_info:
            BaseApplicationAdapter._click_submit()
        assert "must never happen" in str(exc_info.value).lower() or "submit" in str(exc_info.value).lower()

    def test_layer2_safe_click_blocks_submit_label(self, workday_adapter) -> None:
        """Layer 2: _safe_click() raises on submit-pattern label."""
        for submit_label in ["Submit", "Apply", "Submit Application", "Confirm"]:
            with pytest.raises(SubmitGuardError):
                asyncio.run(workday_adapter._safe_click(None, label=submit_label))

    def test_layer2_safe_click_allows_next(self, workday_adapter) -> None:
        """Layer 2: _safe_click() does NOT raise for safe button labels."""
        safe_labels = ["Next", "Continue", "Save", "Back", "Upload"]
        for label in safe_labels:
            # Should not raise (element=None so no actual click)
            asyncio.run(workday_adapter._safe_click(None, label=label))

    def test_layer3_submit_patterns_are_comprehensive(self) -> None:
        """Layer 3: SUBMIT_PATTERNS covers all known submission actions."""
        required_patterns = {
            "submit", "apply", "confirm", "finish",
            "complete", "send", "send application",
        }
        for pattern in required_patterns:
            assert pattern in SUBMIT_PATTERNS, f"'{pattern}' missing from SUBMIT_PATTERNS"

    def test_layer4_session_stops_at_ready_for_review(self, workday_adapter) -> None:
        """Layer 4: run() returns in ready_for_review state, never submitted."""
        session = asyncio.run(
            workday_adapter.run(job_id="test-job-123", url="https://myworkdayjobs.com/apply")
        )
        # Must be ready_for_review OR awaiting_login/captcha — never 'submitted'
        assert session.status != "submitted"
        assert session.status != "completed"
        assert session.status in ("ready_for_review", "awaiting_login", "awaiting_captcha", "aborted")

    def test_layer5_submit_guard_error_has_label(self) -> None:
        """Layer 5: SubmitGuardError preserves the button label."""
        exc = SubmitGuardError("Apply Now")
        assert exc.button_label == "Apply Now"
        assert "Apply Now" in str(exc)

    def test_no_auto_submit_ever(self, workday_adapter) -> None:
        """
        Comprehensive test: run the full adapter workflow and verify
        the returned session has NEVER been submitted.
        """
        session = asyncio.run(
            workday_adapter.run(
                job_id="guard-test-job",
                url="https://wd3.myworkdayjobs.com/bakerhughes/jobs/123",
            )
        )
        # The guard test: status must NOT be any form of 'submitted'
        forbidden_statuses = {"submitted", "completed", "applied", "sent", "confirmed"}
        assert session.status not in forbidden_statuses

        # Audit log must NOT contain any submit action
        submit_log_patterns = ["clicked submit", "submitted application", "final submit"]
        full_log = " ".join(session.audit_log).lower()
        for pattern in submit_log_patterns:
            assert pattern not in full_log, f"Found forbidden log entry: '{pattern}'"


# ── Sensitive field tests ─────────────────────────────────────────────────

class TestSensitiveFields:

    def test_sensitive_fields_list_is_comprehensive(self) -> None:
        """Verify all legally/ethically sensitive fields are covered."""
        must_be_sensitive = [
            "salary", "disability", "ethnicity", "gender",
            "veteran", "criminal", "sponsorship", "visa",
            "consent", "relocation",
        ]
        for field_name in must_be_sensitive:
            assert any(
                field_name in s for s in SENSITIVE_FIELDS
            ), f"'{field_name}' not in SENSITIVE_FIELDS"

    def test_sensitive_field_never_auto_filled(self, workday_adapter, sample_profile) -> None:
        """Sensitive fields get needs_user_input=True and no proposed_value."""
        fields = [
            FieldMapping("salary_expectation", "Salary Expectation", "text",
                        None, False, True, None, True),
            FieldMapping("disability", "Do you have a disability?", "select",
                        None, False, True, None, True),
        ]
        workday_adapter._map_profile_to_fields(fields)
        for f in fields:
            assert f.proposed_value is None

    def test_workday_mock_fields_include_sensitive(self, workday_adapter) -> None:
        """WorkdayAdapter's mock fields include sensitive fields correctly marked."""
        fields = WorkdayAdapter._mock_workday_fields()
        sensitive = [f for f in fields if f.is_sensitive]
        assert len(sensitive) >= 4  # salary, sponsorship, disability, gender at minimum

    def test_is_sensitive_matches_salary(self) -> None:
        assert BaseApplicationAdapter._is_sensitive("Salary Expectation", "salary_field") is True

    def test_is_sensitive_does_not_match_name(self) -> None:
        assert BaseApplicationAdapter._is_sensitive("First Name", "first_name") is False


# ── Field mapping tests ───────────────────────────────────────────────────

class TestFieldMapping:

    def test_first_name_extracted_from_full_name(self, workday_adapter, sample_profile) -> None:
        fields = [
            FieldMapping("first_name", "First Name", "text", None, True, False, None, False)
        ]
        workday_adapter._map_profile_to_fields(fields)
        assert fields[0].proposed_value == "Noureldeen"
        assert fields[0].evidence == "full_name"

    def test_last_name_extracted_correctly(self, workday_adapter, sample_profile) -> None:
        fields = [
            FieldMapping("last_name", "Last Name", "text", None, True, False, None, False)
        ]
        workday_adapter._map_profile_to_fields(fields)
        assert fields[0].proposed_value == "Elshawaf"

    def test_email_mapped_directly(self, workday_adapter, sample_profile) -> None:
        fields = [
            FieldMapping("email", "Email", "email", None, True, False, None, False)
        ]
        workday_adapter._map_profile_to_fields(fields)
        assert fields[0].proposed_value == "nour@example.com"

    def test_city_mapped_directly(self, workday_adapter, sample_profile) -> None:
        fields = [
            FieldMapping("city", "City", "text", None, False, False, None, False)
        ]
        workday_adapter._map_profile_to_fields(fields)
        assert fields[0].proposed_value == "Budapest"

    def test_unknown_field_gets_no_value(self, workday_adapter) -> None:
        fields = [
            FieldMapping("xyzzy_field", "Unknown Weird Field", "text", None, False, False, None, False)
        ]
        workday_adapter._map_profile_to_fields(fields)
        assert fields[0].proposed_value is None

    def test_evidence_recorded_for_all_filled_fields(self, workday_adapter) -> None:
        fields = [
            FieldMapping("email", "Email", "email", None, True, False, None, False),
            FieldMapping("city", "City", "text", None, False, False, None, False),
        ]
        workday_adapter._map_profile_to_fields(fields)
        for f in fields:
            if f.proposed_value is not None:
                assert f.evidence is not None, f"No evidence for {f.label}"


# ── Adapter registry tests ────────────────────────────────────────────────

class TestAdapterRegistry:

    def test_workday_is_registered(self) -> None:
        assert "workday" in AdapterRegistry.all_names()

    def test_get_workday_returns_class(self) -> None:
        cls = AdapterRegistry.get("workday")
        assert cls is WorkdayAdapter

    def test_detect_workday_from_url(self) -> None:
        url = "https://wd3.myworkdayjobs.com/bakerhughes/jobs/123"
        cls = AdapterRegistry.detect(url)
        assert cls is WorkdayAdapter

    def test_detect_returns_none_for_unknown_ats(self) -> None:
        url = "https://some-unknown-ats.io/apply/123"
        cls = AdapterRegistry.detect(url)
        assert cls is None

    def test_get_nonexistent_returns_none(self) -> None:
        assert AdapterRegistry.get("nonexistent_ats") is None


# ── Session summary test ──────────────────────────────────────────────────

class TestApplicationSession:

    def test_summary_structure(self) -> None:
        session = ApplicationSession(
            job_id="test-123",
            adapter_name="workday",
            url="https://example.com/apply",
        )
        session.log("Test log entry")
        summary = session.to_summary()

        assert "job_id" in summary
        assert "adapter" in summary
        assert "status" in summary
        assert "fields_auto_filled" in summary
        assert "fields_needing_user_input" in summary
        assert "audit_log" in summary

    def test_log_entries_appear_in_summary(self) -> None:
        session = ApplicationSession(
            job_id="log-test",
            adapter_name="workday",
            url="https://example.com",
        )
        session.log("First entry")
        session.log("Second entry")
        summary = session.to_summary()
        log_text = " ".join(summary["audit_log"])
        assert "First entry" in log_text
        assert "Second entry" in log_text


# ── Workday-specific tests ────────────────────────────────────────────────

class TestWorkdayAdapter:

    def test_run_returns_session(self, workday_adapter) -> None:
        session = asyncio.run(
            workday_adapter.run("job-001", "https://myworkdayjobs.com/apply")
        )
        assert isinstance(session, ApplicationSession)

    def test_run_fills_name_fields_in_simulation(self, workday_adapter) -> None:
        session = asyncio.run(
            workday_adapter.run("job-002", "https://myworkdayjobs.com/apply")
        )
        summary = session.to_summary()
        # Check that name was auto-filled
        filled_labels = [f["label"] for f in summary["fields_auto_filled"]]
        assert any("name" in label.lower() for label in filled_labels)

    def test_run_marks_salary_as_needing_input(self, workday_adapter) -> None:
        session = asyncio.run(
            workday_adapter.run("job-003", "https://myworkdayjobs.com/apply")
        )
        summary = session.to_summary()
        sensitive_labels = [f["label"] for f in summary["fields_needing_user_input"]]
        assert any("salary" in label.lower() for label in sensitive_labels)

    def test_advance_step_in_simulation(self, workday_adapter) -> None:
        session = ApplicationSession(
            job_id="step-test", adapter_name="workday", url="https://example.com"
        )
        result = asyncio.run(workday_adapter.advance_step(session))
        assert result is True  # simulation always returns True
        assert any("Would click Next" in entry for entry in session.audit_log)


# ── Greenhouse adapter tests ──────────────────────────────────────────────

class TestGreenhouseAdapter:

    @pytest.fixture
    def gh_adapter(self, sample_profile):
        from jobhunter.application.adapters.greenhouse import GreenhouseAdapter
        return GreenhouseAdapter(profile=sample_profile)

    def test_greenhouse_is_registered(self) -> None:
        from jobhunter.application.adapters.greenhouse import GreenhouseAdapter
        assert "greenhouse" in AdapterRegistry.all_names()

    def test_detect_greenhouse_from_url(self) -> None:
        url = "https://boards.greenhouse.io/somecorp/jobs/12345"
        from jobhunter.application.adapters.greenhouse import GreenhouseAdapter
        cls = AdapterRegistry.detect(url)
        assert cls is GreenhouseAdapter

    def test_run_returns_session(self, gh_adapter) -> None:
        session = asyncio.run(
            gh_adapter.run("gh-job-001", "https://boards.greenhouse.io/bosch/jobs/456")
        )
        assert isinstance(session, ApplicationSession)

    def test_run_never_submits(self, gh_adapter) -> None:
        session = asyncio.run(
            gh_adapter.run("gh-job-002", "https://boards.greenhouse.io/siemens/jobs/789")
        )
        assert session.status not in {"submitted", "completed", "applied"}

    def test_custom_question_flagged_for_user(self, gh_adapter) -> None:
        session = asyncio.run(
            gh_adapter.run("gh-job-003", "https://boards.greenhouse.io/test/jobs/999")
        )
        summary = session.to_summary()
        sensitive_labels = [f["label"] for f in summary["fields_needing_user_input"]]
        assert any("authorised" in label.lower() or "salary" in label.lower()
                   for label in sensitive_labels)

    def test_submit_guard_applies_to_greenhouse(self, gh_adapter) -> None:
        with pytest.raises(SubmitGuardError):
            asyncio.run(gh_adapter._safe_click(None, label="Submit Application"))
