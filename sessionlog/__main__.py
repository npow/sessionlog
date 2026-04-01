"""CLI entry point: sessionlog start / status / stop."""

import os

import click

from sessionlog.config import get_source_specs


@click.group()
def cli():
    """sessionlog — real-time ingestion for AI coding agent sessions."""


@cli.command()
@click.option(
    "--db",
    default="~/.sessionlog/data.sqlite",
    show_default=True,
    help="SQLite database path",
)
@click.option(
    "--sources-dir",
    "sources_dirs",
    multiple=True,
    help="Session source directory (repeatable). Use name=/path for explicit source labels.",
)
def start(db: str, sources_dirs: tuple[str, ...]):
    """Start the ingestion daemon."""
    db_path = os.path.expanduser(db)
    os.environ["SESSIONLOG_DB"] = db_path
    # Backward compatibility for modules still reading the legacy name.
    os.environ["CLAUDE_RETRO_DB"] = db_path

    source_specs = get_source_specs(sources_dirs)
    source_display = ", ".join(f"{name}={src}" for name, src in source_specs)

    from sessionlog.watcher import IngestionWorker

    click.echo(f"Watching {source_display} → {db_path}")
    worker = IngestionWorker(run_immediately=True, source_specs=source_specs)
    worker.start()
    try:
        worker.join()
    except KeyboardInterrupt:
        worker.stop()


@cli.command()
def status():
    """Show ingestion daemon status."""
    click.echo("Not implemented yet")


@cli.command()
@click.option(
    "--force",
    is_flag=True,
    default=False,
    help="Re-ingest all files, ignoring the ingestion log.",
)
@click.option(
    "--sources-dir",
    "sources_dirs",
    multiple=True,
    help="Session source directory (repeatable). Use name=/path for explicit source labels.",
)
@click.option(
    "--db",
    default="~/.sessionlog/data.sqlite",
    show_default=True,
    help="SQLite database path",
)
def ingest(force: bool, sources_dirs: tuple[str, ...], db: str):
    """Run a one-shot incremental ingestion of all JSONL files."""
    db_path = os.path.expanduser(db)
    os.environ["SESSIONLOG_DB"] = db_path
    os.environ["CLAUDE_RETRO_DB"] = db_path

    source_specs = get_source_specs(sources_dirs)

    from sessionlog.db import get_writer
    from sessionlog.ingest import run_ingest

    if force:
        conn = get_writer()
        conn.execute("DELETE FROM ingestion_log")
        conn.commit()
        click.echo("Cleared ingestion log — will re-ingest all files.")

    stats = run_ingest(source_specs=source_specs)
    click.echo(
        f"Done. "
        f"{stats['ingested_files']}/{stats['total_files']} files ingested, "
        f"{stats['total_entries']} raw entries, "
        f"{stats['total_progress_entries']} progress entries "
        f"({stats['skipped_files']} skipped, {stats['failed_files']} failed). "
        f"DB totals: {stats['total_entries_in_db']} entries, "
        f"{stats['total_sessions_found']} sessions, "
        f"{stats['total_projects']} projects."
    )


if __name__ == "__main__":
    cli()
