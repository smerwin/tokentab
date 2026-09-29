# tokentab

What your Claude Code token spend bought, per merged PR. Local-first: it reads the transcripts
Claude Code already writes to `~/.claude/projects`, joins them to your GitHub PRs with the `gh`
CLI, and prints a table or writes a one-file HTML report. No hosted service, no API key.

**How this differs from [ccusage](https://github.com/ryoppippi/ccusage):** ccusage computes cost per
day, session, and model from the same files and stops at the session; tokentab joins sessions to
the pull requests they produced, so the unit is "cost per merged PR", and it writes a report a
non-engineer can read.

![tokentab report in the terminal](docs/terminal-report.svg)

![HTML report](docs/html-report.png)

## Run it

Needs Python 3.11+, [uv](https://docs.astral.sh/uv/), and an authenticated `gh`.

```bash
uv sync
```

```bash
uv run tokentab report --since 30d
```

```bash
uv run tokentab report --since 30d --html out.html
```

| command | what it does |
|---|---|
| `report [--since 30d] [--repo PATH] [--by pr\|session\|branch\|model\|week]` | spend table; footer shows total, unattributed, dead spend, median per merged PR, cache ratio |
| `report --html FILE` | self-contained HTML (inline CSS and SVG, no scripts, works offline) |
| `export --csv FILE` | one row per (session, PR) with every field, for finance |
| `doctor` | observed schema, models and their price rows, unpriced models, repos, `gh auth status` |

`--since` takes `12h`, `30d`, `2w`, or a date. `--no-db` skips the history database.

## What the numbers mean

**API-equivalent USD.** On a Claude subscription there is no per-token bill, so tokentab prices every
token at the published API rate: `input×p_in + output×p_out + cache_write_5m×p_in×1.25 +
cache_write_1h×p_in×2 + cache_read×p_in×cache_read_mult`, per million tokens. That is what the same
work would cost on the API, and the honest denominator for whether a seat pays for itself. Claude Code
writes both 5-minute and 1-hour cache entries, and the transcripts record which, so the two are priced
separately.

- **cache ratio**: cache reads ÷ (input + cache writes + cache reads).
- **output share**: output ÷ all tokens.
- **cost per PR**: sum of the spend attributed to that PR.
- **unattributed**: spend tied to no PR. Always shown as its own rows, never dropped.
- **dead spend**: spend on PRs closed without merging, plus work in a GitHub repo that never reached a PR.
  Work outside any repo (a scratch directory, Downloads) is unattributed but not dead.

## How attribution works

Each API message is attributed on its own, so a session that moves between branches or opens several
PRs is split by time.

1. **Repo** from the message's `cwd`. Worktrees collapse onto their main checkout, and deleted worktrees
   resolve through the nearest parent directory that still exists. Repos are keyed by the `origin` remote.
2. **Branch** from `gitBranch`. Subagent messages on a detached `HEAD` inherit the branch their parent
   session was on at that moment.
3. **PR by branch**: a PR whose `headRefName` is that branch and whose window
   `[createdAt − 24h, mergedAt or closedAt or now]` contains the message.
4. **PR by explicit link**, for everything else: Claude Code's own `pr-link` records in the transcript,
   and `Claude-Session:` trailers on squash-merged commits (matched to local sessions through the
   `bridge-session` records). A message goes to the next linked PR opened after it, within that PR's window.
5. Otherwise **repo-only** (repo known, no PR) or **none**.

The `confidence` column is the weakest link among a row's messages: `pr-link` and `trailer` (explicit
records) > `branch+window` > `repo-only` > `none`.

`Co-Authored-By: Claude` trailers are counted in `doctor` but not used to attribute spend: they don't
name a session, so tying one to a session would mean guessing between whatever sessions were running
at the time.

## Prices

`tokentab/prices.yaml` is keyed by model-ID substring (longest match wins). Rows marked `verify: true`
were filled from Anthropic's published price table rather than by you; tokentab warns when a report
uses them. A model with no matching row is priced at the nearest row and flagged `unpriced` in every
view. Override the file with `TOKENTAB_PRICES=/path/to/prices.yaml`.

## Files it writes

- `~/.tokentab/usage.db`: SQLite history of every message seen, unique on `(message.id, requestId)`.
  Claude Code deletes old transcripts (about 30 days by default), so this is what keeps older months reportable.
- `~/.tokentab/gh_cache.json`: `gh pr list` results, reused for an hour.

Nothing leaves your machine except the `gh` calls to GitHub.

## Known gaps

- `gh pr list --limit 500` misses older PRs in busy repos; tokentab warns when a repo hits the limit.
- Sessions whose transcripts were pruned before the first run are gone. `Claude-Session:` trailers
  still name them, but there are no tokens left to count.
- `tokentab/loaders/otel.py` describes the planned v1 sources: Claude Code OpenTelemetry, the Anthropic
  Admin API usage and cost reports, and OpenAI's organization usage endpoints.

## Develop

```bash
uv run pytest
```
