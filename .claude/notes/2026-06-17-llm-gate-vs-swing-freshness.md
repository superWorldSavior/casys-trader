# 2026-06-17: LLM Gate vs Swing Freshness

Context: investigation after no overnight trades / apparent no model activity on
the Taiwan universe.

Key distinction: synthetic infra `HOLD` is not the same as an LLM-authored
`HOLD`. In the problematic cycles, the model was not called:

- 2026-06-17 04:29 Taipei: 24 synthetic `HOLD`, `llm_model=None`,
  `reason=stale_market_data`, `model_calls_used=0`.
- 2026-06-17 09:01 Taipei: 25 synthetic `HOLD`, `llm_model=None`,
  `reason=stale_market_data`, `model_calls_used=0`.
- Later, around 09:20 and 09:24 Taipei, `gpt-5.5/medium` was called again; most
  outputs were real LLM `HOLD`, with one `BUY` blocked by risk as
  `risk:confidence_below_required`.

Current design mismatch to study:

- The mandate/context recently moved toward swing/daily reasoning.
- The model-call gate still depends on intraday runtime freshness
  (`DEFAULT_RUNTIME_INTERVAL = "15m"` and a short freshness budget).
- If yfinance has no fresh Taiwan intraday bars, symbols are removed before
  daily/swing context is built, producing no LLM call.

Second mismatch to study:

- Daily freshness semantics are not swing-aware enough.
- yfinance daily bars for Taiwan can be timestamped at the session date/midnight.
- A raw minute-age freshness check can mark useful previous completed daily bars
  as stale.
- For swing, daily context validity should be based on completed trading
  sessions/session calendar, while execution eligibility should remain separate
  and require fresh tradable/runtime price data.

Likely direction, not yet implemented:

- Separate "LLM can analyze/plan with swing context" from "system may execute an
  order now".
- Observability quick win landed: decision rows now expose `decision_source` and
  `model_called`; infra fallbacks such as `stale_market_data` and
  `no_decision_in_batch` must not masquerade as model-authored `HOLD`.

Design/decision state after the Claude review:

- D7B remains the current production rule: valid `EXECUTE_ORDER` plans execute
  without LLM re-call, while deterministic RiskGate checks still apply.
- The proposed swing preflight is tracked as D12, not as an implicit rewrite of
  D7B. Until Erwan validates D12 or amends D7B, implementation must preserve D7B.
- Preflight adds a thesis veto by the LLM; it is not the risk fuse. D11 already
  addressed the stale absolute-stop issue from the CFR/ASML postmortem.
- `WAKE_WITH_ORDER_INTENT` is not yet a full preflight path; wiring armed plan
  -> preflight -> execution is real implementation work.
- Post-entry watch must be sticky in D10 rotation, and `last_llm_review` belongs
  in persistent `TradePlan` state because `_LAST_LLM_AT` is RAM-only.

See:

- `docs/superpowers/specs/2026-06-17-swing-watch-preflight-post-entry-design.md`
- `docs/decisions/registre-decisions-metier.md` section D12
