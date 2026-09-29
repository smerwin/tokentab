"""Placeholder for v1 loaders; v0 reads only local Claude Code transcripts.

Planned sources, each producing the same event dicts as claude_jsonl:

- Claude Code OpenTelemetry: with CLAUDE_CODE_ENABLE_TELEMETRY=1 and an OTLP exporter
  configured, Claude Code emits `claude_code.token.usage` / `claude_code.cost.usage` metrics and
  `claude_code.api_request` events carrying session.id, model, and token counts. A small OTLP/HTTP
  receiver (or reading an OTel collector's file exporter) would ingest these for whole teams,
  without shipping transcript files around.
- Anthropic Admin API usage and cost reports (/v1/organizations/usage_report/messages and
  /v1/organizations/cost_report): billed usage per API key and workspace for API-billed users,
  which lets the report show actual invoice dollars beside API-equivalent USD.
- OpenAI organization usage endpoints (/v1/organization/usage/completions and /costs) for teams
  that mix providers, mapped onto the same (session, repo, PR) attribution.
"""
