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
        if msg != "claude co-authored commits":
            err.print(f"[yellow]warning:[/] {msg} ({stats[msg]})")
    warn_prices(evs)
    if html_path:
        from .html import write
        write(evs, prs, Path(html_path), since=since)
        err.print(f"wrote {html_path}")
    else:
        rpt.render(evs, prs, by, Console(width=None if sys.stdout.isatty() else 160))


@main.command()
@common
@click.option("--csv", "csv_path", type=click.Path(dir_okay=False), required=True)
def export(since, repo, no_db, csv_path):
    """One row per (session, PR) with every field, for finance."""
    import csv

    _, evs, prs, _ = gather(since, repo, no_db)
    warn_prices(evs)
    rows = rpt.csv_rows(evs, prs)
    with open(csv_path, "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=rpt.CSV_FIELDS)
        w.writeheader()
        w.writerows(rows)
    err.print(f"wrote {len(rows)} rows to {csv_path}")


@main.command()
@click.option("--no-db", is_flag=True)
def doctor(no_db):
    """Show what tokentab sees: schema, models, prices, repos, gh auth."""
    data, evs, prs, stats = gather(None, None, no_db)
    out = Console(width=None if sys.stdout.isatty() else 120)
    out.rule("schema observed")
    versions = sorted((v for v in data.versions if v), key=lambda v: tuple(int(x) for x in v.split(".") if x.isdigit()))
    out.print(f"transcripts under {DEFAULT_ROOT}; Claude Code versions {versions[0] if versions else '?'} to {versions[-1] if versions else '?'}")
    out.print("line types: " + ", ".join(f"{k} {v}" for k, v in data.line_types.most_common()))
    out.print("usage keys: " + ", ".join(sorted(data.usage_keys)))
    out.print(f"{len(evs)} unique messages across {len({e['session_id'] for e in evs})} sessions"
              f"{'' if no_db else ' (including SQLite history)'}; {len(data.pr_links)} pr-link records; "
              f"{len(data.bridge)} bridge session ids")
    for k, v in data.skipped.items():
        out.print(f"skipped {v} {k}")
    out.rule("models")
    unpriced = []
    for model, g in sorted(rpt.group(evs, lambda e: e["model"]).items()):
        key, _, flag = lookup(model)
        if flag == "unpriced":
            unpriced.append(model)
        note = {"": "priced", "verify": "priced from a # VERIFY row", "unpriced": "NO PRICE ROW, using nearest"}[flag]
        out.print(f"{model:32} {len(g):6} msgs  -> {key:20} {note}")
    out.print(f"[bold]{len(unpriced)} unpriced models[/]" + (": " + ", ".join(unpriced) if unpriced else ""))
    out.rule("repos")
    for slug, g in sorted(rpt.group(evs, lambda e: e["repo"] or "(no repo)").items(), key=lambda kv: -len(kv[1])):
        n_prs = sum(1 for k in prs if k[0] == slug)
        out.print(f"{slug:48} {len(g):6} msgs  {n_prs:4} PRs from gh  {sum(e['usd'] for e in g):10,.2f} usd")
    for k, v in stats.items():
        out.print(f"{k}: {v}")
    out.rule("gh")
    import subprocess
    r = subprocess.run(["gh", "auth", "status"], capture_output=True, text=True)
    out.print((r.stdout + r.stderr).strip() or f"gh auth status exited {r.returncode}")
