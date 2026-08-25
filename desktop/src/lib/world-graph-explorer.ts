import type {
  CanonicalCompanyBinding,
  MarketInfluenceGraph,
} from "./market-influence-network.ts";

export const WORLD_GRAPH_EXPLORER_SCHEMA = "world_graph_explorer.v1";

export type WorldGraphExplorerNode = {
  id: string;
  node_kind: string;
  kind?: string | null;
  entity_kind?: string | null;
  entity_id?: string | null;
  label: string;
  layer?: string | null;
};

export type WorldGraphExplorerEdge = {
  id: string;
  source: string;
  target: string;
  family: "structural" | "knowledge";
  kind: string;
  effective_from?: string | null;
  effective_until?: string | null;
};

export type WorldGraphExplorerPayload = {
  schema_version: string;
  status: string;
  generated_at?: string | null;
  cutoff_at?: string | null;
  authority?: unknown;
  shadow_only?: boolean;
  revision?: Readonly<Record<string, unknown>> | null;
  ontology?: Readonly<Record<string, unknown>> | null;
  nodes: readonly WorldGraphExplorerNode[];
  edges: readonly WorldGraphExplorerEdge[];
  counts?: Readonly<Record<string, unknown>> | null;
  truncation?: Readonly<Record<string, unknown>> | null;
  truncated?: boolean;
};

const UNAVAILABLE_STATUSES = new Set([
  "error",
  "missing",
  "not_started",
  "unavailable",
]);

/**
 * Bind the compact product graph to the exact published World Graph identities.
 *
 * D3 intentionally keeps market/domain/family/company/driver. Instruments and
 * symbols are folded into the visible company node while their canonical
 * geography remains attached for navigation and future country-level views.
 */
export function bindWorldGraphProjection(
  graph: MarketInfluenceGraph,
  payload: WorldGraphExplorerPayload,
): MarketInfluenceGraph {
  const nodeById = new Map(
    payload.nodes.map((node) => [node.id, node] as const),
  );
  const outgoing = indexOutgoing(payload.edges);
  const instrumentsBySymbol = new Map<string, WorldGraphExplorerNode[]>();
  for (const node of payload.nodes) {
    if (entityKind(node) !== "instrument") continue;
    const symbol = symbolFromInstrument(node);
    if (!symbol) continue;
    const bucket = instrumentsBySymbol.get(symbol) ?? [];
    bucket.push(node);
    instrumentsBySymbol.set(symbol, bucket);
  }

  const missingCompanySymbols: string[] = [];
  let mappedCompanyCount = 0;
  const nodes = graph.nodes.map((node) => {
    if (node.data.kind !== "company") {
      return { ...node, data: { ...node.data } };
    }
    const symbol = normalizedSymbol(node.data.symbol);
    const candidates = instrumentsBySymbol.get(symbol) ?? [];
    const instrument = candidates.length === 1 ? candidates[0] : undefined;
    const canonical = instrument
      ? canonicalBinding(instrument, nodeById, outgoing)
      : { mapped: false } satisfies CanonicalCompanyBinding;
    if (canonical.mapped) mappedCompanyCount += 1;
    else if (symbol) missingCompanySymbols.push(symbol);
    const place = canonicalPlaceLabel(canonical, nodeById);
    return {
      ...node,
      data: {
        ...node.data,
        canonical,
        detail: place ? `Company · ${place}` : node.data.detail,
      },
      classes: `${node.classes} ${
        canonical.mapped ? "canonical-mapped" : "canonical-missing"
      }`,
    };
  });

  const revision = payload.revision ?? payload.ontology ?? {};
  return {
    ...graph,
    nodes,
    edges: graph.edges.map((edge) => ({ ...edge, data: { ...edge.data } })),
    projection: {
      schemaVersion: payload.schema_version,
      status: String(payload.status || "unknown"),
      generatedAt: optionalText(payload.generated_at),
      cutoffAt: optionalText(payload.cutoff_at),
      revisionId: firstText(revision, ["revision_id", "id"]),
      canonicalNodeCount: finiteCount(payload.counts?.nodes) ??
        payload.nodes.length,
      canonicalEdgeCount: finiteCount(payload.counts?.edges) ??
        payload.edges.length,
      mappedCompanyCount,
      companyCount: graph.companyCount,
      missingCompanySymbols: Array.from(new Set(missingCompanySymbols)).sort(),
      shadowOnly: isShadowOnly(payload),
      truncated: payload.truncated === true ||
        payload.truncation?.truncated === true,
    },
  };
}

export function worldGraphIsAvailable(
  payload: WorldGraphExplorerPayload | null | undefined,
): payload is WorldGraphExplorerPayload {
  if (!payload || payload.schema_version !== WORLD_GRAPH_EXPLORER_SCHEMA) {
    return false;
  }
  if (UNAVAILABLE_STATUSES.has(normalizedKey(payload.status))) return false;
  return Array.isArray(payload.nodes) && payload.nodes.length > 0;
}

function canonicalBinding(
  instrument: WorldGraphExplorerNode,
  nodes: ReadonlyMap<string, WorldGraphExplorerNode>,
  outgoing: ReadonlyMap<string, readonly WorldGraphExplorerEdge[]>,
): CanonicalCompanyBinding {
  const venueId = uniqueTarget(outgoing, instrument.id, "TRADED_ON");
  const venue = venueId ? nodes.get(venueId) : undefined;
  const firstPlaceId = venueId
    ? uniqueTarget(outgoing, venueId, "LOCATED_IN")
    : undefined;
  const firstPlace = firstPlaceId ? nodes.get(firstPlaceId) : undefined;
  const countryId = entityKind(firstPlace) === "country"
    ? firstPlaceId
    : undefined;
  const regionId = entityKind(firstPlace) === "region"
    ? firstPlaceId
    : countryId
    ? uniqueTarget(outgoing, countryId, "LOCATED_IN")
    : undefined;
  return {
    mapped: Boolean(venue && (countryId || regionId)),
    instrumentId: instrument.id,
    venueId,
    countryId,
    regionId,
  };
}

function indexOutgoing(
  edges: readonly WorldGraphExplorerEdge[],
): Map<string, WorldGraphExplorerEdge[]> {
  const result = new Map<string, WorldGraphExplorerEdge[]>();
  for (const edge of edges) {
    if (edge.family !== "structural") continue;
    const bucket = result.get(edge.source) ?? [];
    bucket.push(edge);
    result.set(edge.source, bucket);
  }
  return result;
}

function uniqueTarget(
  outgoing: ReadonlyMap<string, readonly WorldGraphExplorerEdge[]>,
  source: string,
  kind: string,
): string | undefined {
  const targets = Array.from(
    new Set(
      (outgoing.get(source) ?? [])
        .filter((edge) => edge.kind === kind)
        .map((edge) => edge.target),
    ),
  );
  return targets.length === 1 ? targets[0] : undefined;
}

function canonicalPlaceLabel(
  binding: CanonicalCompanyBinding,
  nodes: ReadonlyMap<string, WorldGraphExplorerNode>,
): string | null {
  const country = binding.countryId ? nodes.get(binding.countryId) : undefined;
  if (country) return countryDisplayLabel(country);
  const region = binding.regionId ? nodes.get(binding.regionId) : undefined;
  return optionalText(region?.label);
}

function countryDisplayLabel(country: WorldGraphExplorerNode): string {
  const code = String(country.entity_id ?? "").split(":").at(-1)?.toUpperCase();
  if (code?.length === 2) {
    try {
      return new Intl.DisplayNames(["en"], { type: "region" }).of(code) ||
        optionalText(country.label) || code;
    } catch {
      // Keep the persisted label when this runtime lacks Intl.DisplayNames.
    }
  }
  return optionalText(country.label) || "Market";
}

function symbolFromInstrument(node: WorldGraphExplorerNode): string {
  const entityId = String(node.entity_id ?? "");
  const marker = ":symbol:";
  const index = entityId.indexOf(marker);
  return index >= 0
    ? normalizedSymbol(entityId.slice(index + marker.length))
    : "";
}

function entityKind(node?: WorldGraphExplorerNode): string {
  return normalizedKey(node?.entity_kind || node?.kind);
}

function isShadowOnly(payload: WorldGraphExplorerPayload): boolean {
  if (payload.shadow_only === true || payload.authority === "shadow_only") {
    return true;
  }
  return Boolean(
    payload.authority && typeof payload.authority === "object" &&
      (payload.authority as Record<string, unknown>).shadow_only === true,
  );
}

function finiteCount(value: unknown): number | null {
  return typeof value === "number" && Number.isFinite(value) && value >= 0
    ? value
    : null;
}

function firstText(
  value: Readonly<Record<string, unknown>>,
  keys: readonly string[],
): string | null {
  for (const key of keys) {
    const text = optionalText(value[key]);
    if (text) return text;
  }
  return null;
}

function optionalText(value: unknown): string | null {
  const text = typeof value === "string" ? value.trim() : "";
  return text || null;
}

function normalizedKey(value: unknown): string {
  return String(value ?? "").trim().toLowerCase().replaceAll("-", "_")
    .replaceAll(" ", "_");
}

function normalizedSymbol(value: unknown): string {
  return String(value ?? "").trim().toUpperCase();
}
