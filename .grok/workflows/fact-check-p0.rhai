let meta = #{
    name: "fact-check-p0",
    description: "Adversarial VRAI/FAUX/NUANCE fact-check of casys-trader P0 review claims",
    when_to_use: "After a multi-axis architecture/product review, before acting on P0 items",
    phases: [
        #{ title: "Skeptics", detail: "one read-only skeptic per P0 claim" },
        #{ title: "Report", detail: "structured verdicts fail-closed" },
    ],
};

let verdict_schema = #{
    "type": "object",
    "required": ["claim_id", "verdict", "reason", "evidence"],
    "properties": #{
        "claim_id": #{ "type": "string" },
        "verdict": #{ "type": "string", "enum": ["VRAI", "FAUX", "NUANCE"] },
        "reason": #{ "type": "string" },
        "evidence": #{ "type": "string" },
        "file": #{ "type": "string" },
    },
};

fn claim_prompt(id, claim, hint) {
    let p = "You are an adversarial fact-checker. READ-ONLY. Do not edit files.\n";
    p += "Repo: /Users/erwanpesle/Documents/GitHub/casys-trader\n";
    p += "Claim id: " + id + "\n";
    p += "CLAIM: " + claim + "\n";
    p += "Start by reading these paths with read_file/grep (do not answer from memory): " + hint + "\n";
    p += "Try to REFUTE the claim. Default to FAUX if the cited file does not support it, or NUANCE if only part is true.\n";
    p += "VRAI only if you independently read the exact constant/branch and it matches the claim as stated.\n";
    p += "Return JSON with claim_id, verdict (VRAI|FAUX|NUANCE), reason, evidence (quote + file:line), file.\n";
    p
}

let claims = [
    #{
        id: "ttl-armed-4h",
        claim: "An armed EXECUTE_ORDER plan has a hard TTL of 240 minutes, so a swing thesis cannot survive overnight.",
        hint: "trader/domain/planning/armed_order.py and tests that mention ARMED_ORDER_MAX_TTL",
    },
    #{
        id: "watch-ttl-60",
        claim: "The default indicator-watch TTL is 60 minutes when the model omits ttl_minutes.",
        hint: "trader/planning/indicator_watch.py and trader/agent/protocol/prompts.py ttl_minutes",
    },
    #{
        id: "wake-15m",
        claim: "symbol_needs_llm treats 15m stretched/sig as a wake reason, and periodic review is 4 hours, so the planner is polled on chop instead of once per session.",
        hint: "trader/domain/planning/relevance_gate.py trader/planning/relevance_gate.py",
    },
    #{
        id: "ingest-every-hold",
        claim: "Every authentic LLM rationale including HOLD is ingested into learnings automatically.",
        hint: "trader/application/record/decision_recorder.py learnings ingest",
    },
    #{
        id: "watch-removed-before-session",
        claim: "On indicator-watch trigger the watch is removed before execution.enabled / session.open is checked, so a post-close trigger can consume the plan with no fill and no LLM.",
        hint: "trader/application/cycle/watch_scanner.py trader/application/exit/armed_plans.py",
    },
    #{
        id: "fx-silent-1",
        claim: "If FX is missing the system silently uses fx_rate=1.0 even for non-USD symbols.",
        hint: "trader/application/cycle/market_snapshot.py trader/infrastructure/market_sources/fx_rates.py",
    },
    #{
        id: "bench-toy-contract",
        claim: "Official decisions bench prompts BUY/SELL/HOLD reviews, not the live Pine-like calls contract from build_batch_prompt.",
        hint: "trader/reporting/bench/decision_bench.py trader/agent/protocol/prompts.py",
    },
    #{
        id: "audit-stale-july",
        claim: "state/decision_audit.json is frozen around 2026-07-11 and therefore misses live Kimi/Luna August trades.",
        hint: "state/decision_audit.json first and last cycle_ts audit_code_version",
    },
];

phase("Skeptics");
let jobs = [];
for c in claims {
    jobs.push(#{
        prompt: claim_prompt(c.id, c.claim, c.hint),
        label: "skeptic:" + c.id,
        capability_mode: "read-only",
        agent_type: "explore",
        output_schema: verdict_schema,
    });
}
let results = parallel(jobs);

phase("Report");
let rows = [];
let i = 0;
for r in results {
    let claim = claims[i];
    if r == () || !r.success || r.output == () || r.output.verdict == () {
        rows.push(#{
            claim_id: claim.id,
            claim: claim.claim,
            verdict: "UNVERIFIED",
            reason: "skeptic failed or returned no verdict",
            evidence: "",
            file: "",
        });
    } else {
        let ev = r.output.evidence;
        if ev == () { ev = ""; }
        let f = r.output.file;
        if f == () { f = ""; }
        rows.push(#{
            claim_id: claim.id,
            claim: claim.claim,
            verdict: r.output.verdict,
            reason: r.output.reason,
            evidence: ev,
            file: f,
        });
    }
    i += 1;
}

let vrai = 0;
let faux = 0;
let nuance = 0;
let unverified = 0;
for row in rows {
    if row.verdict == "VRAI" { vrai += 1; }
    else if row.verdict == "FAUX" { faux += 1; }
    else if row.verdict == "NUANCE" { nuance += 1; }
    else { unverified += 1; }
}

let summary = vrai.to_string() + " VRAI / " + faux.to_string() + " FAUX / " + nuance.to_string() + " NUANCE / " + unverified.to_string() + " UNVERIFIED";
log(summary);
let path = write_scratch_file("fact-check-p0.json", json_encode(#{ summary: summary, rows: rows }));
complete(#{ summary: summary, path: path, rows: rows });
