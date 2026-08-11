"""Tests for the scheduler service."""
from __future__ import annotations

import os

import pytest

# All tests run with JOBHUNTER_TESTING=1 so no real scheduling occurs
os.environ["JOBHUNTER_TESTING"] = "1"

from jobhunter.scheduler import Scheduler, _TESTING


class TestScheduler:

    def test_testing_flag_is_set(self) -> None:
        assert _TESTING is True

    def test_build_creates_scheduler(self) -> None:
        s = Scheduler()
        aps = s.build()
        assert aps is not None

    def test_build_registers_three_jobs(self) -> None:
        s = Scheduler()
        aps = s.build()
        jobs = aps.get_jobs()
        job_ids = [j.id for j in jobs]
        assert "ingestion" in job_ids
        assert "daily_digest" in job_ids
        assert "deadline_check" in job_ids

    def test_start_is_noop_in_testing(self) -> None:
        """In testing mode, start() should return immediately without blocking."""
        s = Scheduler()
        s.start()  # should return immediately

    def test_dry_run_prints_jobs(self, capsys) -> None:
        s = Scheduler()
        s.dry_run()
        captured = capsys.readouterr()
        assert "ingestion" in captured.out
        assert "daily_digest" in captured.out
        assert "deadline_check" in captured.out
        assert "Dry-run" in captured.out

    def test_run_ingestion_does_not_raise(self) -> None:
        """_run_ingestion() should not raise even with empty/mock DB."""
        s = Scheduler()
        # Should complete without error (DB empty, no connectors enabled for real fetch)
        s._run_ingestion(connector_names=["eures"])  # will likely fail gracefully

    def test_run_deadline_check_does_not_raise(self) -> None:
        s = Scheduler()
        s._run_deadline_check()

    def test_run_once_completes(self) -> None:
        s = Scheduler()
        s.run_once(connector_names=[])  # empty list = nothing to run

    def test_scheduler_interval_from_settings(self) -> None:
        from jobhunter.config import get_settings
        s = Scheduler()
        assert s.settings.search_interval_hours >= 1

    def test_daily_digest_time_parseable(self) -> None:
        from jobhunter.config import get_search_config
        config = get_search_config()
        hh, mm = config.daily_summary_time.split(":")
        assert 0 <= int(hh) <= 23
        assert 0 <= int(mm) <= 59
