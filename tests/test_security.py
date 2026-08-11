"""Tests for the security checker."""
from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest

from jobhunter.security import SecurityChecker


@pytest.fixture
def temp_repo(tmp_path: Path) -> Path:
    """Create a minimal fake repository directory for testing."""
    # .gitignore covering required patterns
    gitignore = tmp_path / ".gitignore"
    gitignore.write_text(
        ".env\n*.db\ndata/\nlogs/\n*.pdf\n*.docx\nbrowser_profiles/\n"
    )
    # Safe .env.example (no real values)
    env_example = tmp_path / ".env.example"
    env_example.write_text("SMTP_PASSWORD=your-app-password\nSECRET_KEY=REPLACE_WITH_RANDOM\n")
    return tmp_path


class TestSecurityChecker:

    def test_clean_repo_has_no_issues(self, temp_repo: Path) -> None:
        checker = SecurityChecker(root=temp_repo)
        issues = checker.run()
        assert len(issues) == 0

    def test_detects_missing_gitignore_entry(self, temp_repo: Path) -> None:
        # Remove *.pdf from gitignore
        gi = temp_repo / ".gitignore"
        gi.write_text(".env\n*.db\ndata/\nlogs/\n")
        checker = SecurityChecker(root=temp_repo)
        issues = checker.run()
        assert any("*.pdf" in issue or "*.docx" in issue for issue in issues)

    def test_detects_database_in_root(self, temp_repo: Path) -> None:
        db_file = temp_repo / "jobhunter.db"
        db_file.write_bytes(b"SQLite database")
        checker = SecurityChecker(root=temp_repo)
        issues = checker.run()
        assert any("DATABASE" in issue or "database" in issue.lower() for issue in issues)

    def test_detects_env_with_real_credentials(self, temp_repo: Path) -> None:
        env_file = temp_repo / ".env"
        env_file.write_text(
            "SMTP_PASSWORD=my_real_secret_password\n"
            "ANTHROPIC_API_KEY=sk-ant-api03-realkey\n"
        )
        checker = SecurityChecker(root=temp_repo)
        issues = checker.run()
        assert any("credential" in i.lower() or "env" in i.lower() for i in issues)

    def test_detects_browser_profile_directory(self, temp_repo: Path) -> None:
        bp = temp_repo / "browser_profiles"
        bp.mkdir()
        (bp / "profile1").mkdir()
        checker = SecurityChecker(root=temp_repo)
        issues = checker.run()
        assert any("browser" in i.lower() or "profile" in i.lower() for i in issues)

    def test_no_issues_when_db_in_data_dir(self, temp_repo: Path) -> None:
        data_dir = temp_repo / "data"
        data_dir.mkdir()
        db = data_dir / "jobhunter.db"
        db.write_bytes(b"SQLite")
        checker = SecurityChecker(root=temp_repo)
        issues = checker.run()
        # DB in data/ should not trigger an issue
        db_issues = [i for i in issues if "DATABASE" in i and "data" not in i.lower()]
        assert len(db_issues) == 0

    def test_missing_gitignore_reported(self, tmp_path: Path) -> None:
        checker = SecurityChecker(root=tmp_path)
        issues = checker.run()
        assert any("gitignore" in i.lower() or "MISSING" in i for i in issues)

    def test_run_returns_list(self, temp_repo: Path) -> None:
        checker = SecurityChecker(root=temp_repo)
        result = checker.run()
        assert isinstance(result, list)
