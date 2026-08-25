import { assert, assertEquals, assertNotStrictEquals } from "@std/assert";
import type { MarketInfluenceGraph } from "../src/lib/market-influence-network.ts";
import {
  bindWorldGraphProjection,
  type WorldGraphExplorerPayload,
  worldGraphIsAvailable,
} from "../src/lib/world-graph-explorer.ts";

const graph: MarketInfluenceGraph = {
  nodes: [
    {
      data: {
        id: "company:2330-tw",
        kind: "company",
        label: "TSMC",
        symbol: "2330.TW",
        detail: "Company",
      },
      classes: "company",
    },
    {
      data: {
        id: "company:missing",
        kind: "company",
        label: "Missing Co",
        symbol: "MISS",
        detail: "Company",
      },
      classes: "company",
    },
    {
      data: { id: "domain:technology", kind: "domain", label: "Technology" },
      classes: "domain structural-node",
    },
  ],
  edges: [],
  scopeCount: 1,
  domainCount: 1,
  familyCount: 0,
  companyCount: 2,
  supportedPathCount: 0,
  possiblePathCount: 0,
};

const payload: WorldGraphExplorerPayload = {
  schema_version: "world_graph_explorer.v1",
  status: "loaded",
  generated_at: "2026-08-25T06:00:00Z",
  cutoff_at: "2026-08-25T05:59:00Z",
  shadow_only: true,
  revision: { revision_id: "revision-7" },
  counts: { nodes: 4, edges: 3 },
  nodes: [
    {
      id: "instrument:tw-2330",
      node_kind: "world_entity",
      entity_kind: "instrument",
      entity_id: "mic:XTAI:symbol:2330.TW",
      label: "2330.TW",
    },
    {
      id: "venue:xtai",
      node_kind: "world_entity",
      entity_kind: "venue",
      entity_id: "mic:XTAI",
      label: "XTAI",
    },
    {
      id: "country:tw",
      node_kind: "world_entity",
      entity_kind: "country",
      entity_id: "iso-3166:TW",
      label: "Taiwan",
    },
    {
      id: "region:east-asia",
      node_kind: "world_entity",
      entity_kind: "region",
      entity_id: "region:east-asia",
      label: "East Asia",
    },
  ],
  edges: [
    {
      id: "trade",
      source: "instrument:tw-2330",
      target: "venue:xtai",
      family: "structural",
      kind: "TRADED_ON",
    },
    {
      id: "venue-place",
      source: "venue:xtai",
      target: "country:tw",
      family: "structural",
      kind: "LOCATED_IN",
    },
    {
      id: "country-place",
      source: "country:tw",
      target: "region:east-asia",
      family: "structural",
      kind: "LOCATED_IN",
    },
  ],
};

Deno.test("World Graph binding enriches compact companies without adding technical nodes", () => {
  const projected = bindWorldGraphProjection(graph, payload);
  assertNotStrictEquals(projected, graph);
  assertEquals(projected.nodes.map((node) => node.data.kind), [
    "company",
    "company",
    "domain",
  ]);

  const mapped = projected.nodes[0];
  assertEquals(mapped.data.canonical, {
    mapped: true,
    instrumentId: "instrument:tw-2330",
    venueId: "venue:xtai",
    countryId: "country:tw",
    regionId: "region:east-asia",
  });
  assertEquals(mapped.data.detail, "Company · Taiwan");
  assert(mapped.classes.includes("canonical-mapped"));

  assertEquals(projected.nodes[1].data.canonical, { mapped: false });
  assert(projected.nodes[1].classes.includes("canonical-missing"));
  assertEquals(projected.projection?.mappedCompanyCount, 1);
  assertEquals(projected.projection?.missingCompanySymbols, ["MISS"]);
  assertEquals(projected.projection?.revisionId, "revision-7");
  assertEquals(projected.projection?.shadowOnly, true);
  assertEquals(graph.nodes[0].data.canonical, undefined);
});

Deno.test("World Graph availability rejects absent and error payloads", () => {
  assertEquals(worldGraphIsAvailable(payload), true);
  assertEquals(worldGraphIsAvailable(null), false);
  assertEquals(
    worldGraphIsAvailable({ ...payload, status: "error" }),
    false,
  );
  assertEquals(
    worldGraphIsAvailable({ ...payload, schema_version: "legacy.v3" }),
    false,
  );
});
