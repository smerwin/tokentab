"""A cost comment for one PR, posted and then kept up to date as a single comment."""
from __future__ import annotations

import json
import subprocess
from statistics import median

from . import report as rpt

MARKER = "<!-- tokentab:cost -->"


def render(key: tuple, evs: list[dict], prs: dict) -> str:
    """Markdown for PR `key` = (slug, number) from the events attributed to it."""
    mine = [e for e in evs if e["pr"] == key]
    t = rpt.totals(mine)
    siblings = [e for e in evs if e["pr"] and e["pr"][0] == key[0] and e["pr"] != key and prs[e["pr"]]["state"] == "merged"]
    others = [sum(e["usd"] for e in g) for g in rpt.group(siblings, lambda e: e["pr"]).values()]
    lines = [MARKER, "### Claude Code cost for this PR", ""]
    if not mine:
        lines += ["No Claude Code usage recorded for this PR on the machine that posted this comment."]
        return "\n".join(lines) + "\n"
    lines += [
        "| | |", "|---|---|",
        f"| API-equivalent spend | **${t['usd']:,.2f}** |",
        f"| Sessions | {t['sessions']} |",
        f"| Tokens | {rpt.human(t['tokens'])} ({t['cache_ratio']:.0%} of prompt tokens from cache) |",
        f"| Attribution | {rpt.weakest(mine)} |",
    ]
    if others:
        m = median(others)
        lines.append(f"| Median merged PR in this repo | ${m:,.2f} ({t['usd'] / m:.1f}× that) |" if m else
                     f"| Median merged PR in this repo | ${m:,.2f} |")
    lines += ["", "| Model | Tokens | Spend |", "|---|---:|---:|"]
    for model, g in sorted(rpt.group(mine, lambda e: e["model"]).items(), key=lambda kv: -rpt.totals(kv[1])["usd"]):
        mt = rpt.totals(g)
        flag = " (unpriced, estimated)" if "unpriced" in mt["flags"] else ""
        lines.append(f"| `{model}`{flag} | {rpt.human(mt['tokens'])} | ${mt['usd']:,.2f} |")
    lines += ["", "<sub>API-equivalent USD: every token priced at the published API rate, whatever plan paid for it. "
              "Counted by [tokentab](https://github.com/smerwin/tokentab) from the posting machine's local "
              "Claude Code transcripts; "
              "other contributors' usage is not included.</sub>"]
    return "\n".join(lines) + "\n"


def _gh(*args: str, stdin: str | None = None) -> str:
    r = subprocess.run(["gh", *args], input=stdin, capture_output=True, text=True, timeout=60)
    if r.returncode != 0:
        raise RuntimeError(f"gh {' '.join(args[:3])} failed: {r.stderr.strip()}")
    return r.stdout


def post(slug: str, number: int, body: str, gh=_gh) -> str:
    """Update this user's existing tokentab comment on the PR, or create one. Returns its URL."""
    me = gh("api", "user", "--jq", ".login").strip()
    comments = json.loads(gh("api", "--paginate", "--slurp", f"repos/{slug}/issues/{number}/comments"))
    mine = [c for page in comments for c in page
            if c.get("user", {}).get("login") == me and c.get("body", "").startswith(MARKER)]
    payload = json.dumps({"body": body})
    if mine:
        out = gh("api", "-X", "PATCH", f"repos/{slug}/issues/comments/{mine[-1]['id']}", "--input", "-", stdin=payload)
    else:
        out = gh("api", "-X", "POST", f"repos/{slug}/issues/{number}/comments", "--input", "-", stdin=payload)
    return json.loads(out)["html_url"]
