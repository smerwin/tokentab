"""Claude Code's ~/.claude/stats-cache.json, used only as an upper bound for usage whose
transcripts no longer exist.

It is not usable as spend: its counts include every streamed line of a message, not the
final one (on days that also have transcripts it overstates input+output 1.8-2.5x), daily
figures cover input+output only, and cache tokens exist only as lifetime totals per model.
"""
from __future__ import annotations

import json
from datetime import date
from pathlib import Path

from ..pricing import cost

DEFAULT_PATH = Path.home() / ".claude" / "stats-cache.json"


def upper_bound(covered: set[tuple[date, str]], path: Path = DEFAULT_PATH) -> dict | None:
    """Per-model token and USD upper bounds for the (day, model) pairs not in `covered`.

    Each model's lifetime token split is apportioned by the share of its daily input+output
    on uncovered days. Returns None when the file is missing, unreadable, or fully covered.
    """
    try:
        d = json.loads(path.read_text())
        daily = [(date.fromisoformat(r["date"]), r["tokensByModel"]) for r in d["dailyModelTokens"]]
        usage = d["modelUsage"]
    except (OSError, ValueError, KeyError, TypeError):
        return None
    models = {}
    for model, u in usage.items():
        total = sum(t.get(model, 0) for _, t in daily)
        early = sum(t.get(model, 0) for day, t in daily if (day, model) not in covered)
        if not total or not early:
            continue
        share = early / total
        cw = u.get("cacheCreationInputTokens", 0) * share
        ev = {"model": model, "input": u.get("inputTokens", 0) * share, "output": u.get("outputTokens", 0) * share,
              "cache_w": cw, "cache_w_1h": cw, "cache_r": u.get("cacheReadInputTokens", 0) * share}
        models[model] = {**ev, "usd": cost(ev)}
    days = sorted({day for day, t in daily for m in t if m in models and (day, m) not in covered})
    return {"days": days, "models": models} if models else None
