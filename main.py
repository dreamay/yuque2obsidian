#!/usr/bin/env python3
"""CLI entry point for yuque2obsidian."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import click

from yuque2obsidian.config import load_config
from yuque2obsidian.exporter import Exporter
from yuque2obsidian.logger import setup_logging


@click.command()
@click.option(
    "--config",
    "-c",
    default="config.yaml",
    help="Path to configuration file.",
    type=click.Path(exists=True, path_type=Path),
)
@click.option(
    "--full-sync",
    is_flag=True,
    default=False,
    help="Force a full re-sync, ignoring incremental state.",
)
@click.option(
    "--repo",
    default=None,
    help="Only sync the specified repository name.",
)
@click.option(
    "--verbose",
    "-v",
    is_flag=True,
    default=False,
    help="Enable verbose (debug) logging.",
)
def main(config: Path, full_sync: bool, repo: str | None, verbose: bool) -> None:
    """Export Yuque docs to Obsidian-compatible Markdown."""
    setup_logging(level="DEBUG" if verbose else "INFO")

    try:
        cfg = load_config(config)
    except Exception as exc:
        click.echo(f"Error: {exc}", err=True)
        sys.exit(1)

    if full_sync:
        cfg.export.full_sync = True

    async def _run() -> None:
        exporter = Exporter(cfg)
        if repo:
            exporter.set_repo_filter(repo)
        try:
            await exporter.run()
        finally:
            await exporter.close()

    try:
        asyncio.run(_run())
    except Exception as exc:
        click.echo(f"Error: {exc}", err=True)
        sys.exit(1)


if __name__ == "__main__":
    main()
