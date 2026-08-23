import { assert } from "@std/assert/assert";
import { assertEquals } from "@std/assert/equals";
import type {
  AtlasEvidenceLink,
  AtlasFamilyInput,
  AtlasFamilyThreadInput,
} from "../src/lib/market-intelligence-atlas.ts";
import {
  buildAtlasFamilyThreads,
  buildMarketAtlas,
} from "../src/lib/market-intelligence-atlas.ts";
import {
  buildMarketInfluenceGraph,
  chooseInitialPortfolioFocus,
  directedContextPath,
  guidedPortfolioPath,
  type HoldingInput,
  impliedPathOrigin,
  type MarketInfluenceGraph,
  projectHoldings,
  recordedEvidenceFreshness,
  recordedEvidencePoints,
  retainedPathOrigin,
} from "../src/lib/market-influence-network.ts";

function family(
  venue: string,
  name: string,
  extra: Partial<AtlasFamilyInput> = {},
): AtlasFamilyInput {
  return { venue, family: name, ...extra };
}

function fixture() {
  const comparisons: AtlasFamilyThreadInput[] = [
    {
      group: "energy",
      label: "Energy",
      venues: {
        FR: {
          leader: "fr_energy",
          families: [
            { family: "fr_energy" },
            { family: "fr_utilities" },
          ],
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
  const evidence: AtlasEvidenceLink[] = [{
    driverKey: "oil",
    driverLabel: "Oil",
    kind: "shared_source",
    tone: "supportive",
    driverPoint: "Oil rose.",
    familyPoint: "Energy earnings improved.",
  }];
  const graph = buildMarketInfluenceGraph({
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
  return { graph, layout, threads };
}

function graphWithHoldings(
  holdings: readonly HoldingInput[],
): MarketInfluenceGraph {
  const { layout, threads } = fixture();
  return buildMarketInfluenceGraph({
    nodes: layout.nodes,
    threads,
    evidenceByNode: [{
      nodeId: "FR::fr_energy",
      links: [{
        driverKey: "oil",
        driverLabel: "Oil",
        kind: "shared_source",
        tone: "supportive",
        driverPoint: "Oil rose.",
        familyPoint: "Energy earnings improved.",
      }],
    }],
    companyNames: {
      "ENGI.PA": "Engie",
      "TTE.PA": "TotalEnergies",
      "PTT.BK": "PTT",
      PBR: "Petrobras",
    },
    holdings,
  });
}

function companyNode(
  graph: MarketInfluenceGraph,
  symbol: string,
): MarketInfluenceGraph["nodes"][number] {
  const node = graph.nodes.find((item) =>
    (item.data.symbol ?? "").toUpperCase() === symbol.toUpperCase()
  );
  assert(node, `missing company ${symbol}`);
  return node;
}

function familyNode(
  graph: MarketInfluenceGraph,
  atlasNodeId: string,
): MarketInfluenceGraph["nodes"][number] {
  const node = graph.nodes.find((item) =>
    item.data.atlasNodeId === atlasNodeId
  );
  assert(node, `missing family ${atlasNodeId}`);
  return node;
}

function driverNode(
  graph: MarketInfluenceGraph,
  label: string,
): MarketInfluenceGraph["nodes"][number] {
  const node = graph.nodes.find((item) =>
    item.data.kind === "driver" && item.data.label === label
  );
  assert(node, `missing driver ${label}`);
  return node;
}

Deno.test("global influence graph keeps every market domain and governed family", () => {
  const { graph, layout } = fixture();
  const kinds = (kind: string) =>
    graph.nodes.filter((node) => node.data.kind === kind);

  assertEquals(kinds("market").length, 3);
  assertEquals(kinds("domain").length, 2);
  assertEquals(kinds("family").length, 5);
  assertEquals(graph.familyCount, 5);
  assert(
    layout.nodes.some((node) => node.id === "LATAM::latam_materials"),
    "a governed family without a live observation must remain visible",
  );
  assertEquals(
    new Set(kinds("market").map((node) => node.data.scopeKey)),
    new Set(["FR", "ASEAN", "LATAM"]),
  );
  assertEquals(
    kinds("market").some((node) => node.data.scopeKey === "TW"),
    false,
  );
});

Deno.test("global influence graph connects named companies to their families", () => {
  const { graph } = fixture();
  const companies = graph.nodes.filter((node) => node.data.kind === "company");
  const companyEdges = graph.edges.filter((edge) =>
    edge.data.basis === "family_membership"
  );

  assertEquals(companies.length, 4);
  assertEquals(graph.companyCount, 4);
  assertEquals(
    new Set(companies.map((node) => node.data.label)),
    new Set(["Engie", "TotalEnergies", "PTT", "Petrobras"]),
  );
  assertEquals(companyEdges.length, 5);
  assert(
    companyEdges.every((edge) =>
      edge.data.kind === "membership" &&
      edge.classes.includes("company-path")
    ),
  );
});

Deno.test("direct shared evidence keeps recorded driver and family points on the edge", () => {
  const { graph } = fixture();
  const evidence = graph.edges.filter((edge) => edge.data.kind === "evidence");
  assertEquals(evidence.length, 1);
  assertEquals(evidence[0].data.basis, "shared_source");
  assertEquals(evidence[0].data.driverPoint, "Oil rose.");
  assertEquals(evidence[0].data.familyPoint, "Energy earnings improved.");
  assertEquals(
    recordedEvidencePoints(evidence[0]),
    { driverPoint: "Oil rose.", familyPoint: "Energy earnings improved." },
  );
});

Deno.test("heuristic edges cannot masquerade as recorded evidence", () => {
  const { layout, threads } = fixture();
  const graph = buildMarketInfluenceGraph({
    nodes: layout.nodes,
    threads,
    evidenceByNode: [{
      nodeId: "FR::fr_energy",
      links: [{
        driverKey: "oil",
        driverLabel: "Oil prices",
        kind: "topic_match",
        tone: "context",
        driverPoint: "Oil rose.",
        familyPoint: "Energy earnings improved.",
      }],
    }],
  });
  const heuristic = graph.edges.filter((edge) =>
    edge.data.basis === "topic_match"
  );
  assertEquals(heuristic.length, 1);
  assertEquals(heuristic[0].data.kind, "possible");
  assertEquals(heuristic[0].data.driverPoint, undefined);
  assertEquals(heuristic[0].data.familyPoint, undefined);
  assertEquals(recordedEvidencePoints(heuristic[0]), null);
  assertEquals(
    recordedEvidencePoints({
      data: {
        id: "forged",
        source: "driver:oil",
        target: "family:fr",
        kind: "possible",
        basis: "topic_match",
        tone: "context",
        label: "Possible influence",
        driverPoint: "Oil rose.",
        familyPoint: "Forged measured claim.",
      },
      classes: "possible",
    }),
    null,
  );
});

Deno.test("shared evidence and possible influence remain distinct", () => {
  const { graph, threads } = fixture();
  const evidence = graph.edges.filter((edge) => edge.data.kind === "evidence");
  const possible = graph.edges.filter((edge) =>
    edge.data.basis === "heuristic_domain"
  );

  assertEquals(evidence.length, 1);
  assertEquals(evidence[0].data.basis, "shared_source");
  assert(evidence[0].classes.includes("tone-supportive"));
  assertEquals(possible.length, threads.length);
  assert(possible.every((edge) => edge.classes.includes("possible")));
  assertEquals(graph.supportedPathCount, 1);
  assertEquals(graph.possiblePathCount, threads.length);
});

Deno.test("documented evidence upgrades a repeated possible factor", () => {
  const { layout, threads } = fixture();
  const repeatedLabel = threads[0].possibleInfluence;
  const graph = buildMarketInfluenceGraph({
    nodes: layout.nodes,
    threads,
    evidenceByNode: [{
      nodeId: threads[0].nodeIds[0],
      links: [{
        driverKey: "repeated-factor",
        driverLabel: repeatedLabel,
        kind: "shared_source",
        tone: "context",
        driverPoint: "Recorded in the source material.",
        familyPoint: "Connected to this market theme.",
      }],
    }],
  });
  const factor = graph.nodes.find((node) =>
    node.data.kind === "driver" && node.data.label === repeatedLabel
  );

  assert(factor);
  assert(factor.classes.includes("supported"));
  assertEquals(factor.classes.includes("heuristic"), false);
  assertEquals(factor.data.detail, "Shared evidence");
});

Deno.test("every family keeps its market and governed-domain structure", () => {
  const { graph } = fixture();
  const families = graph.nodes.filter((node) => node.data.kind === "family");
  const marketEdges = graph.edges.filter((edge) =>
    edge.data.basis === "market_membership"
  );
  const domainEdges = graph.edges.filter((edge) =>
    edge.data.basis === "governed_domain"
  );

  assertEquals(marketEdges.length, families.length);
  assertEquals(domainEdges.length, families.length);
  for (const familyNode of families) {
    assert(
      marketEdges.some((edge) => edge.data.target === familyNode.data.id),
    );
    assert(
      domainEdges.some((edge) => edge.data.target === familyNode.data.id),
    );
  }
});

Deno.test("global influence graph is deterministic with unique valid ids", () => {
  const first = fixture().graph;
  const second = fixture().graph;
  assertEquals(first, second);

  const nodeIds = first.nodes.map((node) => node.data.id);
  const edgeIds = first.edges.map((edge) => edge.data.id);
  const knownNodes = new Set(nodeIds);
  assertEquals(new Set(nodeIds).size, nodeIds.length);
  assertEquals(new Set(edgeIds).size, edgeIds.length);
  assert(
    first.edges.every((edge) =>
      knownNodes.has(edge.data.source) && knownNodes.has(edge.data.target)
    ),
  );
});

Deno.test("global influence graph handles an empty model without phantom nodes", () => {
  const graph = buildMarketInfluenceGraph({ nodes: [], threads: [] });
  assertEquals(graph.nodes, []);
  assertEquals(graph.edges, []);
  assertEquals(graph.scopeCount, 0);
  assertEquals(graph.domainCount, 0);
  assertEquals(graph.familyCount, 0);
  assertEquals(graph.companyCount, 0);
  assertEquals(graph.supportedPathCount, 0);
  assertEquals(graph.possiblePathCount, 0);
});

Deno.test("projectHoldings derives side and known notional share without inventing zeros", () => {
  const projected = projectHoldings([
    { symbol: "aaa", quantity: 10, last_price: 2, fx_rate: 1.1 },
    { symbol: "BBB", quantity: -4, last_price: 5, fx_rate: 1 },
    { symbol: "ccc", quantity: 3, last_price: 10 },
    { symbol: "ddd", quantity: 8, last_price: 4, fx_rate: Number.NaN },
    {
      symbol: "eee",
      quantity: 1,
      last_price: Number.POSITIVE_INFINITY,
      fx_rate: 1,
    },
    { symbol: "fff", quantity: 0, last_price: 9, fx_rate: 1 },
  ]);

  assertEquals(projected.get("AAA")?.side, "long");
  assertEquals(projected.get("AAA")?.quantity, 10);
  assertEquals(projected.get("AAA")?.notionalUsd, 22);
  assertEquals(projected.get("BBB")?.side, "short");
  assertEquals(projected.get("BBB")?.notionalUsd, 20);
  assertEquals(projected.get("CCC")?.side, "long");
  assertEquals(projected.get("CCC")?.notionalUsd, null);
  assertEquals(projected.get("CCC")?.shareOfKnownGross, null);
  assertEquals(projected.get("DDD")?.notionalUsd, null);
  assertEquals(projected.get("EEE")?.notionalUsd, null);
  assertEquals(projected.has("FFF"), false);
  assertEquals(projected.get("AAA")?.shareOfKnownGross, 22 / 42);
  assertEquals(projected.get("BBB")?.shareOfKnownGross, 20 / 42);
});

Deno.test("projectHoldings treats non-positive price or FX as unknown money", () => {
  const projected = projectHoldings([
    { symbol: "zero-p", quantity: 4, last_price: 0, fx_rate: 1 },
    { symbol: "neg-p", quantity: -3, last_price: -12, fx_rate: 1 },
    { symbol: "zero-fx", quantity: 5, last_price: 10, fx_rate: 0 },
    { symbol: "neg-fx", quantity: 2, last_price: 8, fx_rate: -1.1 },
    { symbol: "ok", quantity: 1, last_price: 10, fx_rate: 1 },
  ]);

  assertEquals(projected.get("ZERO-P")?.side, "long");
  assertEquals(projected.get("ZERO-P")?.quantity, 4);
  assertEquals(projected.get("ZERO-P")?.notionalUsd, null);
  assertEquals(projected.get("ZERO-P")?.shareOfKnownGross, null);
  assertEquals(projected.get("NEG-P")?.side, "short");
  assertEquals(projected.get("NEG-P")?.quantity, -3);
  assertEquals(projected.get("NEG-P")?.notionalUsd, null);
  assertEquals(projected.get("NEG-P")?.shareOfKnownGross, null);
  assertEquals(projected.get("ZERO-FX")?.side, "long");
  assertEquals(projected.get("ZERO-FX")?.notionalUsd, null);
  assertEquals(projected.get("ZERO-FX")?.shareOfKnownGross, null);
  assertEquals(projected.get("NEG-FX")?.side, "long");
  assertEquals(projected.get("NEG-FX")?.notionalUsd, null);
  assertEquals(projected.get("NEG-FX")?.shareOfKnownGross, null);
  assertEquals(projected.get("OK")?.notionalUsd, 10);
  assertEquals(projected.get("OK")?.shareOfKnownGross, 1);
});

Deno.test("sticky family symbols still receive a holding mark when the name is only on the sticky list", () => {
  const layout = buildMarketAtlas({
    current: { FR: {} },
    families: [
      family("FR", "energy", {
        rank: 1,
        symbols: ["TTE.PA"],
        sticky_symbols: ["ENGI.PA"],
      }),
    ],
  });
  const graph = buildMarketInfluenceGraph({
    nodes: layout.nodes,
    threads: [],
    companyNames: { "ENGI.PA": "Engie", "TTE.PA": "TotalEnergies" },
    holdings: [
      { symbol: "engi.pa", quantity: 2, last_price: 10, fx_rate: 1 },
      { symbol: "ZZZ", quantity: 9, last_price: 3, fx_rate: 1 },
    ],
  });
  const engie = graph.nodes.find((node) => node.data.symbol === "ENGI.PA");
  const total = graph.nodes.find((node) => node.data.symbol === "TTE.PA");
  const invented = graph.nodes.find((node) =>
    (node.data.symbol ?? "").toUpperCase() === "ZZZ"
  );

  assert(engie);
  assert(total);
  assertEquals(invented, undefined);
  assert(engie.classes.includes("held"));
  assertEquals(engie.data.holding?.side, "long");
  assertEquals(engie.data.holding?.notionalUsd, 20);
  assertEquals(engie.data.holding?.shareOfKnownGross, 20 / (20 + 27));
  assertEquals(total.classes.includes("held"), false);
  assertEquals(total.data.holding, undefined);
});

Deno.test("initial focus picks the largest known holding and ties on symbol then id", () => {
  const graph = graphWithHoldings([
    { symbol: "TTE.PA", quantity: 2, last_price: 10, fx_rate: 1 },
    { symbol: "ENGI.PA", quantity: 4, last_price: 10, fx_rate: 1 },
  ]);
  assertEquals(
    chooseInitialPortfolioFocus(graph),
    companyNode(graph, "ENGI.PA").data.id,
  );

  const tied = graphWithHoldings([
    { symbol: "TTE.PA", quantity: 2, last_price: 10, fx_rate: 1 },
    { symbol: "engi.pa", quantity: 1, last_price: 20, fx_rate: 1 },
  ]);
  assertEquals(
    chooseInitialPortfolioFocus(tied),
    companyNode(tied, "ENGI.PA").data.id,
  );
  assertEquals(
    chooseInitialPortfolioFocus(tied),
    chooseInitialPortfolioFocus(tied),
  );
});

Deno.test("initial focus does not invent zeros when every held exposure is unknown", () => {
  const graph = graphWithHoldings([
    { symbol: "TTE.PA", quantity: 80, last_price: 10 },
    { symbol: "ENGI.PA", quantity: 1, last_price: 10 },
    { symbol: "PTT.BK", quantity: 9, last_price: 0, fx_rate: 1 },
  ]);
  const engie = companyNode(graph, "ENGI.PA");
  const total = companyNode(graph, "TTE.PA");
  const ptt = companyNode(graph, "PTT.BK");

  assertEquals(engie.data.holding?.notionalUsd, null);
  assertEquals(total.data.holding?.notionalUsd, null);
  assertEquals(ptt.data.holding?.notionalUsd, null);
  assertEquals(chooseInitialPortfolioFocus(graph), engie.data.id);
  assertEquals(
    chooseInitialPortfolioFocus(graph) === total.data.id,
    false,
    "quantity must not rank unknown exposures across currencies",
  );
});

Deno.test("without a holding, a favored family with recorded evidence beats a supplied fallback", () => {
  const graph = graphWithHoldings([
    { symbol: "ZZZ", quantity: 9, last_price: 3, fx_rate: 1 },
  ]);
  const materials = familyNode(graph, "LATAM::latam_materials");
  const energy = familyNode(graph, "FR::fr_energy");

  assertEquals(
    chooseInitialPortfolioFocus(graph, materials.data.id),
    energy.data.id,
  );
  assertEquals(chooseInitialPortfolioFocus(graph), energy.data.id);
});

Deno.test("without evidenced favored families, a valid fallback theme is used before rank", () => {
  const { layout, threads } = fixture();
  const graph = buildMarketInfluenceGraph({
    nodes: layout.nodes,
    threads,
    companyNames: {
      "ENGI.PA": "Engie",
      "TTE.PA": "TotalEnergies",
      "PTT.BK": "PTT",
      PBR: "Petrobras",
    },
  });
  const materials = familyNode(graph, "LATAM::latam_materials");
  const energy = familyNode(graph, "FR::fr_energy");

  assertEquals(
    chooseInitialPortfolioFocus(graph, materials.data.id),
    materials.data.id,
  );
  assertEquals(
    chooseInitialPortfolioFocus(graph, materials.data.atlasNodeId),
    materials.data.id,
  );
  assertEquals(
    chooseInitialPortfolioFocus(graph, "missing-theme"),
    energy.data.id,
  );
  assertEquals(chooseInitialPortfolioFocus(graph), energy.data.id);
});

Deno.test("evidenced fallback prefers shared source or symbol, then best favored rank", () => {
  const layout = buildMarketAtlas({
    current: { EU: {}, US: {} },
    families: [
      family("EU", "energy", {
        rank: 2,
        priority: "favored",
        symbols: ["TTE.PA"],
      }),
      family("US", "energy", {
        rank: 1,
        priority: "favored",
        symbols: ["XOM"],
      }),
      family("EU", "materials", {
        rank: 3,
        priority: "deprioritized",
        symbols: ["RIO.L"],
      }),
    ],
  });
  const oil = (
    familyPoint: string,
    kind: "shared_source" | "shared_symbol" | "topic_match",
  ): AtlasEvidenceLink => ({
    driverKey: "oil",
    driverLabel: "Oil",
    kind,
    tone: "supportive",
    driverPoint: "Oil moved.",
    familyPoint,
  });
  const graph = buildMarketInfluenceGraph({
    nodes: layout.nodes,
    threads: [],
    evidenceByNode: [
      {
        nodeId: "EU::energy",
        links: [oil("European energy.", "shared_source")],
      },
      { nodeId: "US::energy", links: [oil("US energy.", "shared_symbol")] },
      {
        nodeId: "EU::materials",
        links: [oil("Materials headline.", "topic_match")],
      },
    ],
    companyNames: { "TTE.PA": "TotalEnergies", XOM: "Exxon", "RIO.L": "Rio" },
  });
  const euEnergy = familyNode(graph, "EU::energy");
  const usEnergy = familyNode(graph, "US::energy");
  const materials = familyNode(graph, "EU::materials");

  assertEquals(
    chooseInitialPortfolioFocus(graph, euEnergy.data.id),
    usEnergy.data.id,
  );
  assertEquals(
    chooseInitialPortfolioFocus(graph, materials.data.id),
    usEnergy.data.id,
  );

  const heuristicOnly = buildMarketInfluenceGraph({
    nodes: layout.nodes,
    threads: [],
    evidenceByNode: [{
      nodeId: "EU::materials",
      links: [oil("Materials headline.", "topic_match")],
    }],
  });
  assertEquals(
    chooseInitialPortfolioFocus(
      heuristicOnly,
      familyNode(heuristicOnly, "EU::materials").data.id,
    ),
    familyNode(heuristicOnly, "EU::materials").data.id,
  );
});

Deno.test("guided path prefers shared evidence over heuristic and structure", () => {
  const { graph } = fixture();
  const companyId = companyNode(graph, "ENGI.PA").data.id;
  const path = guidedPortfolioPath(graph, companyId);
  const oil = driverNode(graph, "Oil");
  const energy = familyNode(graph, "FR::fr_energy");

  assertEquals(path.kind, "shared_evidence");
  assertEquals(path.nodeIds, [oil.data.id, energy.data.id, companyId]);
  assertEquals(path.edgeIds.length, 2);
  assertEquals(
    path.nodeIds.includes(familyNode(graph, "FR::fr_utilities").data.id),
    false,
  );
  assertEquals(
    path.nodeIds.includes(companyNode(graph, "TTE.PA").data.id),
    false,
  );
});

Deno.test("guided path keeps exact 2- and 3-edge threads without neighbor leakage", () => {
  const { graph } = fixture();
  const engie = companyNode(graph, "ENGI.PA").data.id;
  const ptt = companyNode(graph, "PTT.BK").data.id;
  const shared = guidedPortfolioPath(graph, engie);
  const possible = guidedPortfolioPath(graph, ptt);
  const energyDomain = graph.nodes.find((item) =>
    item.data.kind === "domain" && item.data.domainKey === "energy"
  );
  const aseanEnergy = familyNode(graph, "ASEAN::asean_energy");

  assert(energyDomain);
  assertEquals(shared.edgeIds.length, 2);
  assertEquals(shared.nodeIds.length, 3);
  assertEquals(
    graph.nodes.some((item) =>
      item.data.kind === "company" &&
      item.data.id !== engie &&
      shared.nodeIds.includes(item.data.id)
    ),
    false,
  );
  assertEquals(possible.kind, "possible");
  assertEquals(possible.edgeIds.length, 3);
  assertEquals(possible.nodeIds, [
    driverNode(graph, "Oil and gas prices").data.id,
    energyDomain.data.id,
    aseanEnergy.data.id,
    ptt,
  ]);
  assertEquals(
    possible.nodeIds.includes(familyNode(graph, "FR::fr_energy").data.id),
    false,
  );
  assertEquals(
    possible.nodeIds.includes(companyNode(graph, "PBR").data.id),
    false,
  );
});

Deno.test("guided path picks the favored better-ranked family for a multi-theme company", () => {
  const layout = buildMarketAtlas({
    current: { FR: {}, TW: {} },
    families: [
      family("FR", "energy", {
        rank: 1,
        priority: "deprioritized",
        symbols: ["OMV.VI"],
      }),
      family("TW", "utilities", {
        rank: 4,
        priority: "favored",
        symbols: ["OMV.VI"],
      }),
      family("FR", "materials", {
        rank: 2,
        priority: "favored",
        symbols: ["OMV.VI"],
      }),
    ],
  });
  const graph = buildMarketInfluenceGraph({
    nodes: layout.nodes,
    threads: [],
    companyNames: { "OMV.VI": "OMV" },
  });
  const companyId = companyNode(graph, "OMV.VI").data.id;
  const path = guidedPortfolioPath(graph, companyId);
  const chosenFamily = familyNode(graph, "FR::materials");

  assertEquals(path.kind, "structure");
  assert(path.nodeIds.includes(chosenFamily.data.id));
  assertEquals(
    path.nodeIds.includes(familyNode(graph, "TW::utilities").data.id),
    false,
  );
  assertEquals(
    path.nodeIds.includes(familyNode(graph, "FR::energy").data.id),
    false,
  );
  assertEquals(
    guidedPortfolioPath(graph, companyId),
    guidedPortfolioPath(graph, companyId),
  );
});

Deno.test("guided path falls back to the company alone and preserves a valid origin", () => {
  const solo: MarketInfluenceGraph = {
    nodes: [{
      data: { id: "company:solo", kind: "company", label: "Solo" },
      classes: "company",
    }],
    edges: [],
    scopeCount: 0,
    domainCount: 0,
    familyCount: 0,
    companyCount: 1,
    supportedPathCount: 0,
    possiblePathCount: 0,
  };
  assertEquals(guidedPortfolioPath(solo, "company:solo"), {
    nodeIds: ["company:solo"],
    edgeIds: [],
    kind: "structure",
  });

  const { graph } = fixture();
  const companyId = companyNode(graph, "ENGI.PA").data.id;
  const utilities = familyNode(graph, "FR::fr_utilities");
  const fromUtilities = guidedPortfolioPath(
    graph,
    companyId,
    utilities.data.id,
  );
  assertEquals(fromUtilities.kind, "structure");
  assertEquals(fromUtilities.nodeIds[0], utilities.data.id);
  assertEquals(
    fromUtilities.nodeIds.includes(familyNode(graph, "FR::fr_energy").data.id),
    false,
  );
  assertEquals(
    guidedPortfolioPath(graph, companyId, "driver:missing").kind,
    "shared_evidence",
  );
});

Deno.test("directed context path keeps factor→theme→company within three edges", () => {
  const { graph } = fixture();
  const oil = driverNode(graph, "Oil");
  const energy = familyNode(graph, "FR::fr_energy");
  const company = companyNode(graph, "ENGI.PA");
  const path = directedContextPath(graph, oil.data.id, company.data.id);

  assert(path);
  assertEquals(path.nodeIds, [oil.data.id, energy.data.id, company.data.id]);
  assertEquals(path.edgeIds.length, 2);
  assertEquals(
    directedContextPath(graph, oil.data.id, energy.data.id)?.nodeIds,
    [oil.data.id, energy.data.id],
  );
});

Deno.test("directed context path keeps factor→domain→family→company", () => {
  const { graph } = fixture();
  const oilPrices = driverNode(graph, "Oil and gas prices");
  const energyDomain = graph.nodes.find((item) =>
    item.data.kind === "domain" && item.data.domainKey === "energy"
  );
  const aseanEnergy = familyNode(graph, "ASEAN::asean_energy");
  const ptt = companyNode(graph, "PTT.BK");
  assert(energyDomain);
  const path = directedContextPath(graph, oilPrices.data.id, ptt.data.id);

  assert(path);
  assertEquals(path.nodeIds, [
    oilPrices.data.id,
    energyDomain.data.id,
    aseanEnergy.data.id,
    ptt.data.id,
  ]);
  assertEquals(path.edgeIds.length, 3);
});

Deno.test("directed context path returns null for missing, identical, or unrelated nodes", () => {
  const { graph } = fixture();
  const oil = driverNode(graph, "Oil");
  const materials = familyNode(graph, "LATAM::latam_materials");
  const company = companyNode(graph, "ENGI.PA");

  assertEquals(directedContextPath(graph, oil.data.id, oil.data.id), null);
  assertEquals(directedContextPath(graph, "", company.data.id), null);
  assertEquals(directedContextPath(graph, oil.data.id, "missing"), null);
  assertEquals(
    directedContextPath(graph, oil.data.id, materials.data.id),
    null,
  );
  assertEquals(
    impliedPathOrigin(graph, materials.data.id, oil.data.id, company.data.id),
    null,
  );
});

Deno.test("directed context path refuses more than three edges", () => {
  const graph: MarketInfluenceGraph = {
    nodes: ["a", "b", "c", "d", "e"].map((id) => ({
      data: { id, kind: "driver" as const, label: id },
      classes: "driver",
    })),
    edges: [
      edge("a", "b"),
      edge("b", "c"),
      edge("c", "d"),
      edge("d", "e"),
    ],
    scopeCount: 0,
    domainCount: 0,
    familyCount: 0,
    companyCount: 0,
    supportedPathCount: 0,
    possiblePathCount: 0,
  };

  assertEquals(directedContextPath(graph, "a", "d")?.nodeIds, [
    "a",
    "b",
    "c",
    "d",
  ]);
  assertEquals(directedContextPath(graph, "a", "e"), null);
});

Deno.test("implied origin stays when it can still reach, otherwise starts a new context", () => {
  const { graph } = fixture();
  const oil = driverNode(graph, "Oil");
  const energy = familyNode(graph, "FR::fr_energy");
  const company = companyNode(graph, "ENGI.PA");
  const materials = familyNode(graph, "LATAM::latam_materials");

  assertEquals(
    impliedPathOrigin(graph, energy.data.id, null, oil.data.id),
    oil.data.id,
  );
  assertEquals(
    impliedPathOrigin(graph, company.data.id, oil.data.id, energy.data.id),
    oil.data.id,
  );
  assertEquals(
    impliedPathOrigin(graph, materials.data.id, oil.data.id, energy.data.id),
    null,
  );
});

Deno.test("retained origin clears when the origin disappears or no longer reaches", () => {
  const { graph } = fixture();
  const oil = driverNode(graph, "Oil");
  const energy = familyNode(graph, "FR::fr_energy");
  const company = companyNode(graph, "ENGI.PA");
  const withoutOil: MarketInfluenceGraph = {
    ...graph,
    nodes: graph.nodes.filter((node) => node.data.id !== oil.data.id),
    edges: graph.edges.filter((item) =>
      item.data.source !== oil.data.id && item.data.target !== oil.data.id
    ),
  };

  assertEquals(
    retainedPathOrigin(graph, company.data.id, oil.data.id),
    oil.data.id,
  );
  assertEquals(
    retainedPathOrigin(withoutOil, company.data.id, oil.data.id),
    null,
  );
  assertEquals(
    retainedPathOrigin(graph, energy.data.id, "driver:missing"),
    null,
  );
  assertEquals(retainedPathOrigin(graph, company.data.id, null), null);
});

Deno.test("supported evidence edges carry source freshness, heuristic edges do not", () => {
  const { layout, threads } = fixture();
  const graph = buildMarketInfluenceGraph({
    nodes: layout.nodes,
    threads,
    evidenceByNode: [
      {
        nodeId: "FR::fr_energy",
        as_of: "2026-01-01T00:00:00Z",
        valid_until: "2026-01-02T00:00:00Z",
        status: "success",
        coverage: "full",
        links: [{
          driverKey: "oil",
          driverLabel: "Oil",
          kind: "shared_source",
          tone: "supportive",
          driverPoint: "Oil rose.",
          familyPoint: "Energy earnings improved.",
        }],
      },
      {
        nodeId: "ASEAN::asean_energy",
        as_of: "2026-08-23T00:00:00Z",
        valid_until: "2099-01-01T00:00:00Z",
        status: "success",
        coverage: { status: "partial" },
        links: [{
          driverKey: "oil",
          driverLabel: "Oil",
          kind: "topic_match",
          tone: "context",
          driverPoint: "Oil rose.",
          familyPoint: "Would leak if copied.",
        }],
      },
    ],
  });
  const supported = graph.edges.find((item) =>
    item.data.basis === "shared_source"
  );
  const heuristic = graph.edges.find((item) =>
    item.data.basis === "topic_match"
  );

  assert(supported);
  assert(heuristic);
  assertEquals(recordedEvidenceFreshness(supported), {
    as_of: "2026-01-01T00:00:00Z",
    valid_until: "2026-01-02T00:00:00Z",
    status: "success",
    coverage: "full",
  });
  assertEquals(recordedEvidenceFreshness(heuristic), null);
  assertEquals(heuristic.data.evidenceAsOf, undefined);
  assertEquals(heuristic.data.driverPoint, undefined);
});

function edge(source: string, target: string) {
  return {
    data: {
      id: `edge:${source}:${target}`,
      source,
      target,
      kind: "taxonomy" as const,
      basis: "governed_domain" as const,
      tone: "context" as const,
      label: "link",
    },
    classes: "structural-path",
  };
}
