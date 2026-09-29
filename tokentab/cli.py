from __future__ import annotations

import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import click
from rich.console import Console

from . import db, report as rpt
from .attribute import attribute, resolve_repo
from .loaders.claude_jsonl import DEFAULT_ROOT, load
from .pricing import lookup

err = Console(stderr=True)


def parse_since(s: str) -> datetime:
    m = re.fullmatch(r"(\d+)([hdw])", s)
    if m:
        return datetime.now(timezone.utc) - timedelta(**{{"h": "hours", "d": "days", "w": "weeks"}[m[2]]: int(m[1])})
    return datetime.fromisoformat(s).replace(tzinfo=timezone.utc)


def gather(since, repo, no_db, root=DEFAULT_ROOT):
    data = load(root)
    if not no_db:
        data = db.sync(data)
    evs = list(data.events.values())
    prs, stats = attribute(evs, data.pr_links, data.bridge)
    if since:
        cutoff = parse_since(since)
        evs = [e for e in evs if e["ts"] >= cutoff]
    if repo:
        r = resolve_repo(str(Path(repo).expanduser().resolve()))
        if r is None:
            raise click.BadParameter(f"{repo} is not inside a git repository", param_hint="--repo")
        evs = [e for e in evs if e["repo"] == r.slug]
    return data, evs, prs, stats


def warn_prices(evs) -> None:
    for flag, label in (("unpriced", "no price row; priced at the nearest row"), ("verify", "priced from a # VERIFY row")):
        models = sorted({e["model"] for e in evs if e["price_flag"] == flag})
        if models:
            err.print(f"[yellow]warning:[/] {label}: {', '.join(f'{m} (as {lookup(m)[0]})' for m in models)}")


def common(f):
    f = click.option("--since", help="e.g. 30d, 2w, 12h, or 2026-09-01")(f)
    f = click.option("--repo", type=click.Path(exists=True), help="only this repo")(f)
    return click.option("--no-db", is_flag=True, help="skip the ~/.tokentab/usage.db history")(f)


@click.group()
def main():
    """What Claude Code token spend bought, per PR."""


@main.command()
@common
@click.option("--by", type=click.Choice(["pr", "session", "branch", "model", "week"]), default="pr")
@click.option("--html", "html_path", type=click.Path(dir_okay=False), help="write a self-contained HTML report")
def report(since, repo, no_db, by, html_path):
    """Spend table (or HTML report) joined to PRs."""
    _, evs, prs, stats = gather(since, repo, no_db)
    for msg in stats:
        if "failed" in msg or "did not return" in msg:
            err.print(f"[yellow]warning:[/] {msg} ({stats[msg]})")
    warn_prices(evs)
    if html_path:
        from .html import write
        write(evs, prs, Path(html_path), since=since)
        err.print(f"wrote {html_path}")
    else:
        rpt.render(evs, prs, by, Console(width=None if sys.stdout.isatty() else 160))
