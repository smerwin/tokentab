"""Join usage events to repos and PRs. Each event (one API message) is attributed on its
own, so a session that spans branches or PRs is split by time automatically."""
from __future__ import annotations

import json
import os
import re
import subprocess
import threading
import time
import urllib.error
import urllib.request
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from functools import cache
from pathlib import Path

from . import STATE
from .loaders.claude_jsonl import assign_branches, parse_ts
from .pricing import cost, lookup

GH_CACHE = STATE / "gh_cache.json"
GH_FIELDS = "number,title,headRefName,createdAt,mergedAt,closedAt"
RANK = {"pr-link": 3, "trailer": 3, "branch+window": 2, "repo-only": 1, "none": 0}
SESSION_RE = re.compile(r"^Claude-Session:\s*\S*session_(\w+)", re.M)
COAUTHOR_RE = re.compile(r"^Co-Authored-By:\s*Claude", re.M | re.I)
PR_REF_RE = re.compile(r"\(#(\d+)\)\s*$|^Merge pull request #(\d+)")


@dataclass(frozen=True)
class Repo:
    slug: str  # "owner/repo" for GitHub remotes, else the remote URL or local path
    path: str | None  # main checkout (worktrees collapse onto it); None if only known from a pr-link
    github: bool


def _run(*cmd: str) -> str | None:
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return r.stdout.strip() if r.returncode == 0 else None


@cache
def resolve_repo(cwd: str) -> Repo | None:
    p = Path(cwd)
    while not p.exists() and p != p.parent:  # deleted worktrees: fall back to an existing parent
        p = p.parent
    common = _run("git", "-C", str(p), "rev-parse", "--path-format=absolute", "--git-common-dir")
    if not common:
        return None
    main = str(Path(common).parent) if Path(common).name == ".git" else common
    url = _run("git", "-C", str(p), "remote", "get-url", "origin") or ""
    m = re.search(r"github\.com[:/]([^/]+/[^/]+?)(?:\.git)?/?$", url)
    return Repo(m.group(1) if m else (url or main), main, bool(m))


def _rest_prs(slug: str) -> list[dict] | None:
    """GitHub REST fallback for when `gh` is missing or signed out: uses GH_TOKEN or GITHUB_TOKEN
    if set, else anonymous access (public repos only). Newest 500 PRs, in `gh pr list` field names."""
    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    headers = {"Accept": "application/vnd.github+json", "User-Agent": "tokentab"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    prs: list[dict] = []
    for page in range(1, 6):
        url = f"https://api.github.com/repos/{slug}/pulls?state=all&per_page=100&page={page}"
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=30) as r:
                batch = json.loads(r.read())
        except (urllib.error.URLError, OSError, ValueError):
            return prs or None
        prs += [{"number": p["number"], "title": p["title"], "headRefName": p["head"]["ref"],
                 "createdAt": p["created_at"], "mergedAt": p["merged_at"], "closedAt": p["closed_at"]} for p in batch]
        if len(batch) < 100:
            break
    return prs


_cache_lock = threading.Lock()


def gh_prs(slug: str, cache_path: Path = GH_CACHE, ttl: int = 3600) -> list[dict] | None:
    """PRs for `slug`, cached for `ttl` seconds. Safe to call from several threads at once."""
    with _cache_lock:
        hit = (json.loads(cache_path.read_text()) if cache_path.exists() else {}).get(slug)
    if hit and time.time() - hit["fetched"] < ttl:
        return hit["prs"]
    out = _run("gh", "pr", "list", "-R", slug, "--state", "all", "--limit", "500", "--json", GH_FIELDS)
    prs = json.loads(out) if out is not None else _rest_prs(slug)
    if prs is None:
        return None
    with _cache_lock:
        store = json.loads(cache_path.read_text()) if cache_path.exists() else {}
        store[slug] = {"fetched": time.time(), "prs": prs}
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_text(json.dumps(store))
    return prs


def git_commits(path: str) -> list[dict]:
    out = _run("git", "-C", path, "log", "--all", "--format=%H%x09%aI%x09%s%x09%b%x1e") or ""
    commits = []
    for rec in out.split("\x1e"):
        parts = rec.strip("\n").split("\t", 3)
        if len(parts) == 4:
            commits.append(dict(zip(("sha", "date", "subject", "body"), parts)))
    return commits


def _pr_record(slug: str, p: dict, now: datetime) -> dict:
    end = p.get("mergedAt") or p.get("closedAt")
    return {
        **p,
        "repo": slug,
        "state": "merged" if p.get("mergedAt") else "closed" if p.get("closedAt") else "open",
        "created": parse_ts(p["createdAt"]),
        "start": parse_ts(p["createdAt"]) - timedelta(hours=24),
        "end": parse_ts(end) if end else now,
    }


def attribute(events, pr_links, bridge, now=None, resolve=resolve_repo, fetch_prs=gh_prs, fetch_commits=git_commits):
    """Annotate events in place with repo, github, pr, confidence, usd, price_flag. Returns (prs, stats)."""
    now = now or datetime.now(timezone.utc)
    stats = defaultdict(int)
    assign_branches(events)
    link_repos = defaultdict(set)
    for session, slug, _, _ in pr_links:
        link_repos[session].add(slug)

    repos: dict[str, Repo] = {}
    for ev in events:
        repo = resolve(ev["cwd"]) if ev["cwd"] else None
        if repo is None and ev.get("repo_hint"):
            repo = Repo(ev["repo_hint"], None, True)
        if repo is None and len(link_repos[ev["session_id"]]) == 1:
            repo = Repo(next(iter(link_repos[ev["session_id"]])), None, True)
        ev["repo"], ev["github"] = (repo.slug, repo.github) if repo else (None, False)
        if repo and (repo.slug not in repos or repos[repo.slug].path is None):
            repos[repo.slug] = repo
    for slug in {s for slugs in link_repos.values() for s in slugs}:
        repos.setdefault(slug, Repo(slug, None, True))

    prs: dict[tuple, dict] = {}
    github = [slug for slug, repo in repos.items() if repo.github]
    with ThreadPoolExecutor(max_workers=8) as pool:
        fetched = dict(zip(github, pool.map(fetch_prs, github)))
    for slug, repo in repos.items():
        data = fetched.get(slug)
        if repo.github and data is None:
            stats[f"no PR data for {slug}: gh unavailable and the GitHub API refused (install gh or set GH_TOKEN)"] += 1
        elif data is not None and len(data) >= 500:
            stats[f"{slug} has more than 500 PRs; older ones are matched only through Claude Code's own records"] += 1
        for p in data or []:
            prs[(slug, p["number"])] = _pr_record(slug, p, now)

    links: dict[str, dict] = defaultdict(dict)  # session -> {pr key: (anchor ts, source)}
    first_link: dict[tuple, str] = {}
    for _, slug, number, ts in pr_links:
        first_link[(slug, number)] = min(ts, first_link.get((slug, number), ts))
    for key, ts in first_link.items():
        if key not in prs:  # no GitHub data: keep the PR Claude Code recorded, without title or status
            prs[key] = {**_pr_record(key[0], {"number": key[1], "title": "", "headRefName": None, "createdAt": ts}, now),
                        "state": "unknown"}
            stats["PRs known only from Claude Code's records, without titles or merge status"] += 1
    for session, slug, number, ts in pr_links:
        key = (slug, number)
        old = links[session].get(key)
        links[session][key] = (min(old[0], parse_ts(ts)) if old else parse_ts(ts), "pr-link")
    for slug, repo in repos.items():
        for c in fetch_commits(repo.path) if repo.path else []:
            if COAUTHOR_RE.search(c["body"]):
                stats["claude co-authored commits"] += 1
            m = PR_REF_RE.search(c["subject"])
            if not m or (slug, int(m.group(1) or m.group(2))) not in prs:
                continue
            key = (slug, int(m.group(1) or m.group(2)))
            for cloud_id in SESSION_RE.findall(c["body"]):
                local = bridge.get(f"cse_{cloud_id}")
                if local:
                    links[local].setdefault(key, (prs[key]["created"], "trailer"))

    by_branch = defaultdict(list)
    for key, p in prs.items():
        by_branch[(p["repo"], p["headRefName"])].append(key)

    for ev in events:
        ev["usd"] = cost(ev)
        ev["price_flag"] = lookup(ev["model"])[2]
        s, ts, pick, conf = ev["session_id"], ev["ts"], None, None
        if ev["repo"] and ev["branch"]:
            cands = [k for k in by_branch.get((ev["repo"], ev["branch"]), []) if prs[k]["start"] <= ts <= prs[k]["end"]]
            if cands:
                pick = min(cands, key=lambda k: prs[k]["end"])
                conf = links[s][pick][1] if pick in links[s] else "branch+window"
        if pick is None and links.get(s):
            cands = [
                (anchor, key, src) for key, (anchor, src) in links[s].items()
                if prs[key]["start"] <= ts <= prs[key]["end"] and ev["repo"] in (None, key[0])
            ]
            after = [c for c in cands if c[0] >= ts]
            if cands:
                _, pick, conf = min(after) if after else max(cands)
        if pick is None:
            conf = "repo-only" if ev["repo"] else "none"
        else:
            ev["repo"], ev["github"] = pick[0], True
        ev["pr"], ev["confidence"] = pick, conf
    return prs, stats
