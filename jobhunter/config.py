"""
Configuration system.

Loads secrets from .env via pydantic-settings,
loads search/connector config from config.yaml.
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml
from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application settings loaded from environment variables / .env file."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ── Database ──────────────────────────────
    database_url: str = Field(
        default="sqlite:///./data/jobhunter.db",
        description="SQLAlchemy database URL",
    )
    # Connection pool settings (PostgreSQL only — ignored for SQLite)
    db_pool_size: int = Field(default=5)
    db_max_overflow: int = Field(default=10)
    db_pool_timeout: int = Field(default=30)
    db_pool_recycle: int = Field(default=1800)

    # ── Security ──────────────────────────────
    secret_key: SecretStr = Field(
        default=SecretStr("dev-secret-change-in-production"),
        description="Secret key for signing tokens",
    )
    profile_encryption_key: SecretStr = Field(
        default=SecretStr(""),
        description="Fernet key for encrypting sensitive profile fields",
    )

    # ── Email IMAP (alert ingestion) ──────────
    alert_email_host: str = Field(default="imap.gmail.com")
    alert_email_port: int = Field(default=993)
    alert_email_user: str = Field(default="")
    alert_email_password: SecretStr = Field(default=SecretStr(""))
    alert_email_folder: str = Field(default="INBOX")

    # ── SMTP (outbound notifications) ─────────
    smtp_host: str = Field(default="smtp.gmail.com")
    smtp_port: int = Field(default=587)
    smtp_user: str = Field(default="")
    smtp_password: SecretStr = Field(default=SecretStr(""))
    notify_to_email: str = Field(default="")

    # ── Telegram ──────────────────────────────
    telegram_bot_token: SecretStr = Field(default=SecretStr(""))
    telegram_chat_id: str = Field(default="")

    # ── AI providers ──────────────────────────
    anthropic_api_key: SecretStr = Field(default=SecretStr(""))
    openai_api_key: SecretStr = Field(default=SecretStr(""))
    ai_provider: str = Field(default="none")  # anthropic | openai | none
    ai_model: str = Field(default="claude-sonnet-4-6")

    # ── Scheduler ─────────────────────────────
    search_interval_hours: int = Field(default=4)
    search_interval_jitter: int = Field(default=600)

    # ── Rate limiting ─────────────────────────
    min_request_delay_seconds: float = Field(default=5.0)
    max_request_delay_seconds: float = Field(default=15.0)

    # ── API & Dashboard ───────────────────────
    dashboard_port: int = Field(default=8501)
    api_port: int = Field(default=8000)

    # ── Logging ───────────────────────────────
    log_level: str = Field(default="INFO")
    log_file: str = Field(default="logs/jobhunter.log")

    @field_validator("ai_provider")
    @classmethod
    def validate_ai_provider(cls, v: str) -> str:
        """Ensure AI provider is a known value."""
        allowed = {"anthropic", "openai", "none"}
        if v not in allowed:
            raise ValueError(f"ai_provider must be one of {allowed}")
        return v

    @property
    def is_sqlite(self) -> bool:
        """True if the database URL points to SQLite."""
        return self.database_url.startswith("sqlite")

    @property
    def data_dir(self) -> Path:
        """Path to the data directory."""
        return Path("data")

    @property
    def log_path(self) -> Path:
        """Path to the log file."""
        return Path(self.log_file)


class SearchConfig:
    """
    Search and connector configuration loaded from config.yaml.

    Kept separate from Settings so it can be edited without
    touching secrets or restarting the process.
    """

    def __init__(self, path: str | Path = "config.yaml") -> None:
        self._path = Path(path)
        self._data: dict[str, Any] = {}
        self.reload()

    def reload(self) -> None:
        """Reload config from disk — useful for live config edits."""
        if not self._path.exists():
            example = self._path.parent / "config.example.yaml"
            if example.exists():
                import shutil
                shutil.copy(example, self._path)
            else:
                self._data = {}
                return
        with self._path.open() as f:
            self._data = yaml.safe_load(f) or {}

    # ── Convenience accessors ─────────────────

    @property
    def candidate(self) -> dict[str, Any]:
        return self._data.get("candidate", {})

    @property
    def keywords(self) -> dict[str, list[str]]:
        return self._data.get("search", {}).get("keywords", {})

    @property
    def all_keywords(self) -> list[str]:
        """Flat list of all search keywords."""
        result: list[str] = []
        for group in self.keywords.values():
            result.extend(group)
        return result

    @property
    def primary_locations(self) -> list[str]:
        return self._data.get("search", {}).get("locations", {}).get("primary", [])

    @property
    def all_locations(self) -> list[str]:
        locs = self._data.get("search", {}).get("locations", {})
        return locs.get("primary", []) + locs.get("secondary", [])

    @property
    def exclude_keywords(self) -> list[str]:
        return self._data.get("search", {}).get("exclude_keywords", [])

    @property
    def priority_companies(self) -> list[str]:
        return self._data.get("scoring", {}).get("priority_companies", [])

    @property
    def min_score_to_notify(self) -> int:
        return self._data.get("scoring", {}).get("min_score_to_notify", 75)

    @property
    def min_score_to_save(self) -> int:
        return self._data.get("scoring", {}).get("min_score_to_save", 40)

    @property
    def auto_reject_below(self) -> int:
        return self._data.get("scoring", {}).get("auto_reject_below", 20)

    @property
    def hungarian_mandatory_penalty(self) -> int:
        return self._data.get("scoring", {}).get("hungarian_mandatory_penalty", 20)

    @property
    def enabled_connectors(self) -> list[str]:
        return self._data.get("connectors", {}).get("enabled", [])

    def connector_config(self, name: str) -> dict[str, Any]:
        """Return config block for a specific connector."""
        return self._data.get("connectors", {}).get(name, {})

    @property
    def sensitive_fields(self) -> list[str]:
        return self._data.get("application", {}).get("sensitive_fields", [])

    @property
    def notification_channels(self) -> list[str]:
        return self._data.get("notifications", {}).get("channels", ["email"])

    @property
    def daily_summary_time(self) -> str:
        return self._data.get("notifications", {}).get("daily_summary_time", "08:00")


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return cached Settings instance (singleton)."""
    return Settings()


@lru_cache(maxsize=1)
def get_search_config() -> SearchConfig:
    """Return cached SearchConfig instance."""
    config_path = os.environ.get("JOBHUNTER_CONFIG", "config.yaml")
    return SearchConfig(path=config_path)
