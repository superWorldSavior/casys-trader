import {
  type AtlasDriverKind,
  type AtlasEvidenceLink,
  type AtlasFamilyThread,
  type AtlasNode,
  driverKind,
} from "./market-intelligence-atlas.ts";

export type InfluenceNodeKind =
  | "company"
  | "domain"
  | "driver"
  | "family"
  | "market";

export type InfluenceEdgeKind =
  | "correlation"
  | "evidence"
  | "membership"
  | "possible"
  | "taxonomy";

export type InfluenceEdgeBasis =
  | AtlasEvidenceLink["kind"]
  | "family_membership"
  | "governed_domain"
  | "heuristic_domain"
  | "market_membership"
  | "measured_correlation";

export type InfluenceAssertionKind =
  | "observed"
  | "inferred"
  | "hypothesized";

export type CanonicalCompanyBinding = {
  mapped: boolean;
  instrumentId?: string;
  venueId?: string;
  countryId?: string;
  regionId?: string;
};

export type InfluenceGraphNode = {
  data: {
    id: string;
    kind: InfluenceNodeKind;
    label: string;
    family?: string;
    scopeKey?: string;
    domainKey?: string;
    atlasNodeId?: string;
    symbol?: string;
    priority?: AtlasNode["priority"];
    rank?: number | null;
    detail?: string;
    holding?: CompanyHolding;
    driverKind?: AtlasDriverKind;
    activity?: number;
    activeFrom?: string | null;
    expectedUntil?: string | null;
    canonical?: CanonicalCompanyBinding;
  };
  classes: string;
};

export type InfluenceGraphEdge = {
  data: {
    id: string;
    source: string;
    target: string;
    kind: InfluenceEdgeKind;
    basis: InfluenceEdgeBasis;
    tone: AtlasEvidenceLink["tone"];
    label: string;
    driverPoint?: string;
    familyPoint?: string;
    evidenceAsOf?: string | null;
    evidenceValidUntil?: string | null;
    evidenceStatus?: string | null;
    evidenceCoverage?: string | Record<string, unknown> | null;
    effectiveFrom?: string | null;
    effectiveUntil?: string | null;
    assertionKind?: InfluenceAssertionKind;
  };
  classes: string;
};

export type MarketInfluenceGraph = {
  nodes: InfluenceGraphNode[];
  edges: InfluenceGraphEdge[];
  scopeCount: number;
  domainCount: number;
  familyCount: number;
  companyCount: number;
  supportedPathCount: number;
  possiblePathCount: number;
  projection?: {
    schemaVersion: string;
    status: string;
    generatedAt: string | null;
    cutoffAt: string | null;
    revisionId: string | null;
    canonicalNodeCount: number;
    canonicalEdgeCount: number;
    mappedCompanyCount: number;
    companyCount: number;
    missingCompanySymbols: readonly string[];
    shadowOnly: boolean;
    truncated: boolean;
  };
};

export type EvidenceFreshness = {
  as_of?: string | null;
  valid_until?: string | null;
  status?: string | null;
  coverage?: string | Record<string, unknown> | null;
};

export type NodeEvidence = EvidenceFreshness & {
  nodeId: string;
  links: readonly AtlasEvidenceLink[];
};

export type HoldingInput = {
  symbol?: string | null;
  quantity?: number | null;
  last_price?: number | null;
  fx_rate?: number | null;
};

export type CompanyHolding = {
  symbol: string;
  side: "long" | "short";
  quantity: number;
  notionalUsd: number | null;
  shareOfKnownGross: number | null;
};

export type MarketInfluenceGraphInput = {
  nodes: readonly AtlasNode[];
  threads: readonly AtlasFamilyThread[];
  evidenceByNode?: readonly NodeEvidence[];
  companyNames?: Readonly<Record<string, string>>;
  holdings?: readonly HoldingInput[] | null;
  now?: Date | string;
};

export function projectHoldings(
  holdings?: readonly HoldingInput[] | null,
): Map<string, CompanyHolding> {
  const bySymbol = new Map<string, CompanyHolding>();
  for (const row of holdings ?? []) {
    const symbol = String(row?.symbol ?? "").trim();
    const quantity = finiteNumber(row?.quantity);
    if (!symbol || quantity == null || quantity === 0) continue;
    const lastPrice = finiteNumber(row?.last_price);
    const fxRate = finiteNumber(row?.fx_rate);
    const knownPrice = lastPrice != null && lastPrice > 0;
    const knownFx = fxRate != null && fxRate > 0;
    const notionalUsd = knownPrice && knownFx
      ? Math.abs(quantity) * lastPrice * fxRate
      : null;
    bySymbol.set(symbol.toUpperCase(), {
      symbol,
      side: quantity < 0 ? "short" : "long",
      quantity,
      notionalUsd,
      shareOfKnownGross: null,
    });
  }
  let knownGross = 0;
  for (const holding of bySymbol.values()) {
    if (holding.notionalUsd != null) knownGross += holding.notionalUsd;
  }
  if (knownGross > 0) {
    for (const holding of bySymbol.values()) {
      if (holding.notionalUsd == null) continue;
      holding.shareOfKnownGross = holding.notionalUsd / knownGross;
    }
  }
  return bySymbol;
}

/**
 * Build one complete, directed market topology.
 *
 * Every market and family remains in the graph. Governed domains and market
 * memberships describe structure; they are not causal claims. Brief evidence
 * is connected only to the family for which it was observed, while a domain's
 * generic driver is explicitly marked as a possible influence. Selection is a
 * rendering concern and never changes this topology.
 *
 * The public node/edge vocabulary already reserves `company`,
 * `family_membership`, `correlation`, and `measured_correlation` so those
 * future layers can be added without changing the graph contract.
 */
export function buildMarketInfluenceGraph(
  input: MarketInfluenceGraphInput,
): MarketInfluenceGraph {
  const nodes: InfluenceGraphNode[] = [];
  const edges: InfluenceGraphEdge[] = [];
  const seenNodeIds = new Set<string>();
  const nodeById = new Map<string, InfluenceGraphNode>();
  const seenEdgeIds = new Set<string>();
  const atlasNodeIds = new Set(input.nodes.map((node) => node.id));
  const companyNames = new Map(
    Object.entries(input.companyNames ?? {}).map(([symbol, name]) => [
      symbol.trim().toUpperCase(),
      String(name).trim(),
    ]),
  );
  const holdings = projectHoldings(input.holdings);
  const now = referenceDate(input.now);

  const addNode = (node: InfluenceGraphNode) => {
    const existing = nodeById.get(node.data.id);
    if (existing) {
      if (
        existing.data.kind === "driver" && node.data.kind === "driver" &&
        node.classes.split(" ").includes("supported") &&
        !existing.classes.split(" ").includes("supported")
      ) {
        existing.classes = existing.classes
          .split(" ")
          .filter((className) => className !== "heuristic")
          .concat("supported")
          .join(" ");
        existing.data.detail = node.data.detail;
      }
      if (node.data.kind === "driver" && existing.data.kind === "driver") {
        existing.data.activity = Math.max(
          existing.data.activity ?? 0,
          node.data.activity ?? 0,
        );
        existing.data.activeFrom = earliestTimestamp(
          existing.data.activeFrom,
          node.data.activeFrom,
        );
        existing.data.expectedUntil = latestTimestamp(
          existing.data.expectedUntil,
          node.data.expectedUntil,
        );
        if (
          (!existing.data.driverKind ||
            existing.data.driverKind === "observed_signal") &&
          node.data.driverKind && node.data.driverKind !== "observed_signal"
        ) {
          existing.data.driverKind = node.data.driverKind;
        }
      }
      return;
    }
    seenNodeIds.add(node.data.id);
    nodeById.set(node.data.id, node);
    nodes.push(node);
  };
  const addEdge = (edge: InfluenceGraphEdge) => {
    if (
      seenEdgeIds.has(edge.data.id) ||
      !seenNodeIds.has(edge.data.source) ||
      !seenNodeIds.has(edge.data.target)
    ) return;
    seenEdgeIds.add(edge.data.id);
    edges.push(edge);
  };

  const scopeKeys = unique(input.nodes.map((node) => node.scopeKey));
  for (const scopeKey of scopeKeys) {
    addNode({
      data: {
        id: marketGraphId(scopeKey),
        kind: "market",
        label: scopeKey,
        scopeKey,
        detail: "Market",
      },
      classes: "market structural-node",
    });
  }

  for (const thread of input.threads) {
    addNode({
      data: {
        id: domainGraphId(thread.key),
        kind: "domain",
        label: thread.label,
        domainKey: thread.key,
        detail: "Market domain",
      },
      classes: "domain structural-node",
    });
  }

  for (const atlasNode of input.nodes) {
    addNode({
      data: {
        id: familyGraphId(atlasNode.id),
        kind: "family",
        label: atlasNode.family,
        family: atlasNode.family,
        scopeKey: atlasNode.scopeKey,
        atlasNodeId: atlasNode.id,
        priority: atlasNode.priority,
        rank: atlasNode.rank,
        detail: "Market theme",
      },
      classes: [
        "family",
        `priority-${atlasNode.priority}`,
        atlasNode.rank == null ? "unranked" : "ranked",
      ].join(" "),
    });
  }

  for (const atlasNode of input.nodes) {
    for (const symbol of atlasNode.symbols) {
      const label = companyNames.get(symbol.toUpperCase()) || symbol;
      const holding = holdings.get(symbol.toUpperCase());
      addNode({
        data: {
          id: companyGraphId(symbol),
          kind: "company",
          label,
          symbol,
          scopeKey: atlasNode.scopeKey,
          detail: "Company",
          ...(holding ? { holding } : {}),
        },
        classes: holding ? "company held" : "company",
      });
    }
  }

  // Add all drivers before their edges so one shared factor can fan out to
  // several domains or families without being duplicated.
  for (const thread of input.threads) {
    addNode(driverNode(
      thread.possibleInfluence,
      "Possible influence",
      "heuristic",
      driverKind(thread.possibleInfluence),
      { activity: 0.52 },
    ));
  }
  for (const evidence of input.evidenceByNode ?? []) {
    if (!atlasNodeIds.has(evidence.nodeId)) continue;
    for (const link of evidence.links) {
      addNode(driverNode(
        link.driverLabel,
        link.kind === "topic_match"
          ? "Possible influence"
          : "Observed association",
        link.kind === "topic_match" ? "heuristic" : "supported",
        link.driverKind,
        driverActivation(evidence, now),
      ));
    }
  }

  for (const atlasNode of input.nodes) {
    addEdge({
      data: {
        id: edgeId("market", atlasNode.scopeKey, atlasNode.id),
        source: marketGraphId(atlasNode.scopeKey),
        target: familyGraphId(atlasNode.id),
        kind: "membership",
        basis: "market_membership",
        tone: "context",
        label: "In this market",
      },
      classes: "structural-path market-path",
    });
    for (const symbol of atlasNode.symbols) {
      addEdge({
        data: {
          id: edgeId("company", atlasNode.id, symbol),
          source: familyGraphId(atlasNode.id),
          target: companyGraphId(symbol),
          kind: "membership",
          basis: "family_membership",
          tone: "context",
          label: "Company in this theme",
        },
        classes: "structural-path company-path",
      });
    }
  }

  for (const thread of input.threads) {
    const domainId = domainGraphId(thread.key);
    const possibleDriverId = driverGraphId(thread.possibleInfluence);
    addEdge({
      data: {
        id: edgeId("possible", possibleDriverId, domainId),
        source: possibleDriverId,
        target: domainId,
        kind: "possible",
        basis: "heuristic_domain",
        tone: "context",
        label: "Possible influence",
        assertionKind: "hypothesized",
      },
      classes: "influence-path possible",
    });

    for (const atlasNodeId of unique(thread.nodeIds)) {
      if (!atlasNodeIds.has(atlasNodeId)) continue;
      addEdge({
        data: {
          id: edgeId("domain", thread.key, atlasNodeId),
          source: domainId,
          target: familyGraphId(atlasNodeId),
          kind: "taxonomy",
          basis: "governed_domain",
          tone: "context",
          label: "Part of this domain",
        },
        classes: "structural-path domain-path",
      });
    }
  }

  let supportedPathCount = 0;
  let possiblePathCount = input.threads.length;
  for (const evidence of input.evidenceByNode ?? []) {
    if (!atlasNodeIds.has(evidence.nodeId)) continue;
    for (const link of evidence.links) {
      const supported = link.kind !== "topic_match";
      const source = driverGraphId(link.driverLabel);
      const target = familyGraphId(evidence.nodeId);
      const driverPoint = String(link.driverPoint ?? "").trim();
      const familyPoint = String(link.familyPoint ?? "").trim();
      const freshness = supported ? evidenceFreshness(evidence) : null;
      const activation = driverActivation(evidence, now);
      addEdge({
        data: {
          id: edgeId(link.kind, source, target),
          source,
          target,
          kind: supported ? "evidence" : "possible",
          basis: link.kind,
          tone: link.tone,
          label: supported ? "Observed association" : "Possible influence",
          assertionKind: supported
            ? link.kind === "shared_source" ? "observed" : "inferred"
            : "hypothesized",
          effectiveFrom: activation.activeFrom ?? null,
          effectiveUntil: activation.expectedUntil ?? null,
          ...(supported && driverPoint ? { driverPoint } : {}),
          ...(supported && familyPoint ? { familyPoint } : {}),
          ...(freshness ?? {}),
        },
        classes: [
          "influence-path",
          supported ? "evidence" : "possible",
          `tone-${link.tone}`,
        ].join(" "),
      });
      if (supported) supportedPathCount += 1;
      else possiblePathCount += 1;
    }
  }

  return {
    nodes,
    edges,
    scopeCount: scopeKeys.length,
    domainCount: input.threads.length,
    familyCount: input.nodes.length,
    companyCount: nodes.filter((node) => node.data.kind === "company").length,
    supportedPathCount,
    possiblePathCount,
  };
}

export type GuidedRelationshipKind =
  | "shared_evidence"
  | "possible"
  | "structure";

export type GuidedPortfolioPath = {
  nodeIds: string[];
  edgeIds: string[];
  kind: GuidedRelationshipKind;
};

/**
 * Pick a stable first-read node for the market map.
 *
 * Known USD holdings win by size. Unknown money is never treated as zero and
 * is never compared across currencies. Without a represented holding, a favored
 * family with direct shared evidence wins, then a supplied theme, then rank.
 */
export function chooseInitialPortfolioFocus(
  graph: MarketInfluenceGraph,
  fallbackThemeId?: string | null,
): string | null {
  const held = graph.nodes.filter((node) =>
    node.data.kind === "company" && node.data.holding
  );
  const known = held.filter((node) => node.data.holding?.notionalUsd != null);
  if (known.length) {
    return [...known].sort((left, right) => {
      const notional = (right.data.holding?.notionalUsd ?? 0) -
        (left.data.holding?.notionalUsd ?? 0);
      if (notional !== 0) return notional;
      return compareSymbolThenId(left, right);
    })[0].data.id;
  }
  if (held.length) {
    return [...held].sort(compareSymbolThenId)[0].data.id;
  }
  const evidenced = favoredFamiliesWithDirectEvidence(graph);
  if (evidenced.length) {
    return [...evidenced].sort(compareFamilyNodes)[0].data.id;
  }
  const fallback = resolveFallbackTheme(graph, fallbackThemeId);
  if (fallback) return fallback;
  const families = graph.nodes.filter((node) => node.data.kind === "family");
  if (!families.length) return null;
  return [...families].sort(compareFamilyNodes)[0].data.id;
}

/**
 * One bounded, non-causal context thread for a company.
 *
 * Allowed shapes only: shared-source evidence, a heuristic domain chain, or
 * structural membership. Neighboring companies and extra upstream branches
 * are never included.
 */
export function guidedPortfolioPath(
  graph: MarketInfluenceGraph,
  companyId: string,
  originId?: string | null,
): GuidedPortfolioPath {
  const candidates = collectGuidedPaths(graph, companyId);
  const origin = String(originId ?? "").trim();
  const fromOrigin = origin && origin !== companyId
    ? candidates.filter((path) => path.nodeIds[0] === origin)
    : [];
  const pool = fromOrigin.length ? fromOrigin : candidates;
  if (!pool.length) {
    return {
      nodeIds: companyId ? [companyId] : [],
      edgeIds: [],
      kind: "structure",
    };
  }
  const chosen = [...pool].sort(compareGuidedPaths)[0];
  return {
    nodeIds: chosen.nodeIds,
    edgeIds: chosen.edgeIds,
    kind: chosen.kind,
  };
}

export function recordedEvidencePoints(
  edge: InfluenceGraphEdge,
): { driverPoint: string; familyPoint: string } | null {
  if (
    edge.data.basis !== "shared_source" &&
    edge.data.basis !== "shared_symbol"
  ) {
    return null;
  }
  return {
    driverPoint: String(edge.data.driverPoint ?? "").trim(),
    familyPoint: String(edge.data.familyPoint ?? "").trim(),
  };
}

export function recordedEvidenceFreshness(
  edge: InfluenceGraphEdge,
): EvidenceFreshness | null {
  if (!recordedEvidencePoints(edge)) return null;
  const freshness: EvidenceFreshness = {
    as_of: edge.data.evidenceAsOf ?? null,
    valid_until: edge.data.evidenceValidUntil ?? null,
    status: edge.data.evidenceStatus ?? null,
    coverage: edge.data.evidenceCoverage ?? null,
  };
  if (
    !freshness.as_of &&
    !freshness.valid_until &&
    !freshness.status &&
    freshness.coverage == null
  ) {
    return null;
  }
  return freshness;
}

export type DirectedContextPath = {
  nodeIds: string[];
  edgeIds: string[];
};

/**
 * One bounded directed context thread from origin to target.
 *
 * Governed shapes only, at most three edges, no undirected expansion.
 * This is context, not a causal claim.
 */
export function directedContextPath(
  graph: MarketInfluenceGraph,
  originId?: string | null,
  targetId?: string | null,
): DirectedContextPath | null {
  const origin = String(originId ?? "").trim();
  const target = String(targetId ?? "").trim();
  if (!origin || !target || origin === target) return null;
  const known = new Set(graph.nodes.map((node) => node.data.id));
  if (!known.has(origin) || !known.has(target)) return null;

  const outgoing = new Map<string, InfluenceGraphEdge[]>();
  for (const edge of graph.edges) {
    const list = outgoing.get(edge.data.source) ?? [];
    list.push(edge);
    outgoing.set(edge.data.source, list);
  }
  for (const list of outgoing.values()) {
    list.sort((left, right) => left.data.id.localeCompare(right.data.id));
  }

  type Frame = { nodeId: string; nodeIds: string[]; edgeIds: string[] };
  const queue: Frame[] = [{ nodeId: origin, nodeIds: [origin], edgeIds: [] }];
  const seen = new Set<string>([origin]);
  while (queue.length) {
    const current = queue.shift();
    if (!current || current.edgeIds.length >= 3) continue;
    for (const edge of outgoing.get(current.nodeId) ?? []) {
      const next = edge.data.target;
      if (seen.has(next)) continue;
      const nodeIds = [...current.nodeIds, next];
      const edgeIds = [...current.edgeIds, edge.data.id];
      if (next === target) return { nodeIds, edgeIds };
      seen.add(next);
      queue.push({ nodeId: next, nodeIds, edgeIds });
    }
  }
  return null;
}

export function impliedPathOrigin(
  graph: MarketInfluenceGraph,
  targetId: string,
  currentOriginId?: string | null,
  currentFocusId?: string | null,
): string | null {
  for (const candidate of [currentOriginId, currentFocusId]) {
    const origin = String(candidate ?? "").trim();
    if (!origin || origin === targetId) continue;
    if (directedContextPath(graph, origin, targetId)) return origin;
  }
  return null;
}

export function retainedPathOrigin(
  graph: MarketInfluenceGraph,
  focusId?: string | null,
  originId?: string | null,
): string | null {
  const focus = String(focusId ?? "").trim();
  const origin = String(originId ?? "").trim();
  if (!focus || !origin) return null;
  if (!graph.nodes.some((node) => node.data.id === focus)) return null;
  return directedContextPath(graph, origin, focus) ? origin : null;
}

export function familyGraphId(atlasNodeId: string): string {
  return `family:${atlasNodeId}`;
}

function domainGraphId(domainKey: string): string {
  return `domain:${stableKey(domainKey)}`;
}

function marketGraphId(scopeKey: string): string {
  return `market:${stableKey(scopeKey)}`;
}

function driverGraphId(label: string): string {
  return `driver:${stableKey(label)}`;
}

function companyGraphId(symbol: string): string {
  return `company:${stableKey(symbol)}`;
}

function driverNode(
  label: string,
  detail: string,
  supportClass: "heuristic" | "supported",
  kind: AtlasDriverKind,
  activation: DriverActivation,
): InfluenceGraphNode {
  return {
    data: {
      id: driverGraphId(label),
      kind: "driver",
      label,
      detail,
      driverKind: kind,
      activity: activation.activity,
      activeFrom: activation.activeFrom ?? null,
      expectedUntil: activation.expectedUntil ?? null,
    },
    classes: `driver ${supportClass}`,
  };
}

type DriverActivation = {
  activity: number;
  activeFrom?: string | null;
  expectedUntil?: string | null;
};

function driverActivation(
  evidence: EvidenceFreshness,
  now: Date,
): DriverActivation {
  const activeFrom = optionalTimestamp(evidence.as_of);
  const expectedUntil = optionalTimestamp(evidence.valid_until);
  const status = String(evidence.status ?? "").trim().toLowerCase();
  let activity = status === "stale" || status === "expired" ? 0.34 : 1;
  const until = expectedUntil ? Date.parse(expectedUntil) : Number.NaN;
  const from = activeFrom ? Date.parse(activeFrom) : Number.NaN;
  if (Number.isFinite(until)) {
    const remaining = until - now.getTime();
    if (remaining <= 0) {
      activity = Math.min(activity, 0.3);
    } else if (Number.isFinite(from) && until > from) {
      const lifetime = until - from;
      const fadeWindow = Math.max(60 * 60 * 1000, lifetime * 0.35);
      if (remaining < fadeWindow) {
        activity = Math.min(
          activity,
          0.46 + 0.54 * Math.max(0, remaining / fadeWindow),
        );
      }
    }
  }
  return {
    activity: Math.max(0.24, Math.min(1, activity)),
    activeFrom,
    expectedUntil,
  };
}

function referenceDate(value?: Date | string): Date {
  if (value instanceof Date && Number.isFinite(value.getTime())) return value;
  const parsed = typeof value === "string" ? new Date(value) : new Date();
  return Number.isFinite(parsed.getTime()) ? parsed : new Date();
}

function optionalTimestamp(value?: string | null): string | null {
  const text = String(value ?? "").trim();
  return text || null;
}

function earliestTimestamp(
  left?: string | null,
  right?: string | null,
): string | null {
  if (!left) return right ?? null;
  if (!right) return left;
  const leftTime = Date.parse(left);
  const rightTime = Date.parse(right);
  if (!Number.isFinite(leftTime)) return right;
  if (!Number.isFinite(rightTime)) return left;
  return leftTime <= rightTime ? left : right;
}

function latestTimestamp(
  left?: string | null,
  right?: string | null,
): string | null {
  if (!left) return right ?? null;
  if (!right) return left;
  const leftTime = Date.parse(left);
  const rightTime = Date.parse(right);
  if (!Number.isFinite(leftTime)) return right;
  if (!Number.isFinite(rightTime)) return left;
  return leftTime >= rightTime ? left : right;
}

function edgeId(kind: string, source: string, target: string): string {
  return `edge:${stableKey(kind)}:${stableKey(source)}:${stableKey(target)}`;
}

function unique(values: readonly string[]): string[] {
  return Array.from(new Set(values.filter(Boolean)));
}

function evidenceFreshness(
  evidence: NodeEvidence,
): Partial<InfluenceGraphEdge["data"]> | null {
  const asOf = String(evidence.as_of ?? "").trim();
  const validUntil = String(evidence.valid_until ?? "").trim();
  const status = String(evidence.status ?? "").trim();
  const coverage = evidence.coverage ?? null;
  if (!asOf && !validUntil && !status && coverage == null) return null;
  return {
    ...(asOf ? { evidenceAsOf: asOf } : {}),
    ...(validUntil ? { evidenceValidUntil: validUntil } : {}),
    ...(status ? { evidenceStatus: status } : {}),
    ...(coverage != null ? { evidenceCoverage: coverage } : {}),
  };
}

type GuidedPathCandidate = GuidedPortfolioPath & {
  familyId: string;
  familyPriority: number;
  familyRank: number;
  familyScope: string;
  structureRank: number;
  startId: string;
};

function collectGuidedPaths(
  graph: MarketInfluenceGraph,
  companyId: string,
): GuidedPathCandidate[] {
  const nodeById = new Map(
    graph.nodes.map((node) => [node.data.id, node] as const),
  );
  const company = nodeById.get(companyId);
  if (!company || company.data.kind !== "company") return [];

  const candidates: GuidedPathCandidate[] = [];
  for (const memberEdge of graph.edges) {
    if (
      memberEdge.data.target !== companyId ||
      memberEdge.data.basis !== "family_membership"
    ) continue;
    const family = nodeById.get(memberEdge.data.source);
    if (!family || family.data.kind !== "family") continue;
    const familyId = family.data.id;
    const intoFamily = graph.edges.filter((edge) =>
      edge.data.target === familyId
    );

    for (const evidence of intoFamily) {
      if (
        evidence.data.basis !== "shared_source" &&
        evidence.data.basis !== "shared_symbol"
      ) continue;
      const driver = nodeById.get(evidence.data.source);
      if (driver?.data.kind !== "driver") continue;
      candidates.push(
        guidedCandidate(
          family,
          "shared_evidence",
          0,
          [driver.data.id, familyId, companyId],
          [evidence.data.id, memberEdge.data.id],
        ),
      );
    }

    for (const governed of intoFamily) {
      if (governed.data.basis !== "governed_domain") continue;
      const domain = nodeById.get(governed.data.source);
      if (domain?.data.kind !== "domain") continue;
      for (const heuristic of graph.edges) {
        if (
          heuristic.data.target !== domain.data.id ||
          heuristic.data.basis !== "heuristic_domain"
        ) continue;
        const driver = nodeById.get(heuristic.data.source);
        if (driver?.data.kind !== "driver") continue;
        candidates.push(
          guidedCandidate(
            family,
            "possible",
            0,
            [driver.data.id, domain.data.id, familyId, companyId],
            [heuristic.data.id, governed.data.id, memberEdge.data.id],
          ),
        );
      }
      candidates.push(
        guidedCandidate(
          family,
          "structure",
          1,
          [domain.data.id, familyId, companyId],
          [governed.data.id, memberEdge.data.id],
        ),
      );
    }

    for (const marketEdge of intoFamily) {
      if (marketEdge.data.basis !== "market_membership") continue;
      const market = nodeById.get(marketEdge.data.source);
      if (market?.data.kind !== "market") continue;
      candidates.push(
        guidedCandidate(
          family,
          "structure",
          0,
          [market.data.id, familyId, companyId],
          [marketEdge.data.id, memberEdge.data.id],
        ),
      );
    }

    candidates.push(
      guidedCandidate(
        family,
        "structure",
        2,
        [familyId, companyId],
        [memberEdge.data.id],
      ),
    );
  }
  return candidates;
}

function guidedCandidate(
  family: InfluenceGraphNode,
  kind: GuidedRelationshipKind,
  structureRank: number,
  nodeIds: string[],
  edgeIds: string[],
): GuidedPathCandidate {
  return {
    nodeIds,
    edgeIds,
    kind,
    familyId: family.data.id,
    familyPriority: familyPriorityRank(family),
    familyRank: familyRankValue(family),
    familyScope: family.data.scopeKey ?? "",
    structureRank,
    startId: nodeIds[0] ?? "",
  };
}

function compareGuidedPaths(
  left: GuidedPathCandidate,
  right: GuidedPathCandidate,
): number {
  const kind = relationshipRank(left.kind) - relationshipRank(right.kind);
  if (kind !== 0) return kind;
  const family = compareFamilyMeta(left, right);
  if (family !== 0) return family;
  const structure = left.structureRank - right.structureRank;
  if (structure !== 0) return structure;
  const start = left.startId.localeCompare(right.startId);
  if (start !== 0) return start;
  return left.nodeIds.join("\0").localeCompare(right.nodeIds.join("\0"));
}

function compareFamilyMeta(
  left: Pick<
    GuidedPathCandidate,
    "familyPriority" | "familyRank" | "familyScope" | "familyId"
  >,
  right: Pick<
    GuidedPathCandidate,
    "familyPriority" | "familyRank" | "familyScope" | "familyId"
  >,
): number {
  if (left.familyPriority !== right.familyPriority) {
    return left.familyPriority - right.familyPriority;
  }
  if (left.familyRank !== right.familyRank) {
    return left.familyRank - right.familyRank;
  }
  const scope = left.familyScope.localeCompare(right.familyScope);
  if (scope !== 0) return scope;
  return left.familyId.localeCompare(right.familyId);
}

function compareFamilyNodes(
  left: InfluenceGraphNode,
  right: InfluenceGraphNode,
): number {
  return compareFamilyMeta({
    familyPriority: familyPriorityRank(left),
    familyRank: familyRankValue(left),
    familyScope: left.data.scopeKey ?? "",
    familyId: left.data.id,
  }, {
    familyPriority: familyPriorityRank(right),
    familyRank: familyRankValue(right),
    familyScope: right.data.scopeKey ?? "",
    familyId: right.data.id,
  });
}

function compareSymbolThenId(
  left: InfluenceGraphNode,
  right: InfluenceGraphNode,
): number {
  const symbol = normalizedSymbol(left.data.symbol).localeCompare(
    normalizedSymbol(right.data.symbol),
  );
  if (symbol !== 0) return symbol;
  return left.data.id.localeCompare(right.data.id);
}

function favoredFamiliesWithDirectEvidence(
  graph: MarketInfluenceGraph,
): InfluenceGraphNode[] {
  const familyById = new Map(
    graph.nodes
      .filter((node) => node.data.kind === "family")
      .map((node) => [node.data.id, node] as const),
  );
  const evidenced = new Set<string>();
  for (const edge of graph.edges) {
    if (
      edge.data.basis !== "shared_source" &&
      edge.data.basis !== "shared_symbol"
    ) continue;
    const family = familyById.get(edge.data.target);
    if (!family || family.data.priority !== "favored") continue;
    const source = graph.nodes.find((node) =>
      node.data.id === edge.data.source
    );
    if (source?.data.kind !== "driver") continue;
    evidenced.add(family.data.id);
  }
  return Array.from(evidenced)
    .map((id) => familyById.get(id))
    .filter((node): node is InfluenceGraphNode => Boolean(node));
}

function resolveFallbackTheme(
  graph: MarketInfluenceGraph,
  fallbackThemeId?: string | null,
): string | null {
  const raw = String(fallbackThemeId ?? "").trim();
  if (!raw) return null;
  for (const id of [raw, familyGraphId(raw)]) {
    const node = graph.nodes.find((item) => item.data.id === id);
    if (node?.data.kind === "family") return node.data.id;
  }
  return graph.nodes.find((item) =>
    item.data.kind === "family" && item.data.atlasNodeId === raw
  )?.data.id ?? null;
}

function familyPriorityRank(node: InfluenceGraphNode): number {
  if (node.data.priority === "favored") return 0;
  if (node.data.priority === "deprioritized") return 2;
  return 1;
}

function familyRankValue(node: InfluenceGraphNode): number {
  return typeof node.data.rank === "number" && Number.isFinite(node.data.rank)
    ? node.data.rank
    : Number.POSITIVE_INFINITY;
}

function relationshipRank(kind: GuidedRelationshipKind): number {
  if (kind === "shared_evidence") return 0;
  if (kind === "possible") return 1;
  return 2;
}

function normalizedSymbol(value?: string): string {
  return String(value ?? "").trim().toUpperCase();
}

function finiteNumber(value: unknown): number | null {
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

function stableKey(value: string): string {
  return String(value)
    .trim()
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-+|-+$/g, "") || "unknown";
}
