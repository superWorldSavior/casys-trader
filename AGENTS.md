# Codex Notes

Before investigating no-trade or no-LLM-call incidents in this repo, read:

- `.claude/notes/2026-06-17-llm-gate-vs-swing-freshness.md`
- `docs/superpowers/specs/2026-06-17-swing-watch-preflight-post-entry-design.md`
- `docs/decisions/registre-decisions-metier.md` section D12

That note captures the 2026-06-17 Taiwan-market incident where synthetic infra
`HOLD` rows caused by stale market data were initially easy to confuse with real
LLM-authored `HOLD` decisions.
