let meta = #{
    name: "fix-p0",
    description: "Implement fact-checked casys-trader P0s then adversarially verify",
    when_to_use: "After fact-check-p0, to land the surviving VRAI fixes with tests",
    phases: [
        #{ title: "Implement", detail: "one implementer per disjoint P0 file set" },
        #{ title: "Verify", detail: "one skeptic per P0, fail-closed" },
    ],
};

let impl_schema = #{
    "type": "object",
    "required": ["p0_id", "ok", "files", "tests", "notes"],
    "properties": #{
        "p0_id": #{ "type": "string" },
        "ok": #{ "type": "boolean" },
        "files": #{ "type": "array", "items": #{ "type": "string" } },
        "tests": #{ "type": "string" },
        "notes": #{ "type": "string" },
    },
};

let verify_schema = #{
    "type": "object",
    "required": ["p0_id", "real", "reason", "evidence"],
    "properties": #{
        "p0_id": #{ "type": "string" },
        "real": #{ "type": "boolean" },
        "reason": #{ "type": "string" },
        "evidence": #{ "type": "string" },
    },
};

fn impl_prompt(id, title, files, spec) {
    let p = "You are a TDD implementer in casys-trader. WRITE TESTS FIRST, then the smallest impl.\n";
    p += "Repo: /Users/erwanpesle/Documents/GitHub/casys-trader\n";
    p += "P0 id: " + id + "\n";
    p += "Title: " + title + "\n";
    p += "You MAY edit ONLY these paths (and new tests under tests/ that they need):\n";
    p += files + "\n";
    p += "Do NOT edit .env, live presets, or ops/model-presets except docs if listed.\n";
    p += "Do NOT reopen D12. Do NOT change ARMED_ORDER_MAX_TTL_MINUTES.\n";
    p += "Do NOT switch the live daemon off kimi.\n";
    p += "Read the listed files with read_file/grep BEFORE editing. Do not answer from memory.\n";
    p += "Run the targeted pytest files with: cd /Users/erwanpesle/Documents/GitHub/casys-trader && python -m pytest -q <tests> \n";
    p += "SPEC:\n" + spec + "\n";
    p += "Return JSON {p0_id, ok, files, tests, notes}. ok=true only if targeted tests passed.\n";
    p
}

fn verify_prompt(id, claim, files) {
    let p = "You are an adversarial verifier. READ-ONLY. Do not edit files.\n";
    p += "Repo: /Users/erwanpesle/Documents/GitHub/casys-trader\n";
    p += "P0 id: " + id + "\n";
    p += "CLAIM TO CONFIRM: " + claim + "\n";
    p += "Read these paths with read_file/grep (do not answer from memory): " + files + "\n";
    p += "Set real=true only if the shipped code independently matches the claim.\n";
    p += "If tests were not added, or behavior is still the old one, real=false.\n";
    p += "Return JSON {p0_id, real, reason, evidence} with file:line quotes.\n";
    p
}

phase("Implement");

let jobs = [];

let ttl_spec = "";
ttl_spec += "Default indicator-watch TTL when the model omits ttl_minutes is 60 minutes.\n";
ttl_spec += "Product is paper swing (hours-to-days). Change the omitted-ttl default to 240.0 minutes.\n";
ttl_spec += "Introduce a named constant DEFAULT_WATCH_TTL_MINUTES = 240.0 in indicator_watch.py and use it as _bounded_float default.\n";
ttl_spec += "EXECUTE_ORDER still does ttl = min(ttl, ARMED_ORDER_MAX_TTL_MINUTES). Do not raise that max (D12).\n";
ttl_spec += "Update prompts.py watch vocabulary: replace 'defaut 60 minutes' with 'defaut 240 minutes (4 h)'.\n";
ttl_spec += "Tell the model that overnight theses must set ttl_minutes explicitly (max 1440).\n";
ttl_spec += "Update tests/test_codex_client_watch_vocabulary.py assertion.\n";
ttl_spec += "Add a domain test: omitted ttl_minutes yields 240, and EXECUTE_ORDER still caps at 240.\n";
ttl_spec += "Do not change explicit ttl_minutes=60 fixtures unless they test the default.\n";

jobs.push(#{
    prompt: impl_prompt(
        "watch-ttl",
        "Default watch TTL 60 -> 240 minutes",
        "trader/domain/planning/indicator_watch.py ; trader/agent/protocol/prompts.py ; tests/test_codex_client_watch_vocabulary.py ; tests/domain/planning/test_indicator_watch.py ; docs/reference/wake-scheduler.md",
        ttl_spec
    ),
    label: "impl:watch-ttl",
    capability_mode: "all",
    output_schema: impl_schema,
});

let consume_spec = "";
consume_spec += "Bug: scan_indicator_watches removes EVERY triggered watch, including EXECUTE_ORDER, BEFORE execution.enabled / session.open is checked.\n";
consume_spec += "cycle_decision logs 'watch/wake conserves' on execution_blocked but the watch is already gone. Post-close trigger = plan consumed, no fill, no LLM.\n";
consume_spec += "Fix:\n";
consume_spec += "1) watch_scanner.scan_indicator_watches: still wake the symbol. Remove the watch IFF on_trigger != EXECUTE_ORDER. Keep EXECUTE_ORDER watches in the scheduler.\n";
consume_spec += "2) ArmedPlanResolution: add consume_watch_ids: list[str]. Populate for TERMINAL cancels only: position_exists, exit_unresolved*, stop_incoherent, armed_conflict. Do NOT consume on armed_plan_cancelled:stale.\n";
consume_spec += "3) After prepare_decision_scope in trader/runtime/daemon.py, if sched is not None, remove each consume_watch_ids entry. Keep this a short loop; do not import resolve_armed_plan_triggers into daemon (layout test).\n";
consume_spec += "4) cycle_decision: when an armed plan (sym in ctx.armed_plan_ids) is admitted/executed (reason ok, including dry_run), remove that watch via ctx.sched.remove_indicator_watch. Do NOT remove when execution_blocked_reason is set.\n";
consume_spec += "Tests required:\n";
consume_spec += "- test_watch_scanner: EXECUTE_ORDER trigger returns the event, wakes symbol, watch STILL active.\n";
consume_spec += "- WAKE trigger still removes the watch (existing test must stay green).\n";
consume_spec += "- test_armed_plans: stale cancel does not list consume_watch_ids; position_exists / conflict does.\n";
consume_spec += "- cycle_decision or a focused test: execution_blocked keeps watch; successful admit removes it.\n";
consume_spec += "Do not implement D12 preflight. Do not change ARMED_ORDER_MAX_TTL_MINUTES.\n";

jobs.push(#{
    prompt: impl_prompt(
        "watch-consume",
        "Do not consume EXECUTE_ORDER watch before execution.enabled",
        "trader/application/cycle/watch_scanner.py ; trader/application/exit/armed_plans.py ; trader/application/execute/cycle_decision.py ; trader/runtime/daemon.py ; tests/application/test_watch_scanner.py ; tests/application/test_armed_plans.py ; tests/application/test_cycle_decision.py",
        consume_spec
    ),
    label: "impl:watch-consume",
    capability_mode: "all",
    output_schema: impl_schema,
});

let fx_spec = "";
fx_spec += "NUANCE from fact-check: unconfigured FX already WARNINGs then silently uses 1.0. That 1.0 is still a wrong conversion for TWD/EUR/GBP.\n";
fx_spec += "Fail closed for missing non-USD FX. Do not silently convert at 1.0.\n";
fx_spec += "1) rates_for_symbols: if ccy missing from fx.yaml, LOG error/warning and OMIT the key. Never set rates[ccy]=1.0 for a non-base currency.\n";
fx_spec += "   Live fetch still falls back to spec['fallback'] when the pair is configured. That is not silent 1.0.\n";
fx_spec += "2) MarketSnapshot.rate_for_symbol: USD/base -> 1.0. If non-USD rate missing, non-finite, or <=0, raise a small typed error (e.g. MissingFxRate) with symbol+ccy. No .get(..., 1.0).\n";
fx_spec += "3) After resolving fx_rate_by_ccy (including provider exception -> {USD:1.0}), mark any symbol whose currency is missing from fx_rate_by_ccy as non-tradable (stale_reason missing_fx_rate) and drop it from tradable_* .\n";
fx_spec += "4) trader/agent/context.py cockpit: do not stamp 1.0 for a missing non-USD rate. Use None or omit rather than a fake 1.0.\n";
fx_spec += "Update tests/test_fx_rates.py: unconfigured TWD/GBP must NOT be 1.0 (key absent). Update test_fx.py comment about silent 1.0. Add market_snapshot test: 2330.TW without TWD rate is not tradable; rate_for_symbol raises.\n";
fx_spec += "Configured fallback (0.031) must still work.\n";

jobs.push(#{
    prompt: impl_prompt(
        "fx-fail-closed",
        "Refuse silent fx_rate=1.0 for non-USD",
        "trader/infrastructure/market_sources/fx_rates.py ; trader/application/cycle/market_snapshot.py ; trader/agent/context.py ; tests/test_fx_rates.py ; tests/test_fx.py ; tests/test_market_snapshot.py ; tests/test_agent_context.py",
        fx_spec
    ),
    label: "impl:fx",
    capability_mode: "all",
    output_schema: impl_schema,
});

let bench_spec = "";
bench_spec += "Official decisions bench currently prompts BUY/SELL/HOLD reviews, not the live Pine-like calls contract from build_batch_prompt.\n";
bench_spec += "Add an explicit contract flag. Default MUST stay reviews (safe default, no behavior change).\n";
bench_spec += "1) decision_bench.py: contract in {reviews, production}. build_prompt(..., contract=).\n";
bench_spec += "   reviews = existing prompt.\n";
bench_spec += "   production = ask for the live planner JSON: {decisions:[{decision_id, symbol, confidence, rationale, opportunity_side, decision_reason_code, calls:[...] }]}.\n";
bench_spec += "   Reuse the production grammar from trader/agent/protocol/prompts.py (_SYMBOL_CALLS_FINAL_CONTRACT / strategy_entry). Do not invent a third contract.\n";
bench_spec += "2) Scoring stays BUY/SELL/HOLD internally. Map production calls:\n";
bench_spec += "   - strategy_entry direction long -> BUY\n";
bench_spec += "   - strategy_entry direction short -> SELL\n";
bench_spec += "   - strategy_close -> SELL if original was long-ish else HOLD is wrong; map strategy_close to SELL only when flattening a long is the historical action, otherwise treat as SELL for short-cover? Keep it simple: strategy_close -> SELL (exit). Document it.\n";
bench_spec += "   - empty calls or only set_next_wake / propose_indicator_watch WAKE -> HOLD\n";
bench_spec += "   - propose_indicator_watch EXECUTE_ORDER with order.direction long/short -> BUY/SELL\n";
bench_spec += "3) score_model_reviews accepts both shapes (reviews[] or decisions[]).\n";
bench_spec += "4) CLI trader/runtime/cli.py: --contract reviews|production default reviews. Thread through dry_run_payload and run_bench.\n";
bench_spec += "5) Audit freshness: when loading decision_audit.json for bench, if the newest row cycle_ts is older than 7 days, attach payload['audit_stale']=true and audit_as_of. Print a warning on the human path. Do NOT regenerate the 99MB file.\n";
bench_spec += "6) Docs: docs/how-to/measure-and-replay.md add a short section for `casys-trader decisions audit` refresh and `decisions bench --contract production`.\n";
bench_spec += "Tests: parse/build_prompt/score for production mapping; CLI default remains reviews; stale flag when cycle_ts is old. No live LLM calls.\n";

jobs.push(#{
    prompt: impl_prompt(
        "bench-audit",
        "Production bench contract + stale audit warning",
        "trader/reporting/bench/decision_bench.py ; trader/runtime/cli.py ; tests/test_decision_bench.py ; docs/how-to/measure-and-replay.md ; docs/reference/reporting.md",
        bench_spec
    ),
    label: "impl:bench-audit",
    capability_mode: "all",
    output_schema: impl_schema,
});

let impls = parallel(jobs);

phase("Verify");

let vjobs = [];
vjobs.push(#{
    prompt: verify_prompt(
        "watch-ttl",
        "Omitted ttl_minutes now defaults to 240 minutes via DEFAULT_WATCH_TTL_MINUTES; EXECUTE_ORDER still caps at ARMED_ORDER_MAX_TTL_MINUTES; prompt text no longer says defaut 60 minutes.",
        "trader/domain/planning/indicator_watch.py trader/agent/protocol/prompts.py tests/domain/planning/test_indicator_watch.py tests/test_codex_client_watch_vocabulary.py"
    ),
    label: "verify:watch-ttl",
    capability_mode: "read-only",
    agent_type: "explore",
    output_schema: verify_schema,
});
vjobs.push(#{
    prompt: verify_prompt(
        "watch-consume",
        "EXECUTE_ORDER watches are not removed in scan_indicator_watches; they are removed only after successful admit or a terminal cancel; stale and execution_blocked keep the watch.",
        "trader/application/cycle/watch_scanner.py trader/application/exit/armed_plans.py trader/application/execute/cycle_decision.py trader/runtime/daemon.py tests/application/test_watch_scanner.py"
    ),
    label: "verify:watch-consume",
    capability_mode: "read-only",
    agent_type: "explore",
    output_schema: verify_schema,
});
vjobs.push(#{
    prompt: verify_prompt(
        "fx-fail-closed",
        "Missing non-USD FX is no longer stamped as 1.0; rate_for_symbol fails closed; unconfigured currencies are omitted from rates_for_symbols.",
        "trader/infrastructure/market_sources/fx_rates.py trader/application/cycle/market_snapshot.py trader/agent/context.py tests/test_fx_rates.py"
    ),
    label: "verify:fx",
    capability_mode: "read-only",
    agent_type: "explore",
    output_schema: verify_schema,
});
vjobs.push(#{
    prompt: verify_prompt(
        "bench-audit",
        "decisions bench has --contract production (default still reviews) that prompts the live calls contract, and bench payload flags a stale decision_audit.",
        "trader/reporting/bench/decision_bench.py trader/runtime/cli.py tests/test_decision_bench.py docs/how-to/measure-and-replay.md"
    ),
    label: "verify:bench-audit",
    capability_mode: "read-only",
    agent_type: "explore",
    output_schema: verify_schema,
});

let verdicts = parallel(vjobs);

let impl_rows = [];
let i = 0;
let ids = ["watch-ttl", "watch-consume", "fx-fail-closed", "bench-audit"];
for r in impls {
    let id = ids[i];
    if r == () || !r.success || r.output == () {
        impl_rows.push(#{ p0_id: id, ok: false, notes: "implementer failed" });
    } else {
        impl_rows.push(r.output);
    }
    i += 1;
}

let verify_rows = [];
let j = 0;
for v in verdicts {
    let id = ids[j];
    if v == () || !v.success || v.output == () || v.output.real == () {
        verify_rows.push(#{ p0_id: id, real: false, reason: "verifier failed", evidence: "" });
    } else {
        verify_rows.push(v.output);
    }
    j += 1;
}

let passed = 0;
let failed = 0;
let k = 0;
while k < 4 {
    let impl_ok = impl_rows[k].ok == true;
    let verified = verify_rows[k].real == true;
    if impl_ok && verified { passed += 1; } else { failed += 1; }
    k += 1;
}

let summary = passed.to_string() + " confirmed / " + failed.to_string() + " not confirmed";
log(summary);
let path = write_scratch_file("fix-p0.json", json_encode(#{
    summary: summary,
    impl: impl_rows,
    verify: verify_rows,
}));
complete(#{ summary: summary, path: path, impl: impl_rows, verify: verify_rows });
