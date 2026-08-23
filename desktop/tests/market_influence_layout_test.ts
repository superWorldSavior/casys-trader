import { assert } from "@std/assert/assert";
import { assertEquals } from "@std/assert/equals";
import { assertGreater } from "@std/assert/greater";
import type {
  AtlasEvidenceLink,
  AtlasFamilyInput,
  AtlasFamilyThreadInput,
} from "../src/lib/market-intelligence-atlas.ts";
import {
  buildAtlasFamilyThreads,
  buildMarketAtlas,
} from "../src/lib/market-intelligence-atlas.ts";
import type {
  InfluenceEdgeBasis,
  InfluenceGraphEdge,
  InfluenceGraphNode,
  MarketInfluenceGraph,
} from "../src/lib/market-influence-network.ts";
import { buildMarketInfluenceGraph } from "../src/lib/market-influence-network.ts";
import {
  clusterIdForNode,
  explorerFallbackForFocus,
  explorerGroups,
  FAMILY_HULL_PADDING,
  focusedPath,
  graphHulls,
  groupEnvelope,
  groupGraph,
  LENS_HULL_PADDING,
  linkForceParams,
  MAX_LABEL_LEADER_DISTANCE,
  nodeLabelVisible,
  placeGraphLabels,
  presentGraph,
  seedNodePositions,
  separateGroupEnvelopes,
  separateSiblingGroupEnvelopes,
} from "../src/lib/market-influence-layout.ts";

function family(
  venue: string,
  name: string,
  extra: Partial<AtlasFamilyInput> = {},
): AtlasFamilyInput {
  return { venue, family: name, ...extra };
}

function atlasGraph(extraEvidence: AtlasEvidenceLink[] = []) {
  const comparisons: AtlasFamilyThreadInput[] = [
    {
      group: "energy",
      label: "Energy",
      venues: {
        FR: {
          leader: "fr_energy",
          families: [{ family: "fr_energy" }, { family: "fr_utilities" }],
        },
        ASEAN: {
          leader: "asean_energy",
          families: [{ family: "asean_energy" }],
        },
        LATAM: {
          leader: "latam_energy",
          families: [{ family: "latam_energy" }],
        },
      },
    },
    {
      group: "materials",
      label: "Materials",
      venues: {
        LATAM: {
          leader: "latam_materials",
          families: [{ family: "latam_materials" }],
        },
      },
    },
  ];
  const layout = buildMarketAtlas({
    current: { FR: {}, ASEAN: {}, LATAM: {} },
    families: [
      family("FR", "fr_energy", {
        rank: 1,
        priority: "favored",
        symbols: ["ENGI.PA", "TTE.PA"],
      }),
      family("FR", "fr_utilities", { rank: 4, symbols: ["ENGI.PA"] }),
      family("ASEAN", "asean_energy", { rank: 2, symbols: ["PTT.BK"] }),
      family("LATAM", "latam_energy", {
        rank: 3,
        priority: "deprioritized",
        symbols: ["PBR"],
      }),
    ],
    comparisons,
  });
  const threads = buildAtlasFamilyThreads(layout.nodes, comparisons);
  const evidence: AtlasEvidenceLink[] = [
    {
      driverKey: "oil",
      driverLabel: "Oil",
      kind: "shared_source",
      tone: "supportive",
      driverPoint: "Oil rose.",
      familyPoint: "Energy earnings improved.",
    },
    ...extraEvidence,
  ];
  return buildMarketInfluenceGraph({
    nodes: layout.nodes,
    threads,
    evidenceByNode: [{ nodeId: "FR::fr_energy", links: evidence }],
    companyNames: {
      "ENGI.PA": "Engie",
      "TTE.PA": "TotalEnergies",
      "PTT.BK": "PTT",
      PBR: "Petrobras",
    },
  });
}

function node(
  id: string,
  kind: InfluenceGraphNode["data"]["kind"],
  extra: Partial<InfluenceGraphNode["data"]> = {},
): InfluenceGraphNode {
  return {
    data: { id, kind, label: id, ...extra },
    classes: kind,
  };
}

function edge(
  id: string,
  source: string,
  target: string,
  basis: InfluenceEdgeBasis,
  extra: Partial<InfluenceGraphEdge["data"]> = {},
): InfluenceGraphEdge {
  return {
    data: {
      id,
      source,
      target,
      kind: "membership",
      basis,
      tone: "context",
      label: basis,
      ...extra,
    },
    classes: basis,
  };
}

function handmadeGraph(): MarketInfluenceGraph {
  return {
    nodes: [
      node("market:fr", "market", { scopeKey: "FR" }),
      node("market:tw", "market", { scopeKey: "TW" }),
      node("domain:energy", "domain", { domainKey: "energy" }),
      node("domain:banks", "domain", { domainKey: "banks" }),
      node("family:fr-energy", "family", {
        family: "fr_energy",
        scopeKey: "FR",
      }),
      node("family:fr-banks", "family", { family: "fr_banks", scopeKey: "FR" }),
      node("family:tw-energy", "family", {
        family: "tw_energy",
        scopeKey: "TW",
      }),
      node("company:unique", "company", { symbol: "TTE" }),
      node("company:shared-market", "company", { symbol: "ENGI" }),
      node("company:cross-market", "company", { symbol: "ACME" }),
      node("driver:oil-prices", "driver"),
      node("driver:rates", "driver"),
      node("driver:evidence-oil", "driver"),
    ],
    edges: [
      edge("m1", "market:fr", "family:fr-energy", "market_membership"),
      edge("m2", "market:fr", "family:fr-banks", "market_membership"),
      edge("m3", "market:tw", "family:tw-energy", "market_membership"),
      edge("d1", "domain:energy", "family:fr-energy", "governed_domain"),
      edge("d2", "domain:energy", "family:tw-energy", "governed_domain"),
      edge("d3", "domain:banks", "family:fr-banks", "governed_domain"),
      edge("h1", "driver:oil-prices", "domain:energy", "heuristic_domain"),
      edge("h2", "driver:rates", "domain:banks", "heuristic_domain"),
      edge(
        "e1",
        "driver:evidence-oil",
        "family:fr-energy",
        "shared_source",
        { kind: "evidence", tone: "supportive", label: "Shared evidence" },
      ),
      edge(
        "e2",
        "driver:evidence-oil",
        "family:fr-banks",
        "shared_source",
        { kind: "evidence", tone: "headwind", label: "Shared evidence" },
      ),
      edge("c1", "family:fr-energy", "company:unique", "family_membership"),
      edge(
        "c2",
        "family:fr-energy",
        "company:shared-market",
        "family_membership",
      ),
      edge(
        "c3",
        "family:fr-banks",
        "company:shared-market",
        "family_membership",
      ),
      edge(
        "c4",
        "family:fr-energy",
        "company:cross-market",
        "family_membership",
      ),
      edge(
        "c5",
        "family:tw-energy",
        "company:cross-market",
        "family_membership",
      ),
    ],
    scopeCount: 2,
    domainCount: 2,
    familyCount: 3,
    companyCount: 3,
    supportedPathCount: 2,
    possiblePathCount: 2,
  };
}

function hullMembers(
  graph: MarketInfluenceGraph,
  lens: "domain" | "macro" | "market",
  hullId: string,
): string[] {
  const hull = graphHulls(graph, lens).find((item) => item.id === hullId);
  return hull?.memberIds.slice().sort() ?? [];
}

function applyEnvelopeMoves(
  envelopes: Array<{
    id: string;
    x: number;
    y: number;
    halfWidth: number;
    halfHeight: number;
  }>,
  moves: Array<{ id: string; dx: number; dy: number }>,
) {
  const moved = envelopes.map((item) => ({ ...item }));
  const byId = new Map(moved.map((item) => [item.id, item]));
  for (const move of moves) {
    const item = byId.get(move.id);
    if (!item) continue;
    item.x += move.dx;
    item.y += move.dy;
  }
  return moved;
}

function envelopesOverlap(
  left: { x: number; y: number; halfWidth: number; halfHeight: number },
  right: { x: number; y: number; halfWidth: number; halfHeight: number },
): boolean {
  const overlapX = left.halfWidth + right.halfWidth -
    Math.abs(left.x - right.x);
  const overlapY = left.halfHeight + right.halfHeight -
    Math.abs(left.y - right.y);
  return overlapX > 1e-6 && overlapY > 1e-6;
}

function familyHullEnvelopes(
  graph: MarketInfluenceGraph,
  lens: "domain" | "macro" | "market",
  positions: Record<string, { x: number; y: number; radius: number }>,
) {
  const grouping = groupGraph(graph, lens);
  const envelopes: Array<{
    id: string;
    parentId: string;
    x: number;
    y: number;
    halfWidth: number;
    halfHeight: number;
  }> = [];
  for (const hull of graphHulls(graph, lens)) {
    if (hull.kind !== "family") continue;
    const members = hull.memberIds.flatMap((id) => {
      const position = positions[id];
      return position ? [position] : [];
    });
    const envelope = groupEnvelope(members, FAMILY_HULL_PADDING);
    if (!envelope) continue;
    envelopes.push({
      id: hull.groupId,
      parentId: grouping.groupByFamily.get(hull.groupId) ?? "other",
      ...envelope,
    });
  }
  return envelopes;
}

function labelRequest(
  id: string,
  text: string,
  priority: "selected" | "hovered" | "path" | "lens" | "market" | "secondary",
) {
  return {
    id,
    text,
    x: 80,
    y: 60,
    nodeRadius: 8,
    fontSize: 11,
    priority,
  };
}

function labelBoxesOverlap(
  left: { left: number; top: number; width: number; height: number },
  right: { left: number; top: number; width: number; height: number },
): boolean {
  return left.left < right.left + right.width &&
    left.left + left.width > right.left &&
    left.top < right.top + right.height &&
    left.top + left.height > right.top;
}

Deno.test("company size is uniform because reach is not a company metric", () => {
  const graph = atlasGraph();
  const presented = presentGraph(graph);
  const companies = graph.nodes.filter((item) => item.data.kind === "company");
  const radii = companies.map((item) => presented.radiusById.get(item.data.id));
  const weights = companies.map((item) =>
    presented.weightById.get(item.data.id)
  );

  assertEquals(new Set(radii).size, 1);
  assertEquals(new Set(weights).size, 1);
  const engie = companies.find((item) => item.data.symbol === "ENGI.PA");
  const total = companies.find((item) => item.data.symbol === "TTE.PA");
  assert(engie && total);
  assertGreater(
    graph.edges.filter((item) => item.data.target === engie.data.id).length,
    graph.edges.filter((item) => item.data.target === total.data.id).length,
  );
  assertEquals(
    presented.radiusById.get(engie.data.id),
    presented.radiusById.get(total.data.id),
  );
});

Deno.test("semantic node size is bounded and scales with degree within kind", () => {
  const graph = atlasGraph();
  const presented = presentGraph(graph);
  const markets = graph.nodes.filter((item) => item.data.kind === "market");
  const byDegree = markets.map((item) => ({
    id: item.data.id,
    degree:
      graph.edges.filter((edge) =>
        edge.data.source === item.data.id || edge.data.target === item.data.id
      ).length,
    radius: presented.radiusById.get(item.data.id) ?? 0,
  })).sort((left, right) => left.degree - right.degree);

  assertGreater(byDegree.length, 1);
  assertGreater(byDegree.at(-1)!.radius, byDegree[0].radius);
  for (const item of graph.nodes) {
    const radius = presented.radiusById.get(item.data.id) ?? 0;
    if (item.data.kind === "company") {
      assert(radius < 8);
    } else {
      assert(radius >= 8);
      assert(radius <= 24);
    }
  }
});

Deno.test("family hulls keep uniquely owned companies and leave shared companies unassigned", () => {
  const graph = handmadeGraph();
  const grouping = groupGraph(graph, "market");
  const familyHulls = graphHulls(graph, "market").filter((item) =>
    item.kind === "family"
  );

  assertEquals(
    grouping.uniqueFamilyByCompany.get("company:unique"),
    "family:fr-energy",
  );
  assertEquals(
    grouping.uniqueFamilyByCompany.has("company:shared-market"),
    false,
  );
  assertEquals(
    grouping.uniqueFamilyByCompany.has("company:cross-market"),
    false,
  );

  assertEquals(
    familyHulls.find((item) => item.groupId === "family:fr-energy")?.memberIds
      .slice()
      .sort(),
    ["company:unique", "family:fr-energy"],
  );
  assertEquals(
    familyHulls.find((item) => item.groupId === "family:fr-banks")?.memberIds,
    ["family:fr-banks"],
  );
  assert(
    familyHulls.every((item) =>
      !item.memberIds.includes("company:shared-market")
    ),
  );
  assert(
    familyHulls.every((item) =>
      !item.memberIds.includes("company:cross-market")
    ),
  );
});

Deno.test("markets lens hulls group by scope without inventing company ownership", () => {
  const graph = handmadeGraph();
  const grouping = groupGraph(graph, "market");

  assertEquals(grouping.groupByFamily.get("family:fr-energy"), "market:fr");
  assertEquals(grouping.groupByFamily.get("family:fr-banks"), "market:fr");
  assertEquals(grouping.groupByFamily.get("family:tw-energy"), "market:tw");

  assertEquals(
    hullMembers(graph, "market", "lens:market:market:fr").sort(),
    [
      "company:shared-market",
      "company:unique",
      "family:fr-banks",
      "family:fr-energy",
      "market:fr",
    ],
  );
  assertEquals(
    hullMembers(graph, "market", "lens:market:market:tw").sort(),
    ["family:tw-energy", "market:tw"],
  );
  assert(
    !hullMembers(graph, "market", "lens:market:market:fr").includes(
      "company:cross-market",
    ),
  );
  assert(
    !hullMembers(graph, "market", "lens:market:market:tw").includes(
      "company:cross-market",
    ),
  );
});

Deno.test("domains lens hulls group by governed domain", () => {
  const graph = handmadeGraph();
  const grouping = groupGraph(graph, "domain");

  assertEquals(grouping.groupByFamily.get("family:fr-energy"), "domain:energy");
  assertEquals(grouping.groupByFamily.get("family:tw-energy"), "domain:energy");
  assertEquals(grouping.groupByFamily.get("family:fr-banks"), "domain:banks");
  assertEquals(
    hullMembers(graph, "domain", "lens:domain:domain:energy").sort(),
    [
      "company:cross-market",
      "company:unique",
      "domain:energy",
      "family:fr-energy",
      "family:tw-energy",
    ],
  );
});

Deno.test("ambiguous taxonomy stays shared instead of choosing the last group", () => {
  const graph = handmadeGraph();
  graph.edges.push(
    edge("d4", "domain:banks", "family:fr-energy", "governed_domain"),
  );

  assertEquals(
    groupGraph(graph, "domain").groupByFamily.get("family:fr-energy"),
    "other",
  );
  assertEquals(
    groupGraph(graph, "macro").groupByFamily.get("family:fr-energy"),
    "other",
  );
  assertEquals(
    groupGraph(graph, "market").groupByFamily.get("family:fr-energy"),
    "market:fr",
  );
});

Deno.test("macro lens groups by the explicit heuristic domain driver only", () => {
  const graph = handmadeGraph();
  const grouping = groupGraph(graph, "macro");

  assertEquals(
    grouping.groupByFamily.get("family:fr-energy"),
    "driver:oil-prices",
  );
  assertEquals(
    grouping.groupByFamily.get("family:tw-energy"),
    "driver:oil-prices",
  );
  assertEquals(grouping.groupByFamily.get("family:fr-banks"), "driver:rates");
  assertEquals(
    grouping.groupByFamily.get("family:fr-energy") === "driver:evidence-oil",
    false,
  );
  assertEquals(
    hullMembers(graph, "macro", "lens:macro:driver:oil-prices").includes(
      "driver:evidence-oil",
    ),
    false,
  );
  assertEquals(
    hullMembers(graph, "macro", "lens:macro:driver:oil-prices").includes(
      "driver:oil-prices",
    ),
    true,
  );
});

Deno.test("macro grouping does not pick a dominant driver from many-to-many evidence", () => {
  const graph = atlasGraph([{
    driverKey: "liquidity",
    driverLabel: "Global liquidity",
    kind: "shared_source",
    tone: "headwind",
    driverPoint: "Liquidity tightened.",
    familyPoint: "Energy stayed preferred.",
  }]);
  const familyId = graph.nodes.find((item) =>
    item.data.atlasNodeId === "FR::fr_energy"
  )?.data.id;
  const energyDomain = graph.nodes.find((item) =>
    item.data.kind === "domain" && item.data.domainKey === "energy"
  )?.data.id;
  assert(familyId && energyDomain);
  const heuristic = graph.edges.find((item) =>
    item.data.basis === "heuristic_domain" && item.data.target === energyDomain
  );
  const evidenceDrivers = graph.edges
    .filter((item) =>
      item.data.target === familyId && item.data.kind === "evidence"
    )
    .map((item) => item.data.source);
  assert(heuristic);
  assertGreater(evidenceDrivers.length, 1);
  assertEquals(
    groupGraph(graph, "macro").groupByFamily.get(familyId),
    heuristic.data.source,
  );
  assertEquals(
    evidenceDrivers.includes(heuristic.data.source),
    false,
  );
});

Deno.test("switching lens keeps every semantic node and only changes grouping", () => {
  const graph = handmadeGraph();
  const nodeIds = graph.nodes.map((item) => item.data.id).sort();
  for (const lens of ["market", "domain", "macro"] as const) {
    const hulls = graphHulls(graph, lens);
    assertEquals(
      graph.nodes.map((item) => item.data.id).sort(),
      nodeIds,
    );
    assert(hulls.some((item) => item.kind === "lens"));
    assert(hulls.some((item) => item.kind === "family"));
    assert(
      hulls.filter((item) => item.kind === "family").every((item) =>
        !item.memberIds.includes("company:cross-market")
      ),
    );
  }
  assertEquals(
    clusterIdForNode(graph, "market", "family:fr-energy"),
    "market:fr",
  );
  assertEquals(
    clusterIdForNode(graph, "domain", "family:fr-energy"),
    "domain:energy",
  );
  assertEquals(
    clusterIdForNode(graph, "macro", "family:fr-energy"),
    "driver:oil-prices",
  );
  assertEquals(clusterIdForNode(graph, "market", "company:cross-market"), null);
  assertEquals(
    clusterIdForNode(graph, "domain", "company:cross-market"),
    "domain:energy",
  );
  assertEquals(
    clusterIdForNode(graph, "market", "company:shared-market"),
    "market:fr",
  );
});

Deno.test("link forces are strongest for company-to-family and weakest for influence", () => {
  const family = linkForceParams("family_membership");
  const domain = linkForceParams("governed_domain");
  const market = linkForceParams("market_membership");
  const heuristic = linkForceParams("heuristic_domain");
  const evidence = linkForceParams("shared_source");
  const compactFamily = linkForceParams("family_membership", true);

  assertGreater(family.strength, domain.strength);
  assertGreater(domain.strength, market.strength);
  assertGreater(market.strength, heuristic.strength);
  assertEquals(heuristic.strength, evidence.strength);
  assertGreater(family.distance, 0);
  assertGreater(domain.distance, family.distance);
  assertGreater(market.distance, domain.distance);
  assertGreater(heuristic.distance, market.distance);
  assertGreater(family.distance, compactFamily.distance);
});

Deno.test("focus lights connected paths without dropping the rest of the map", () => {
  const graph = handmadeGraph();
  const familyFocus = focusedPath(graph, "family:fr-energy");
  const companyFocus = focusedPath(graph, "company:unique");
  const marketFocus = focusedPath(graph, "market:fr");

  assert(familyFocus.nodeIds.has("family:fr-energy"));
  assert(familyFocus.nodeIds.has("company:unique"));
  assert(familyFocus.nodeIds.has("market:fr"));
  assert(familyFocus.nodeIds.has("domain:energy"));
  assert(familyFocus.nodeIds.has("driver:oil-prices"));
  assert(familyFocus.nodeIds.has("driver:evidence-oil"));
  assertEquals(familyFocus.nodeIds.has("family:tw-energy"), false);
  assertEquals(graph.nodes.length > familyFocus.nodeIds.size, true);

  assert(companyFocus.nodeIds.has("family:fr-energy"));
  assertEquals(companyFocus.nodeIds.has("company:shared-market"), false);

  assert(marketFocus.nodeIds.has("family:fr-banks"));
  assert(marketFocus.nodeIds.has("company:shared-market"));
  assertEquals(marketFocus.nodeIds.has("family:tw-energy"), false);
  assertEquals(focusedPath(graph, null).nodeIds.size, 0);
});

Deno.test("driver tone follows supported edge majority and stays amber when mixed", () => {
  const graph = handmadeGraph();
  const presented = presentGraph(graph);
  assertEquals(presented.extraClassById.get("driver:evidence-oil"), undefined);
  assertEquals(presented.extraClassById.get("driver:oil-prices"), undefined);

  const supportive = structuredClone(graph) as MarketInfluenceGraph;
  supportive.edges = supportive.edges.map((item) =>
    item.data.source === "driver:evidence-oil" &&
      item.data.target === "family:fr-banks"
      ? {
        ...item,
        data: { ...item.data, tone: "supportive" },
      }
      : item
  );
  assertEquals(
    presentGraph(supportive).extraClassById.get("driver:evidence-oil"),
    "signal-supportive",
  );
});

Deno.test("initial seeds are deterministic and not a fixed column layout", () => {
  const ids = ["a", "b", "c", "d", "e", "f"];
  const first = seedNodePositions(ids, 800, 640);
  const second = seedNodePositions(ids, 800, 640);
  assertEquals(first, second);
  const xs = ids.map((id) => first.get(id)?.x ?? 0);
  const ys = ids.map((id) => first.get(id)?.y ?? 0);
  assertEquals(new Set(xs).size, ids.length);
  assertEquals(new Set(ys).size, ids.length);
  const minX = Math.min(...xs);
  const maxX = Math.max(...xs);
  const minY = Math.min(...ys);
  const maxY = Math.max(...ys);
  assertGreater(maxX - minX, 40);
  assertGreater(maxY - minY, 40);
});

Deno.test("held companies keep a uniform radius and a held class", () => {
  const graph = atlasGraph();
  const withHoldings = buildMarketInfluenceGraph({
    nodes: buildMarketAtlas({
      current: { FR: {}, ASEAN: {}, LATAM: {} },
      families: [
        family("FR", "fr_energy", {
          rank: 1,
          symbols: ["ENGI.PA", "TTE.PA"],
        }),
      ],
    }).nodes,
    threads: [],
    companyNames: { "ENGI.PA": "Engie", "TTE.PA": "TotalEnergies" },
    holdings: [{ symbol: "ENGI.PA", quantity: 3, last_price: 12, fx_rate: 1 }],
  });
  const presented = presentGraph(withHoldings);
  const engie = withHoldings.nodes.find((item) =>
    item.data.symbol === "ENGI.PA"
  );
  const total = withHoldings.nodes.find((item) =>
    item.data.symbol === "TTE.PA"
  );
  assert(engie && total);
  assert(engie.classes.includes("held"));
  assertEquals(total.classes.includes("held"), false);
  assertEquals(
    presented.radiusById.get(engie.data.id),
    presented.radiusById.get(total.data.id),
  );
  assertEquals(
    presented.radiusById.get(engie.data.id),
    presentGraph(graph).radiusById.get(
      graph.nodes.find((item) => item.data.symbol === "ENGI.PA")!.data.id,
    ),
  );
});

Deno.test("groupEnvelope includes node radius and the outer lens-hull padding", () => {
  const single = groupEnvelope(
    [{ x: 10, y: -4, radius: 10 }],
    LENS_HULL_PADDING,
  );
  assert(single);
  assertEquals(single.x, 10);
  assertEquals(single.y, -4);
  assertEquals(single.halfWidth, 10 + LENS_HULL_PADDING);
  assertEquals(single.halfHeight, 10 + LENS_HULL_PADDING);

  const pair = groupEnvelope([
    { x: 0, y: 0, radius: 10 },
    { x: 20, y: 0, radius: 10 },
  ], LENS_HULL_PADDING);
  assert(pair);
  assertEquals(pair.x, 10);
  assertEquals(pair.halfWidth, 10 + 10 + LENS_HULL_PADDING);
  assertEquals(pair.halfHeight, 10 + LENS_HULL_PADDING);
});

Deno.test("separateGroupEnvelopes is deterministic and clears covering group overlap", () => {
  const envelopes = [
    {
      id: "real-estate",
      x: 120,
      y: 80,
      halfWidth: 90,
      halfHeight: 70,
    },
    {
      id: "utilities",
      x: 130,
      y: 85,
      halfWidth: 40,
      halfHeight: 36,
    },
    {
      id: "energy",
      x: 118,
      y: 78,
      halfWidth: 55,
      halfHeight: 48,
    },
  ];
  const first = separateGroupEnvelopes(envelopes, { passes: 6 });
  const second = separateGroupEnvelopes(envelopes, { passes: 6 });
  assertEquals(first, second);

  const moved = applyEnvelopeMoves(envelopes, first);
  for (let index = 0; index < moved.length; index += 1) {
    for (let other = index + 1; other < moved.length; other += 1) {
      assertEquals(envelopesOverlap(moved[index], moved[other]), false);
    }
  }
});

Deno.test("separateGroupEnvelopes resolves the overlap on the axis of least penetration", () => {
  const left = {
    id: "a",
    x: 0,
    y: 0,
    halfWidth: 40,
    halfHeight: 40,
  };
  const right = {
    id: "b",
    x: 10,
    y: 0,
    halfWidth: 40,
    halfHeight: 40,
  };
  const moves = Object.fromEntries(
    separateGroupEnvelopes([left, right], { passes: 1 }).map((item) => [
      item.id,
      item,
    ]),
  );
  assert(Math.abs(moves.a.dx) > 0);
  assertEquals(moves.a.dy, 0);
  assertEquals(moves.b.dy, 0);
  assert(moves.a.dx < 0);
  assert(moves.b.dx > 0);
});

Deno.test("sibling family envelopes inside one market parent separate", () => {
  assertEquals(FAMILY_HULL_PADDING, 14);
  const graph = handmadeGraph();
  const envelopes = familyHullEnvelopes(graph, "market", {
    "family:fr-energy": { x: 120, y: 80, radius: 12 },
    "company:unique": { x: 126, y: 82, radius: 4.25 },
    "family:fr-banks": { x: 128, y: 84, radius: 11 },
    "family:tw-energy": { x: 400, y: 80, radius: 11 },
  });
  const frEnergy = envelopes.find((item) => item.id === "family:fr-energy");
  const frBanks = envelopes.find((item) => item.id === "family:fr-banks");
  assert(frEnergy && frBanks);
  assertEquals(frEnergy.parentId, "market:fr");
  assertEquals(frBanks.parentId, "market:fr");
  assertEquals(envelopesOverlap(frEnergy, frBanks), true);

  const first = separateSiblingGroupEnvelopes(envelopes, { passes: 6 });
  const second = separateSiblingGroupEnvelopes(envelopes, { passes: 6 });
  assertEquals(first, second);

  const moved = applyEnvelopeMoves(envelopes, first);
  const movedFrEnergy = moved.find((item) => item.id === "family:fr-energy");
  const movedFrBanks = moved.find((item) => item.id === "family:fr-banks");
  assert(movedFrEnergy && movedFrBanks);
  assertEquals(envelopesOverlap(movedFrEnergy, movedFrBanks), false);
});

Deno.test("sibling family envelopes inside one domain parent separate", () => {
  const graph = handmadeGraph();
  const envelopes = familyHullEnvelopes(graph, "domain", {
    "family:fr-energy": { x: 40, y: 30, radius: 12 },
    "company:unique": { x: 46, y: 32, radius: 4.25 },
    "family:tw-energy": { x: 48, y: 34, radius: 11 },
    "family:fr-banks": { x: 320, y: 30, radius: 11 },
  });
  const frEnergy = envelopes.find((item) => item.id === "family:fr-energy");
  const twEnergy = envelopes.find((item) => item.id === "family:tw-energy");
  assert(frEnergy && twEnergy);
  assertEquals(frEnergy.parentId, "domain:energy");
  assertEquals(twEnergy.parentId, "domain:energy");
  assertEquals(envelopesOverlap(frEnergy, twEnergy), true);

  const moved = applyEnvelopeMoves(
    envelopes,
    separateSiblingGroupEnvelopes(envelopes, { passes: 6 }),
  );
  const movedFrEnergy = moved.find((item) => item.id === "family:fr-energy");
  const movedTwEnergy = moved.find((item) => item.id === "family:tw-energy");
  assert(movedFrEnergy && movedTwEnergy);
  assertEquals(envelopesOverlap(movedFrEnergy, movedTwEnergy), false);
});

Deno.test("family envelopes with different parents are not pushed by the inner resolver", () => {
  const graph = handmadeGraph();
  const envelopes = familyHullEnvelopes(graph, "market", {
    "family:fr-energy": { x: 100, y: 80, radius: 12 },
    "company:unique": { x: 106, y: 82, radius: 4.25 },
    "family:tw-energy": { x: 108, y: 84, radius: 11 },
    "family:fr-banks": { x: 400, y: 80, radius: 11 },
  });
  const frEnergy = envelopes.find((item) => item.id === "family:fr-energy");
  const twEnergy = envelopes.find((item) => item.id === "family:tw-energy");
  assert(frEnergy && twEnergy);
  assertEquals(frEnergy.parentId, "market:fr");
  assertEquals(twEnergy.parentId, "market:tw");
  assertEquals(envelopesOverlap(frEnergy, twEnergy), true);

  const moves = Object.fromEntries(
    separateSiblingGroupEnvelopes(envelopes, { passes: 6 }).map((item) => [
      item.id,
      item,
    ]),
  );
  assertEquals(moves["family:fr-energy"]?.dx ?? 0, 0);
  assertEquals(moves["family:fr-energy"]?.dy ?? 0, 0);
  assertEquals(moves["family:tw-energy"]?.dx ?? 0, 0);
  assertEquals(moves["family:tw-energy"]?.dy ?? 0, 0);

  const moved = applyEnvelopeMoves(envelopes, Object.values(moves));
  const movedFrEnergy = moved.find((item) => item.id === "family:fr-energy");
  const movedTwEnergy = moved.find((item) => item.id === "family:tw-energy");
  assert(movedFrEnergy && movedTwEnergy);
  assertEquals(envelopesOverlap(movedFrEnergy, movedTwEnergy), true);
});

Deno.test("a fixed family envelope stays put and the free sibling takes the full displacement", () => {
  const left = {
    id: "family:fr-energy",
    parentId: "market:fr",
    x: 0,
    y: 0,
    halfWidth: 40,
    halfHeight: 40,
  };
  const right = {
    id: "family:fr-banks",
    parentId: "market:fr",
    x: 10,
    y: 0,
    halfWidth: 40,
    halfHeight: 40,
  };
  const split = Object.fromEntries(
    separateSiblingGroupEnvelopes([left, right], { passes: 1 }).map((item) => [
      item.id,
      item,
    ]),
  );
  const pinned = Object.fromEntries(
    separateSiblingGroupEnvelopes([left, right], {
      passes: 1,
      fixedIds: new Set(["family:fr-energy"]),
    }).map((item) => [item.id, item]),
  );

  assertEquals(pinned["family:fr-energy"].dx, 0);
  assertEquals(pinned["family:fr-energy"].dy, 0);
  assertEquals(pinned["family:fr-banks"].dy, 0);
  assertEquals(
    pinned["family:fr-banks"].dx,
    split["family:fr-banks"].dx - split["family:fr-energy"].dx,
  );
  assert(pinned["family:fr-banks"].dx > split["family:fr-banks"].dx);

  const bothFixed = separateSiblingGroupEnvelopes([left, right], {
    passes: 1,
    fixedIds: new Set(["family:fr-energy", "family:fr-banks"]),
  });
  assert(bothFixed.every((item) => item.dx === 0 && item.dy === 0));

  const moved = applyEnvelopeMoves(
    [left, right],
    separateSiblingGroupEnvelopes([left, right], {
      passes: 6,
      fixedIds: new Set(["family:fr-energy"]),
    }),
  );
  assertEquals(moved[0].x, left.x);
  assertEquals(moved[0].y, left.y);
  assertEquals(envelopesOverlap(moved[0], moved[1]), false);
});

Deno.test("placeGraphLabels never accepts overlapping boxes and is deterministic", () => {
  const requests = [
    labelRequest("selected", "Selected company", "selected"),
    labelRequest("hovered", "Hovered theme", "hovered"),
    labelRequest("path", "Path market", "path"),
    labelRequest("lens", "Energy", "lens"),
    labelRequest("market", "France", "market"),
    labelRequest("sec-a", "Secondary A", "secondary"),
    labelRequest("sec-b", "Secondary B", "secondary"),
    labelRequest("sec-c", "Secondary C", "secondary"),
  ];
  const first = placeGraphLabels(requests);
  const second = placeGraphLabels(requests);
  assertEquals(first, second);

  const visible = first.filter((item) => item.visible);
  assert(visible.length >= 3);
  for (let index = 0; index < visible.length; index += 1) {
    for (let other = index + 1; other < visible.length; other += 1) {
      assertEquals(labelBoxesOverlap(visible[index], visible[other]), false);
    }
  }
  assertEquals(first.find((item) => item.id === "selected")?.visible, true);
  assertEquals(first.find((item) => item.id === "hovered")?.visible, true);
  assertEquals(first.find((item) => item.id === "path")?.visible, true);
});

Deno.test("placeGraphLabels keeps selected and hovered labels when the field is crowded", () => {
  const requests = Array.from({ length: 16 }, (_, index) =>
    labelRequest(
      `n${index}`,
      "Same crowded label",
      index === 0
        ? "selected"
        : index === 1
        ? "hovered"
        : index === 2
        ? "path"
        : "secondary",
    ));
  const placed = placeGraphLabels(requests);
  assertEquals(placed.find((item) => item.id === "n0")?.visible, true);
  assertEquals(placed.find((item) => item.id === "n1")?.visible, true);
  const visible = placed.filter((item) => item.visible);
  for (let index = 0; index < visible.length; index += 1) {
    for (let other = index + 1; other < visible.length; other += 1) {
      assertEquals(labelBoxesOverlap(visible[index], visible[other]), false);
    }
  }
});

Deno.test("placeGraphLabels hides dense path labels instead of sending them off-canvas", () => {
  const bounds = { width: 320, height: 220 };
  const requests = [
    {
      id: "selected",
      text: "Taiwan",
      x: 28,
      y: 24,
      nodeRadius: 16,
      fontSize: 11,
      priority: "selected" as const,
    },
    {
      id: "hovered",
      text: "Hovered theme",
      x: 292,
      y: 198,
      nodeRadius: 8,
      fontSize: 10,
      priority: "hovered" as const,
    },
    ...Array.from({ length: 48 }, (_, index) => ({
      id: `path-${index}`,
      text: `Path company ${index}`,
      x: 150 + (index % 6) * 4,
      y: 110 + Math.floor(index / 6) * 4,
      nodeRadius: 4.25,
      fontSize: 9,
      priority: "path" as const,
    })),
  ];
  const first = placeGraphLabels(requests, { bounds });
  const second = placeGraphLabels(requests, { bounds });
  assertEquals(first, second);

  const byId = new Map(requests.map((item) => [item.id, item]));
  const visible = first.filter((item) => item.visible);
  assertEquals(first.find((item) => item.id === "selected")?.visible, true);
  assertEquals(first.find((item) => item.id === "hovered")?.visible, true);
  assert(
    first.filter((item) => item.id.startsWith("path-") && item.visible).length <
      48,
  );
  for (const label of visible) {
    const request = byId.get(label.id);
    assert(request);
    assert(label.left >= 8 - 1e-6);
    assert(label.top >= 8 - 1e-6);
    assert(label.left + label.width <= bounds.width - 8 + 1e-6);
    assert(label.top + label.height <= bounds.height - 8 + 1e-6);
    assert(
      Math.hypot(label.x - request.x, label.y - request.y) <=
        MAX_LABEL_LEADER_DISTANCE + 1e-6,
    );
  }
  for (let index = 0; index < visible.length; index += 1) {
    for (let other = index + 1; other < visible.length; other += 1) {
      assertEquals(labelBoxesOverlap(visible[index], visible[other]), false);
    }
  }
});

Deno.test("shared companies and lens changes do not keep an unresolved explorer group", () => {
  const graph = handmadeGraph();
  assertEquals(
    explorerFallbackForFocus(graph, "market", "market:tw"),
    "market:tw",
  );
  assertEquals(
    explorerFallbackForFocus(
      graph,
      "market",
      "company:cross-market",
      "market:tw",
    ),
    null,
  );
  assertEquals(
    explorerFallbackForFocus(graph, "domain", "company:cross-market"),
    "domain:energy",
  );
  assertEquals(explorerFallbackForFocus(graph, "domain", "market:tw"), null);
  assertEquals(explorerFallbackForFocus(graph, "macro", "market:tw"), null);
});

Deno.test("explorer groups follow the active lens without dropping semantic nodes", () => {
  const graph = handmadeGraph();
  const nodeIds = graph.nodes.map((item) => item.data.id).sort();
  const markets = explorerGroups(graph, "market");
  const domains = explorerGroups(graph, "domain");
  const macro = explorerGroups(graph, "macro");

  assertEquals(markets.map((group) => group.id), ["market:fr", "market:tw"]);
  assertEquals(
    markets.find((group) => group.id === "market:fr")?.childIds.slice().sort(),
    ["family:fr-banks", "family:fr-energy"],
  );
  assertEquals(domains.map((group) => group.id), [
    "domain:energy",
    "domain:banks",
  ]);
  assertEquals(
    domains.find((group) => group.id === "domain:energy")?.childIds.slice()
      .sort(),
    ["family:fr-energy", "family:tw-energy"],
  );
  assertEquals(macro.map((group) => group.id).sort(), [
    "driver:oil-prices",
    "driver:rates",
  ]);
  assertEquals(
    macro.some((group) => group.id === "driver:evidence-oil"),
    false,
  );
  assertEquals(
    macro.find((group) => group.id === "driver:oil-prices")?.childIds.slice()
      .sort(),
    ["family:fr-energy", "family:tw-energy"],
  );
  assertEquals(graph.nodes.map((item) => item.data.id).sort(), nodeIds);
  for (const lens of ["market", "domain", "macro"] as const) {
    assertEquals(
      graphHulls(graph, lens).some((item) => item.kind === "lens"),
      true,
    );
    assertEquals(graph.nodes.map((item) => item.data.id).sort(), nodeIds);
  }
});

Deno.test("semantic zoom hides company names until hover, path, or close zoom", () => {
  assertEquals(
    nodeLabelVisible({ kind: "market", zoom: 0.2 }),
    true,
  );
  assertEquals(
    nodeLabelVisible({ kind: "domain", zoom: 0.2 }),
    true,
  );
  assertEquals(
    nodeLabelVisible({ kind: "driver", zoom: 0.2 }),
    true,
  );
  assertEquals(
    nodeLabelVisible({ kind: "family", zoom: 0.35 }),
    false,
  );
  assertEquals(
    nodeLabelVisible({ kind: "family", zoom: 0.8 }),
    false,
  );
  assertEquals(
    nodeLabelVisible({ kind: "family", zoom: 1.1 }),
    true,
  );
  assertEquals(
    nodeLabelVisible({ kind: "company", zoom: 0.8 }),
    false,
  );
  assertEquals(
    nodeLabelVisible({ kind: "company", zoom: 0.8, hovered: true }),
    true,
  );
  assertEquals(
    nodeLabelVisible({ kind: "company", zoom: 0.8, focused: true }),
    true,
  );
  assertEquals(
    nodeLabelVisible({ kind: "company", zoom: 1.6 }),
    true,
  );
});
