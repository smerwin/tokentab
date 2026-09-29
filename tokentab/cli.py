from __future__ import annotations

import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import click
from rich.console import Console

from . import db, report as rpt
from .attribute import attribute, resolve_repo
from .loaders import otel
from .loaders.claude_jsonl import DEFAULT_ROOT, load
from .pricing import lookup

err = Console(stderr=True)
WARN = ("failed", "limit", "did not return", "assumed")


def parse_since(s: str) -> datetime:
    m = re.fullmatch(r"(\d+)([hdw])", s)
    if m:
        return datetime.now(timezone.utc) - timedelta(**{{"h": "hours", "d": "days", "w": "weeks"}[m[2]]: int(m[1])})
    return datetime.fromisoformat(s).replace(tzinfo=timezone.utc)


def gather(since, repo, no_db, root=DEFAULT_ROOT):
    data = load(root)
    otel_events, otel_repos = otel.load()
    if no_db:
        evs = list(data.events.values()) + otel_events
    else:
        data = db.sync(data, extra=otel_events)
        evs = list(data.events.values())
    evs, otel_stats = otel.reconcile(evs, otel_repos)
    prs, stats = attribute(evs, data.pr_links, data.bridge)
    stats.update(otel_stats)
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
@click.option("--by", type=click.Choice(["pr", "session", "branch", "model", "week", "user"]), default="pr")
@click.option("--html", "html_path", type=click.Path(dir_okay=False), help="write a self-contained HTML report")
def report(since, repo, no_db, by, html_path):
    """Spend table (or HTML report) joined to PRs."""
    _, evs, prs, stats = gather(since, repo, no_db)
    for msg in stats:
        if any(w in msg for w in WARN):
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
    from .loaders.stats_cache import upper_bound
    ub = upper_bound({(e["ts"].date(), e["model"]) for e in evs})
    if ub:
        out.rule("usage with no surviving transcript (upper bound only)")
        out.print(f"~/.claude/stats-cache.json has {len(ub['days'])} days ({ub['days'][0]} to {ub['days'][-1]}) of model usage "
                  "whose transcripts are gone. It counts every streamed line rather than each "
                  "message once, so these are ceilings, not spend, and are left out of every report total.")
        for model, u in sorted(ub["models"].items(), key=lambda kv: -kv[1]["usd"]):
            out.print(f"{model:32} at most {rpt.human(u['input'] + u['output'] + u['cache_w'] + u['cache_r']):>7} tokens"
                      f"  at most ${u['usd']:,.2f}")
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
    out.rule("other sources")
    from . import ledgers
    n_otel = sum(e.get("source") == "otel" for e in evs)
    out.print(f"OpenTelemetry: {n_otel} requests from {otel.DEFAULT_DIR} not already in transcripts "
              f"({stats.get('otel duplicates of transcript requests', 0)} duplicates dropped)")
    for name, ok in ledgers.configured().items():
        out.print(f"{ledgers.KEYS[name]:24} {'set' if ok else 'not set'}  ({name} ledger for `tokentab billed`)")
    out.rule("gh")
    import subprocess
    r = subprocess.run(["gh", "auth", "status"], capture_output=True, text=True)
    out.print((r.stdout + r.stderr).strip() or f"gh auth status exited {r.returncode}")


def _comment(number: int | None, repo: str, post: bool, no_db: bool) -> str:
    import json
    import subprocess

    from . import comment as cm
    from .attribute import gh_prs

    r = resolve_repo(str(Path(repo).expanduser().resolve()))
    if r is None or not r.github:
        raise click.UsageError(f"{repo} is not a checkout of a GitHub repository")
    if number is None:
        out = subprocess.run(["gh", "pr", "view", "--json", "number"], cwd=repo, capture_output=True, text=True)
        if out.returncode != 0:
            raise click.UsageError("no PR for the current branch; pass --pr")
        number = json.loads(out.stdout)["number"]
    if not any(p["number"] == number for p in gh_prs(r.slug) or []):
        gh_prs(r.slug, ttl=0)  # a PR opened in the last hour isn't in the cached list yet
    _, evs, prs, _ = gather(None, None, no_db)
    if (r.slug, number) not in prs:
        raise click.UsageError(f"gh did not return PR #{number} for {r.slug}")
    body = cm.render((r.slug, number), evs, prs)
    return f"posted {cm.post(r.slug, number, body)}" if post else body


@main.command()
@click.option("--pr", "number", type=int, help="PR number (default: the PR for the current branch)")
@click.option("--repo", type=click.Path(exists=True), default=".", help="a checkout of the PR's repo")
@click.option("--post", is_flag=True, help="post or update the comment on GitHub (default: print it)")
@click.option("--no-db", is_flag=True)
def comment(number, repo, post, no_db):
    """Render this PR's Claude Code cost as a PR comment (InfraCost-style)."""
    result = _comment(number, repo, post, no_db)
    (err.print if post else click.echo)(result)


@main.command()
def hook():
    """Claude Code PostToolUse hook: after `git push` or `gh pr create`, post or update the PR's cost comment.

    Reads the hook's JSON from stdin. Never fails the tool call: any problem is reported on stderr
    with exit code 0.
    """
    import json

    try:
        data = json.load(sys.stdin)
    except ValueError:
        return
    command = (data.get("tool_input") or {}).get("command") or ""
    if data.get("tool_name") != "Bash" or not re.search(r"\b(git\s+push|gh\s+pr\s+create)\b", command):
        return
    try:
        err.print(_comment(None, data.get("cwd") or ".", True, False))
    except (click.ClickException, RuntimeError, OSError) as e:
        err.print(f"tokentab hook: {getattr(e, 'message', e)}")


@main.group(name="otel")
def otel_group():
    """Collect Claude Code OpenTelemetry data."""


@otel_group.command()
@click.option("--host", default="127.0.0.1", show_default=True, help="use 0.0.0.0 to collect from other machines")
@click.option("--port", default=4318, show_default=True)
@click.option("--dir", "out_dir", type=click.Path(file_okay=False), default=str(otel.DEFAULT_DIR), show_default=True)
@click.option("--token", envvar="TOKENTAB_OTEL_TOKEN", help="require this bearer token (env TOKENTAB_OTEL_TOKEN)")
def serve(host, port, out_dir, token):
    """Receive OTLP/HTTP JSON from Claude Code and store it for reports."""
    if host not in ("127.0.0.1", "localhost", "::1") and not token:
        raise click.UsageError("listening beyond localhost needs --token, or anyone who can reach the port can write data")
    server = otel.serve(host, port, Path(out_dir), token)
    err.print(f"listening on http://{host}:{port} (writing to {out_dir}); point Claude Code at it with:")
    err.print("  CLAUDE_CODE_ENABLE_TELEMETRY=1 OTEL_LOGS_EXPORTER=otlp OTEL_METRICS_EXPORTER=otlp "
              f"OTEL_EXPORTER_OTLP_PROTOCOL=http/json OTEL_EXPORTER_OTLP_ENDPOINT=http://{host}:{port}"
              + (f" OTEL_EXPORTER_OTLP_HEADERS='Authorization=Bearer <token>'" if token else ""))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.shutdown()


@main.command()
@click.option("--since", default="30d", show_default=True, help="e.g. 30d, 2w, or 2026-09-01")
def billed(since):
    """Invoiced and org-level usage from vendor APIs, shown apart from API-equivalent spend."""
    from collections import defaultdict
    from decimal import Decimal

    from rich.table import Table

    from . import ledgers

    have = ledgers.configured()
    if not any(have.values()):
        raise click.UsageError("no vendor keys set; export one of " + ", ".join(ledgers.KEYS.values()))
    end = datetime.now(timezone.utc).date() + timedelta(days=1)
    rows, errors = ledgers.fetch_all(parse_since(since).date(), end)
    for e in errors:
        err.print(f"[red]error:[/] {e}")
    out = Console(width=None if sys.stdout.isatty() else 140)
    by_source = defaultdict(list)
    for r in rows:
        by_source[r["source"]].append(r)
    for source, rs in by_source.items():
        people = "who" in rs[0]
        table = Table(title=source, header_style="bold")
        cols = ["who", "sessions", "PRs", "commits", "usd", "usd/PR"] if people else ["model", "tokens", "usd"]
        for c in cols:
            table.add_column(c, justify="left" if c in ("who", "model") else "right")
        agg = defaultdict(lambda: defaultdict(Decimal))
        for r in rs:
            key = r["who"] if people else r["model"]
            for f in ("usd", "sessions", "prs", "commits", "input", "output", "cache_w", "cache_r"):
                agg[key][f] += Decimal(str(r.get(f, 0)))
        for key, a in sorted(agg.items(), key=lambda kv: -kv[1]["usd"]):
            tokens = a["input"] + a["output"] + a["cache_w"] + a["cache_r"]
            if people:
                per_pr = f"${a['usd'] / a['prs']:,.2f}" if a["prs"] else "-"
                table.add_row(key, str(a["sessions"]), str(a["prs"]), str(a["commits"]), f"${a['usd']:,.2f}", per_pr)
            else:
                table.add_row(key, rpt.human(float(tokens)) if tokens else "-", f"${a['usd']:,.2f}" if a["usd"] else "-")
        out.print(table)
    out.print("These are the vendors' own numbers. They overlap tokentab's API-equivalent spend for any usage that "
              "also appears in local transcripts, so the two are never added together.")
