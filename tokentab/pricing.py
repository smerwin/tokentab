"""API-equivalent USD from prices.yaml. Unknown models are priced at the nearest row and flagged."""
from __future__ import annotations

import os
from functools import cache
from pathlib import Path

import yaml

DEFAULT_PRICES = Path(os.environ.get("TOKENTAB_PRICES", Path(__file__).with_name("prices.yaml")))


@cache
def table(path: Path = DEFAULT_PRICES) -> dict:
    return yaml.safe_load(Path(path).read_text())


def _common_prefix(a: str, b: str) -> int:
    n = 0
    while n < min(len(a), len(b)) and a[n] == b[n]:
        n += 1
    return n


def lookup(model: str, prices: dict | None = None) -> tuple[str, dict, str]:
    """Return (row key, row, flag). flag is "" when priced, "verify" for unconfirmed rows,
    or "unpriced" when the model matched no key and borrowed the nearest row."""
    prices = prices or table()
    hits = [k for k in prices if k in model]
    if hits:
        key = max(hits, key=len)
        return key, prices[key], "verify" if prices[key].get("verify") else ""
    family = [k for k in prices if any(f in model and f in k for f in ("opus", "sonnet", "haiku", "fable", "mythos"))]
    key = max(family or prices, key=lambda k: _common_prefix(k, model))
    return key, prices[key], "unpriced"


def cost(ev: dict, prices: dict | None = None) -> float:
    _, p, _ = lookup(ev["model"], prices)
    cw_1h = ev["cache_w_1h"]
    cw_5m = ev["cache_w"] - cw_1h
    return (
        ev["input"] * p["input"]
        + ev["output"] * p["output"]
        + cw_5m * p["input"] * p["cache_write_mult"]
        + cw_1h * p["input"] * p.get("cache_write_1h_mult", p["cache_write_mult"])
        + ev["cache_r"] * p["input"] * p["cache_read_mult"]
    ) / 1e6
