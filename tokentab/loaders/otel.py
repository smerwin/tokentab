"""Claude Code OpenTelemetry: a stdlib OTLP/HTTP JSON receiver, and a loader that turns the
captured `claude_code.api_request` log events into usage events.

Claude Code exports with:
    CLAUDE_CODE_ENABLE_TELEMETRY=1 OTEL_LOGS_EXPORTER=otlp OTEL_EXPORTER_OTLP_PROTOCOL=http/json
    OTEL_EXPORTER_OTLP_ENDPOINT=http://127.0.0.1:4318
Only the http/json protocol is accepted (protobuf would need a new dependency).
"""
from __future__ import annotations

import gzip
import json
import os
import threading
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from ..pricing import lookup
from .claude_jsonl import merge

DEFAULT_DIR = Path(os.environ.get("TOKENTAB_OTEL_DIR", Path.home() / ".tokentab" / "otel"))
SIGNALS = {"/v1/logs": "logs", "/v1/metrics": "metrics", "/v1/traces": "traces"}


def _handler(out_dir: Path, token: str | None, lock: threading.Lock):
    class Handler(BaseHTTPRequestHandler):
        def _reply(self, code: int, msg: str = "{}") -> None:
            data = msg.encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_POST(self):
            signal = SIGNALS.get(self.path.split("?")[0])
            if signal is None:
                return self._reply(404, json.dumps({"message": f"unknown path {self.path}"}))
            if token and self.headers.get("Authorization") != f"Bearer {token}":
                return self._reply(401, json.dumps({"message": "missing or wrong bearer token"}))
            if "json" not in (self.headers.get("Content-Type") or ""):
                return self._reply(415, json.dumps({"message": "only OTLP http/json is supported; "
                                                               "set OTEL_EXPORTER_OTLP_PROTOCOL=http/json"}))
            length = self.headers.get("Content-Length")
            if length is None:
                return self._reply(411, json.dumps({"message": "Content-Length required"}))
            raw = self.rfile.read(int(length))
            if (self.headers.get("Content-Encoding") or "").lower() == "gzip":
                raw = gzip.decompress(raw)
            try:
                payload = json.loads(raw)
            except ValueError:
                return self._reply(400, json.dumps({"message": "body is not JSON"}))
            now = datetime.now(timezone.utc)
            line = json.dumps({"signal": signal, "received": now.isoformat(), "payload": payload})
            with lock, (out_dir / f"otlp-{now:%Y-%m-%d}.jsonl").open("a") as fh:
                fh.write(line + "\n")
            self._reply(200)

        def log_message(self, *args):
            pass

    return Handler


def serve(host: str = "127.0.0.1", port: int = 4318, out_dir: Path = DEFAULT_DIR, token: str | None = None):
    out_dir.mkdir(parents=True, exist_ok=True)
    server = ThreadingHTTPServer((host, port), _handler(out_dir, token, threading.Lock()))
    return server


def _value(v: dict):
    for kind in ("stringValue", "boolValue", "doubleValue"):
        if kind in v:
            return v[kind]
    if "intValue" in v:
        return int(v["intValue"])  # OTLP JSON encodes int64 as a string; some exporters send a number
    return None


def _attrs(items: list | None) -> dict:
    return {a["key"]: _value(a.get("value") or {}) for a in items or []}


def _iter_payloads(root: Path):
    """Yield (signal, payload) from tokentab captures and from raw OTLP JSON lines (collector file exporter)."""
    for path in sorted(Path(root).glob("*.jsonl")) if Path(root).is_dir() else [Path(root)]:
        with path.open(errors="replace") as fh:
            for line in fh:
                try:
                    d = json.loads(line)
                except ValueError:
                    continue
                if "payload" in d:
                    yield d.get("signal"), d["payload"]
                elif "resourceLogs" in d:
                    yield "logs", d
                elif "resourceMetrics" in d:
                    yield "metrics", d


def _ts(nanos, fallback: str | None = None) -> datetime:
    if fallback:
        return datetime.fromisoformat(fallback.replace("Z", "+00:00"))
    return datetime.fromtimestamp(int(nanos) / 1e9, tz=timezone.utc)


def _user(a: dict) -> str:
    return a.get("user.email") or a.get("user.account_uuid") or a.get("user.id") or ""


def _base(session: str, model: str, ts: datetime, a: dict) -> dict:
    return {"session_id": session, "ts": ts, "model": model or "unknown", "cwd": "", "git_branch": "",
            "sidechain": False, "source": "otel", "user": _user(a), "repo_hint": "", "cache_w_1h": 0}


def load(root: Path = DEFAULT_DIR) -> tuple[list[dict], dict]:
    """Return (events, session -> "owner/repo") from captured OTLP data.

    Per-request `claude_code.api_request` log events are the primary source. Token metrics
    are used only for sessions that have no api_request events, so the two never add up.
    """
    if not Path(root).exists():
        return [], {}
    events, series, repos = [], {}, {}
    for signal, payload in _iter_payloads(root):
        if signal == "logs":
            for rl in payload.get("resourceLogs", []):
                for sl in rl.get("scopeLogs", []):
                    for rec in sl.get("logRecords", []):
                        a = _attrs(rec.get("attributes"))
                        if a.get("event.name") not in ("api_request", "claude_code.api_request"):
                            continue
                        session = a.get("session.id") or ""
                        rid = a.get("request_id") or ""
                        key = rid or a.get("client_request_id") or f"{session}:{a.get('event.sequence')}"
                        ev = _base(session, a.get("model"), _ts(rec.get("timeUnixNano"), a.get("event.timestamp")), a)
                        ev.update(msg_id=f"otel:{key}", request_id=rid, input=a.get("input_tokens") or 0,
                                  output=a.get("output_tokens") or 0, cache_w=a.get("cache_creation_tokens") or 0,
                                  cache_r=a.get("cache_read_tokens") or 0, reported_usd=a.get("cost_usd"))
                        events.append(ev)
        elif signal == "metrics":
            for rm in payload.get("resourceMetrics", []):
                for sm in rm.get("scopeMetrics", []):
                    for m in sm.get("metrics", []):
                        if m.get("name") not in ("claude_code.token.usage", "claude_code.cost.usage"):
                            continue
                        agg = m.get("sum") or {}
                        cumulative = agg.get("aggregationTemporality") == 2
                        for dp in agg.get("dataPoints", []):
                            a = _attrs(dp.get("attributes"))
                            session = a.get("session.id") or ""
                            if a.get("vcs.provider.name") == "github" and a.get("vcs.owner.name") and a.get("vcs.repository.name"):
                                repos[session] = f"{a['vcs.owner.name']}/{a['vcs.repository.name']}"
                            field = "usd" if m["name"] == "claude_code.cost.usage" else a.get("type")
                            val = dp.get("asDouble", dp.get("asInt", 0))
                            val = float(val) if field == "usd" else int(float(val))
                            start, end = int(dp.get("startTimeUnixNano", 0)), int(dp.get("timeUnixNano", 0))
                            ident = tuple(sorted((key, str(v)) for key, v in a.items() if key != "type"))
                            k = (session, a.get("model"), field, start, None if cumulative else end, ident)
                            old = series.get(k)
                            if old is None or end >= old[0]:  # cumulative: keep the latest total per series
                                series[k] = (end, val, a)
    have_events = {e["session_id"] for e in events}
    buckets: dict[tuple, dict] = {}
    for (session, model, field, start, end, _), (last, val, a) in series.items():
        if session in have_events:
            continue
        b = buckets.setdefault((session, model, start, end), {
            **_base(session, model, _ts(last), a), "msg_id": f"otelm:{session}:{model}:{start}:{end}",
            "request_id": "", "input": 0, "output": 0, "cache_w": 0, "cache_r": 0, "reported_usd": 0.0})
        slot = {"input": "input", "output": "output", "cacheCreation": "cache_w", "cacheRead": "cache_r", "usd": "reported_usd"}.get(field)
        if slot:
            b[slot] += val
    unique: dict = {}
    for ev in events + list(buckets.values()):
        merge(unique, ev)  # an exporter retry can deliver the same batch twice
    for ev in unique.values():
        split_ttl(ev)
    return list(unique.values()), repos


def split_ttl(ev: dict) -> str:
    """OTel reports cache writes as one number. Set ev["cache_w_1h"] to the 1-hour share that
    reproduces Claude Code's own cost_usd under prices.yaml; if none does, assume all 1-hour
    (Claude Code's main-thread default). Returns "derived" or "assumed"."""
    _, p, flag = lookup(ev["model"])
    cw, reported = ev["cache_w"], ev.get("reported_usd")
    ev["cache_w_1h"] = cw
    if not cw or reported is None or flag == "unpriced":
        return "derived" if not cw else "assumed"
    m5, m1h = p["cache_write_mult"], p.get("cache_write_1h_mult", p["cache_write_mult"])
    base = (ev["input"] * p["input"] + ev["output"] * p["output"] + ev["cache_r"] * p["input"] * p["cache_read_mult"]
            + cw * p["input"] * m5) / 1e6
    per_token = p["input"] * (m1h - m5) / 1e6
    if per_token <= 0:
        return "assumed"
    x = (reported - base) / per_token
    if -0.5 <= x <= cw + 0.5 and abs(x - round(x)) * per_token < 1e-6 + 1e-3 * reported:
        ev["cache_w_1h"] = min(max(round(x), 0), cw)
        return "derived"
    return "assumed"


def reconcile(events: list[dict], repos: dict) -> tuple[list[dict], dict]:
    """Merge OTel events into transcript events without counting any request twice.

    A request already in a transcript (same request_id) is dropped; metric-derived buckets are
    dropped for any session that has transcripts. Kept OTel events borrow cwd and branch from
    their session's transcript when one exists, else carry the repo from `vcs.*` attributes.
    """
    transcript = [e for e in events if e.get("source", "transcript") == "transcript"]
    otel = [e for e in events if e.get("source") == "otel"]
    have_req = {e["request_id"] for e in transcript if e["request_id"]}
    by_session: dict[str, list] = {}
    for e in transcript:
        by_session.setdefault(e["session_id"], []).append(e)
    for evs in by_session.values():
        evs.sort(key=lambda e: e["ts"])
    kept, stats = [], {"otel duplicates of transcript requests": 0, "otel cache-write TTL assumed 1h": 0}
    for ev in otel:
        if (ev["request_id"] and ev["request_id"] in have_req) or (
                ev["msg_id"].startswith("otelm:") and ev["session_id"] in by_session):
            stats["otel duplicates of transcript requests"] += 1
            continue
        if split_ttl(ev) == "assumed":
            stats["otel cache-write TTL assumed 1h"] += 1
        same = by_session.get(ev["session_id"])
        if same:
            near = min(same, key=lambda t: abs((t["ts"] - ev["ts"]).total_seconds()))
            ev["cwd"], ev["git_branch"], ev["user"] = near["cwd"], near["git_branch"], ev["user"] or near["user"]
        else:
            ev["repo_hint"] = repos.get(ev["session_id"], "")
        kept.append(ev)
    return transcript + kept, {k: v for k, v in stats.items() if v}
