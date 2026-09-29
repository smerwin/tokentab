# Claude Code JSONL schema, as observed (Claude Code 2.1.161 – 2.1.282)

Files: `~/.claude/projects/<cwd-slug>/<sessionId>.jsonl`, plus subagent transcripts at
`<cwd-slug>/<sessionId>/subagents/agent-<agentId>.jsonl`.

Assistant lines (`type: "assistant"`) — the only lines with usage:

| field | notes |
|---|---|
| `sessionId` | subagent lines carry the **parent** session's id |
| `requestId` | missing only on `<synthetic>` error placeholders (0 tokens) |
| `timestamp` | ISO-8601 UTC, `Z` suffix |
| `cwd`, `gitBranch` | `gitBranch` is `"HEAD"` when detached (common in worktrees) |
| `parentUuid`, `uuid`, `isSidechain` | `isSidechain: true` exactly for subagent files |
| `message.id`, `message.model` | |
| `message.usage.{input,output,cache_creation_input,cache_read_input}_tokens` | |
| `message.usage.cache_creation.ephemeral_{5m,1h}_input_tokens` | split of cache writes by TTL |

Streaming writes one line per content block with the same `(message.id, requestId)`;
input/cache counts repeat and `output_tokens` grows, so dedupe keeps the per-field max.

Other useful line types:

- `pr-link`: `{sessionId, prNumber, prUrl, prRepository, timestamp}` — Claude Code's own
  record that a session opened or linked a PR.
- `bridge-session`: `{sessionId, bridgeSessionId: "cse_X"}` — `X` is the id in commit
  trailers `Claude-Session: https://claude.ai/code/session_X`.

## Claude Code OpenTelemetry, as observed (Claude Code 2.1.283, OTLP http/json)

`claude_code.api_request` log records (body `claude_code.api_request`, attribute `event.name: "api_request"`)
carry `session.id`, `request_id` (same value as the transcript's `requestId`), `client_request_id`,
`prompt.id`, `event.timestamp`, `event.sequence`, `model` (dated form, e.g. `claude-haiku-4-5-20251001`),
`input_tokens`, `output_tokens`, `cache_read_tokens`, `cache_creation_tokens` (no 5m/1h split),
`cost_usd`, `cost_usd_micros`, `duration_ms`, `ttft_ms`, `speed` (`normal`), `query_source`, plus
`user.id`, `user.email`, `user.account_uuid`, `user.account_id`, `organization.id`, `terminal.type`.
Integers arrived as JSON numbers inside `intValue` (the OTLP spec allows strings too).

`claude_code.token.usage` (attribute `type`: `input`, `output`, `cacheRead`, `cacheCreation`) and
`claude_code.cost.usage` are delta sums (`aggregationTemporality: 1`) with the undated model name
(`claude-haiku-4-5`). With `OTEL_METRICS_INCLUDE_REPOSITORY=true` they add `vcs.repository.url.full`,
`vcs.owner.name`, `vcs.repository.name`, `vcs.provider.name`; no branch, and not on log events.

`cost_usd` equals the published price with cache writes at the 1-hour rate when the transcript shows
1-hour writes, so it matches tokentab's formula exactly.
