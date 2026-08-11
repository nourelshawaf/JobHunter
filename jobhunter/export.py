"""
Export module — application tracking data to CSV and Google Sheets.

Exports job application status, scores, and metadata to:
  1. Local CSV file (always available, no credentials needed)
  2. Google Sheets (optional — requires service-account credentials)

CSV columns match a sensible job-tracking spreadsheet format.
Google Sheets uses the gspread library with service-account auth.

Usage::

    from jobhunter.export import Exporter
    exporter = Exporter(db)
    exporter.to_csv("data/applications_2024.csv")
    exporter.to_google_sheets("1BxiMVs0XRA5nFMdKvBdBZjgmUUqptlbs74OgVE2upms")
"""
from __future__ import annotations

import csv
import io
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import structlog
from sqlalchemy.orm import Session

from jobhunter.models.job import Job, JobStatus

logger = structlog.get_logger(__name__)

# Columns exported to CSV / Google Sheets (in order)
EXPORT_COLUMNS = [
    "id",
    "title",
    "company",
    "location",
    "status",
    "relevance_score",
    "job_type",
    "work_mode",
    "salary_raw",
    "posted_at",
    "discovered_at",
    "deadline",
    "application_url",
    "source",
    "hungarian_mandatory",
    "student_friendly",
    "score_explanation",
    "notes",
    "last_checked_at",
]

# Statuses included in the default export (excludes auto-rejected noise)
DEFAULT_STATUSES = [
    JobStatus.SCORED,
    JobStatus.SAVED,
    JobStatus.APPLICATION_STARTED,
    JobStatus.FORM_PARTIALLY_COMPLETED,
    JobStatus.READY_FOR_FINAL_REVIEW,
    JobStatus.MANUALLY_SUBMITTED,
    JobStatus.INTERVIEW,
    JobStatus.OFFER,
    JobStatus.WITHDRAWN,
    JobStatus.REJECTED,
]


class Exporter:
    """Exports job data to CSV or Google Sheets."""

    def __init__(self, db: Session) -> None:
        self.db = db

    def _fetch_jobs(
        self,
        statuses: Optional[list[str]] = None,
        min_score: int = 0,
    ) -> list[Job]:
        """Fetch jobs for export with optional filters."""
        query = self.db.query(Job)
        if statuses:
            query = query.filter(Job.status.in_(statuses))
        else:
            query = query.filter(Job.status.in_(DEFAULT_STATUSES))
        if min_score > 0:
            query = query.filter(Job.relevance_score >= min_score)
        return query.order_by(Job.relevance_score.desc().nullslast()).all()

    def _job_to_row(self, job: Job) -> dict[str, Any]:
        """Convert a Job to a flat dict for CSV/Sheets output."""
        row: dict[str, Any] = {}
        for col in EXPORT_COLUMNS:
            val = getattr(job, col, None)
            if isinstance(val, datetime):
                val = val.strftime("%Y-%m-%d %H:%M")
            elif val is None:
                val = ""
            else:
                val = str(val)
            row[col] = val
        return row

    # ── CSV export ────────────────────────────────────────────────────────

    def to_csv(
        self,
        path: str | Path = "data/applications.csv",
        statuses: Optional[list[str]] = None,
        min_score: int = 0,
    ) -> Path:
        """
        Export jobs to a CSV file.

        Returns the path of the written file.
        """
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)

        jobs = self._fetch_jobs(statuses, min_score)
        rows = [self._job_to_row(j) for j in jobs]

        with path.open("w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=EXPORT_COLUMNS)
            writer.writeheader()
            writer.writerows(rows)

        logger.info("export.csv_written", path=str(path), rows=len(rows))
        return path

    def to_csv_string(
        self,
        statuses: Optional[list[str]] = None,
        min_score: int = 0,
    ) -> str:
        """Return CSV as a string (for Streamlit download button)."""
        jobs = self._fetch_jobs(statuses, min_score)
        rows = [self._job_to_row(j) for j in jobs]

        buf = io.StringIO()
        writer = csv.DictWriter(buf, fieldnames=EXPORT_COLUMNS)
        writer.writeheader()
        writer.writerows(rows)
        return buf.getvalue()

    # ── Google Sheets export ──────────────────────────────────────────────

    def to_google_sheets(
        self,
        spreadsheet_id: str,
        sheet_name: str = "Applications",
        credentials_path: Optional[str | Path] = None,
        statuses: Optional[list[str]] = None,
        min_score: int = 0,
    ) -> bool:
        """
        Export jobs to a Google Sheets spreadsheet.

        Args:
            spreadsheet_id: The Google Sheets document ID (from the URL).
            sheet_name:      Name of the worksheet tab to write to.
            credentials_path: Path to a service-account JSON credentials file.
                              Falls back to GOOGLE_CREDENTIALS_PATH env var,
                              then to application default credentials.
            statuses:        Job statuses to include (default: all active).
            min_score:       Minimum relevance score to include.

        Returns True on success, False on any error.

        Authentication options (in priority order):
          1. credentials_path argument
          2. GOOGLE_CREDENTIALS_PATH environment variable
          3. Application default credentials (gcloud auth)
        """
        try:
            import gspread
            from google.oauth2.service_account import Credentials
        except ImportError:
            logger.error(
                "export.google_sheets.missing_deps",
                hint="pip install gspread google-auth",
            )
            return False

        creds_path = (
            credentials_path
            or os.environ.get("GOOGLE_CREDENTIALS_PATH")
        )

        try:
            if creds_path:
                creds = Credentials.from_service_account_file(
                    str(creds_path),
                    scopes=[
                        "https://spreadsheets.google.com/feeds",
                        "https://www.googleapis.com/auth/drive",
                    ],
                )
                gc = gspread.authorize(creds)
            else:
                # Application default credentials
                gc = gspread.oauth()

            sh = gc.open_by_key(spreadsheet_id)

            # Get or create the worksheet
            try:
                ws = sh.worksheet(sheet_name)
            except gspread.WorksheetNotFound:
                ws = sh.add_worksheet(title=sheet_name, rows=1000, cols=len(EXPORT_COLUMNS))

            jobs = self._fetch_jobs(statuses, min_score)
            rows = [self._job_to_row(j) for j in jobs]

            # Clear and rewrite
            ws.clear()
            ws.append_row(EXPORT_COLUMNS, value_input_option="USER_ENTERED")
            if rows:
                ws.append_rows(
                    [[r[col] for col in EXPORT_COLUMNS] for r in rows],
                    value_input_option="USER_ENTERED",
                )

            # Format header row (bold)
            try:
                ws.format("A1:T1", {"textFormat": {"bold": True}})
            except Exception:
                pass  # formatting is cosmetic

            logger.info(
                "export.sheets_written",
                spreadsheet_id=spreadsheet_id,
                sheet=sheet_name,
                rows=len(rows),
            )
            return True

        except Exception as exc:
            logger.error("export.google_sheets_error", error=str(exc))
            return False

    # ── Summary stats ─────────────────────────────────────────────────────

    def summary_stats(self) -> dict[str, Any]:
        """Return a summary dict for the dashboard stats card."""
        from sqlalchemy import func
        total = self.db.query(Job).count()
        by_status = dict(
            self.db.query(Job.status, func.count(Job.id)).group_by(Job.status).all()
        )
        avg_score = self.db.query(func.avg(Job.relevance_score)).scalar()
        top_jobs = (
            self.db.query(Job)
            .filter(
                Job.relevance_score >= 75,
                Job.status.notin_([JobStatus.REJECTED_BY_FILTER, JobStatus.EXPIRED]),
            )
            .order_by(Job.relevance_score.desc())
            .limit(5)
            .all()
        )
        return {
            "total": total,
            "by_status": by_status,
            "avg_score": round(avg_score or 0, 1),
            "high_match_count": by_status.get(JobStatus.SCORED, 0),
            "saved_count": by_status.get(JobStatus.SAVED, 0),
            "submitted_count": by_status.get(JobStatus.MANUALLY_SUBMITTED, 0),
            "top_jobs": [
                {
                    "title": j.title,
                    "company": j.company,
                    "score": j.relevance_score,
                    "url": j.application_url,
                }
                for j in top_jobs
            ],
            "exported_at": datetime.now(timezone.utc).isoformat(),
        }
