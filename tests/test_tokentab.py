import json
import shutil
from datetime import datetime, timezone
from pathlib import Path

import pytest
from click.testing import CliRunner
from rich.console import Console

from tokentab import db, report
from tokentab.attribute import Repo, attribute
from tokentab.loaders.claude_jsonl import TOKEN_FIELDS, load
from tokentab.pricing import lookup

FIX = Path(__file__).parent / "fixtures"
NOW = datetime(2026, 9, 10, tzinfo=timezone.utc)


def fake_resolve(cwd):
    return Repo("me/repo", "/tmp/repo", True) if cwd.startswith("/tmp/repo") else None


def run(root=FIX / "projects"):
    data = load(root)
    evs = list(data.events.values())
    prs, stats = attribute(
        evs, data.pr_links, data.bridge, now=NOW, resolve=fake_resolve,
        fetch_prs=lambda slug: json.loads((FIX / "gh_prs.json").read_text()),
        fetch_commits=lambda path: json.loads((FIX / "git_log.json").read_text()),
    )
    return data, {e["msg_id"]: e for e in evs}, prs


def sums(data):
    return {f: sum(e[f] for e in data.events.values()) for f in TOKEN_FIELDS}


def test_dedupe_keeps_final_streamed_counts_and_is_idempotent(tmp_path):
    data, ev, _ = run()
    assert ev["msg_a"]["output"] == 50 and ev["msg_a"]["cache_r"] == 5000
    assert len(data.events) == 9
    assert data.skipped["<synthetic> client-side error line"] == 1

    copy = tmp_path / "projects"
    shutil.copytree(FIX / "projects", copy)
    with (copy / "-tmp-repo" / "sess-1.jsonl").open("a") as fh:
        fh.write((FIX / "projects" / "-tmp-repo" / "sess-1.jsonl").read_text())
    assert sums(load(copy)) == sums(load(FIX / "projects"))

    first = sums(db.sync(load(FIX / "projects"), tmp_path / "usage.db"))
    second = sums(db.sync(load(FIX / "projects"), tmp_path / "usage.db"))
    assert first == second == sums(load(FIX / "projects"))


def test_sidechain_rolls_up_to_parent_session_and_branch():
    _, ev, _ = run()
    side = ev["msg_s"]
    assert side["sidechain"] and side["session_id"] == "sess-1"
    assert side["branch"] == "feat-a" and side["pr"] == ("me/repo", 1)


def test_session_spanning_branches_is_split_by_time():
    _, ev, _ = run()
    assert ev["msg_a"]["pr"] == ev["msg_b"]["pr"] == ("me/repo", 1)
    assert ev["msg_c"]["pr"] == ("me/repo", 2)
    assert ev["msg_c"]["confidence"] == "branch+window"
    assert ev["msg_f"]["pr"] is None and ev["msg_f"]["confidence"] == "repo-only"


def test_pr_link_and_trailer_attribute_detached_sessions():
    _, ev, _ = run()
    assert (ev["msg_p"]["pr"], ev["msg_p"]["confidence"]) == (("me/repo", 3), "pr-link")
    assert (ev["msg_t"]["pr"], ev["msg_t"]["confidence"]) == (("me/repo", 1), "trailer")
    assert (ev["msg_n"]["repo"], ev["msg_n"]["confidence"]) == (None, "none")


def test_dead_spend_is_closed_unmerged_plus_never_a_pr():
    _, ev, prs = run()
    s = report.summary(list(ev.values()), prs)
    assert s["dead_usd"] == pytest.approx(ev["msg_c"]["usd"] + ev["msg_f"]["usd"])
    assert s["unattributed_usd"] == pytest.approx(ev["msg_f"]["usd"] + ev["msg_n"]["usd"])
    assert s["merged_prs"] == 1


def test_unattributed_rows_are_reported_not_dropped():
    _, ev, prs = run()
    rows = report.pr_rows(list(ev.values()), prs)
    assert sum(r["usd"] for r in rows) == pytest.approx(sum(e["usd"] for e in ev.values()))
    assert {r["label"] for r in rows if r["state"] == "unattributed"} == {"(repo)", "(no repo)"}
    csv_rows = report.csv_rows(list(ev.values()), prs)
    assert sum(r["usd"] for r in csv_rows) == pytest.approx(sum(e["usd"] for e in ev.values()), abs=1e-3)


def test_unknown_model_is_priced_nearest_and_flagged():
    _, ev, prs = run()
    e = ev["msg_e"]
    assert e["price_flag"] == "unpriced" and e["usd"] > 0
    assert lookup("claude-opus-9")[0].startswith("claude-opus")
    console = Console(record=True, width=200)
    report.render(list(ev.values()), prs, "model", console)
    text = console.export_text()
    assert "claude-zeta-9" in text and "unpriced" in text


def test_cache_write_tiers_priced_separately():
    _, ev, _ = run()
    a, b = ev["msg_a"], ev["msg_b"]
    p = lookup("claude-opus-5-5")[1]
    extra = 400 * p["input"] * (p["cache_write_1h_mult"] - p["cache_write_mult"]) / 1e6
    assert a["usd"] - b["usd"] == pytest.approx(extra + (50 - 100) * p["output"] / 1e6)


def test_html_report_is_self_contained(tmp_path):
    from tokentab.html import write

    _, ev, prs = run()
    out = tmp_path / "r.html"
    write(list(ev.values()), prs, out)
    html = out.read_text()
    assert "<script" not in html and "<link" not in html and "src=" not in html
    assert "Feature A" in html and "<svg" in html


def test_cli_help_runs():
    from tokentab.cli import main

    result = CliRunner().invoke(main, ["--help"])
    assert result.exit_code == 0 and "report" in result.output
