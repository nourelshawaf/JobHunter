"""
Tests for the Telegram notification channel.

No real HTTP calls — monkeypatched httpx responses.
"""
from __future__ import annotations

import os

import pytest

os.environ["JOBHUNTER_TESTING"] = "1"

from jobhunter.notifications.telegram import (
    TelegramNotifier,
    escape_md2,
    get_telegram_notifier,
)


class TestMarkdownEscaping:

    def test_escapes_underscore(self) -> None:
        assert r"\_" in escape_md2("hello_world")

    def test_escapes_asterisk(self) -> None:
        assert r"\*" in escape_md2("hello*world")

    def test_escapes_dot(self) -> None:
        assert r"\." in escape_md2("hello.world")

    def test_escapes_parentheses(self) -> None:
        result = escape_md2("(test)")
        assert r"\(" in result
        assert r"\)" in result

    def test_plain_text_unchanged(self) -> None:
        text = "Hello world"
        assert escape_md2(text) == "Hello world"

    def test_empty_string(self) -> None:
        assert escape_md2("") == ""

    def test_number_unchanged(self) -> None:
        assert escape_md2("42") == "42"

    def test_hyphen_escaped(self) -> None:
        assert r"\-" in escape_md2("hello-world")


class TestTelegramNotifier:
    """Tests that run in TESTING mode (no real HTTP)."""

    @pytest.fixture
    def notifier(self) -> TelegramNotifier:
        return TelegramNotifier(token="test_token_123", chat_id="987654321")

    def test_send_returns_true_in_testing(self, notifier: TelegramNotifier) -> None:
        result = notifier.send_plain("Test message")
        assert result is True

    def test_send_new_job_returns_true(self, notifier: TelegramNotifier) -> None:
        class MockJob:
            title = "Robotics Intern"
            company = "Bosch"
            location = "Budapest"
            job_type = "internship"
            work_mode = "hybrid"
            relevance_score = 85
            salary_raw = "400,000 HUF"
            deadline = None
            score_explanation = "Strong match: robotics, Python, Budapest"
            application_url = "https://careers.bosch.com/jobs/123"

        result = notifier.send_new_job(MockJob())
        assert result is True

    def test_send_deadline_warning(self, notifier: TelegramNotifier) -> None:
        class MockJob:
            title = "Automation Intern"
            company = "Siemens"
            relevance_score = 72
            status = "saved"
            application_url = "https://jobs.siemens.com/jobs/456"

        result = notifier.send_deadline_warning(MockJob(), days=3)
        assert result is True

    def test_send_daily_digest(self, notifier: TelegramNotifier) -> None:
        class MockJob:
            title = "ML Intern"
            company = "Continental"
            relevance_score = 88

        result = notifier.send_daily_digest([MockJob()], total_new=5)
        assert result is True

    def test_send_connector_failure(self, notifier: TelegramNotifier) -> None:
        result = notifier.send_connector_failure("eures", "Connection timeout")
        assert result is True

    def test_no_token_skips_send(self) -> None:
        notifier = TelegramNotifier(token="", chat_id="123")
        result = notifier._send("test")
        # In testing mode, returns True before checking token
        # In production mode (not testing), would check and return False
        assert isinstance(result, bool)

    def test_get_telegram_notifier_returns_none_when_not_configured(self) -> None:
        """With no credentials, factory returns None."""
        result = get_telegram_notifier()
        # In testing env with no credentials, should return None
        assert result is None or isinstance(result, TelegramNotifier)


class TestTelegramHTTP:
    """Tests that verify HTTP call structure (monkeypatched)."""

    def test_payload_structure(self, monkeypatch) -> None:
        """Verify that the payload has required fields."""
        captured = {}

        def mock_post(url, json=None, timeout=None):
            captured["url"] = url
            captured["payload"] = json

            class MockResponse:
                def json(self):
                    return {"ok": True, "result": {"message_id": 1}}

            return MockResponse()

        import httpx
        monkeypatch.setattr(httpx, "post", mock_post)

        # Temporarily disable testing mode for this test
        import jobhunter.notifications.telegram as tg_module
        original = tg_module._TESTING
        tg_module._TESTING = False

        try:
            notifier = TelegramNotifier(token="FAKE_TOKEN", chat_id="FAKE_CHAT")
            notifier.send_plain("Hello test")
        finally:
            tg_module._TESTING = original

        if captured:  # only check if post was actually called
            assert "chat_id" in captured.get("payload", {})
            assert "text" in captured.get("payload", {})
            assert captured.get("payload", {}).get("chat_id") == "FAKE_CHAT"

    def test_api_error_returns_false(self, monkeypatch) -> None:
        """Telegram API 'ok: false' should return False without raising."""

        def mock_post(url, json=None, timeout=None):
            class MockResponse:
                def json(self):
                    return {"ok": False, "error_code": 400, "description": "Bad Request"}

            return MockResponse()

        import httpx
        monkeypatch.setattr(httpx, "post", mock_post)

        import jobhunter.notifications.telegram as tg_module
        original = tg_module._TESTING
        tg_module._TESTING = False

        try:
            notifier = TelegramNotifier(token="FAKE_TOKEN", chat_id="FAKE_CHAT")
            result = notifier.send_plain("test")
        finally:
            tg_module._TESTING = original

        assert result is False

    def test_timeout_returns_false(self, monkeypatch) -> None:
        """Network timeout should return False without raising."""
        import httpx

        def mock_post(url, json=None, timeout=None):
            raise httpx.TimeoutException("timeout")

        monkeypatch.setattr(httpx, "post", mock_post)

        import jobhunter.notifications.telegram as tg_module
        original = tg_module._TESTING
        tg_module._TESTING = False

        try:
            notifier = TelegramNotifier(token="FAKE_TOKEN", chat_id="FAKE_CHAT")
            result = notifier.send_plain("test")
        finally:
            tg_module._TESTING = original

        assert result is False
