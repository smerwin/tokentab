"""Billed and org-level usage from vendor APIs. These are shown beside, never added to, the
API-equivalent totals: the same Claude Code request can appear both in a transcript and in
an organization report, and nothing in the reports identifies individual requests.

Credentials come from the environment and are sent only to the vendor that issued them:
  ANTHROPIC_ADMIN_KEY      Admin API key (Claude Console orgs): cost, usage, Claude Code analytics
  ANTHROPIC_ANALYTICS_KEY  Analytics API key, read:analytics scope (Claude Enterprise orgs)
  OPENAI_ADMIN_KEY         OpenAI organization admin key: usage and costs
"""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

ANTHROPIC = "https://api.anthropic.com"
OPENAI = "https://api.openai.com"
ENTERPRISE_EPOCH = date(2026, 1, 1)
KEYS = {"anthropic": "ANTHROPIC_ADMIN_KEY", "enterprise": "ANTHROPIC_ANALYTICS_KEY", "openai": "OPENAI_ADMIN_KEY"}


class LedgerError(RuntimeError):
    pass


def http_get(url: str, headers: dict, retries: int = 3) -> dict:
    for attempt in range(retries + 1):
        req = urllib.request.Request(url, headers={**headers, "User-Agent": "tokentab/0.1"})
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            if e.code == 429 and attempt < retries:
                time.sleep(min(int(e.headers.get("Retry-After") or 2 ** attempt), 60))
                continue
            detail = e.read().decode(errors="replace")[:300]
            raise LedgerError(f"HTTP {e.code} from {urllib.parse.urlsplit(url).path}: {detail}") from None
        except urllib.error.URLError as e:
            raise LedgerError(f"cannot reach {urllib.parse.urlsplit(url).netloc}: {e.reason}") from None
    raise LedgerError("rate limited")


def _pages(get, base: str, path: str, params: dict, headers: dict) -> list[dict]:
    """Every page of a cursor-paginated endpoint (`next_page` passed back as `page`). Pages are
    collected before returning so a restart after an expired cursor (HTTP 410) can't duplicate rows."""
    for attempt in range(2):
        params, pages = {k: v for k, v in params.items() if k != "page"}, []
        try:
            while True:
                page = get(f"{base}{path}?{urllib.parse.urlencode(params, doseq=True)}", headers)
                pages.append(page)
                nxt = page.get("next_page")
                if not nxt or page.get("has_more") is False:
                    return pages
                params["page"] = nxt
        except LedgerError as e:
            if "HTTP 410" not in str(e) or attempt:
                raise
    return []


def _rfc3339(d: date) -> str:
    return f"{d.isoformat()}T00:00:00Z"


def _windows(start: date, end: date, days: int):
    while start < end:
        stop = min(end, start + timedelta(days=days))
        yield start, stop
        start = stop


def _anthropic_headers(key: str) -> dict:
    return {"x-api-key": key, "anthropic-version": "2023-06-01"}


def anthropic_cost(key: str, start: date, end: date, get=http_get) -> list[dict]:
    """Invoiced USD per day and model from /v1/organizations/cost_report (amounts are cents)."""
    rows = []
    params = {"starting_at": _rfc3339(start), "ending_at": _rfc3339(end), "group_by[]": ["description"], "limit": 31}
    for page in _pages(get, ANTHROPIC, "/v1/organizations/cost_report", params, _anthropic_headers(key)):
        for bucket in page.get("data", []):
            for r in bucket.get("results", []):
                rows.append({"source": "anthropic cost report", "date": bucket["starting_at"][:10],
                             "model": r.get("model") or r.get("description") or r.get("cost_type") or "other",
                             "usd": Decimal(r["amount"]) / 100, "currency": r.get("currency", "USD")})
    return rows


def anthropic_usage(key: str, start: date, end: date, get=http_get) -> list[dict]:
    """Billed tokens per day and model from /v1/organizations/usage_report/messages."""
    rows = []
    params = {"starting_at": _rfc3339(start), "ending_at": _rfc3339(end), "group_by[]": ["model"],
              "bucket_width": "1d", "limit": 31}
    for page in _pages(get, ANTHROPIC, "/v1/organizations/usage_report/messages", params, _anthropic_headers(key)):
        for bucket in page.get("data", []):
            for r in bucket.get("results", []):
                cc = r.get("cache_creation") or {}
                rows.append({"source": "anthropic usage report", "date": bucket["starting_at"][:10],
                             "model": r.get("model") or "all", "input": r.get("uncached_input_tokens", 0),
                             "output": r.get("output_tokens", 0),
                             "cache_w": cc.get("ephemeral_5m_input_tokens", 0) + cc.get("ephemeral_1h_input_tokens", 0),
                             "cache_r": r.get("cache_read_input_tokens", 0)})
    return rows


def claude_code_analytics(key: str, start: date, end: date, get=http_get) -> list[dict]:
    """Per user per day Claude Code activity and estimated cost from
    /v1/organizations/usage_report/claude_code (one request per UTC day; cost in cents)."""
    rows = []
    day = start
    while day < end:
        params = {"starting_at": day.isoformat(), "limit": 1000}
        for page in _pages(get, ANTHROPIC, "/v1/organizations/usage_report/claude_code", params, _anthropic_headers(key)):
            for r in page.get("data", []):
                actor = r.get("actor") or {}
                core = r.get("core_metrics") or {}
                models = r.get("model_breakdown") or []
                rows.append({"source": "claude code analytics", "date": r.get("date", day.isoformat())[:10],
                             "who": actor.get("email_address") or actor.get("api_key_name") or "unknown",
                             "usd": sum(Decimal(str((m.get("estimated_cost") or {}).get("amount", 0))) for m in models) / 100,
                             "sessions": core.get("num_sessions", 0),
                             "prs": core.get("pull_requests_by_claude_code", 0),
                             "commits": core.get("commits_by_claude_code", 0)})
        day += timedelta(days=1)
    return rows


def enterprise(key: str, start: date, end: date, get=http_get) -> list[dict]:
    """Per-user Claude Code cost and activity for a Claude Enterprise org, joined on user id:
    cost from /analytics/user_cost_report (products[]=claude_code, fractional cents, 31-day
    windows), PRs/commits/sessions from /analytics/users in date-range mode."""
    today = datetime.now(timezone.utc).date()
    start, end = max(start, ENTERPRISE_EPOCH, today - timedelta(days=365)), min(end, today)
    headers = _anthropic_headers(key)
    people: dict[str, dict] = defaultdict(lambda: {"usd": Decimal(0), "prs": 0, "commits": 0, "sessions": 0, "who": ""})
    for a, b in _windows(start, end, 31):
        params = {"starting_at": _rfc3339(a), "ending_at": _rfc3339(b), "products[]": ["claude_code"], "limit": 1000}
        for page in _pages(get, ANTHROPIC, "/v1/organizations/analytics/user_cost_report", params, headers):
            for r in page.get("data", []):
                actor = r.get("actor") or {}
                p = people[actor.get("user_id") or actor.get("email") or "unknown"]
                p["usd"] += Decimal(r.get("amount") or "0") / 100
                p["who"] = p["who"] or actor.get("email") or actor.get("name") or actor.get("user_id") or "unknown"
    for a, b in _windows(start, end, 366):
        params = {"starting_date": a.isoformat(), "ending_date": b.isoformat(), "limit": 1000}
        for page in _pages(get, ANTHROPIC, "/v1/organizations/analytics/users", params, headers):
            for r in page.get("data", []):
                user = r.get("user") or {}
                core = (r.get("claude_code_metrics") or {}).get("core_metrics") or {}
                p = people[user.get("id") or user.get("email_address") or "unknown"]
                p["who"] = p["who"] or user.get("email_address") or user.get("id") or "unknown"
                p["prs"] += core.get("pull_request_count", 0)
                p["commits"] += core.get("commit_count", 0)
                p["sessions"] += core.get("distinct_session_count") or 0
    return [{"source": "claude enterprise analytics", "date": f"{start}..{end}", **p} for p in people.values()
            if p["usd"] or p["prs"] or p["commits"] or p["sessions"]]


def openai(key: str, start: date, end: date, get=http_get) -> list[dict]:
    """OpenAI org costs per day and line item, and tokens per day and model."""
    headers = {"Authorization": f"Bearer {key}"}
    t0 = int(datetime(start.year, start.month, start.day, tzinfo=timezone.utc).timestamp())
    t1 = int(datetime(end.year, end.month, end.day, tzinfo=timezone.utc).timestamp())
    day = lambda ts: datetime.fromtimestamp(ts, tz=timezone.utc).date().isoformat()
    rows = []
    params = {"start_time": t0, "end_time": t1, "bucket_width": "1d", "group_by": ["line_item"], "limit": 180}
    for page in _pages(get, OPENAI, "/v1/organization/costs", params, headers):
        for bucket in page.get("data", []):
            for r in bucket.get("results", []):
                amount = r.get("amount") or {}
                rows.append({"source": "openai costs", "date": day(bucket["start_time"]),
                             "model": r.get("line_item") or "all", "usd": Decimal(str(amount.get("value", 0))),
                             "currency": (amount.get("currency") or "usd").upper()})
    params = {"start_time": t0, "end_time": t1, "bucket_width": "1d", "group_by": ["model"], "limit": 31}
    for page in _pages(get, OPENAI, "/v1/organization/usage/completions", params, headers):
        for bucket in page.get("data", []):
            for r in bucket.get("results", []):
                cached, written = r.get("input_cached_tokens", 0), r.get("input_cache_write_tokens", 0)
                uncached = r.get("input_uncached_tokens", max(r.get("input_tokens", 0) - cached - written, 0))
                rows.append({"source": "openai usage", "date": day(bucket["start_time"]), "model": r.get("model") or "all",
                             "input": uncached, "output": r.get("output_tokens", 0),
                             "cache_w": written, "cache_r": cached,
                             "requests": r.get("num_model_requests", 0)})
    return rows


FETCHERS = {"anthropic": (anthropic_cost, anthropic_usage, claude_code_analytics), "enterprise": (enterprise,),
            "openai": (openai,)}


def configured() -> dict[str, bool]:
    return {name: bool(os.environ.get(var)) for name, var in KEYS.items()}


def fetch_all(start: date, end: date, get=http_get) -> tuple[list[dict], list[str]]:
    """Rows from every source whose key is set, plus one error line per source that failed."""
    rows, errors = [], []
    for name, fns in FETCHERS.items():
        key = os.environ.get(KEYS[name])
        if not key:
            continue
        for fn in fns:
            try:
                rows += fn(key, start, end, get)
            except LedgerError as e:
                errors.append(f"{fn.__name__}: {e}")
    return rows, errors
