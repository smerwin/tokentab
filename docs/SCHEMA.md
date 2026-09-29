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
