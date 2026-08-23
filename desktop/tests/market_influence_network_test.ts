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
import { buildMarketInfluenceGraph } from "../src/lib/market-influence-network.ts";

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
