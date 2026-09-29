import json
import sqlite3
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from tokentab import comment, db, ledgers, report
from tokentab.attribute import Repo, attribute
from tokentab.loaders import otel, stats_cache
from tokentab.loaders.claude_jsonl import load
from tokentab.pricing import cost

from test_tokentab import FIX, NOW, fake_resolve


def attr(key, value):
    if isinstance(value, bool):
        v = {"boolValue": value}
    elif isinstance(value, int):
        v = {"intValue": str(value)}
    elif isinstance(value, float):
        v = {"doubleValue": value}
    else:
        v = {"stringValue": value}
    return {"key": key, "value": v}


def api_request(session, request_id, model="claude-haiku-4-5-20251001", ts="2026-09-01T12:00:00.000Z", **tok):
    a = {"event.name": "api_request", "event.timestamp": ts, "session.id": session, "request_id": request_id,
         "user.email": "dev@example.com", "model": model, "input_tokens": tok.get("inp", 10),
         "output_tokens": tok.get("out", 41), "cache_read_tokens": tok.get("cr", 13689),
         "cache_creation_tokens": tok.get("cw", 11918), "cost_usd": tok.get("usd", 0.0254199)}
    return {"timeUnixNano": "1788264000000000000", "body": {"stringValue": "claude_code.api_request"},
            "attributes": [attr(k, v) for k, v in a.items()]}


def logs(*records):
    return {"resourceLogs": [{"resource": {"attributes": [attr("service.name", "claude-code")]},
                              "scopeLogs": [{"logRecords": list(records)}]}]}


def metric_point(session, typ, value, start, end, extra=()):
    a = [attr("session.id", session), attr("model", "claude-haiku-4-5"), attr("query_source", "main"), *extra]
    if typ != "usd":
        a.append(attr("type", typ))
    key = "asDouble" if isinstance(value, float) else "asInt"
    return {"attributes": a, "startTimeUnixNano": str(start), "timeUnixNano": str(end), key: value}


def metrics(points_by_name, temporality=1):
    return {"resourceMetrics": [{"scopeMetrics": [{"metrics": [
        {"name": name, "sum": {"aggregationTemporality": temporality, "isMonotonic": True, "dataPoints": pts}}
        for name, pts in points_by_name.items()]}]}]}


def write(path, *payloads, wrap=True):
    with path.open("w") as fh:
        for sig, p in payloads:
            fh.write(json.dumps({"signal": sig, "received": "x", "payload": p} if wrap else p) + "\n")
    return path


def test_otel_request_priced_like_claude_code_and_ttl_derived(tmp_path):
    write(tmp_path / "a.jsonl", ("logs", logs(api_request("s-otel", "req_zz"))))
    evs, _ = otel.load(tmp_path)
    (e,) = evs
    assert (e["input"], e["output"], e["cache_w"], e["cache_r"]) == (10, 41, 11918, 13689)
    assert e["cache_w_1h"] == 11918 and cost(e) == pytest.approx(0.0254199, abs=1e-9)


def test_otel_ttl_split_recovers_mixed_writes(tmp_path):
    # 1000 cache-write tokens, 400 of them 1h, on claude-haiku-4-5 ($1/M input)
    usd = (10 * 1 + 41 * 5 + 13689 * 0.1 + 600 * 1.25 + 400 * 2.0) / 1e6
    write(tmp_path / "a.jsonl", ("logs", logs(api_request("s", "req_1", cw=1000, usd=usd))))
    (e,), _ = otel.load(tmp_path)
    assert e["cache_w_1h"] == 400


def test_otel_duplicate_of_transcript_is_dropped_and_retries_collapse(tmp_path):
    record = api_request("sess-1", "req_a", model="claude-opus-5-5")
    write(tmp_path / "a.jsonl", ("logs", logs(record)), ("logs", logs(record)), ("logs", logs(api_request("s2", "req_new"))))
    otel_evs, repos = otel.load(tmp_path)
    assert len(otel_evs) == 2
    transcript = list(load(FIX / "projects").events.values())
    merged, stats = otel.reconcile(transcript + otel_evs, repos)
    assert stats["otel duplicates of transcript requests"] == 1
    assert [e["request_id"] for e in merged if e["source"] == "otel"] == ["req_new"]


def test_otel_only_session_gets_repo_from_vcs_metrics(tmp_path):
    vcs = (attr("vcs.provider.name", "github"), attr("vcs.owner.name", "me"), attr("vcs.repository.name", "repo"))
    write(tmp_path / "a.jsonl", ("logs", logs(api_request("s-far", "req_far", model="claude-opus-5-5"))),
          ("metrics", metrics({"claude_code.token.usage": [metric_point("s-far", "input", 10, 1, 2, vcs)]})))
    otel_evs, repos = otel.load(tmp_path)
    assert repos == {"s-far": "me/repo"}
    merged, _ = otel.reconcile(otel_evs, repos)
    prs, _ = attribute(merged, set(), {}, now=NOW, resolve=fake_resolve,
                       fetch_prs=lambda s: json.loads((FIX / "gh_prs.json").read_text()), fetch_commits=lambda p: [])
    (e,) = merged
    assert (e["repo"], e["confidence"], e["pr"]) == ("me/repo", "repo-only", None)


def test_otel_metrics_fallback_delta_and_cumulative(tmp_path):
    delta = metrics({"claude_code.token.usage": [
        metric_point("m1", "input", 5, 0, 10), metric_point("m1", "input", 7, 10, 20),
        metric_point("m1", "output", 3, 0, 10, (attr("query_source", "sdk"),)),
        metric_point("m1", "output", 4, 0, 10)]})
    cumulative = metrics({"claude_code.token.usage": [
        metric_point("m2", "cacheRead", 100, 0, 10), metric_point("m2", "cacheRead", 250, 0, 20)]}, temporality=2)
    write(tmp_path / "a.jsonl", ("metrics", delta), ("metrics", cumulative))
    evs, _ = otel.load(tmp_path)
    tot = lambda s, f: sum(e[f] for e in evs if e["session_id"] == s)
    assert tot("m1", "input") == 12 and tot("m1", "output") == 7
    assert tot("m2", "cache_r") == 250


def test_otel_metrics_ignored_when_session_has_log_events(tmp_path):
    write(tmp_path / "a.jsonl", ("logs", logs(api_request("s", "req_1"))),
          ("metrics", metrics({"claude_code.token.usage": [metric_point("s", "input", 10, 0, 1)]})))
    evs, _ = otel.load(tmp_path)
    assert [e["msg_id"] for e in evs] == ["otel:req_1"]


def test_otel_reads_raw_collector_file_export(tmp_path):
    write(tmp_path / "raw.jsonl", ("logs", logs(api_request("s", "req_raw"))), wrap=False)
    evs, _ = otel.load(tmp_path)
    assert evs[0]["request_id"] == "req_raw"


def test_receiver_rejects_protobuf_and_writes_json(tmp_path):
    import threading
    import urllib.request
    server = otel.serve(port=0, out_dir=tmp_path, token="t")
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{server.server_address[1]}/v1/logs"

    def post(ctype, body, auth="Bearer t"):
        req = urllib.request.Request(url, data=body, headers={"Content-Type": ctype, "Authorization": auth})
        try:
            return urllib.request.urlopen(req).status
        except urllib.error.HTTPError as e:
            return e.code

    assert post("application/x-protobuf", b"x") == 415
    assert post("application/json", b"{}", auth="Bearer wrong") == 401
    assert post("application/json", json.dumps(logs(api_request("s", "r"))).encode()) == 200
    server.shutdown()
    assert otel.load(tmp_path)[0][0]["request_id"] == "r"


def test_db_migrates_old_schema_and_keeps_otel_rows(tmp_path):
    path = tmp_path / "usage.db"
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE events (msg_id, request_id, session_id, ts, model, cwd, git_branch, sidechain, "
                "input, output, cache_w, cache_w_1h, cache_r)")
    con.execute("INSERT INTO events VALUES ('old', 'r0', 's0', '2026-08-01T00:00:00+00:00', 'claude-opus-5-5', '', '', 0, 1, 2, 3, 0, 4)")
    con.commit()
    con.close()
    write(tmp_path / "o.jsonl", ("logs", logs(api_request("s", "req_1"))))
    otel_evs, _ = otel.load(tmp_path)
    data = db.sync(load(FIX / "projects"), path, extra=otel_evs)
    data = db.sync(load(FIX / "projects"), path, extra=otel_evs)
    events = data.events
    assert events[("old", "r0")]["source"] == "transcript"
    assert events[("otel:req_1", "req_1")]["source"] == "otel"
    assert len(events) == 9 + 2


def test_stats_cache_upper_bound_skips_covered_days(tmp_path):
    path = tmp_path / "stats.json"
    path.write_text(json.dumps({
        "dailyModelTokens": [{"date": "2026-02-01", "tokensByModel": {"claude-opus-4-6": 30}},
                             {"date": "2026-02-02", "tokensByModel": {"claude-opus-4-6": 10}}],
        "modelUsage": {"claude-opus-4-6": {"inputTokens": 10, "outputTokens": 30, "cacheReadInputTokens": 400,
                                           "cacheCreationInputTokens": 40}}}))
    ub = stats_cache.upper_bound({(date(2026, 2, 2), "claude-opus-4-6")}, path)
    m = ub["models"]["claude-opus-4-6"]
    assert ub["days"] == [date(2026, 2, 1)]
    assert (m["input"], m["output"], m["cache_r"]) == (7.5, 22.5, 300.0)
    assert stats_cache.upper_bound(set(), tmp_path / "missing.json") is None


# --- vendor ledgers, fed the documented example responses ---------------------------------

def fake_get(routes):
    calls = []

    def get(url, headers):
        calls.append(url)
        for path, pages in routes.items():
            if path in url:
                if callable(pages):
                    return pages(url)
                idx = 1 if "page=" in url else 0
                return pages[idx]
        raise AssertionError(url)
    get.calls = calls
    return get


def test_anthropic_cost_report_amounts_are_cents_and_paginate():
    page1 = {"data": [{"starting_at": "2025-08-01T00:00:00Z", "ending_at": "2025-08-02T00:00:00Z", "results": [
        {"amount": "123.78912", "currency": "USD", "model": "claude-opus-5", "description": "x"}]}],
        "has_more": True, "next_page": "page_2"}
    page2 = {"data": [{"starting_at": "2025-08-02T00:00:00Z", "results": [{"amount": "50", "currency": "USD", "model": None,
                                                                            "description": "Code Execution Usage"}]}],
             "has_more": False, "next_page": None}
    get = fake_get({"/cost_report": [page1, page2]})
    rows = ledgers.anthropic_cost("k", date(2025, 8, 1), date(2025, 8, 3), get)
    assert [r["usd"] for r in rows] == [Decimal("1.2378912"), Decimal("0.5")]
    assert rows[1]["model"] == "Code Execution Usage"
    assert "group_by%5B%5D=description" in get.calls[0] and "page=page_2" in get.calls[1]


def test_expired_cursor_restarts_without_duplicates():
    state = {"n": 0}

    def pages(url):
        state["n"] += 1
        if state["n"] == 2:
            raise ledgers.LedgerError("HTTP 410 from /x: cursor expired")
        if "page=" in url:
            return {"data": [{"starting_at": "2025-08-02T00:00:00Z", "results": [{"amount": "100"}]}], "has_more": False}
        return {"data": [{"starting_at": "2025-08-01T00:00:00Z", "results": [{"amount": "100"}]}],
                "has_more": True, "next_page": "p2"}
    rows = ledgers.anthropic_cost("k", date(2025, 8, 1), date(2025, 8, 3), fake_get({"/cost_report": pages}))
    assert len(rows) == 2


def test_anthropic_usage_report_sums_both_cache_ttls():
    page = {"data": [{"starting_at": "2025-08-01T00:00:00Z", "results": [
        {"uncached_input_tokens": 1500, "output_tokens": 500, "cache_read_input_tokens": 200, "model": "claude-opus-5",
         "cache_creation": {"ephemeral_1h_input_tokens": 7, "ephemeral_5m_input_tokens": 3}}]}], "has_more": False}
    (r,) = ledgers.anthropic_usage("k", date(2025, 8, 1), date(2025, 8, 2), fake_get({"/usage_report/messages": [page]}))
    assert (r["input"], r["output"], r["cache_w"], r["cache_r"]) == (1500, 500, 10, 200)


def test_claude_code_analytics_cost_in_cents_one_request_per_day():
    page = {"data": [{"date": "2025-09-08T00:00:00Z", "actor": {"type": "user_actor", "email_address": "dev@company.com"},
                      "core_metrics": {"num_sessions": 5, "commits_by_claude_code": 12, "pull_requests_by_claude_code": 2},
                      "model_breakdown": [{"model": "claude-opus-5-5", "estimated_cost": {"currency": "USD", "amount": 113}}]}],
            "has_more": False, "next_page": None}
    get = fake_get({"/usage_report/claude_code": [page]})
    rows = ledgers.claude_code_analytics("k", date(2025, 9, 8), date(2025, 9, 10), get)
    assert len(get.calls) == 2 and "starting_at=2025-09-08" in get.calls[0]
    assert rows[0]["usd"] == Decimal("1.13") and rows[0]["prs"] == 2 and rows[0]["who"] == "dev@company.com"


def test_enterprise_joins_cost_and_activity_per_user(monkeypatch):
    cost_page = {"data": [{"actor": {"type": "user_actor", "user_id": "user_1", "email": "jane@example.com", "deleted": False},
                           "amount": "41280.000000", "currency": "USD"}], "has_more": False, "next_page": None}
    users_page = {"data": [{"user": {"id": "user_1", "email_address": "jane@example.com", "type": "user"},
                            "claude_code_metrics": {"core_metrics": {"pull_request_count": 4, "commit_count": 9,
                                                                     "distinct_session_count": 6}}}], "next_page": None}
    get = fake_get({"/analytics/user_cost_report": [cost_page], "/analytics/users": [users_page]})
    (r,) = ledgers.enterprise("k", date(2026, 9, 1), date(2026, 9, 20), get)
    assert (r["who"], r["usd"], r["prs"], r["commits"], r["sessions"]) == ("jane@example.com", Decimal("412.8"), 4, 9, 6)
    assert any("products%5B%5D=claude_code" in c for c in get.calls)


def test_openai_uncached_input_excludes_cached_and_cache_writes():
    usage = {"object": "page", "data": [{"start_time": 1730419200, "end_time": 1730505600, "results": [
        {"input_tokens": 1000, "input_cached_tokens": 400, "input_cache_write_tokens": 100, "output_tokens": 500,
         "num_model_requests": 5, "model": "gpt-x"}]}], "has_more": False, "next_page": None}
    costs = {"object": "page", "data": [{"start_time": 1730419200, "end_time": 1730505600, "results": [
        {"amount": {"value": 0.06, "currency": "usd"}, "line_item": "gpt-x, input"}]}], "has_more": False, "next_page": None}
    rows = ledgers.openai("k", date(2024, 11, 1), date(2024, 11, 2),
                          fake_get({"/organization/costs": [costs], "/usage/completions": [usage]}))
    use = next(r for r in rows if r["source"] == "openai usage")
    assert (use["input"], use["cache_r"], use["cache_w"]) == (500, 400, 100)
    assert next(r for r in rows if r["source"] == "openai costs")["usd"] == Decimal("0.06")


def test_fetch_all_skips_unset_keys_and_reports_errors(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_ADMIN_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_ANALYTICS_KEY", raising=False)
    monkeypatch.setenv("OPENAI_ADMIN_KEY", "k")

    def boom(url, headers):
        assert url.startswith("https://api.openai.com/") and headers["Authorization"] == "Bearer k"
        raise ledgers.LedgerError("HTTP 403 from /v1/organization/costs: nope")
    rows, errors = ledgers.fetch_all(date(2026, 9, 1), date(2026, 9, 2), boom)
    assert rows == [] and errors == ["openai: HTTP 403 from /v1/organization/costs: nope"]


# --- PR comment --------------------------------------------------------------------------

def attributed():
    from test_tokentab import run
    _, ev, prs = run()
    return list(ev.values()), prs


def test_comment_renders_cost_and_marker():
    evs, prs = attributed()
    body = comment.render(("me/repo", 1), evs, prs)
    usd = sum(e["usd"] for e in evs if e["pr"] == ("me/repo", 1))
    assert body.startswith(comment.MARKER) and f"${usd:,.2f}" in body
    assert "claude-zeta-9` (unpriced, estimated)" in body


def test_comment_post_updates_existing_else_creates():
    calls = []

    def gh(*args, stdin=None):
        calls.append((args, stdin))
        if args[:2] == ("api", "user"):
            return "me\n"
        if "--paginate" in args:
            return json.dumps([[{"id": 7, "user": {"login": "me"}, "body": comment.MARKER + "\nold"},
                                {"id": 8, "user": {"login": "someone"}, "body": comment.MARKER}]])
        return json.dumps({"html_url": "https://example.test/c/7"})
    assert comment.post("me/repo", 1, "new", gh) == "https://example.test/c/7"
    assert calls[-1][0][:4] == ("api", "-X", "PATCH", "repos/me/repo/issues/comments/7")
    assert json.loads(calls[-1][1]) == {"body": "new"}

    calls.clear()

    def gh_empty(*args, stdin=None):
        calls.append((args, stdin))
        if args[:2] == ("api", "user"):
            return "me\n"
        if "--paginate" in args:
            return "[[]]"
        return json.dumps({"html_url": "u"})
    comment.post("me/repo", 1, "new", gh_empty)
    assert calls[-1][0][:4] == ("api", "-X", "POST", "repos/me/repo/issues/1/comments")


def test_hook_ignores_other_commands_and_never_fails(monkeypatch):
    from click.testing import CliRunner

    from tokentab import cli
    seen = []
    monkeypatch.setattr(cli, "_comment", lambda number, repo, post, no_db: seen.append((repo, post)) or "posted u")
    run = lambda payload: CliRunner().invoke(cli.main, ["hook"], input=payload)
    assert run('{"tool_name": "Bash", "tool_input": {"command": "git status"}, "cwd": "/r"}').exit_code == 0
    assert run("not json").exit_code == 0
    assert run('{"tool_name": "Edit", "tool_input": {"command": "git push"}}').exit_code == 0
    assert seen == []
    assert run('{"tool_name": "Bash", "tool_input": {"command": "cd x && gh  pr create --fill"}, "cwd": "/r"}').exit_code == 0
    assert run('{"tool_name": "Bash", "tool_input": {"command": "git push -u origin feat"}, "cwd": "/r"}').exit_code == 0
    assert seen == [("/r", True), ("/r", True)]

    def broken(*a):
        raise cli.click.UsageError("no PR for the current branch; pass --pr")
    monkeypatch.setattr(cli, "_comment", broken)
    result = run('{"tool_name": "Bash", "tool_input": {"command": "git push"}, "cwd": "/r"}')
    assert result.exit_code == 0 and "no PR for the current branch" in result.output


def test_no_github_access_still_attributes_to_recorded_prs():
    data = load(FIX / "projects")
    evs = list(data.events.values())
    prs, stats = attribute(evs, data.pr_links, data.bridge, now=NOW, resolve=fake_resolve,
                           fetch_prs=lambda slug: None, fetch_commits=lambda path: [])
    ev = {e["msg_id"]: e for e in evs}
    assert (ev["msg_p"]["pr"], ev["msg_p"]["confidence"]) == (("me/repo", 3), "pr-link")
    assert prs[("me/repo", 3)]["state"] == "unknown"
    assert ev["msg_a"]["confidence"] == "repo-only"  # branch matching needs GitHub's head branch names
    assert any("no PR data" in k for k in stats) and any("known only" in k for k in stats)
    rows = report.pr_rows(evs, prs)
    assert next(r for r in rows if r["label"] == "repo#3")["merged"] == "?"


def test_rest_fallback_maps_github_pulls(monkeypatch):
    import io

    from tokentab import attribute as A
    payload = [{"number": 9, "title": "T", "head": {"ref": "feat"}, "created_at": "2026-09-01T00:00:00Z",
                "merged_at": None, "closed_at": None}]
    seen = []

    def urlopen(req, timeout):
        seen.append(req)
        return io.BytesIO(json.dumps(payload).encode())
    monkeypatch.setattr(A.urllib.request, "urlopen", urlopen)
    monkeypatch.setenv("GH_TOKEN", "tok")
    assert A._rest_prs("me/repo") == [{"number": 9, "title": "T", "headRefName": "feat",
                                        "createdAt": "2026-09-01T00:00:00Z", "mergedAt": None, "closedAt": None}]
    assert seen[0].full_url.startswith("https://api.github.com/repos/me/repo/pulls?state=all")
    assert seen[0].get_header("Authorization") == "Bearer tok"
