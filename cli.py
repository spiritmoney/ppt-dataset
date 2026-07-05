#!/usr/bin/env python3
"""CLI — 6M search-driven URL discovery (no full file downloads)."""

from __future__ import annotations

import logging
import os
import shutil
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from src.database import Database
from src.discovery import Discovery
from src.prefilter import Phase2Prefilter
from src.reporter import Reporter
from src.settings import Settings
from src.utils import make_batch_id

app = typer.Typer(help="Discover 6M qualified PPT/PPTX URLs — search only, no full downloads.")
console = Console()


def _configure_logging() -> None:
    level = getattr(logging, os.getenv("LOG_LEVEL", "INFO").upper(), logging.INFO)
    handlers: list[logging.Handler] = [logging.StreamHandler()]
    log_file = os.getenv("LOG_FILE", "").strip()
    if log_file:
        path = Path(log_file)
        path.parent.mkdir(parents=True, exist_ok=True)
        handlers.append(
            RotatingFileHandler(path, maxBytes=10_000_000, backupCount=5, encoding="utf-8")
        )
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=handlers,
        force=True,
    )


def _load() -> tuple[Settings, Database]:
    _configure_logging()
    settings = Settings.load()
    settings.ensure_dirs()
    db = Database(settings.database_url)
    db.init()
    return settings, db


@app.command()
def init_db():
    """Initialize database schema."""
    settings, db = _load()
    console.print(f"[green]Database ready:[/green] {settings.database_url}")


@app.command()
def preflight():
    """Verify production prerequisites before starting the pipeline."""
    settings, db = _load()
    checks: list[tuple[str, bool, str]] = []

    checks.append(("PostgreSQL configured", db.is_postgres, settings.database_url))
    if db.is_postgres:
        try:
            with db.connect() as conn:
                conn.execute("SELECT 1")
            checks.append(("PostgreSQL connection", True, "ok"))
        except Exception as exc:
            checks.append(("PostgreSQL connection", False, str(exc)[:120]))
    else:
        checks.append(("PostgreSQL connection", False, "SQLite is dev-only; set DATABASE_URL to PostgreSQL"))

    config_path = settings.config_path
    checks.append(("Config file", config_path.exists(), str(config_path)))

    for name in ("audit", "manifests", "reports"):
        path = settings.data_dir / name
        writable = path.exists() and os.access(path, os.W_OK)
        checks.append((f"Writable {path.name}/", writable, str(path)))

    free_gb = shutil.disk_usage(settings.data_dir).free / (1024 ** 3)
    checks.append(("Disk free >= 10 GB", free_gb >= 10, f"{free_gb:.1f} GB free"))

    table = Table(title="Preflight Checks")
    table.add_column("Check")
    table.add_column("Status")
    table.add_column("Detail")
    failed = 0
    for name, ok, detail in checks:
        status = "[green]PASS[/green]" if ok else "[red]FAIL[/red]"
        if not ok:
            failed += 1
        table.add_row(name, status, detail)
    console.print(table)

    if failed:
        raise typer.Exit(code=1)
    console.print("[green bold]Ready for deployment.[/green bold]")


@app.command()
def dedupe():
    """Normalize URLs and remove duplicate candidates."""
    settings, db = _load()
    counts = db.dedupe_existing()
    console.print(
        f"[green]Dedup complete:[/green] "
        f"normalized={counts['normalized']:,} deleted={counts['deleted']:,} "
        f"(unique URLs: {db.total_url_count():,})"
    )


@app.command("discover")
def discover_cmd(
    batch_id: str = typer.Option(None, help="Optional batch tag"),
):
    """Phase 1: search engines + light crawl (no seed files)."""
    settings, db = _load()
    n = Discovery(settings, db).run_sync(batch_id)
    console.print(f"[green]Discovery done:[/green] +{n:,} new (pending: {db.pending_count():,})")


@app.command("validate")
def validate_cmd(
    batch_id: str = typer.Option(None, help="Batch ID (auto-generated if omitted)"),
    limit: int = typer.Option(5000, help="Max candidates to validate"),
):
    """Phase 2: GET probe + signature check -> qualified URL records."""
    settings, db = _load()
    reclaimed = db.reclaim_checking()
    if reclaimed:
        console.print(f"[yellow]Reclaimed {reclaimed:,} stuck checking rows[/yellow]")
    batch_id = batch_id or make_batch_id(db.next_batch_seq())
    counts = Phase2Prefilter(settings, db).run(batch_id, limit)
    reporter = Reporter(settings, db)
    reporter.write_batch_manifest(batch_id)
    reporter.write_progress()
    console.print(
        f"[green]Validate {batch_id}:[/green] "
        f"qualified={counts['qualified']:,} rejected={counts['rejected']:,} "
        f"(total: {db.qualified_count():,})"
    )


@app.command()
def run(
    target: int = typer.Option(None, help="Override 6M target"),
    validate_limit: int = typer.Option(5000, help="Candidates per validation cycle"),
):
    """Continuous search -> validate until 6M qualified URLs."""
    settings, db = _load()
    if target:
        settings.target_count = target

    reclaimed = db.reclaim_checking()
    if reclaimed:
        console.print(f"[yellow]Reclaimed {reclaimed:,} stuck checking rows[/yellow]")

    discovery = Discovery(settings, db)
    prefilter = Phase2Prefilter(settings, db)
    reporter = Reporter(settings, db)

    console.print(
        f"[bold]Target:[/bold] {settings.target_count:,} URLs "
        f"(search + GET probe, config={settings.config_path.name})"
    )

    log = logging.getLogger(__name__)
    try:
        _run_loop(settings, db, discovery, prefilter, reporter, validate_limit)
    except Exception:
        log.exception("Pipeline crashed")
        raise typer.Exit(code=1)


def _run_loop(
    settings: Settings,
    db: Database,
    discovery: Discovery,
    prefilter: Phase2Prefilter,
    reporter: Reporter,
    validate_limit: int,
) -> None:
    while db.qualified_count() < settings.target_count:
        if db.pending_count() < validate_limit:
            console.print("[cyan]Discovering via search + light crawl...[/cyan]")
            n = discovery.run_sync()
            console.print(f"  +{n:,} candidates (pending: {db.pending_count():,})")

        if db.pending_count() == 0:
            console.print("[yellow]No pending candidates this cycle — retrying search...[/yellow]")
            continue

        batch_id = make_batch_id(db.next_batch_seq())
        console.print(f"[cyan]Validate {batch_id}...[/cyan]")
        counts = prefilter.run(batch_id, validate_limit)
        reporter.write_batch_manifest(batch_id)
        reporter.write_progress()

        qualified = db.qualified_count()
        console.print(
            f"  +{counts['qualified']:,} qualified | total: {qualified:,}/{settings.target_count:,}"
        )

        if qualified >= settings.target_count:
            break

    reporter.write_master_manifest()
    console.print("[green bold]Done. Master manifest written.[/green bold]")


@app.command()
def report(
    batch_id: str = typer.Option(None, help="Optional single batch manifest"),
    master: bool = typer.Option(True, help="Write MASTER_MANIFEST"),
):
    """Generate CSV/Excel URL reports."""
    settings, db = _load()
    reporter = Reporter(settings, db)
    if batch_id:
        csv_p, xlsx_p = reporter.write_batch_manifest(batch_id)
        console.print(f"Batch manifest: {csv_p}")
        if xlsx_p:
            console.print(f"Batch Excel: {xlsx_p}")
    if master:
        csv_p, xlsx_p = reporter.write_master_manifest()
        console.print(f"Master CSV: {csv_p}")
        if xlsx_p:
            console.print(f"Master Excel: {xlsx_p}")
    progress = reporter.write_progress()
    console.print(f"Progress: {progress}")


@app.command()
def status():
    """Show progress toward 6M URL target."""
    settings, db = _load()
    qualified = db.qualified_count()
    pending = db.pending_count()
    target = settings.target_count
    pct = 100 * qualified / target if target else 0

    table = Table(title="URL Discovery Status")
    table.add_column("Metric")
    table.add_column("Value")
    table.add_row("Qualified URLs", f"{qualified:,}")
    table.add_row("Target", f"{target:,}")
    table.add_row("Progress", f"{pct:.4f}%")
    table.add_row("Remaining", f"{max(0, target - qualified):,}")
    table.add_row("Pending candidates", f"{pending:,}")
    table.add_row("Config", str(settings.config_path))
    table.add_row("Database", settings.database_url)
    console.print(table)


if __name__ == "__main__":
    app()
