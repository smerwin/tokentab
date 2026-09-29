"""Load Claude Code transcripts from ~/.claude/projects. Field names: docs/SCHEMA.md."""
from __future__ import annotations

import bisect
import json
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

DEFAULT_ROOT = Path.home() / ".claude" / "projects"
TOKEN_FIELDS = ("input", "output", "cache_w", "cache_w_1h", "cache_r")


@dataclass
class Load:
    events: dict = field(default_factory=dict)  # (msg_id, request_id) -> event
    pr_links: set = field(default_factory=set)  # (session_id, "owner/repo", number, ts)
    bridge: dict = field(default_factory=dict)  # "cse_X" -> session_id
    line_types: Counter = field(default_factory=Counter)
    usage_keys: Counter = field(default_factory=Counter)
    versions: Counter = field(default_factory=Counter)
    skipped: Counter = field(default_factory=Counter)


def parse_ts(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def merge(events: dict, ev: dict) -> None:
    """Streaming repeats a message with growing counts: keep the per-field max so any
    order or number of re-reads yields the same totals."""
    key = (ev["msg_id"], ev["request_id"])
    old = events.get(key)
    if old is None:
        events[key] = ev
        return
    for f in TOKEN_FIELDS:
        old[f] = max(old[f], ev[f])
    if ev["ts"] < old["ts"]:
        old["ts"] = ev["ts"]


def _event(d: dict, path: Path) -> dict | None:
    m = d.get("message") or {}
    u = m.get("usage")
    if not u or not m.get("id"):
        return None
    cc = u.get("cache_creation") or {}
    session = d.get("sessionId") or d.get("session_id")
    if not session:
        session = path.parent.parent.name if path.parent.name == "subagents" else path.stem
    return {
        "msg_id": m["id"],
        "request_id": d.get("requestId") or "",
        "session_id": session,
        "ts": parse_ts(d["timestamp"]),
        "model": m.get("model") or "unknown",
        "cwd": d.get("cwd") or "",
        "git_branch": d.get("gitBranch") or "",
        "sidechain": bool(d.get("isSidechain")),
        "input": u.get("input_tokens") or 0,
        "output": u.get("output_tokens") or 0,
        "cache_w": u.get("cache_creation_input_tokens") or 0,
        "cache_w_1h": cc.get("ephemeral_1h_input_tokens") or 0,
        "cache_r": u.get("cache_read_input_tokens") or 0,
        "source": "transcript",
        "user": "",
        "reported_usd": None,
        "repo_hint": "",
    }


def load(root: Path = DEFAULT_ROOT) -> Load:
    out = Load()
    for path in sorted(root.glob("**/*.jsonl")):
        with path.open(errors="replace") as fh:
            for line in fh:
                try:
                    d = json.loads(line)
                except json.JSONDecodeError:
                    out.skipped["unparseable line"] += 1
                    continue
                t = d.get("type")
                out.line_types[t] += 1
                if t == "pr-link" and d.get("prNumber"):
                    out.pr_links.add((d["sessionId"], d["prRepository"], d["prNumber"], d["timestamp"]))
                elif t == "bridge-session" and d.get("bridgeSessionId"):
                    out.bridge[d["bridgeSessionId"]] = d["sessionId"]
                elif t == "assistant":
                    if (d.get("message") or {}).get("model") == "<synthetic>":
                        out.skipped["<synthetic> client-side error line"] += 1
                        continue
                    ev = _event(d, path)
                    if ev is None:
                        out.skipped["assistant line without usage"] += 1
                        continue
                    out.usage_keys.update(d["message"]["usage"].keys())
                    out.versions[d.get("version")] += 1
                    merge(out.events, ev)
    return out


def assign_branches(events: list[dict]) -> None:
    """Set ev["branch"]: gitBranch, or None when detached. Subagent (sidechain) lines on a
    detached HEAD inherit the branch the parent session was on at that moment."""
    main: dict[str, list] = {}
    for ev in sorted(events, key=lambda e: e["ts"]):
        if not ev["sidechain"] and ev["git_branch"] not in ("", "HEAD"):
            main.setdefault(ev["session_id"], []).append((ev["ts"], ev["git_branch"]))
    for ev in events:
        b = ev["git_branch"]
        if b in ("", "HEAD"):
            b = None
            timeline = main.get(ev["session_id"]) if ev["sidechain"] else None
            if timeline:
                i = bisect.bisect_right([t for t, _ in timeline], ev["ts"])
                b = timeline[i - 1][1] if i else None
        ev["branch"] = b
