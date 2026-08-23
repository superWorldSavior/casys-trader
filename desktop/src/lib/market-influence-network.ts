import type {
  AtlasEvidenceLink,
  AtlasFamilyThread,
  AtlasNode,
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
};

export type NodeEvidence = {
  nodeId: string;
  links: readonly AtlasEvidenceLink[];
};

export type MarketInfluenceGraphInput = {
  nodes: readonly AtlasNode[];
  threads: readonly AtlasFamilyThread[];
  evidenceByNode?: readonly NodeEvidence[];
  companyNames?: Readonly<Record<string, string>>;
};

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
      addNode({
        data: {
          id: companyGraphId(symbol),
          kind: "company",
          label,
          symbol,
          scopeKey: atlasNode.scopeKey,
          detail: "Company",
        },
        classes: "company",
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
    ));
  }
  for (const evidence of input.evidenceByNode ?? []) {
    if (!atlasNodeIds.has(evidence.nodeId)) continue;
    for (const link of evidence.links) {
      addNode(driverNode(
        link.driverLabel,
        link.kind === "topic_match" ? "Possible influence" : "Shared evidence",
        link.kind === "topic_match" ? "heuristic" : "supported",
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
      addEdge({
        data: {
          id: edgeId(link.kind, source, target),
          source,
          target,
          kind: supported ? "evidence" : "possible",
          basis: link.kind,
          tone: link.tone,
          label: supported ? "Shared evidence" : "Possible influence",
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
): InfluenceGraphNode {
  return {
    data: {
      id: driverGraphId(label),
      kind: "driver",
      label,
      detail,
    },
    classes: `driver ${supportClass}`,
  };
}

function edgeId(kind: string, source: string, target: string): string {
  return `edge:${stableKey(kind)}:${stableKey(source)}:${stableKey(target)}`;
}

function unique(values: readonly string[]): string[] {
  return Array.from(new Set(values.filter(Boolean)));
}

function stableKey(value: string): string {
  return String(value)
    .trim()
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-+|-+$/g, "") || "unknown";
}
