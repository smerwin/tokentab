"""Self-contained HTML report: inline CSS and SVG, no scripts or network fetches."""
from __future__ import annotations

import math
from datetime import datetime
from html import escape
from pathlib import Path

from . import report as rpt

W, H, PAD_L, PAD_B, PAD_T = 640, 220, 56, 28, 12

CSS = """
:root { color-scheme: light; --surface:#fcfcfb; --raised:#f4f3f0; --ink:#0b0b0b; --ink-2:#52514e; --ink-3:#8a8984;
  --grid:#e4e3df; --series:#2a78d6; --series-hover:#1c5cab; --warn:#b25c00; }
@media (prefers-color-scheme: dark) { :root:not([data-theme="light"]) { color-scheme: dark; --surface:#1a1a19;
  --raised:#242422; --ink:#ffffff; --ink-2:#c3c2b7; --ink-3:#8f8e86; --grid:#333331; --series:#3987e5;
  --series-hover:#86b6ef; --warn:#f0a050; } }
:root[data-theme="dark"] { color-scheme: dark; --surface:#1a1a19; --raised:#242422; --ink:#ffffff; --ink-2:#c3c2b7;
  --ink-3:#8f8e86; --grid:#333331; --series:#3987e5; --series-hover:#86b6ef; --warn:#f0a050; }
* { box-sizing: border-box; }
body { margin:0; background:var(--surface); color:var(--ink); font:15px/1.5 -apple-system, BlinkMacSystemFont,
  "Segoe UI", Roboto, sans-serif; }
main { max-width: 1040px; margin: 0 auto; padding: 32px 16px 64px; }
h1 { font-size: 28px; margin: 0 0 4px; } h2 { font-size: 18px; margin: 40px 0 4px; }
.sub, .note { color: var(--ink-2); margin: 0 0 12px; } .note { font-size: 13px; }
.tiles { display:grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); gap: 12px; margin: 24px 0; }
.tile { background: var(--raised); border-radius: 8px; padding: 14px 16px; }
.tile b { display:block; font-size: 26px; font-variant-numeric: tabular-nums; }
.tile span { color: var(--ink-2); font-size: 13px; }
.charts { display:grid; grid-template-columns: repeat(auto-fit, minmax(420px, 1fr)); gap: 8px 32px; }
@media (max-width: 480px) { .charts { grid-template-columns: 1fr; } }
svg { width: 100%; height: auto; display: block; }
svg text { fill: var(--ink-2); font-size: 12px; } svg .val { fill: var(--ink); }
svg .grid { stroke: var(--grid); stroke-width: 1; } svg .mark { fill: var(--series); }
svg .mark:hover, svg g.pt:hover .mark { fill: var(--series-hover); }
svg .line { fill: none; stroke: var(--series); stroke-width: 2; }
svg .dot { fill: var(--series); stroke: var(--surface); stroke-width: 2; }
.scroll { overflow-x: auto; }
table { border-collapse: collapse; width: 100%; font-size: 13px; font-variant-numeric: tabular-nums; }
th, td { text-align: left; padding: 6px 8px; border-bottom: 1px solid var(--grid); white-space: nowrap; }
th { color: var(--ink-2); font-weight: 600; } td.num, th.num { text-align: right; }
td.title { white-space: normal; min-width: 240px; } tr.unattr td { color: var(--ink-2); }
a { color: var(--series); } details { margin-top: 8px; } summary { color: var(--ink-2); cursor: pointer; font-size: 13px; }
.warn { color: var(--warn); }
"""


def _money(v: float) -> str:
    return f"${v:,.0f}" if abs(v) >= 100 else f"${v:,.2f}"


def _axis_money(v: float) -> str:
    return f"${v:,.0f}" if v == int(v) else f"${v:,.2f}"


def _ticks(vmax: float, n: int = 4) -> list[float]:
    if vmax <= 0:
        return [0.0, 1.0]
    mag = 10 ** math.floor(math.log10(vmax / n))
    step = next(k * mag for k in (1, 2, 2.5, 5, 10) if k * mag >= vmax / n)
    return [i * step for i in range(math.ceil(vmax / step) + 1)]


def _frame(ticks: list[float], fmt, y) -> str:
    return "".join(
        f'<line class="grid" x1="{PAD_L}" x2="{W}" y1="{y(t):.1f}" y2="{y(t):.1f}"/>'
        f'<text x="{PAD_L - 8}" y="{y(t) + 4:.1f}" text-anchor="end">{fmt(t)}</text>' for t in ticks)


def bars(points: list[tuple[str, float | None, str]], fmt, label: str) -> str:
    """Vertical bars; a None value leaves a labelled gap."""
    vals = [v for _, v, _ in points if v is not None]
    ticks = _ticks(max(vals, default=0))
    top = ticks[-1] or 1
    y = lambda v: PAD_T + (H - PAD_T - PAD_B) * (1 - v / top)
    slot = (W - PAD_L) / max(len(points), 1)
    bw, r, base = min(slot * 0.6, 48), 4, y(0)
    out = [_frame(ticks, fmt, y)]
    for i, (name, v, tip) in enumerate(points):
        x = PAD_L + i * slot + (slot - bw) / 2
        if v is None:
            out.append(f'<text x="{x + bw / 2:.1f}" y="{base - 6:.1f}" text-anchor="middle">–</text>')
        elif v > 0:
            t = y(v)
            rr = min(r, base - t)
            out.append(
                f'<path class="mark" d="M{x:.1f},{base:.1f}V{t + rr:.1f}Q{x:.1f},{t:.1f} {x + rr:.1f},{t:.1f}'
                f'H{x + bw - rr:.1f}Q{x + bw:.1f},{t:.1f} {x + bw:.1f},{t + rr:.1f}V{base:.1f}Z">'
                f'<title>{escape(tip)}</title></path>')
        if i % max(1, len(points) // 8) == 0:
            out.append(f'<text x="{x + bw / 2:.1f}" y="{H - 8}" text-anchor="middle">{escape(name)}</text>')
    return f'<svg viewBox="0 0 {W} {H}" role="img" aria-label="{escape(label)}">{"".join(out)}</svg>'


def line(points: list[tuple[str, float, str]], fmt, label: str, floor: float = 0.0) -> str:
    lo = min((v for _, v, _ in points), default=0)
    lo = floor if lo >= floor else lo
    ticks = [lo + (1 - lo) * i / 4 for i in range(5)]
    y = lambda v: PAD_T + (H - PAD_T - PAD_B) * (1 - (v - lo) / ((1 - lo) or 1))
    slot = (W - PAD_L) / max(len(points), 1)
    xs = [PAD_L + slot * (i + 0.5) for i in range(len(points))]
    out = [_frame(ticks, fmt, y)]
    out.append('<polyline class="line" points="' + " ".join(f"{x:.1f},{y(v):.1f}" for x, (_, v, _) in zip(xs, points)) + '"/>')
    for i, (x, (name, v, tip)) in enumerate(zip(xs, points)):
        out.append(f'<g class="pt"><rect x="{x - slot / 2:.1f}" y="0" width="{slot:.1f}" height="{H}" fill="transparent"/>'
                   f'<circle class="dot mark" cx="{x:.1f}" cy="{y(v):.1f}" r="4"/><title>{escape(tip)}</title></g>')
        if i % max(1, len(points) // 8) == 0:
            out.append(f'<text x="{x:.1f}" y="{H - 8}" text-anchor="middle">{escape(name)}</text>')
    return f'<svg viewBox="0 0 {W} {H}" role="img" aria-label="{escape(label)}">{"".join(out)}</svg>'


def hbars(items: list[tuple[str, float]], label: str) -> str:
    row, lab_w, val_w = 30, 190, 80
    h = row * len(items) + 8
    top = max((v for _, v in items), default=0) or 1
    out = []
    for i, (name, v) in enumerate(items):
        yy = 4 + i * row
        w = (W - lab_w - val_w) * v / top
        out.append(f'<text x="{lab_w - 10}" y="{yy + 18}" text-anchor="end">{escape(name)}</text>')
        if w > 0:
            rr = min(4, w)
            out.append(f'<path class="mark" d="M{lab_w},{yy + 6}H{lab_w + w - rr:.1f}Q{lab_w + w:.1f},{yy + 6} '
                       f'{lab_w + w:.1f},{yy + 6 + rr}V{yy + 20 - rr}Q{lab_w + w:.1f},{yy + 20} {lab_w + w - rr:.1f},{yy + 20}'
                       f'H{lab_w}Z"><title>{escape(name)}: {_money(v)}</title></path>')
        out.append(f'<text class="val" x="{lab_w + w + 8:.1f}" y="{yy + 18}">{_money(v)}</text>')
    return f'<svg viewBox="0 0 {W} {h}" role="img" aria-label="{escape(label)}">{"".join(out)}</svg>'


def _table(head: list[str], rows: list[list[str]], num: set[int], classes: list[str] | None = None) -> str:
    th = "".join(f'<th class="num">{h}</th>' if i in num else f"<th>{h}</th>" for i, h in enumerate(head))
    body = "".join(
        f'<tr class="{classes[j] if classes else ""}">' + "".join(
            f'<td class="{"num" if i in num else "title" if h == "Title" else ""}">{c}</td>'
            for i, (h, c) in enumerate(zip(head, r))) + "</tr>" for j, r in enumerate(rows))
    return f'<div class="scroll"><table><thead><tr>{th}</tr></thead><tbody>{body}</tbody></table></div>'


def write(evs: list[dict], prs: dict, path: Path, since: str | None = None) -> None:
    s = rpt.summary(evs, prs)
    rows = rpt.pr_rows(evs, prs)
    weeks = sorted(rpt.group(evs, lambda e: rpt.week(e["ts"])).items())
    merged_by_week: dict[str, list[float]] = {}
    for r in rows:
        if r["state"] == "merged":
            merged_by_week.setdefault(rpt.week(datetime.fromisoformat(r["merged_at"])), []).append(r["usd"])
    wk = [(k, rpt.totals(g)) for k, g in weeks]
    label = lambda k: datetime.fromisoformat(k).strftime("%b %d")
    spend = [(label(k), t["usd"], f"Week of {k}: {_money(t['usd'])}") for k, t in wk]
    per_pr = []
    for k, _ in wk:
        m = merged_by_week.get(k)
        per_pr.append((label(k), sum(m) / len(m) if m else None,
                       f"Week of {k}: {len(m)} merged, {_money(sum(m) / len(m))} each on average" if m else ""))
    cache = [(label(k), t["cache_ratio"], f"Week of {k}: {t['cache_ratio']:.1%} of prompt tokens read from cache") for k, t in wk]
    models = sorted(((m, rpt.totals(g)["usd"]) for m, g in rpt.group(evs, lambda e: e["model"]).items()), key=lambda x: -x[1])
    flagged = sorted({e["model"] for e in evs if e["price_flag"]})
    span = f"Last {since}" if since else "All recorded history"
    if evs:
        span += f" · {min(e['ts'] for e in evs).astimezone():%b %d, %Y} – {max(e['ts'] for e in evs).astimezone():%b %d, %Y}"

    tiles = [
        (_money(s["usd"]), "API-equivalent spend"),
        (str(s["merged_prs"]), "merged PRs with Claude spend"),
        (_money(s["median_usd_per_merged_pr"]), "median cost per merged PR"),
        (_money(s["unattributed_usd"]), "not tied to any PR"),
        (_money(s["dead_usd"]), "dead spend"),
        (f"{s['cache_ratio']:.0%}", "of prompt tokens served from cache"),
    ]
    pr_table = _table(
        ["PR", "Title", "Status", "Sessions", "Tokens", "Cache", "Output", "Spend", "Confidence"],
        [[f'<a href="{r["url"]}">{escape(r["label"])}</a>' if r.get("url") else escape(r["label"]), escape(r["title"]),
          {"merged": "merged", "closed": "closed, not merged", "open": "open"}.get(r["state"], "—"),
          str(r["sessions"]), rpt.human(r["tokens"]), f"{r['cache_ratio']:.0%}", f"{r['output_share']:.1%}",
          _money(r["usd"]), r["confidence"]] for r in rows],
        num={3, 4, 5, 6, 7}, classes=["unattr" if r["state"] == "unattributed" else "" for r in rows])
    week_table = _table(
        ["Week of", "Spend", "Merged PRs", "Avg per merged PR", "Cache"],
        [[k, _money(t["usd"]), str(len(merged_by_week.get(k, []))),
          _money(sum(merged_by_week[k]) / len(merged_by_week[k])) if k in merged_by_week else "—",
          f"{t['cache_ratio']:.1%}"] for k, t in wk], num={1, 2, 3, 4})
    warn = (f'<p class="note warn">Prices for {", ".join(escape(m) for m in flagged)} are unverified or estimated '
            f'(flagged in prices.yaml); confirm them before quoting these numbers.</p>') if flagged else ""

    html = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Claude Spend Report</title><style>{CSS}</style></head><body><main>
<h1>What Claude Code spend bought</h1>
<p class="sub">{escape(span)}. Generated {datetime.now():%b %d, %Y %H:%M}.</p>
<p class="note">“Spend” is <b>API-equivalent USD</b>: every token priced at Anthropic's published per-token rate.
On a Claude subscription you pay the seat fee instead; this is what the same work would have cost on the API,
and the fair yardstick for whether the seat pays for itself.</p>
{warn}
<div class="tiles">{"".join(f'<div class="tile"><b>{v}</b><span>{k}</span></div>' for v, k in tiles)}</div>
<div class="charts">
<section><h2>Spend per week</h2><p class="note">API-equivalent USD, by week the tokens were used.</p>
{bars(spend, _axis_money, "Spend per week")}</section>
<section><h2>Average cost per merged PR</h2><p class="note">Spend on PRs merged that week ÷ PRs merged. A dash means no merges.</p>
{bars(per_pr, _axis_money, "Average cost per merged PR by week merged")}</section>
<section><h2>Cache hit ratio</h2><p class="note">Share of prompt tokens re-read from cache (billed at a fraction of full price).</p>
{line(cache, lambda v: f"{v:.0%}", "Weekly cache hit ratio", floor=0.8)}</section>
<section><h2>Spend by model</h2><p class="note">Which Claude models the money went to.</p>
{hbars(models, "Spend by model")}</section>
</div>
<details><summary>Show weekly numbers as a table</summary>{week_table}</details>
<h2>Spend per pull request</h2>
<p class="note">Rows in grey could not be tied to a PR and are reported, not dropped. <b>Dead spend</b> is spend on PRs
closed without merging, plus work in a GitHub repo that never reached a PR.</p>
{pr_table}
<p class="note"><b>Confidence</b>: <i>pr-link</i> means Claude Code itself recorded opening or linking the PR in that session;
<i>trailer</i> means a merged commit names the session in a <code>Claude-Session:</code> trailer; <i>branch+window</i> means
the session ran on the PR's branch while it was open; <i>repo-only</i> means we know the repository but not the PR.</p>
</main></body></html>
"""
    path.write_text(html)
