# tokentab

What your Claude Code token spend bought, per merged PR. Local-first: it reads the transcripts
Claude Code already writes to `~/.claude/projects`, matches them to your GitHub pull requests, and
opens a one-page report in your browser. No hosted service, no API key, nothing uploaded.

**How this differs from [ccusage](https://github.com/ryoppippi/ccusage):** ccusage computes cost per
day, session, and model from the same files and stops at the session; tokentab joins sessions to
the pull requests they produced, so the unit is "cost per merged PR", and it writes a report a
non-engineer can read.

![tokentab report in the terminal](https://raw.githubusercontent.com/smerwin/tokentab/main/docs/demo-terminal.svg)

![HTML report](https://raw.githubusercontent.com/smerwin/tokentab/main/docs/demo-report.png)

<sub>Screenshots use synthetic data for an imaginary repo; regenerate them with [`scripts/demo.py`](https://github.com/smerwin/tokentab/blob/main/scripts/demo.py).</sub>

## Run it

With [uv](https://docs.astral.sh/uv/) installed:

```bash
uvx tokentab
```

That prints a summary of the last 30 days and opens the full report in your browser (also saved to
`~/.tokentab/report.html`). `uvx tokentab --since 90d` covers a longer period, and `--no-open` skips the
browser. No uv? `pipx run tokentab` does the same.

GitHub data comes from the first of these that works, per repo:

1. the [`gh` CLI](https://cli.github.com/), if installed and signed in (sees private repos);
2. the GitHub API with `GH_TOKEN` or `GITHUB_TOKEN`, if set;
3. the GitHub API without a token (public repos only);
4. none: spend still lands on the PRs Claude Code itself recorded opening, just without titles or
   merge status, and the report says so.

For more detail, the same tool has subcommands:

| command | what it does |
|---|---|
| `report [--since 30d] [--repo PATH] [--by pr\|session\|branch\|model\|week\|user]` | spend table; footer shows total, unattributed, dead spend, median per merged PR, cache ratio |
| `report --html FILE` | self-contained HTML (inline CSS and SVG, no scripts, works offline) |
| `export --csv FILE` | one row per (session, PR) with every field, for finance |
| `comment [--pr N] [--repo PATH] [--post]` | this PR's cost as a PR comment; prints it unless `--post` |
| `otel serve [--host] [--port] [--token]` | receive Claude Code OpenTelemetry and fold it into reports |
| `billed [--since 30d]` | vendor-reported cost and usage (Anthropic Admin, Claude Enterprise, OpenAI) |
| `doctor` | observed schema, models and their price rows, unpriced models, repos, other sources, `gh auth status` |

`--since` takes `12h`, `30d`, `2w`, or a date. `--no-db` skips the history database. tokentab keeps its
state in `~/.tokentab`; set `TOKENTAB_HOME` to put it elsewhere.

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

## PR comments

`tokentab comment` renders the current branch's PR cost as a Markdown comment (spend, sessions, tokens,
cache ratio, per-model split, and how it compares with the repo's median merged PR). It prints the comment
unless you pass `--post`; with `--post` it creates one comment and edits that same comment on every later
run, found by a hidden `<!-- tokentab:cost -->` marker and your GitHub login.

The numbers come from the transcripts on the machine that runs the command, so a PR shows the poster's
Claude Code spend, not a teammate's. Posting puts that spend on the PR for everyone who can read it.

To have Claude Code keep the comment current, install tokentab as a command and add a `PostToolUse`
hook. After any Bash call that runs `git push` or `gh pr create`, `tokentab hook` posts or updates the
comment for that checkout's PR; for anything else it does nothing, and it never fails the tool call.

```bash
uv tool install --editable .
```

In `~/.claude/settings.json` (or a project's `.claude/settings.json`):

```json
{
  "hooks": {
    "PostToolUse": [
      {"matcher": "Bash", "hooks": [{"type": "command", "command": "tokentab hook", "timeout": 60}]}
    ]
  }
}
```

## OpenTelemetry

`tokentab otel serve` is a small OTLP/HTTP receiver (JSON only; protobuf would need a new dependency).
Point Claude Code at it:

```bash
uv run tokentab otel serve
```

```bash
CLAUDE_CODE_ENABLE_TELEMETRY=1 OTEL_LOGS_EXPORTER=otlp OTEL_METRICS_EXPORTER=otlp OTEL_EXPORTER_OTLP_PROTOCOL=http/json OTEL_EXPORTER_OTLP_ENDPOINT=http://127.0.0.1:4318 OTEL_METRICS_INCLUDE_REPOSITORY=true claude
```

Captures land in `~/.tokentab/otel/` (or `TOKENTAB_OTEL_DIR`); lines written by an OpenTelemetry
Collector's file exporter in that directory are read too. Every report folds them in:

- Each `claude_code.api_request` event is one API request. If the same `request_id` is in a transcript,
  the transcript wins and the OTel copy is dropped, so running both never double counts.
- Token metrics are used only for sessions with no request events.
- OTel reports cache writes as one number. tokentab splits it into 5-minute and 1-hour writes by finding
  the split that reproduces Claude Code's own `cost_usd`; when none does, it assumes 1-hour and says so.
- OTel carries no working directory or branch. A request whose session also has a transcript borrows its
  repo and branch from it. Otherwise the repo comes from the `vcs.*` metric attributes
  (`OTEL_METRICS_INCLUDE_REPOSITORY=true`), which name the repo but not the branch, so teammates'
  OTel-only spend is attributed to the repo (`repo-only`), never to a PR.
- `--by user` groups spend by the `user.email` on each request.

To collect from other machines, listen beyond localhost with a token
(`tokentab otel serve --host 0.0.0.0 --token SECRET`) and have Claude Code send
`OTEL_EXPORTER_OTLP_HEADERS='Authorization=Bearer SECRET'`. The receiver has no TLS; put it behind one
if the traffic leaves a trusted network.

## Vendor-reported spend

`tokentab billed` shows what vendors themselves report, next to (never added to) the API-equivalent
numbers, since the same request can appear in both. Each source turns on when its key is set:

| env var | organization | what it shows |
|---|---|---|
| `ANTHROPIC_ADMIN_KEY` | Claude Console (API) | invoiced cost and billed tokens per model; per-user Claude Code sessions, PRs, commits, and estimated cost |
| `ANTHROPIC_ANALYTICS_KEY` | Claude Enterprise (claude.ai seats; key needs `read:analytics`) | per-user Claude Code cost, sessions, PRs, commits, and cost per PR |
| `OPENAI_ADMIN_KEY` | OpenAI | costs per line item and tokens per model |

Keys are sent only to the vendor that issued them. The Enterprise and Claude Code analytics numbers are
per user per day, so "cost per PR" there is a user's spend divided by their PR count, not a per-PR figure.

## Prices

`tokentab/prices.yaml` is keyed by model-ID substring (longest match wins). Rows marked `verify: true`
were filled from Anthropic's published price table rather than by you; tokentab warns when a report
uses them. A model with no matching row is priced at the nearest row and flagged `unpriced` in every
view. Override the file with `TOKENTAB_PRICES=/path/to/prices.yaml`.

## Files it writes

- `~/.tokentab/usage.db`: SQLite history of every message seen, unique on `(message.id, requestId)`.
  Claude Code deletes old transcripts (about 30 days by default), so this is what keeps older months reportable.
- `~/.tokentab/gh_cache.json`: `gh pr list` results, reused for an hour.
- `~/.tokentab/otel/`: OTLP payloads received by `tokentab otel serve`.

Nothing leaves your machine except the `gh` calls to GitHub, `comment --post`, and the vendor API calls
made by `billed` when you set their keys.

## Known gaps

- Only a repo's 500 most recent PRs are fetched; older ones are matched only through Claude Code's own
  records, and tokentab says when a repo hits the limit.
- Sessions whose transcripts were pruned before the first run are gone. `Claude-Session:` trailers
  still name them, but there are no tokens left to count.
- `~/.claude/stats-cache.json` keeps per-model totals for days whose transcripts are gone, but it counts
  every streamed line of a message (1.8 to 2.5 times the real figure where tokentab could check), so
  `doctor` shows it only as an upper bound and it is never added to spend.
- The vendor API clients are written and tested against the documented request and response shapes, not
  against live accounts.

## Develop

```bash
uv sync
```

```bash
uv run pytest
```

## Releasing

Publishing uses PyPI trusted publishing from GitHub Actions
([`publish.yml`](https://github.com/smerwin/tokentab/blob/main/.github/workflows/publish.yml)), so no
PyPI token exists anywhere. Once, on PyPI under Account → Publishing, add a publisher with project
`tokentab`, owner `smerwin`, repository `tokentab`, workflow `publish.yml`, and environment `pypi`.

To release, set `version` in `pyproject.toml`, commit and push, then publish a GitHub release whose tag
is that version with a `v` in front. The workflow checks the tag against the version, runs the tests,
builds, and uploads.

```bash
gh release create v0.1.0 --generate-notes
```

## License

MIT; see [LICENSE](https://github.com/smerwin/tokentab/blob/main/LICENSE).
