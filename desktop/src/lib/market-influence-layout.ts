import type {
  InfluenceEdgeBasis,
  InfluenceNodeKind,
  MarketInfluenceGraph,
} from "./market-influence-network.ts";

export type GraphLens = "domain" | "macro" | "market";

export type GraphGrouping = {
  groupByFamily: Map<string, string>;
  uniqueFamilyByCompany: Map<string, string>;
  familiesByCompany: Map<string, Set<string>>;
};

export type GraphHull = {
  id: string;
  kind: "family" | "lens";
  groupId: string;
  memberIds: string[];
};

export type GraphPresentation = {
  extraClassById: Map<string, string>;
  radiusById: Map<string, number>;
  weightById: Map<string, number>;
};

const COMPANY_RADIUS = 4.25;
const RADIUS_BOUNDS: Record<
  Exclude<InfluenceNodeKind, "company">,
  { min: number; max: number }
> = {
  market: { min: 15, max: 22 },
  domain: { min: 11.5, max: 16.5 },
  driver: { min: 11.5, max: 16.5 },
  family: { min: 9, max: 14.5 },
};

const FAMILY_LABEL_ZOOM = 1.08;
const COMPANY_LABEL_ZOOM = 1.4;

const LINK_FORCE: Record<
  "family" | "domain" | "market" | "influence",
  { strength: number; distance: number; compactDistance: number }
> = {
  family: { strength: 0.92, distance: 28, compactDistance: 22 },
  domain: { strength: 0.5, distance: 70, compactDistance: 56 },
  market: { strength: 0.22, distance: 92, compactDistance: 74 },
  influence: { strength: 0.1, distance: 124, compactDistance: 102 },
};

export function presentGraph(
  graph: MarketInfluenceGraph,
): GraphPresentation {
  const degreeById = new Map<string, number>();
  for (const edge of graph.edges) {
    degreeById.set(
      edge.data.source,
      (degreeById.get(edge.data.source) ?? 0) + 1,
    );
    degreeById.set(
      edge.data.target,
      (degreeById.get(edge.data.target) ?? 0) + 1,
    );
  }
  const maxByKind = new Map<InfluenceNodeKind, number>();
  for (const node of graph.nodes) {
    const degree = degreeById.get(node.data.id) ?? 0;
    maxByKind.set(
      node.data.kind,
      Math.max(maxByKind.get(node.data.kind) ?? 1, degree),
    );
  }
  const weightById = new Map<string, number>();
  const radiusById = new Map<string, number>();
  for (const node of graph.nodes) {
    if (node.data.kind === "company") {
      weightById.set(node.data.id, 0.28);
      radiusById.set(node.data.id, COMPANY_RADIUS);
      continue;
    }
    const degree = degreeById.get(node.data.id) ?? 0;
    const maximum = maxByKind.get(node.data.kind) ?? 1;
    const weight = Math.max(0.12, Math.sqrt(degree / maximum));
    const bounds = RADIUS_BOUNDS[node.data.kind];
    weightById.set(node.data.id, weight);
    radiusById.set(
      node.data.id,
      bounds.min + (bounds.max - bounds.min) * weight,
    );
  }

  const driverTones = new Map<
    string,
    { headwind: number; supportive: number }
  >();
  for (const edge of graph.edges) {
    if (edge.data.kind !== "evidence") continue;
    const source = graph.nodes.find((node) =>
      node.data.id === edge.data.source
    );
    if (source?.data.kind !== "driver") continue;
    const counts = driverTones.get(source.data.id) ?? {
      headwind: 0,
      supportive: 0,
    };
    if (edge.data.tone === "headwind") counts.headwind += 1;
    if (edge.data.tone === "supportive") counts.supportive += 1;
    driverTones.set(source.data.id, counts);
  }
  const extraClassById = new Map<string, string>();
  for (const [nodeId, tones] of driverTones) {
    if (tones.headwind > tones.supportive) {
      extraClassById.set(nodeId, "signal-headwind");
    } else if (tones.supportive > tones.headwind) {
      extraClassById.set(nodeId, "signal-supportive");
    }
  }
  return { extraClassById, radiusById, weightById };
}

export function groupGraph(
  graph: MarketInfluenceGraph,
  lens: GraphLens,
): GraphGrouping {
  const groupByFamily = new Map<string, string>();
  const familiesByCompany = new Map<string, Set<string>>();
  const domainsByFamily = new Map<string, Set<string>>();
  const marketsByFamily = new Map<string, Set<string>>();
  const driversByDomain = new Map<string, Set<string>>();

  for (const edge of graph.edges) {
    if (edge.data.basis === "family_membership") {
      const familyIds = familiesByCompany.get(edge.data.target) ?? new Set();
      familyIds.add(edge.data.source);
      familiesByCompany.set(edge.data.target, familyIds);
    }
    if (edge.data.basis === "governed_domain") {
      const domainIds = domainsByFamily.get(edge.data.target) ?? new Set();
      domainIds.add(edge.data.source);
      domainsByFamily.set(edge.data.target, domainIds);
    }
    if (edge.data.basis === "market_membership") {
      const marketIds = marketsByFamily.get(edge.data.target) ?? new Set();
      marketIds.add(edge.data.source);
      marketsByFamily.set(edge.data.target, marketIds);
    }
    if (edge.data.basis === "heuristic_domain") {
      const driverIds = driversByDomain.get(edge.data.target) ?? new Set();
      driverIds.add(edge.data.source);
      driversByDomain.set(edge.data.target, driverIds);
    }
  }

  for (const node of graph.nodes) {
    if (node.data.kind !== "family") continue;
    if (lens === "market") {
      groupByFamily.set(
        node.data.id,
        onlyValue(marketsByFamily.get(node.data.id)),
      );
      continue;
    }
    const domainIds = domainsByFamily.get(node.data.id);
    if (lens === "domain") {
      groupByFamily.set(node.data.id, onlyValue(domainIds));
      continue;
    }
    const driverIds = new Set<string>();
    for (const domainId of domainIds ?? []) {
      for (const driverId of driversByDomain.get(domainId) ?? []) {
        driverIds.add(driverId);
      }
    }
    groupByFamily.set(node.data.id, onlyValue(driverIds));
  }

  const uniqueFamilyByCompany = new Map<string, string>();
  for (const [companyId, familyIds] of familiesByCompany) {
    if (familyIds.size === 1) {
      uniqueFamilyByCompany.set(companyId, Array.from(familyIds)[0]);
    }
  }
  return { familiesByCompany, groupByFamily, uniqueFamilyByCompany };
}

function onlyValue(values?: ReadonlySet<string>): string {
  return values?.size === 1 ? Array.from(values)[0] : "other";
}

export function graphHulls(
  graph: MarketInfluenceGraph,
  lens: GraphLens,
): GraphHull[] {
  const grouping = groupGraph(graph, lens);
  const knownIds = new Set(graph.nodes.map((node) => node.data.id));
  const hulls: GraphHull[] = [];

  for (const node of graph.nodes) {
    if (node.data.kind !== "family") continue;
    const memberIds = [node.data.id];
    for (const [companyId, familyId] of grouping.uniqueFamilyByCompany) {
      if (familyId === node.data.id) memberIds.push(companyId);
    }
    hulls.push({
      id: `hull:family:${node.data.id}`,
      kind: "family",
      groupId: node.data.id,
      memberIds,
    });
  }

  const familiesByGroup = new Map<string, string[]>();
  for (const [familyId, groupId] of grouping.groupByFamily) {
    const families = familiesByGroup.get(groupId) ?? [];
    families.push(familyId);
    familiesByGroup.set(groupId, families);
  }
  for (const [groupId, familyIds] of familiesByGroup) {
    const familySet = new Set(familyIds);
    const memberIds = new Set<string>(familyIds);
    if (knownIds.has(groupId)) memberIds.add(groupId);
    for (const [companyId, familyIdsForCompany] of grouping.familiesByCompany) {
      if (
        familyIdsForCompany.size > 0 &&
        Array.from(familyIdsForCompany).every((familyId) =>
          familySet.has(familyId)
        )
      ) {
        memberIds.add(companyId);
      }
    }
    hulls.push({
      id: `lens:${lens}:${groupId}`,
      kind: "lens",
      groupId,
      memberIds: Array.from(memberIds),
    });
  }
  return hulls;
}

export function clusterIdForNode(
  graph: MarketInfluenceGraph,
  lens: GraphLens,
  nodeId: string,
): string | null {
  const grouping = groupGraph(graph, lens);
  const node = graph.nodes.find((item) => item.data.id === nodeId);
  if (!node) return null;
  if (node.data.kind === "family") {
    return grouping.groupByFamily.get(nodeId) ?? null;
  }
  if (node.data.kind === "company") {
    const familyIds = grouping.familiesByCompany.get(nodeId);
    if (!familyIds?.size) return null;
    const groups = new Set(
      Array.from(familyIds).map((familyId) =>
        grouping.groupByFamily.get(familyId)
      ).filter((groupId): groupId is string => Boolean(groupId)),
    );
    return groups.size === 1 ? Array.from(groups)[0] : null;
  }
  if (lens === "market" && node.data.kind === "market") return nodeId;
  if (lens === "domain" && node.data.kind === "domain") return nodeId;
  if (lens === "macro" && node.data.kind === "driver") {
    return Array.from(grouping.groupByFamily.values()).includes(nodeId)
      ? nodeId
      : null;
  }
  return null;
}

export function linkForceParams(
  basis: InfluenceEdgeBasis,
  compact = false,
): { strength: number; distance: number } {
  const band = basis === "family_membership"
    ? LINK_FORCE.family
    : basis === "governed_domain"
    ? LINK_FORCE.domain
    : basis === "market_membership"
    ? LINK_FORCE.market
    : LINK_FORCE.influence;
  return {
    strength: band.strength,
    distance: compact ? band.compactDistance : band.distance,
  };
}

export function seedNodePositions(
  nodeIds: readonly string[],
  width: number,
  height: number,
): Map<string, { x: number; y: number }> {
  const cx = width / 2;
  const cy = height / 2;
  const maxR = Math.min(width, height) * 0.38;
  const positions = new Map<string, { x: number; y: number }>();
  for (const id of nodeIds) {
    const angle = hash01(`${id}:angle`) * Math.PI * 2;
    const radius = Math.sqrt(hash01(`${id}:radius`)) * maxR;
    positions.set(id, {
      x: cx + Math.cos(angle) * radius,
      y: cy + Math.sin(angle) * radius,
    });
  }
  return positions;
}

export function focusedPath(
  graph: MarketInfluenceGraph,
  nodeId: string | null,
): { nodeIds: Set<string>; edgeIds: Set<string> } {
  if (!nodeId) return { nodeIds: new Set(), edgeIds: new Set() };
  const node = graph.nodes.find((item) => item.data.id === nodeId);
  if (!node) return { nodeIds: new Set(), edgeIds: new Set() };

  const outgoing = walkPath(graph, nodeId, "outgoing");
  const incoming = walkPath(graph, nodeId, "incoming");
  if (node.data.kind === "driver" || node.data.kind === "market") {
    return outgoing;
  }
  if (node.data.kind === "company") return incoming;
  return {
    nodeIds: new Set([...incoming.nodeIds, ...outgoing.nodeIds]),
    edgeIds: new Set([...incoming.edgeIds, ...outgoing.edgeIds]),
  };
}

export function nodeLabelVisible(input: {
  kind: InfluenceNodeKind;
  zoom: number;
  hovered?: boolean;
  focused?: boolean;
}): boolean {
  if (
    input.kind === "market" || input.kind === "domain" ||
    input.kind === "driver"
  ) {
    return true;
  }
  if (input.hovered || input.focused) return true;
  if (input.kind === "family") return input.zoom >= FAMILY_LABEL_ZOOM;
  return input.zoom >= COMPANY_LABEL_ZOOM;
}

function walkPath(
  graph: MarketInfluenceGraph,
  startId: string,
  direction: "incoming" | "outgoing",
): { nodeIds: Set<string>; edgeIds: Set<string> } {
  const nodeIds = new Set<string>([startId]);
  const edgeIds = new Set<string>();
  const queue = [startId];
  while (queue.length) {
    const current = queue.shift();
    if (!current) continue;
    for (const edge of graph.edges) {
      const next = direction === "outgoing"
        ? (edge.data.source === current ? edge.data.target : null)
        : (edge.data.target === current ? edge.data.source : null);
      if (!next) continue;
      edgeIds.add(edge.data.id);
      if (nodeIds.has(next)) continue;
      nodeIds.add(next);
      queue.push(next);
    }
  }
  return { nodeIds, edgeIds };
}

function hash01(value: string): number {
  let hash = 2166136261;
  for (let index = 0; index < value.length; index += 1) {
    hash ^= value.charCodeAt(index);
    hash = Math.imul(hash, 16777619);
  }
  return (hash >>> 0) / 4294967296;
}
