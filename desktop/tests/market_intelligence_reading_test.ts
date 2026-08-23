import { assert } from "@std/assert/assert";
import { assertEquals } from "@std/assert/equals";
import {
  interactiveGraphLabel,
  matchingCompany,
  projectCompanyDigest,
  projectDomainReading,
  projectFactorReading,
  projectMarketReading,
  projectThemeEvidence,
} from "../src/lib/market-intelligence-reading.ts";
import {
  buildMarketInfluenceGraph,
  type MarketInfluenceGraph,
} from "../src/lib/market-influence-network.ts";
import type {
  CompanyIntelligence,
  FamilyComparison,
  RegionCurrentIntelligence,
} from "../src/lib/types.ts";

function company(
  extra: Partial<CompanyIntelligence> = {},
): CompanyIntelligence {
  return {
    symbol: "TTE.PA",
    name: "TotalEnergies",
    venue: "EU",
    as_of: "2026-08-20T00:00:00Z",
    age_hours: 80,
    depth: "screen",
    coverage: "partial",
    source_count: 2,
    thesis_status: "untested",
    previous_thesis_status: "insufficient_evidence",
    thesis_changed: true,
    summary: "The outlook is on watch given commodity linkage.",
    business_summary: "Oil company JSON should stay hidden.",
    catalyst_count: 1,
    risk_count: 0,
    open_question_count: 1,
    history_count: 3,
    on_book: false,
    stale_market: false,
    decision_count: 4,
    linked_decision_count: 1,
    latest_action: "HOLD",
    latest_decision_at: "2026-08-20T00:00:00Z",
    brief: {
      brief_id: "brief-secret",
      catalysts: [{ point: "Singapore ATM win", source_refs: ["src-9"] }],
      risks: [],
      open_questions: [{ point: "issuer identity is unverified" }],
    },
    ...extra,
  };
}

function displayBlob(value: unknown): string {
  return JSON.stringify(value);
}

function assertNoRawTokens(value: unknown, extra: string[] = []) {
  const text = displayBlob(value);
  const banned = [
    "screen",
    "untested",
    "insufficient_evidence",
    "risk_off",
    "not_observed",
    "not_in_taxonomy",
    "shared_source",
    "topic_match",
    "GDELT",
    "sticky",
    "fallback",
    "brief-secret",
    "src-9",
    "HOLD",
    "latest_action",
    ...extra,
  ];
  for (const token of banned) {
    const leaked = token.includes("_") || token.includes("-")
      ? text.toLowerCase().includes(token.toLowerCase())
      : new RegExp(`\\b${token}\\b`).test(text);
    assert(!leaked, `raw token leaked: ${token} in ${text}`);
  }
}

Deno.test("company digest marks a partial review older than 72h as limited and stale", () => {
  const reading = projectCompanyDigest({ company: company() });

  assertEquals(reading.state, "ready");
  assertEquals(reading.freshnessLabel, "Needs refreshing");
  assert(reading.evidenceLimits.includes("Partial sources"));
  assert(reading.evidenceLimits.includes("Quick review"));
  assert(reading.evidenceLimits.includes("2 sources"));
  assertEquals(reading.evidenceLimits.includes("Complete sources"), false);
  assertEquals(reading.evidenceLimits.includes("In-depth review"), false);
  assertEquals(
    reading.summary,
    "The outlook needs close monitoring given exposure to commodity prices.",
  );
  assertEquals(
    reading.catalyst,
    "Singapore air-traffic-management contract",
  );
  assertEquals(reading.risk, null);
  assertEquals(
    reading.openQuestion,
    "Company identity has not been confirmed",
  );
  assertNoRawTokens(reading, ["Oil company JSON"]);
});

Deno.test("company digest prefers one risk over an open question and ignores latest_action", () => {
  const reading = projectCompanyDigest({
    company: company({
      age_hours: 4,
      depth: "deep",
      coverage: "complete",
      source_count: 6,
      thesis_status: "intact",
      brief: {
        catalysts: [{ point: "Next earnings print" }],
        risks: [{ point: "substantial leverage" }],
        open_questions: [{ point: "guidance sources remain thin" }],
      },
    }),
  });

  assertEquals(reading.state, "ready");
  assertEquals(reading.freshnessLabel === "Needs refreshing", false);
  assertEquals(reading.evidenceLimits.includes("In-depth review"), true);
  assertEquals(reading.evidenceLimits.includes("Complete sources"), true);
  assertEquals(reading.risk, "High debt");
  assertEquals(reading.openQuestion, null);
  assertEquals(reading.catalyst, "Next earnings report");
  assertNoRawTokens(reading, ["deep"]);
});

Deno.test("company digest keeps pending, fetch failure and empty distinct", () => {
  assertEquals(projectCompanyDigest({ pending: true }).state, "pending");
  assertEquals(
    projectCompanyDigest({ pending: true, error: true }).state,
    "pending",
  );
  assertEquals(
    projectCompanyDigest({ error: true }).state,
    "unavailable",
  );
  assertEquals(projectCompanyDigest({}).state, "empty");
  const cachedRefresh = projectCompanyDigest({
    error: true,
    company: company({ age_hours: 2 }),
  });
  assertEquals(cachedRefresh.state, "ready");
  assertEquals(
    cachedRefresh.refreshNote,
    "Could not refresh · Showing the last recorded view",
  );
  assert(cachedRefresh.summary);
  assertEquals(
    projectCompanyDigest({ company: company({ age_hours: 2 }) }).refreshNote,
    null,
  );
});

Deno.test("company digest keeps identity with the requested symbol", () => {
  const omv = company({ symbol: "OMV.VI", name: "OMV" });
  const other = company({ symbol: "FITB", name: "Fifth Third" });
  assertEquals(matchingCompany([omv, other], "omv.vi")?.symbol, "OMV.VI");
  assertEquals(matchingCompany([other], "OMV.VI"), null);
  assertEquals(
    projectCompanyDigest({ companies: [other], symbol: "OMV.VI" }).state,
    "empty",
  );
  assertEquals(
    projectCompanyDigest({
      error: true,
      companies: [other],
      symbol: "OMV.VI",
    }).state,
    "empty",
  );
  assertEquals(
    projectCompanyDigest({
      companies: [omv, other],
      symbol: "omv.vi",
    }).state,
    "ready",
  );
});

Deno.test("company digest does not treat missing age as current or complete", () => {
  const reading = projectCompanyDigest({
    company: company({
      as_of: null,
      age_hours: null,
      coverage: "partial",
      depth: null,
      source_count: 0,
      summary: "",
      brief: { catalysts: [], risks: [], open_questions: [] },
    }),
  });

  assertEquals(reading.state, "ready");
  assertEquals(reading.summary, null);
  assertEquals(reading.catalyst, null);
  assertEquals(reading.freshnessLabel === "Needs refreshing", false);
  assert(reading.evidenceLimits.includes("Partial sources"));
  assertEquals(reading.evidenceLimits.includes("Complete sources"), false);
  assertEquals(
    reading.evidenceLimits.some((item) => item.includes("0")),
    false,
  );
});

Deno.test("theme evidence skips operational points and the duplicated summary", () => {
  const current: Record<string, RegionCurrentIntelligence> = {
    FR: {
      macro_brief: {
        as_of: "2026-08-23T10:00:00Z",
        valid_until: "2099-01-01T00:00:00Z",
        status: "success",
        families: {
          fr_energy: [
            { point: "Feed limit reached", is_operational: true },
            { point: "Energy earnings improved." },
            { point: "Oil inventories fell." },
          ],
        },
      },
    },
  };
  const duplicated = projectThemeEvidence({
    family: "fr_energy",
    scopeKey: "FR",
    summary: "Energy earnings improved.",
    current,
  });
  const unused = projectThemeEvidence({
    family: "fr_energy",
    scopeKey: "FR",
    summary: "A different theme summary.",
    current,
  });

  assertEquals(duplicated.point, "Oil inventories fell.");
  assertEquals(unused.point, "Energy earnings improved.");
  assertEquals(duplicated.freshnessLabel === "Needs refreshing", false);
});

Deno.test("theme evidence says Needs refreshing when the macro brief is expired", () => {
  const reading = projectThemeEvidence({
    family: "fr_energy",
    scopeKey: "FR",
    summary: "Theme summary.",
    current: {
      FR: {
        macro_brief: {
          as_of: "2026-01-01T00:00:00Z",
          valid_until: "2026-01-02T00:00:00Z",
          status: "success",
          families: {
            fr_energy: [{ point: "Oil inventories fell." }],
          },
        },
      },
    },
  });

  assertEquals(reading.point, "Oil inventories fell.");
  assertEquals(reading.freshnessLabel, "Needs refreshing");
});

Deno.test("market reading excludes operational alerts and keeps at most two points", () => {
  const reading = projectMarketReading({
    scopeKey: "NORDIC",
    current: {
      NORDIC: {
        posture: "risk_off",
        regional_run: {
          summary: "Asia sold off while oil was still extending gains.",
          agent_run_id: "run-secret",
        },
        macro_brief: {
          as_of: "2026-08-23T08:00:00Z",
          valid_until: "2099-01-01T00:00:00Z",
          status: "success",
          alerts: [
            { point: "GDELT provider feed limit", is_operational: true },
            { point: "oil was still extending gains." },
            { point: "Asia sold off." },
            { point: "Gold bounced." },
          ],
        },
      },
    },
  });

  assertEquals(reading.points.length, 2);
  assertEquals(reading.points[0], "Oil prices were still rising.");
  assertEquals(reading.points[1], "Asian markets fell sharply.");
  assertEquals(
    reading.summary,
    "Asian markets fell sharply while oil prices were still rising.",
  );
  assertEquals(reading.posture, "Lower risk appetite");
  assertEquals(reading.freshnessLabel === "Needs refreshing", false);
  assertNoRawTokens(reading, ["run-secret", "Gold bounced"]);
});

Deno.test("market reading falls back inside the same brief and flags an expired update", () => {
  const reading = projectMarketReading({
    scopeKey: "ASEAN",
    current: {
      ASEAN: {
        macro_brief: {
          valid_until: "2020-01-01T00:00:00Z",
          status: "success",
          alerts: [
            { point: "Pipeline stall", is_operational: true },
          ],
          families: {
            asean_energy: [{ point: "Regional oil demand held up." }],
          },
          zones: {
            oil: [{ point: "Brent stayed firm." }],
          },
        },
      },
    },
  });

  assertEquals(reading.points, ["Regional oil demand held up."]);
  assertEquals(reading.freshnessLabel, "Needs refreshing");
});

Deno.test("market reading stays empty without inventing a missing brief", () => {
  const reading = projectMarketReading({
    scopeKey: "GCC",
    current: { GCC: {} },
  });
  assertEquals(reading.summary, null);
  assertEquals(reading.posture, null);
  assertEquals(reading.points, []);
  assertEquals(reading.freshnessLabel, null);
});

Deno.test("domain reading labels every dynamic venue without leaking status tokens", () => {
  const comparisons: FamilyComparison[] = [{
    group: "energy",
    label: "Energy",
    venues: {
      FR: {
        status: "active",
        leader: "fr_energy",
        best_rank: 1,
        candidate_count: 4,
        reason: "sticky_fallback GDELT provider",
        families: [],
      },
      ASEAN: {
        status: "observed",
        candidate_count: 0,
        reason: "observed_only",
        families: [],
      },
      LATAM: {
        status: "not_observed",
        candidate_count: 0,
        reason: "missing",
        families: [],
      },
      GCC: {
        status: "not_in_taxonomy",
        candidate_count: 0,
        reason: "unmapped",
        families: [],
      },
    },
  }];
  const reading = projectDomainReading({
    domainKey: "energy",
    label: "Energy",
    comparisons,
  });

  assert(reading);
  assertEquals(
    reading.venues.map((row) => row.venueKey),
    ["FR", "ASEAN", "LATAM", "GCC"],
  );
  assertEquals(
    reading.venues.map((row) => row.statusLabel),
    [
      "Actively considered",
      "Observed",
      "No current reading",
      "Not covered",
    ],
  );
  assertEquals(reading.venues[0].leader, "Energy");
  assertEquals(reading.venues[0].rankLabel, "1st");
  assertEquals(reading.venues[1].leader, null);
  assertEquals(reading.venues[1].rankLabel, null);
  assertNoRawTokens(reading);
});

Deno.test("domain reading matches a label fallback and skips an unknown domain", () => {
  const comparisons: FamilyComparison[] = [{
    group: "materials-group",
    label: "Materials",
    venues: {
      LATAM: {
        status: "active",
        leader: "latam_materials",
        best_rank: 3,
        candidate_count: 2,
        reason: "leader",
        families: [],
      },
    },
  }];

  const byLabel = projectDomainReading({
    domainKey: "Materials",
    label: "Materials",
    comparisons,
  });
  const missing = projectDomainReading({
    domainKey: "healthcare",
    label: "Healthcare",
    comparisons,
  });

  assert(byLabel);
  assertEquals(byLabel.venues.length, 1);
  assertEquals(byLabel.venues[0].leader, "Materials");
  assertEquals(missing, null);
});

Deno.test("factor reading keeps recorded points only on direct shared evidence", () => {
  const graph: MarketInfluenceGraph = {
    nodes: [
      {
        data: { id: "driver:oil", kind: "driver", label: "Oil" },
        classes: "driver supported",
      },
      {
        data: {
          id: "family:fr",
          kind: "family",
          label: "Energy",
          family: "fr_energy",
        },
        classes: "family",
      },
      {
        data: {
          id: "domain:energy",
          kind: "domain",
          label: "Energy",
          domainKey: "energy",
        },
        classes: "domain",
      },
    ],
    edges: [
      {
        data: {
          id: "edge-shared",
          source: "driver:oil",
          target: "family:fr",
          kind: "evidence",
          basis: "shared_source",
          tone: "supportive",
          label: "Shared evidence",
          driverPoint: "Oil rose.",
          familyPoint: "Energy earnings improved.",
        },
        classes: "influence-path evidence",
      },
      {
        data: {
          id: "edge-heuristic",
          source: "driver:oil",
          target: "domain:energy",
          kind: "possible",
          basis: "heuristic_domain",
          tone: "context",
          label: "Possible influence",
          driverPoint: "Oil rose.",
          familyPoint: "Would look measured if leaked.",
        },
        classes: "influence-path possible",
      },
      {
        data: {
          id: "edge-topic",
          source: "driver:oil",
          target: "family:fr",
          kind: "possible",
          basis: "topic_match",
          tone: "context",
          label: "Possible influence",
          driverPoint: "Oil rose.",
          familyPoint: "Topic guess.",
        },
        classes: "influence-path possible",
      },
    ],
    scopeCount: 1,
    domainCount: 1,
    familyCount: 1,
    companyCount: 0,
    supportedPathCount: 1,
    possiblePathCount: 2,
  };
  const reading = projectFactorReading(graph, "driver:oil");

  assertEquals(reading.recorded.length, 1);
  assertEquals(reading.recorded[0].targetId, "family:fr");
  assertEquals(reading.recorded[0].driverPoint, "Oil rose.");
  assertEquals(reading.recorded[0].familyPoint, "Energy earnings improved.");
  assertEquals(reading.recorded[0].freshnessLabel, null);
  assertEquals(reading.possibleTargetIds, ["domain:energy"]);
  assertNoRawTokens(reading.recorded);
});

Deno.test("empty intelligence projections stay empty rather than inventing a reading", () => {
  assertEquals(
    projectThemeEvidence({ family: "x", scopeKey: "Y" }).point,
    null,
  );
  assertEquals(projectMarketReading({ scopeKey: "Y" }).points, []);
  assertEquals(
    projectDomainReading({ domainKey: "energy", comparisons: [] }),
    null,
  );
  const empty = buildMarketInfluenceGraph({ nodes: [], threads: [] });
  assertEquals(projectFactorReading(empty, "driver:oil").recorded, []);
});

Deno.test("company digest rewrites live OMV jargon into plain language", () => {
  const reading = projectCompanyDigest({
    company: company({
      symbol: "OMV.VI",
      name: "OMV",
      summary:
        "OMV remains an untested oils-energy issuer. The current read still leans on supplied company-news headlines rather than operating or financial pillars. Hydrocarbon-linked EU energy and energy names in the 25-name fallback remain exposed to geo risk, an inflation cooldown, and scaled producers, including EMS/ODM names.",
    }),
  });
  assert(reading.summary);
  assertNoRawTokens(reading, [
    "untested",
    "fallback",
    "EMS/ODM",
    "geo risk",
    "hydrocarbon-linked",
    "energy names",
  ]);
  assert(
    reading.summary.includes("still needs supporting evidence"),
    reading.summary,
  );
  assert(
    reading.summary.includes("broader company list") ||
      reading.summary.includes("electronics manufacturers"),
    reading.summary,
  );
  assertEquals(reading.summary.includes("an slower"), false);
});

Deno.test("company digest rewrites the live OMV no-source digest into plain sentences", () => {
  const reading = projectCompanyDigest({
    company: company({
      symbol: "OMV.VI",
      name: "OMV",
      summary:
        "No sourced operating or financial pillars can be established from the supplied company-news headlines. The company outlook is therefore untested.",
      brief: {
        catalysts: [{
          point:
            "The news archive includes a headline reporting installation of the Neptun Deep production unit, but the supplied evidence does not establish its financial significance for OMV.",
        }],
        risks: [{
          point:
            "A news headline states that a hybrid bond issue reshaped OMV's balance sheet and investor risk profile, but no terms or quantified effects were supplied.",
        }],
        open_questions: [],
      },
    }),
  });
  const blob = displayBlob(reading);

  assertEquals(
    reading.summary,
    "Available company research does not yet support a clear operating or financial view. The outlook still needs supporting evidence.",
  );
  assertEquals(
    reading.catalyst,
    "A report covers the installation of the Neptun Deep production unit, but the available information does not show what it could mean financially for OMV.",
  );
  assertEquals(
    reading.risk,
    "A report says the hybrid bond issue changed OMV's balance sheet and investor risk profile, but the available information does not include its terms or measurable effects.",
  );
  assertEquals(blob.includes("No sourced a clear"), false);
  assertEquals(blob.includes("is therefore still needs"), false);
  assertEquals(/supplied/i.test(blob), false);
  assertEquals(/news archive/i.test(blob), false);
  assertNoRawTokens(reading, [
    "supplied",
    "news archive",
    "pillars",
    "quantified",
    "reshaped",
  ]);
});

Deno.test("macro points skip a verbatim regional summary and rewrite factor jargon", () => {
  const reading = projectMarketReading({
    scopeKey: "EU",
    current: {
      EU: {
        regional_run: {
          summary:
            "Hydrocarbon-linked EU energy stays exposed to geo risk and an inflation cooldown among scaled producers.",
        },
        macro_brief: {
          as_of: "2026-08-23T08:00:00Z",
          valid_until: "2099-01-01T00:00:00Z",
          status: "success",
          alerts: [
            {
              point:
                "Hydrocarbon-linked EU energy stays exposed to geo risk and an inflation cooldown among scaled producers.",
            },
            { point: "Energy names in the 25-name fallback still lead." },
            { point: "Pipeline stall", is_operational: true },
          ],
        },
      },
    },
  });
  assertEquals(reading.points.length, 1);
  assertEquals(String(reading.summary).includes("companies stays"), false);
  assertNoRawTokens(reading, [
    "fallback",
    "geo risk",
    "hydrocarbon-linked",
    "energy names",
  ]);
  assertEquals(
    reading.points[0].includes("energy companies") ||
      reading.points[0].includes("broader company list"),
    true,
  );
});

Deno.test("fresh partial coverage is not overwritten by age alone", () => {
  const reading = projectMarketReading({
    scopeKey: "EU",
    current: {
      EU: {
        macro_brief: {
          as_of: new Date().toISOString(),
          valid_until: "2099-01-01T00:00:00Z",
          status: "success",
          coverage: { status: "partial" },
          alerts: [{ point: "Oil inventories fell." }],
        },
      },
    },
  });
  assert(reading.freshnessLabel);
  assertEquals(reading.freshnessLabel.includes("Partial coverage"), true);
  assertEquals(reading.freshnessLabel === "0 seconds ago", false);
});

Deno.test("serialized company summaries fail closed and prefer a safe thesis", () => {
  const reading = projectCompanyDigest({
    company: company({
      symbol: "COLD",
      summary:
        "{'point': 'Warehouse demand held', 'source_refs': ['yfinance:COLD']}",
      brief: {
        company_thesis: { summary: "Warehouse demand is holding up." },
        selection_view: { summary: "Should stay hidden if thesis is safe." },
        catalysts: [{
          point: "{'point': 'Lease win', 'source_refs': ['yfinance:COLD']}",
        }],
        risks: [{ point: "Lease concentration remains high." }],
        open_questions: [],
      },
    }),
  });
  assertEquals(reading.summary, "Warehouse demand is holding up.");
  assertEquals(reading.catalyst, null);
  assertEquals(reading.risk, "Lease concentration remains high.");
  assertNoRawTokens(reading, [
    "yfinance:COLD",
    "source_refs",
    "Should stay hidden",
  ]);
});

Deno.test("coverage full with zero sources never reads as complete", () => {
  const reading = projectCompanyDigest({
    company: company({
      coverage: "full",
      source_count: 0,
      summary: "Plain recorded view.",
    }),
  });
  assertEquals(reading.evidenceLimits.includes("Complete sources"), false);
  assert(reading.evidenceLimits.includes("Limited evidence"));
  const unsupported = projectCompanyDigest({
    company: company({
      coverage: "unsupported",
      source_count: 3,
      summary: "Plain recorded view.",
    }),
  });
  assertEquals(unsupported.evidenceLimits.includes("Complete sources"), false);
  assert(unsupported.evidenceLimits.includes("Limited evidence"));
});

Deno.test("factor associations keep per-edge freshness, not one driver-wide stamp", () => {
  const graph: MarketInfluenceGraph = {
    nodes: [
      {
        data: { id: "driver:oil", kind: "driver", label: "Oil" },
        classes: "driver supported",
      },
      {
        data: {
          id: "family:tw",
          kind: "family",
          label: "energy",
          family: "energy",
          scopeKey: "TW",
        },
        classes: "family",
      },
      {
        data: {
          id: "family:eu",
          kind: "family",
          label: "energy",
          family: "energy",
          scopeKey: "EU",
        },
        classes: "family",
      },
    ],
    edges: [
      {
        data: {
          id: "edge-tw",
          source: "driver:oil",
          target: "family:tw",
          kind: "evidence",
          basis: "shared_source",
          tone: "supportive",
          label: "Shared evidence",
          driverPoint: "Oil rose.",
          familyPoint: "Taiwan energy earnings improved.",
          evidenceAsOf: "2026-01-01T00:00:00Z",
          evidenceValidUntil: "2026-01-02T00:00:00Z",
          evidenceStatus: "success",
          evidenceCoverage: "full",
        },
        classes: "influence-path evidence",
      },
      {
        data: {
          id: "edge-eu",
          source: "driver:oil",
          target: "family:eu",
          kind: "evidence",
          basis: "shared_source",
          tone: "supportive",
          label: "Shared evidence",
          driverPoint: "Oil rose.",
          familyPoint: "European energy earnings improved.",
          evidenceAsOf: "2026-08-23T00:00:00Z",
          evidenceValidUntil: "2099-01-01T00:00:00Z",
          evidenceStatus: "success",
          evidenceCoverage: { status: "partial" },
        },
        classes: "influence-path evidence",
      },
    ],
    scopeCount: 2,
    domainCount: 0,
    familyCount: 2,
    companyCount: 0,
    supportedPathCount: 2,
    possiblePathCount: 0,
  };
  const reading = projectFactorReading(graph, "driver:oil");
  assertEquals(reading.recorded.length, 2);
  assertEquals(
    reading.recorded.find((item) => item.targetId === "family:tw")
      ?.freshnessLabel,
    "Needs refreshing",
  );
  assertEquals(
    reading.recorded.find((item) => item.targetId === "family:eu")
      ?.freshnessLabel
      ?.includes("Partial coverage"),
    true,
  );
});

Deno.test("interactive theme labels capitalize and disambiguate collisions", () => {
  const europe = {
    id: "family:eu",
    kind: "family" as const,
    label: "energy",
    family: "energy",
    scopeKey: "EU",
  };
  const unitedStates = {
    id: "family:us",
    kind: "family" as const,
    label: "energy",
    family: "energy",
    scopeKey: "US",
  };
  const unique = {
    id: "family:tw",
    kind: "family" as const,
    label: "memory",
    family: "tw_memory",
    scopeKey: "TW",
  };
  assertEquals(
    interactiveGraphLabel(europe, [europe, unitedStates]),
    "Energy · Europe",
  );
  assertEquals(
    interactiveGraphLabel(unitedStates, [europe, unitedStates]),
    "Energy · United States",
  );
  assertEquals(interactiveGraphLabel(unique, [unique, europe]), "Memory chips");
});
