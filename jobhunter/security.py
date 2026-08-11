"""
Security audit checker.

Detects common security mistakes in the repository before commits:
- Committed .env files with real credentials
- Database files in tracked paths
- CV or personal document files
- Browser session directories
- Hardcoded tokens or API keys in source files
- Log files with sensitive content
- Telegram tokens or email passwords in config

Run with: python -m jobhunter.cli security-check
"""
from __future__ import annotations

import os
import re
from pathlib import Path

import structlog

logger = structlog.get_logger(__name__)

# Patterns that should never appear in committed source files
SECRET_PATTERNS = [
    re.compile(r"sk-[A-Za-z0-9]{20,}", re.I),          # OpenAI keys
    re.compile(r"sk-ant-[A-Za-z0-9\-]{20,}", re.I),    # Anthropic keys
    re.compile(r"(?i)telegram.*token.*=.*[0-9]{8,}"),
    re.compile(r"(?i)bot_token\s*=\s*['\"][0-9]{8,}"),
    re.compile(r"(?i)password\s*=\s*['\"][^'\"]{6,}"),
    re.compile(r"(?i)smtp_password\s*=\s*['\"][^'\"]{3,}"),
    re.compile(r"(?i)secret_key\s*=\s*['\"][^'\"]{10,}"),
]

# File globs that must not exist in tracked paths
FORBIDDEN_FILE_PATTERNS = [
    "*.db", "*.sqlite", "*.sqlite3",   # databases
    "*.pdf", "*.docx", "*.doc",        # personal documents / CVs
    ".env",                             # secret env file
    "*.log",                            # log files
    "*.eml",                            # email files
]

# Directories that should be .gitignored
FORBIDDEN_DIRS = [
    "data/",
    "logs/",
    "browser_profiles/",
    "screenshots/",
    "email_cache/",
]


class SecurityChecker:
    """
    Audits the repository for common security mistakes.

    Run from the project root directory.
    """

    def __init__(self, root: Path = None) -> None:  # type: ignore[assignment]
        self.root = root or Path.cwd()
        self.issues: list[str] = []

    def run(self) -> list[str]:
        """Run all checks and return a list of issue descriptions."""
        self.issues = []

        self._check_env_committed()
        self._check_gitignore_completeness()
        self._check_forbidden_files()
        self._check_source_for_secrets()
        self._check_database_in_tracked_paths()
        self._check_browser_profiles()

        for issue in self.issues:
            logger.warning("security.issue", issue=issue)

        return self.issues

    def _check_env_committed(self) -> None:
        """Check if .env (with credentials) is committed or exists unprotected."""
        env_file = self.root / ".env"
        if env_file.exists():
            content = env_file.read_text(errors="replace")
            # Check if it has real values (not just placeholders)
            real_value_patterns = [
                r"ALERT_EMAIL_PASSWORD=(?!your-app-password|$)[^\s]+",
                r"SMTP_PASSWORD=(?!your-app-password|$)[^\s]+",
                r"ANTHROPIC_API_KEY=(?!$)[^\s]+",
                r"OPENAI_API_KEY=(?!$)[^\s]+",
                r"TELEGRAM_BOT_TOKEN=(?!$)[^\s]+",
                r"SECRET_KEY=(?!REPLACE_WITH)[^\s]+",
            ]
            for pattern in real_value_patterns:
                if re.search(pattern, content, re.I):
                    self.issues.append(
                        "WARNING: .env contains real credentials. "
                        "Ensure it is in .gitignore and NOT committed."
                    )
                    break

    def _check_gitignore_completeness(self) -> None:
        """Verify .gitignore covers all required patterns."""
        gitignore = self.root / ".gitignore"
        if not gitignore.exists():
            self.issues.append("MISSING: .gitignore not found")
            return

        content = gitignore.read_text()
        required = [".env", "*.db", "data/", "logs/", "*.pdf", "*.docx", "browser_profiles/"]
        for pattern in required:
            if pattern not in content:
                self.issues.append(f"GITIGNORE: '{pattern}' not excluded — add it to .gitignore")

    def _check_forbidden_files(self) -> None:
        """Look for files that should never be committed."""
        import fnmatch
        for path in self.root.rglob("*"):
            if ".git" in path.parts or "__pycache__" in path.parts:
                continue
            for pattern in FORBIDDEN_FILE_PATTERNS:
                if fnmatch.fnmatch(path.name, pattern):
                    rel = path.relative_to(self.root)
                    # Only flag if not in an already-gitignored directory
                    if not any(
                        str(rel).startswith(d.rstrip("/"))
                        for d in FORBIDDEN_DIRS
                    ):
                        self.issues.append(
                            f"FORBIDDEN FILE: {rel} — should be gitignored "
                            f"(matches pattern '{pattern}')"
                        )

    def _check_source_for_secrets(self) -> None:
        """Scan Python source files for hardcoded secrets."""
        for py_file in self.root.rglob("*.py"):
            if ".git" in py_file.parts or "__pycache__" in py_file.parts:
                continue
            # Skip test files — they use obviously fake placeholder tokens
            if py_file.parent.name == "tests" or py_file.name.startswith("test_"):
                continue
            try:
                content = py_file.read_text(errors="replace")
                for pattern in SECRET_PATTERNS:
                    match = pattern.search(content)
                    if match:
                        rel = py_file.relative_to(self.root)
                        self.issues.append(
                            f"HARDCODED SECRET: {rel} — found pattern matching "
                            f"'{pattern.pattern[:40]}...'"
                        )
                        break  # one warning per file
            except Exception:
                pass

    def _check_database_in_tracked_paths(self) -> None:
        """Verify that database files are inside gitignored directories."""
        for db_file in self.root.rglob("*.db"):
            if ".git" in db_file.parts:
                continue
            rel = db_file.relative_to(self.root)
            parts = list(rel.parts)
            if "data" not in parts and len(parts) == 1:
                self.issues.append(
                    f"DATABASE FILE: {rel} is in the root directory — "
                    "move to data/ which is gitignored"
                )

    def _check_browser_profiles(self) -> None:
        """Check that browser profile directories don't exist in tracked paths."""
        for dir_name in ["browser_profiles", "playwright-state", ".playwright"]:
            bp = self.root / dir_name
            if bp.exists() and bp.is_dir():
                self.issues.append(
                    f"BROWSER PROFILE: {dir_name}/ exists — "
                    "browser sessions may contain cookies/tokens. "
                    "Ensure it is gitignored."
                )
