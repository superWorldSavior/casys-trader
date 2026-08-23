import { assert } from "@std/assert/assert";
import { assertEquals } from "@std/assert/equals";
import {
  type AtlasFamilyInput,
  type AtlasLayout,
  buildAtlasEvidenceLinks,
  buildAtlasFamilyThreads,
  buildMarketAtlas,
  collectScopeKeys,
  isEvidenceUnavailable,
  normalizePriority,
  possibleInfluenceLabel,
  rankDeltaSemantics,
  rankedNodeRadius,
  relatedThreadNodes,
  threadForNode,
} from "../src/lib/market-intelligence-atlas.ts";

function family(
  venue: string,
  name: string,
  extra: Partial<AtlasFamilyInput> = {},
): AtlasFamilyInput {
  return { venue, family: name, ...extra };
}

function numbersIn(value: unknown, found: number[] = []): number[] {
  if (typeof value === "number") {
    found.push(value);
    return found;
  }
  if (Array.isArray(value)) {
    for (const item of value) numbersIn(item, found);
    return found;
  }
  if (value && typeof value === "object") {
    for (const item of Object.values(value as Record<string, unknown>)) {
      numbersIn(item, found);
    }
  }
  return found;
}

function assertFiniteLayout(layout: AtlasLayout, message: string) {
  for (const value of numbersIn(layout)) {
    assert(Number.isFinite(value), `${message}: ${value}`);
  }
}

Deno.test("collectScopeKeys keeps arbitrary keys in stable current-then-family order", () => {
  const keys = collectScopeKeys(
    { nordic: {}, asean: {}, andean: {} },
    [
      family("asean", "tech"),
      family("latam", "materials"),
      family("nordic", "energy"),
      family("latam", "banks"),
      family("gcc", "logistics"),
    ],
  );
  assertEquals(keys, ["nordic", "asean", "andean", "latam", "gcc"]);
});

Deno.test("collectScopeKeys does not assume a fixed three-market set", () => {
  const keys = collectScopeKeys({ FR: {}, DE: {} }, [family("UK", "sterling")]);
  assertEquals(keys, ["FR", "DE", "UK"]);
  assertEquals(keys.includes("TW"), false);
  assertEquals(keys.includes("EU"), false);
  assertEquals(keys.includes("US"), false);
});

Deno.test("collectScopeKeys skips empty keys and stays stable across repeats", () => {
  const current = { "  ": {}, APAC: {}, "": {} };
  const families = [
    family("  ", "ghost"),
    family("APAC", "chips"),
    family("", "none"),
  ];
  assertEquals(collectScopeKeys(current, families), ["APAC"]);
  assertEquals(
    collectScopeKeys(current, families),
    collectScopeKeys(current, families),
  );
});

Deno.test("buildMarketAtlas is deterministic for the same heterogeneous input", () => {
  const input = {
    current: { SAHEL: {}, ALPINE: {} },
    families: [
      family("SAHEL", "grains", {
        rank: 2,
        candidate_count: 3,
        priority: "favored",
      }),
      family("ALPINE", "precision", { rank: 1, candidate_count: 0 }),
      family("SAHEL", "logistics", { rank: null, candidate_count: 1 }),
      family("BALTIC", "ports", {
        rank: 4,
        rank_delta: -1,
        candidate_count: 8,
      }),
    ],
  };
  const first = buildMarketAtlas(input);
  const second = buildMarketAtlas(input);
  assertEquals(first, second);
  assertEquals(first.scopes.map((scope) => scope.key), [
    "SAHEL",
    "ALPINE",
    "BALTIC",
  ]);
});

Deno.test("rank 1 sits nearer its own scope center than a worse rank in that scope", () => {
  const layout = buildMarketAtlas({
    current: { NORDIC: {}, ANDES: {} },
    families: [
      family("NORDIC", "best", { rank: 1, candidate_count: 4 }),
      family("NORDIC", "worse", { rank: 6, candidate_count: 4 }),
      family("ANDES", "other-best", { rank: 1, candidate_count: 9 }),
      family("ANDES", "other-mid", { rank: 2, candidate_count: 9 }),
    ],
  });
  const nordicBest = layout.nodes.find((node) => node.family === "best");
  const nordicWorse = layout.nodes.find((node) => node.family === "worse");
  const andesBest = layout.nodes.find((node) => node.family === "other-best");
  const andesMid = layout.nodes.find((node) => node.family === "other-mid");
  assert(nordicBest && nordicWorse && andesBest && andesMid);
  assert(nordicBest.distance < nordicWorse.distance);
  assert(andesBest.distance < andesMid.distance);
  assertEquals(nordicBest.scopeKey, "NORDIC");
  assertEquals(andesBest.scopeKey, "ANDES");
});

Deno.test("candidate_count of zero is preserved and still receives a visible ranked radius", () => {
  const layout = buildMarketAtlas({
    current: { LEVANT: {} },
    families: [
      family("LEVANT", "empty-review", { rank: 2, candidate_count: 0 }),
      family("LEVANT", "busy-review", { rank: 3, candidate_count: 16 }),
    ],
  });
  const empty = layout.nodes.find((node) => node.family === "empty-review");
  const busy = layout.nodes.find((node) => node.family === "busy-review");
  assert(empty && busy);
  assertEquals(empty.candidateCount, 0);
  assertEquals(empty.ranked, true);
  assert(empty.radius >= rankedNodeRadius(0));
  assertEquals(empty.radius, rankedNodeRadius(0));
  assert(busy.radius > empty.radius);
});

Deno.test("rank null is retained in a separate unranked band and is not treated as last", () => {
  const layout = buildMarketAtlas({
    current: { CARIBBEAN: {} },
    families: [
      family("CARIBBEAN", "read", { rank: 8, candidate_count: 2 }),
      family("CARIBBEAN", "unread", {
        rank: null,
        candidate_count: 7,
        priority: "favored",
      }),
      family("CARIBBEAN", "also-unread", { candidate_count: 1 }),
    ],
  });
  const ranked = layout.nodes.find((node) => node.family === "read");
  const unread = layout.nodes.find((node) => node.family === "unread");
  const alsoUnread = layout.nodes.find((node) => node.family === "also-unread");
  const scope = layout.scopes[0];
  assert(ranked && unread && alsoUnread && scope);
  assertEquals(layout.nodes.length, 3);
  assertEquals(unread.ranked, false);
  assertEquals(alsoUnread.ranked, false);
  assertEquals(unread.rank, null);
  assertEquals(alsoUnread.rank, null);
  assertEquals(scope.unrankedCount, 2);
  assertEquals(unread.distance, scope.unrankedRadius);
  assertEquals(alsoUnread.distance, scope.unrankedRadius);
  assert(ranked.distance < scope.unrankedRadius);
  assert(ranked.distance <= scope.rankedOuter);
});

Deno.test("rank delta semantics distinguish improving, weakening, stable, and unknown", () => {
  assertEquals(rankDeltaSemantics(2), "improving");
  assertEquals(rankDeltaSemantics(-3), "weakening");
  assertEquals(rankDeltaSemantics(0), "stable");
  assertEquals(rankDeltaSemantics(null), "unknown");
  assertEquals(rankDeltaSemantics(undefined), "unknown");
  assertEquals(rankDeltaSemantics(Number.NaN), "unknown");
  assertEquals(rankDeltaSemantics(Number.POSITIVE_INFINITY), "unknown");
  assertEquals(rankDeltaSemantics("1"), "unknown");

  const layout = buildMarketAtlas({
    families: [
      family("X", "up", { rank: 1, rank_delta: 2 }),
      family("X", "down", { rank: 2, rank_delta: -1 }),
      family("X", "same", { rank: 3, rank_delta: 0 }),
      family("X", "blank", { rank: 4 }),
    ],
  });
  assertEquals(
    layout.nodes.find((node) => node.family === "up")?.movement,
    "improving",
  );
  assertEquals(
    layout.nodes.find((node) => node.family === "down")?.movement,
    "weakening",
  );
  assertEquals(
    layout.nodes.find((node) => node.family === "same")?.movement,
    "stable",
  );
  assertEquals(
    layout.nodes.find((node) => node.family === "blank")?.movement,
    "unknown",
  );
});

Deno.test("unknown priority normalizes to neutral", () => {
  assertEquals(normalizePriority("favored"), "favored");
  assertEquals(normalizePriority("deprioritized"), "deprioritized");
  assertEquals(normalizePriority("neutral"), "neutral");
  assertEquals(normalizePriority("mystery"), "neutral");
  assertEquals(normalizePriority(null), "neutral");
  assertEquals(normalizePriority(undefined), "neutral");
  assertEquals(normalizePriority(""), "neutral");
  assertEquals(normalizePriority("FAVORED"), "favored");

  const layout = buildMarketAtlas({
    families: [
      family("Y", "plain", { rank: 1, priority: "unclear" }),
      family("Y", "preferred", { rank: 2, priority: "favored" }),
    ],
  });
  assertEquals(
    layout.nodes.find((node) => node.family === "plain")?.priority,
    "neutral",
  );
  assertEquals(
    layout.nodes.find((node) => node.family === "preferred")?.priority,
    "favored",
  );
});

Deno.test("empty input produces a finite layout with no NaN or Infinity", () => {
  const empty = buildMarketAtlas({});
  assertEquals(empty.scopes, []);
  assertEquals(empty.nodes, []);
  assertEquals(empty.links, []);
  assertFiniteLayout(empty, "empty");

  const blank = buildMarketAtlas({ current: {}, families: [] });
  assertFiniteLayout(blank, "blank");
  assertEquals(blank.nodes.length, 0);

  const scopesOnly = buildMarketAtlas({ current: { VOID: {} }, families: [] });
  assertEquals(scopesOnly.scopes.map((scope) => scope.key), ["VOID"]);
  assertEquals(scopesOnly.nodes.length, 0);
  assertEquals(scopesOnly.links.length, 1);
  assertFiniteLayout(scopesOnly, "scopes-only");
});

Deno.test("evidence unavailable covers missing, not_available, and not_reported", () => {
  assertEquals(isEvidenceUnavailable(null), true);
  assertEquals(isEvidenceUnavailable(undefined), true);
  assertEquals(isEvidenceUnavailable(""), true);
  assertEquals(isEvidenceUnavailable("missing"), true);
  assertEquals(isEvidenceUnavailable("not_available"), true);
  assertEquals(isEvidenceUnavailable("not_reported"), true);
  assertEquals(isEvidenceUnavailable("active"), false);
  assertEquals(isEvidenceUnavailable("complete"), false);
});

Deno.test("ranked node radius stays finite for zero and large candidate counts", () => {
  assert(Number.isFinite(rankedNodeRadius(0)));
  assert(Number.isFinite(rankedNodeRadius(10_000)));
  assert(rankedNodeRadius(0) > 0);
  assert(rankedNodeRadius(9) > rankedNodeRadius(0));
});

Deno.test("family threads project governed groups across arbitrary market scopes", () => {
  const layout = buildMarketAtlas({
    current: { FR: {}, ASEAN: {}, LATAM: {} },
    families: [
      family("FR", "memory", { rank: 1 }),
      family("FR", "boards", { rank: 2 }),
      family("ASEAN", "semis", { rank: 4 }),
      family("LATAM", "software", { rank: 3 }),
      family("LATAM", "banks", { rank: 1 }),
    ],
  });
  const threads = buildAtlasFamilyThreads(layout.nodes, [{
    group: "technology",
    label: "Technology & semiconductors",
    venues: {
      FR: { leader: "memory", families: [{ family: "memory" }, { family: "boards" }] },
      ASEAN: { leader: "semis", families: [{ family: "semis" }] },
      LATAM: { leader: "software", families: [{ family: "software" }, { family: "missing" }] },
    },
  }]);

  assertEquals(threads.length, 1);
  assertEquals(threads[0].nodeIds, ["FR::memory", "FR::boards", "ASEAN::semis", "LATAM::software"]);
  assertEquals(threads[0].leaderIds, ["FR::memory", "ASEAN::semis", "LATAM::software"]);
  assertEquals(threads[0].possibleInfluence, "AI and compute investment");
  assertEquals(threadForNode(threads, "LATAM::banks"), null);
  assertEquals(threadForNode(threads, "ASEAN::semis")?.key, "technology");
});

Deno.test("thread relations keep close peers and one leader per other market", () => {
  const layout = buildMarketAtlas({
    current: { ALPHA: {}, BETA: {}, GAMMA: {} },
    families: [
      family("ALPHA", "a1", { rank: 1 }),
      family("ALPHA", "a2", { rank: 2 }),
      family("ALPHA", "a3", { rank: 3 }),
      family("ALPHA", "a4", { rank: 4 }),
      family("ALPHA", "a5", { rank: 5 }),
      family("BETA", "b1", { rank: 7 }),
      family("BETA", "b2", { rank: 2 }),
      family("GAMMA", "g1", { rank: 3 }),
    ],
  });
  const thread = buildAtlasFamilyThreads(layout.nodes, [{
    group: "materials",
    label: "Materials",
    venues: {
      ALPHA: { leader: "a1", families: ["a1", "a2", "a3", "a4", "a5"].map((name) => ({ family: name })) },
      BETA: { leader: "b1", families: [{ family: "b1" }, { family: "b2" }] },
      GAMMA: { leader: "g1", families: [{ family: "g1" }] },
    },
  }])[0];
  const selected = layout.nodes.find((node) => node.id === "ALPHA::a1") ?? null;
  const relations = relatedThreadNodes(thread, layout.nodes, selected, 6);

  assertEquals(relations.map((relation) => relation.nodeId), [
    "ALPHA::a2",
    "ALPHA::a3",
    "ALPHA::a4",
    "BETA::b1",
    "GAMMA::g1",
  ]);
  assertEquals(relations.map((relation) => relation.crossScope), [false, false, false, true, true]);
});

Deno.test("possible influence labels stay heuristic and have a safe fallback", () => {
  assertEquals(possibleInfluenceLabel("energy", "Energy"), "Oil and gas prices");
  assertEquals(possibleInfluenceLabel("real_estate", "Real estate"), "Rates and financing");
  assertEquals(possibleInfluenceLabel("novel", "Unmapped domain"), "Broader market conditions");
});

Deno.test("evidence links prefer shared sources, then symbols, then explicit topic matches", () => {
  const node = buildMarketAtlas({
    current: { X: {} },
    families: [family("X", "energy", { rank: 1, summary: "Oil and inflation remain the main inputs." })],
  }).nodes[0];
  const links = buildAtlasEvidenceLinks(node, {
    zones: {
      oil: [
        { point: "Operational oil placeholder", source_refs: ["ignored"], is_operational: true },
        { point: "Brent crude is elevated.", source_refs: ["oil:1"], symbols: ["AAA"] },
      ],
      rates: [{ point: "Rates remain restrictive.", symbols: ["AAA"] }],
      inflation: [{ point: "Inflation reflects higher input prices." }],
    },
    families: {
      energy: [{
        point: "Oil supports earnings while inflation raises costs.",
        direction: "bullish",
        source_refs: ["oil:1"],
        symbols: ["AAA"],
      }],
    },
  });

  assertEquals(links.map((link) => [link.driverKey, link.kind, link.tone]), [
    ["oil", "shared_source", "supportive"],
    ["rates", "shared_symbol", "supportive"],
    ["inflation", "topic_match", "supportive"],
  ]);
});

Deno.test("opposing family observations make an evidence path mixed", () => {
  const node = buildMarketAtlas({
    families: [family("X", "materials", { rank: 1 })],
  }).nodes[0];
  const links = buildAtlasEvidenceLinks(node, {
    zones: { gold: [{ point: "Gold moved higher.", source_refs: ["gold:1"] }] },
    families: {
      materials: [
        { point: "Gold supports miners.", direction: "bullish", source_refs: ["gold:1"] },
        { point: "Costs pressure margins.", direction: "bearish", source_refs: ["gold:1"] },
      ],
    },
  });
  assertEquals(links[0]?.tone, "mixed");
});
