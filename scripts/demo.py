"""Render the README screenshots from synthetic data: an imaginary acme/webapp repo with a month
of Claude Code sessions. Nothing here reads your transcripts or calls GitHub.

    uv run python scripts/demo.py
"""
from __future__ import annotations

import io
import json
import random
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from rich.console import Console

from tokentab import report
from tokentab.attribute import Repo, attribute
from tokentab.html import write
from tokentab.loaders.claude_jsonl import load

DOCS = Path(__file__).resolve().parent.parent / "docs"
NOW = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
REPO = "/work/webapp"

# (title, branch, days ago opened, days open, outcome, sessions, messages per session, model)
PRS = [
    ("Add CSV export to the billing page", "csv-export", 26, 2, "merged", 2, 120, "claude-opus-5-5"),
    ("Fix flaky login test on CI", "fix-login-flake", 24, 1, "merged", 1, 45, "claude-sonnet-5"),
    ("Migrate settings to the new API client", "settings-api-client", 21, 4, "merged", 3, 160, "claude-opus-5-5"),
    ("Rate-limit password reset emails", "reset-rate-limit", 17, 1, "merged", 1, 60, "claude-sonnet-5"),
    ("Try GraphQL for the orders page", "orders-graphql", 15, 3, "closed", 2, 140, "claude-opus-5-5"),
    ("Dark mode for the dashboard", "dashboard-dark-mode", 11, 2, "merged", 2, 90, "claude-sonnet-5"),
    ("Speed up search indexing", "faster-search-index", 7, 3, "merged", 2, 130, "claude-opus-5-5"),
    ("Refactor the notification service", "notifications-refactor", 3, None, "open", 2, 110, "claude-opus-5-5"),
]


def message(rng, session, ts, branch, model, cwd, i):
    return {
        "type": "assistant", "sessionId": session, "timestamp": ts.isoformat().replace("+00:00", "Z"),
        "cwd": cwd, "gitBranch": branch, "isSidechain": False, "requestId": f"req_{session}_{i}",
        "message": {"id": f"msg_{session}_{i}", "model": model, "usage": {
            "input_tokens": rng.randint(2, 40), "output_tokens": rng.randint(150, 2500),
            "cache_creation_input_tokens": (cw := rng.randint(1500, 12000)),
            "cache_read_input_tokens": rng.randint(40_000, 140_000),
            "cache_creation": {"ephemeral_1h_input_tokens": cw, "ephemeral_5m_input_tokens": 0}}},
    }


def build(root: Path) -> list[dict]:
    rng = random.Random(7)
    prs, n = [], 0
    project = root / "-work-webapp"
    project.mkdir(parents=True)
    for number, (title, branch, ago, days, outcome, sessions, msgs, model) in enumerate(PRS, start=101):
        opened = NOW - timedelta(days=ago)
        closed = opened + timedelta(days=days) if days else None
        prs.append({"number": number, "title": title, "headRefName": branch, "createdAt": opened.isoformat(),
                    "mergedAt": closed.isoformat() if outcome == "merged" else None,
                    "closedAt": closed.isoformat() if closed else None})
        for s in range(sessions):
            n += 1
            start = opened - timedelta(hours=6) + timedelta(hours=20 * s)
            lines = [message(rng, f"s{n}", start + timedelta(minutes=2 * i), branch, model, REPO, i) for i in range(msgs)]
            (project / f"s{n}.jsonl").write_text("\n".join(json.dumps(x) for x in lines) + "\n")
    for ago, msgs, cwd, branch in ((20, 70, REPO, "main"), (9, 50, REPO, "main"), (5, 40, "/work/scratch", "HEAD")):
        n += 1
        start = NOW - timedelta(days=ago)
        lines = [message(rng, f"s{n}", start + timedelta(minutes=2 * i), branch, "claude-sonnet-5", cwd, i) for i in range(msgs)]
        (project / f"s{n}.jsonl").write_text("\n".join(json.dumps(x) for x in lines) + "\n")
    return prs


def main() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        prs_json = build(Path(tmp) / "projects")
        data = load(Path(tmp) / "projects")
        evs = list(data.events.values())
        prs, _ = attribute(evs, data.pr_links, data.bridge, now=NOW,
                           resolve=lambda cwd: Repo("acme/webapp", REPO, True) if cwd.startswith(REPO) else None,
                           fetch_prs=lambda slug: prs_json, fetch_commits=lambda path: [])
        console = Console(record=True, width=150, file=io.StringIO())
        report.render(evs, prs, "pr", console)
        console.save_svg(str(DOCS / "demo-terminal.svg"), title="tokentab report --since 30d")
        html = Path(tmp) / "demo-report.html"
        write(evs, prs, html, since="30d")
        light = html.read_text().replace('<html lang="en">', '<html lang="en" data-theme="light">')
        html.write_text(light)
        if sys.platform == "darwin":  # Quick Look renders the page to a PNG without opening a browser
            subprocess.run(["qlmanage", "-t", "-s", "1400", "-o", tmp, str(html)], capture_output=True, timeout=60)
            (Path(tmp) / "demo-report.html.png").replace(DOCS / "demo-report.png")
        else:
            (DOCS / "demo-report.html").write_text(light)


if __name__ == "__main__":
    main()
