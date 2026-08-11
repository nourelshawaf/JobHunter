"""
Command-line interface.

Usage:
    jobhunter ingest          # run all connectors once
    jobhunter serve           # start the Streamlit dashboard
    jobhunter status          # print database stats
    jobhunter migrate         # apply database migrations
    jobhunter test-email      # test IMAP connection
"""
from __future__ import annotations

import asyncio
import subprocess
import sys
from datetime import datetime

import click
from rich.console import Console
from rich.table import Table

console = Console()


@click.group()
def app() -> None:
    """JobHunter — automated internship discovery and application assistant."""


@app.command()
def migrate() -> None:
    """Apply pending Alembic database migrations."""
    console.print("[bold]Running database migrations...[/bold]")
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        capture_output=False,
    )
    if result.returncode == 0:
        console.print("[green]✓ Migrations applied successfully[/green]")
    else:
        console.print("[red]✗ Migration failed[/red]")
        sys.exit(1)


@app.command()
@click.option("--connectors", "-c", multiple=True, help="Run specific connectors only")
def ingest(connectors: tuple[str, ...]) -> None:
    """Run job discovery pipeline and ingest new listings."""
    from jobhunter.database import init_db
    from jobhunter.pipeline import Pipeline

    init_db()
    console.print("[bold]Starting job ingestion...[/bold]")

    async def _run() -> None:
        pipeline = Pipeline()
        if connectors:
            # Override enabled connectors for this run
            pipeline.config._data.setdefault("connectors", {})["enabled"] = list(connectors)
        result = await pipeline.run()

        console.print(f"\n[green]✓ Ingestion complete[/green]")
        console.print(f"  New jobs:     {result.new_jobs}")
        console.print(f"  Updated:      {result.updated_jobs}")
        console.print(f"  Duplicates:   {result.duplicates}")
        console.print(f"  Rejected:     {result.rejected}")
        console.print(f"  Duration:     {result.duration_seconds:.1f}s")
        if result.errors:
            console.print(f"[yellow]  Errors: {len(result.errors)}[/yellow]")
            for err in result.errors[:5]:
                console.print(f"    • {err}")

    asyncio.run(_run())


@app.command()
@click.option("--port", default=8501, help="Dashboard port")
def serve(port: int) -> None:
    """Start the Streamlit dashboard."""
    import os
    dashboard = os.path.join(os.path.dirname(__file__), "dashboard", "app.py")
    console.print(f"[bold]Starting dashboard on http://localhost:{port}[/bold]")
    subprocess.run([
        sys.executable, "-m", "streamlit", "run", dashboard,
        "--server.port", str(port),
        "--server.headless", "true",
    ])


@app.command()
def status() -> None:
    """Print a summary of the database contents."""
    from jobhunter.database import SessionLocal, init_db
    from jobhunter.models.job import Job, JobStatus

    init_db()
    db = SessionLocal()

    try:
        total = db.query(Job).count()
        by_status = (
            db.query(Job.status, __import__("sqlalchemy").func.count(Job.id))
            .group_by(Job.status)
            .all()
        )

        table = Table(title="JobHunter Database Status", show_header=True)
        table.add_column("Status", style="cyan")
        table.add_column("Count", justify="right", style="green")

        for status, count in sorted(by_status, key=lambda x: -x[1]):
            table.add_row(status, str(count))

        table.add_row("[bold]Total[/bold]", f"[bold]{total}[/bold]")
        console.print(table)

        # Top 5 highest-scoring saved jobs
        top_jobs = (
            db.query(Job)
            .filter(Job.status == JobStatus.SAVED, Job.relevance_score.isnot(None))
            .order_by(Job.relevance_score.desc())
            .limit(5)
            .all()
        )

        if top_jobs:
            console.print("\n[bold]Top saved jobs:[/bold]")
            for job in top_jobs:
                console.print(
                    f"  [{job.relevance_score}/100] [cyan]{job.title}[/cyan] @ {job.company}"
                )
    finally:
        db.close()


@app.command("test-email")
def test_email() -> None:
    """Test the IMAP email connection."""
    from jobhunter.config import get_settings
    import imaplib

    settings = get_settings()
    if not settings.alert_email_user:
        console.print("[yellow]No email credentials configured (ALERT_EMAIL_USER)[/yellow]")
        return

    console.print(f"Connecting to {settings.alert_email_host}:{settings.alert_email_port}...")
    try:
        with imaplib.IMAP4_SSL(settings.alert_email_host, settings.alert_email_port) as imap:
            imap.login(settings.alert_email_user, settings.alert_email_password.get_secret_value())
            status, folders = imap.list()
            console.print(f"[green]✓ Connected successfully as {settings.alert_email_user}[/green]")
            imap.select(settings.alert_email_folder, readonly=True)
            _, msg_ids = imap.search(None, "ALL")
            count = len(msg_ids[0].split()) if msg_ids[0] else 0
            console.print(f"  Folder '{settings.alert_email_folder}': {count} messages")
    except Exception as exc:
        console.print(f"[red]✗ Connection failed: {exc}[/red]")




@app.command()
@click.option("--once", is_flag=True, help="Run all jobs once and exit")
@click.option("--dry-run", "dry_run", is_flag=True, help="Print schedule without executing")
@click.option("--connectors", "-c", multiple=True, help="Connectors to run (--once mode only)")
def scheduler(once: bool, dry_run: bool, connectors: tuple[str, ...]) -> None:
    """Start the job discovery scheduler.

    Default: daemon mode (runs until Ctrl+C).
    --once: run all jobs immediately once, then exit.
    --dry-run: show schedule without executing anything.
    """
    from jobhunter.scheduler import Scheduler
    from jobhunter.database import init_db

    init_db()
    sched = Scheduler()

    if dry_run:
        sched.dry_run()
        return

    if once:
        console.print("[bold]Running all scheduled jobs once...[/bold]")
        sched.run_once(connector_names=list(connectors) if connectors else None)
        console.print("[green]✓ One-shot run complete[/green]")
        return

    console.print("[bold]Starting scheduler daemon (Ctrl+C to stop)...[/bold]")
    sched.start()


@app.command("security-check")
def security_check() -> None:
    """Audit the repository for common security mistakes."""
    from jobhunter.security import SecurityChecker
    checker = SecurityChecker()
    issues = checker.run()
    if issues:
        console.print(f"[red]Found {len(issues)} issue(s):[/red]")
        for issue in issues:
            console.print(f"  [yellow]•[/yellow] {issue}")
        raise SystemExit(1)
    else:
        console.print("[green]✓ No security issues detected[/green]")



@app.command("export-csv")
@click.option("--output", "-o", default="data/applications.csv", help="Output CSV path")
@click.option("--min-score", default=0, help="Minimum relevance score to include")
def export_csv(output: str, min_score: int) -> None:
    """Export job applications to a CSV file."""
    from jobhunter.database import SessionLocal, init_db
    from jobhunter.export import Exporter
    init_db()
    db = SessionLocal()
    try:
        exporter = Exporter(db)
        path = exporter.to_csv(path=output, min_score=min_score)
        console.print(f"[green]✓ Exported to {path}[/green]")
    finally:
        db.close()


@app.command("export-sheets")
@click.argument("spreadsheet_id")
@click.option("--sheet", default="Applications", help="Worksheet tab name")
@click.option("--credentials", "-c", default=None, help="Path to service-account JSON")
@click.option("--min-score", default=0, help="Minimum relevance score to include")
def export_sheets(spreadsheet_id: str, sheet: str, credentials: str, min_score: int) -> None:
    """Export job applications to a Google Sheets document."""
    from jobhunter.database import SessionLocal, init_db
    from jobhunter.export import Exporter
    init_db()
    db = SessionLocal()
    try:
        exporter = Exporter(db)
        ok = exporter.to_google_sheets(
            spreadsheet_id=spreadsheet_id,
            sheet_name=sheet,
            credentials_path=credentials,
            min_score=min_score,
        )
        if ok:
            console.print(f"[green]✓ Exported to Google Sheets: {spreadsheet_id}[/green]")
        else:
            console.print("[red]✗ Export failed — check logs[/red]")
            raise SystemExit(1)
    finally:
        db.close()

if __name__ == "__main__":
    app()
