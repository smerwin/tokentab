"""Aggregations shared by the terminal table, HTML report, and CSV export."""
from __future__ import annotations

from collections import defaultdict
from datetime import timedelta
from statistics import median

from rich.console import Console
from rich.table import Table

from .attribute import RANK
from .loaders.claude_jsonl import TOKEN_FIELDS

PROMPT = ("input", "cache_w", "cache_r")


def totals(evs: list[dict]) -> dict:
    t = {f: sum(e[f] for e in evs) for f in TOKEN_FIELDS}
    t["usd"] = sum(e["usd"] for e in evs)
    t["tokens"] = sum(t[f] for f in PROMPT) + t["output"]
    prompt = sum(t[f] for f in PROMPT)
    t["cache_ratio"] = t["cache_r"] / prompt if prompt else 0.0
    t["output_share"] = t["output"] / t["tokens"] if t["tokens"] else 0.0
    t["sessions"] = len({e["session_id"] for e in evs})
    t["flags"] = sorted({e["price_flag"] for e in evs} - {""})
    return t


def is_dead(ev: dict, prs: dict) -> bool:
    """Spend on a PR closed unmerged, or in a GitHub repo but never tied to any PR."""
    if ev["pr"]:
        return prs[ev["pr"]]["state"] == "closed"
    return ev["confidence"] == "repo-only" and ev["github"]


def weakest(evs: list[dict]) -> str:
    return min((e["confidence"] for e in evs), key=RANK.__getitem__)


def group(evs: list[dict], key) -> dict:
    out = defaultdict(list)
    for e in evs:
        out[key(e)].append(e)
    return out


def pr_rows(evs: list[dict], prs: dict) -> list[dict]:
    rows = []
    for k, g in group(evs, lambda e: e["pr"] or ("unattributed", e["repo"] or "(no repo)")).items():
        t = totals(g)
        if k[0] == "unattributed":
            t.update(label=k[1].rsplit("/", 1)[-1].strip("()").join("()"), title="unattributed: no PR", merged="-", state="unattributed")
        else:
            p = prs[k]
            t.update(label=f"{k[0].rsplit('/', 1)[-1]}#{k[1]}", title=p["title"] or "(title unavailable without GitHub access)", state=p["state"],
                     merged={"merged": "Y", "closed": "N", "unknown": "?"}.get(p["state"], "open"), merged_at=p.get("mergedAt"),
                     url=f"https://github.com/{k[0]}/pull/{k[1]}")
        t["confidence"] = weakest(g)
        rows.append(t)
    return sorted(rows, key=lambda r: (r["state"] == "unattributed", -r["usd"]))


def summary(evs: list[dict], prs: dict) -> dict:
    merged = [r["usd"] for r in pr_rows(evs, prs) if r["state"] == "merged"]
    t = totals(evs)
    t.update(
        unattributed_usd=sum(e["usd"] for e in evs if not e["pr"]),
        dead_usd=sum(e["usd"] for e in evs if is_dead(e, prs)),
        merged_prs=len(merged),
        median_usd_per_merged_pr=median(merged) if merged else 0.0,
    )
    return t


VIEWS = {
    "session": lambda e: e["session_id"][:8],
    "branch": lambda e: f"{(e['repo'] or '(no repo)').rsplit('/', 1)[-1]}:{e['branch'] or '(detached)'}",
    "model": lambda e: e["model"],
    "user": lambda e: e.get("user") or "(this machine)",
    "week": lambda e: week(e["ts"]),
}


def week(ts) -> str:
    local = ts.astimezone()
    return (local.date() - timedelta(days=local.weekday())).isoformat()


def human(n: float) -> str:
    for unit, div in (("B", 1e9), ("M", 1e6), ("k", 1e3)):
        if abs(n) >= div:
            return f"{n / div:.1f}{unit}"
    return str(int(n))


def _metrics(t: dict) -> list[str]:
    return [human(t["input"]), human(t["output"]), human(t["cache_w"]), human(t["cache_r"]),
            f"{t['cache_ratio']:.0%}", f"{t['output_share']:.1%}", f"${t['usd']:,.2f}"]


METRIC_COLS = ("in", "out", "cache_w", "cache_r", "cache%", "out%", "usd")


def _table(head: list[str], rows: list[tuple[list[str], str | None]], flexible: int, width: int) -> Table:
    """Size every column to its content except `flexible`, which gets the leftover width."""
    widths = [max(len(h), *(len(c[i]) for c, _ in rows)) if rows else len(h) for i, h in enumerate(head)]
    spare = width - (sum(widths) - widths[flexible]) - 3 * len(head) - 1
    widths[flexible] = max(8, min(widths[flexible], spare))
    table = Table(header_style="bold")
    for h, w in zip(head, widths):
        table.add_column(h, no_wrap=True, width=w, justify="right" if h in METRIC_COLS or h == "sessions" else "left")
    for cells, style in rows:
        cells = list(cells)
        if len(cells[flexible]) > widths[flexible]:
            cells[flexible] = cells[flexible][: widths[flexible] - 1] + "…"
        table.add_row(*cells, style=style)
    return table


def render(evs: list[dict], prs: dict, by: str, console: Console) -> None:
    if by == "pr":
        rows = []
        for r in pr_rows(evs, prs):
            title = r["title"] if len(r["title"]) <= 40 else r["title"][:39] + "…"
            style = "dim" if r["state"] == "unattributed" else "red" if r["state"] == "closed" else None
            rows.append(([r["label"], title, r["merged"], str(r["sessions"]), *_metrics(r), r["confidence"]], style))
        table = _table(["PR#", "title", "merged", "sessions", *METRIC_COLS, "confidence"], rows, flexible=1, width=console.width)
    else:
        groups = sorted(group(evs, VIEWS[by]).items(), key=lambda kv: kv[0] if by == "week" else -totals(kv[1])["usd"])
        rows = [([str(k), str(t["sessions"]), *_metrics(t), ",".join(t["flags"])], None)
                for k, t in ((k, totals(g)) for k, g in groups)]
        table = _table([by, "sessions", *METRIC_COLS, "flag"], rows, flexible=0, width=console.width)
    s = summary(evs, prs)
    console.print(table)
    console.print(
        f"[bold]total ${s['usd']:,.2f}[/]   unattributed ${s['unattributed_usd']:,.2f}   "
        f"dead_spend ${s['dead_usd']:,.2f}   median ${s['median_usd_per_merged_pr']:,.2f}/merged PR "
        f"({s['merged_prs']} merged)   cache {s['cache_ratio']:.0%}"
    )


CSV_FIELDS = ("session_id", "source", "user", "repo", "pr_number", "pr_title", "pr_state", "confidence", "first_ts", "last_ts",
              "models", "input", "output", "cache_w", "cache_w_1h", "cache_r", "tokens", "cache_ratio",
              "output_share", "usd", "dead_spend", "price_flags")


def csv_rows(evs: list[dict], prs: dict) -> list[dict]:
    rows = []
    for (session, repo, pr), g in sorted(group(evs, lambda e: (e["session_id"], e["repo"] or "", e["pr"])).items(),
                                          key=lambda kv: min(e["ts"] for e in kv[1])):
        t, p = totals(g), prs.get(pr, {})
        rows.append({
            "session_id": session, "source": " ".join(sorted({e.get("source", "transcript") for e in g})),
            "user": " ".join(sorted({e.get("user") or "" for e in g} - {""})), "repo": repo, "pr_number": pr[1] if pr else "", "pr_title": p.get("title", ""),
            "pr_state": p.get("state", "unattributed"), "confidence": weakest(g),
            "first_ts": min(e["ts"] for e in g).isoformat(), "last_ts": max(e["ts"] for e in g).isoformat(),
            "models": " ".join(sorted({e["model"] for e in g})),
            **{f: t[f] for f in (*TOKEN_FIELDS, "tokens")},
            "cache_ratio": round(t["cache_ratio"], 4), "output_share": round(t["output_share"], 4),
            "usd": round(t["usd"], 4), "dead_spend": round(sum(e["usd"] for e in g if is_dead(e, prs)), 4),
            "price_flags": " ".join(t["flags"]),
        })
    return rows
