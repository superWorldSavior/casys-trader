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
- Expose whether a row is synthetic or model-authored, for example via
  `model_called=false` or an equivalent decision-source field.
- Avoid representing stale-data guards as if they were LLM `HOLD` decisions.
