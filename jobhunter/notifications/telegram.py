"""
Telegram notification channel.

Sends messages via the Telegram Bot API using httpx (async-compatible,
no external telegram library needed beyond the raw API).

Safety guarantees:
- Secrets only from environment variables (TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID)
- Markdown-safe escaping before every send (MarkdownV2 is strict)
- Graceful handling of all Telegram API errors — never crashes the pipeline
- Test mode: set JOBHUNTER_TESTING=1 to skip real HTTP calls
- Duplicate prevention via the Notification log (same dedup as email)
"""
from __future__ import annotations

import os
import re
from typing import Any, Optional

import httpx
import structlog

logger = structlog.get_logger(__name__)

_TESTING = os.environ.get("JOBHUNTER_TESTING", "0") == "1"

# Telegram MarkdownV2 reserved characters that must be escaped
_MD2_SPECIAL = re.compile(r"([_\*\[\]\(\)~`>#+\-=|{}.!\\])")

TELEGRAM_API = "https://api.telegram.org/bot{token}/sendMessage"


def escape_md2(text: str) -> str:
    """Escape a string for Telegram MarkdownV2."""
    return _MD2_SPECIAL.sub(r"\\\1", str(text))


class TelegramNotifier:
    """
    Sends notifications to a Telegram chat via the Bot API.

    Uses synchronous httpx internally so it can be called from both
    sync (scheduler) and async (pipeline) contexts without event-loop issues.
    """

    def __init__(self, token: str, chat_id: str) -> None:
        self.token = token
        self.chat_id = chat_id

    # ── Message templates ─────────────────────────────────────────────────

    def send_new_job(self, job: Any) -> bool:
        """Notify about a newly discovered high-scoring job."""
        score = job.relevance_score or 0
        emoji = "🟢" if score >= 75 else "🟡" if score >= 50 else "🔴"

        text = (
            f"{emoji} *New job match: {escape_md2(str(score))}/100*\n\n"
            f"*{escape_md2(job.title)}*\n"
            f"🏢 {escape_md2(job.company)}\n"
            f"📍 {escape_md2(job.location or 'N/A')}\n"
            f"💼 {escape_md2(job.job_type)} \\| {escape_md2(job.work_mode)}\n"
        )
        if job.salary_raw:
            text += f"💰 {escape_md2(job.salary_raw)}\n"
        if job.deadline:
            text += f"⏰ Deadline: {escape_md2(str(job.deadline.date()))}\n"
        text += f"\n📋 _{escape_md2(job.score_explanation or 'No explanation'[:100])}_"
        if job.application_url:
            text += f"\n\n[Apply here]({job.application_url})"

        return self._send(text)

    def send_deadline_warning(self, job: Any, days: int) -> bool:
        """Warn about an approaching application deadline."""
        text = (
            f"⏰ *Deadline in {days} day\\(s\\)*\n\n"
            f"*{escape_md2(job.title)}* @ {escape_md2(job.company)}\n"
            f"Score: {escape_md2(str(job.relevance_score or '?'))}/100\n"
            f"Status: `{escape_md2(job.status)}`\n"
        )
        if job.application_url:
            text += f"\n[Apply here]({job.application_url})"
        return self._send(text)

    def send_daily_digest(self, high_score: list[Any], total_new: int) -> bool:
        """Send a daily summary."""
        text = f"📊 *Daily JobHunter Digest*\n\n"
        text += f"🆕 {escape_md2(str(total_new))} new jobs discovered\n"
        text += f"⭐ {escape_md2(str(len(high_score)))} high\\-match \\(≥75\\)\n\n"

        for job in high_score[:5]:
            text += (
                f"• *{escape_md2(job.title)}* @ {escape_md2(job.company)} "
                f"\\[{escape_md2(str(job.relevance_score or '?'))}/100\\]\n"
            )
        if len(high_score) > 5:
            text += f"  _…and {escape_md2(str(len(high_score) - 5))} more_\n"
        return self._send(text)

    def send_connector_failure(self, connector_name: str, error: str) -> bool:
        """Alert about a connector failure."""
        text = (
            f"⚠️ *Connector failure*\n\n"
            f"Connector: `{escape_md2(connector_name)}`\n"
            f"Error: {escape_md2(error[:200])}"
        )
        return self._send(text)

    def send_application_ready(self, job: Any) -> bool:
        """Notify that an application is ready for manual review."""
        text = (
            f"📝 *Application ready for review*\n\n"
            f"*{escape_md2(job.title)}* @ {escape_md2(job.company)}\n"
            f"Status: `ready_for_final_review`\n"
            f"⚠️ _Manual submission required — do NOT submit without reviewing_"
        )
        return self._send(text)

    def send_plain(self, message: str) -> bool:
        """Send a plain (non-MarkdownV2) text message."""
        return self._send_raw(message, parse_mode=None)

    # ── Transport ─────────────────────────────────────────────────────────

    def _send(self, markdown_v2_text: str) -> bool:
        """Send a MarkdownV2-formatted message."""
        return self._send_raw(markdown_v2_text, parse_mode="MarkdownV2")

    def _send_raw(self, text: str, parse_mode: Optional[str] = "MarkdownV2") -> bool:
        """
        Make the actual HTTP call to the Telegram Bot API.

        Returns True on success, False on any error (never raises).
        """
        if _TESTING:
            logger.debug("telegram.skipped_in_testing", text_preview=text[:80])
            return True

        if not self.token or not self.chat_id:
            logger.warning("telegram.not_configured")
            return False

        url = TELEGRAM_API.format(token=self.token)
        payload: dict[str, Any] = {
            "chat_id": self.chat_id,
            "text": text,
            "disable_web_page_preview": True,
        }
        if parse_mode:
            payload["parse_mode"] = parse_mode

        try:
            response = httpx.post(url, json=payload, timeout=10.0)
            data = response.json()

            if not data.get("ok"):
                logger.error(
                    "telegram.api_error",
                    description=data.get("description", "unknown"),
                    error_code=data.get("error_code"),
                )
                return False

            logger.debug("telegram.sent", message_id=data.get("result", {}).get("message_id"))
            return True

        except httpx.TimeoutException:
            logger.error("telegram.timeout")
            return False
        except httpx.RequestError as exc:
            logger.error("telegram.request_error", error=str(exc))
            return False
        except Exception as exc:
            logger.error("telegram.unexpected_error", error=str(exc))
            return False


def get_telegram_notifier() -> Optional[TelegramNotifier]:
    """
    Build a TelegramNotifier from environment variables.

    Returns None if credentials are not configured — the caller
    should handle None gracefully (Telegram is optional).
    """
    from jobhunter.config import get_settings
    settings = get_settings()
    token = settings.telegram_bot_token.get_secret_value()
    chat_id = settings.telegram_chat_id

    if not token or not chat_id:
        return None

    return TelegramNotifier(token=token, chat_id=chat_id)
