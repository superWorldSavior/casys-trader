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
export const LENS_HULL_PADDING = 28;
export const FAMILY_HULL_PADDING = 14;
const RADIUS_BOUNDS: Record<
  Exclude<InfluenceNodeKind, "company">,
  { min: number; max: number }
> = {
  market: { min: 15, max: 22 },
  domain: { min: 11.5, max: 16.5 },
  driver: { min: 7.5, max: 11.5 },
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
  nodeId: string | null,
): string | null {
  if (!nodeId) return null;
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

/** Explorer opens only a uniquely resolved group; a previous group is never kept. */
export function explorerFallbackForFocus(
  graph: MarketInfluenceGraph,
  lens: GraphLens,
  nodeId: string | null,
  _previousFallback: string | null = null,
): string | null {
  return clusterIdForNode(graph, lens, nodeId);
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

export type GroupEnvelope = {
  id: string;
  x: number;
  y: number;
  halfWidth: number;
  halfHeight: number;
};

export type GroupDisplacement = {
  id: string;
  dx: number;
  dy: number;
};

export function groupEnvelope(
  members: readonly { x: number; y: number; radius: number }[],
  padding = LENS_HULL_PADDING,
): Omit<GroupEnvelope, "id"> | null {
  if (!members.length) return null;
  let minX = Infinity;
  let minY = Infinity;
  let maxX = -Infinity;
  let maxY = -Infinity;
  for (const member of members) {
    minX = Math.min(minX, member.x - member.radius - padding);
    minY = Math.min(minY, member.y - member.radius - padding);
    maxX = Math.max(maxX, member.x + member.radius + padding);
    maxY = Math.max(maxY, member.y + member.radius + padding);
  }
  return {
    x: (minX + maxX) / 2,
    y: (minY + maxY) / 2,
    halfWidth: (maxX - minX) / 2,
    halfHeight: (maxY - minY) / 2,
  };
}

export type GroupEnvelopeOptions = {
  passes?: number;
  gap?: number;
  fixedIds?: ReadonlySet<string>;
};

export type SiblingGroupEnvelope = GroupEnvelope & {
  parentId: string;
};

export function separateGroupEnvelopes(
  envelopes: readonly GroupEnvelope[],
  options?: GroupEnvelopeOptions,
): GroupDisplacement[] {
  const passes = options?.passes ?? 6;
  const gap = options?.gap ?? 4;
  const fixedIds = options?.fixedIds;
  const work = envelopes
    .map((item) => ({ ...item, dx: 0, dy: 0 }))
    .sort((left, right) => left.id.localeCompare(right.id));
  for (let pass = 0; pass < passes; pass += 1) {
    for (let index = 0; index < work.length; index += 1) {
      for (let other = index + 1; other < work.length; other += 1) {
        const left = work[index];
        const right = work[other];
        const leftFixed = fixedIds?.has(left.id) === true;
        const rightFixed = fixedIds?.has(right.id) === true;
        if (leftFixed && rightFixed) continue;
        const overlapX = left.halfWidth + right.halfWidth + gap -
          Math.abs(left.x - right.x);
        const overlapY = left.halfHeight + right.halfHeight + gap -
          Math.abs(left.y - right.y);
        if (overlapX <= 0 || overlapY <= 0) continue;
        if (overlapX <= overlapY) {
          applyEnvelopeAxisPush(
            left,
            right,
            "x",
            overlapX,
            leftFixed,
            rightFixed,
          );
        } else {
          applyEnvelopeAxisPush(
            left,
            right,
            "y",
            overlapY,
            leftFixed,
            rightFixed,
          );
        }
      }
    }
  }
  return work
    .slice()
    .sort((left, right) => left.id.localeCompare(right.id))
    .map((item) => ({ id: item.id, dx: item.dx, dy: item.dy }));
}

export function separateSiblingGroupEnvelopes(
  envelopes: readonly SiblingGroupEnvelope[],
  options?: GroupEnvelopeOptions,
): GroupDisplacement[] {
  const byParent = new Map<string, GroupEnvelope[]>();
  for (const envelope of envelopes) {
    const group = byParent.get(envelope.parentId) ?? [];
    group.push(envelope);
    byParent.set(envelope.parentId, group);
  }
  const moves: GroupDisplacement[] = [];
  for (const parentId of Array.from(byParent.keys()).sort()) {
    const group = byParent.get(parentId);
    if (!group) continue;
    moves.push(...separateGroupEnvelopes(group, options));
  }
  return moves.sort((left, right) => left.id.localeCompare(right.id));
}

type EnvelopeWork = GroupEnvelope & { dx: number; dy: number };

function applyEnvelopeAxisPush(
  left: EnvelopeWork,
  right: EnvelopeWork,
  axis: "x" | "y",
  overlap: number,
  leftFixed: boolean,
  rightFixed: boolean,
): void {
  const delta = axis === "x" ? "dx" : "dy";
  const dir = left[axis] <= right[axis] ? -1 : 1;
  if (leftFixed) {
    right[axis] -= dir * overlap;
    right[delta] -= dir * overlap;
    return;
  }
  if (rightFixed) {
    left[axis] += dir * overlap;
    left[delta] += dir * overlap;
    return;
  }
  const push = overlap / 2;
  left[axis] += dir * push;
  right[axis] -= dir * push;
  left[delta] += dir * push;
  right[delta] -= dir * push;
}

export type LabelPriority =
  | "selected"
  | "hovered"
  | "path"
  | "lens"
  | "market"
  | "secondary";

export type LabelRequest = {
  id: string;
  text: string;
  x: number;
  y: number;
  nodeRadius: number;
  fontSize: number;
  priority: LabelPriority;
};

export type PlacedLabel = {
  id: string;
  x: number;
  y: number;
  width: number;
  height: number;
  left: number;
  top: number;
  visible: boolean;
  leader: boolean;
  textAnchor: "start" | "middle" | "end";
};

const LABEL_PRIORITY_RANK: Record<LabelPriority, number> = {
  selected: 0,
  hovered: 1,
  path: 2,
  lens: 3,
  market: 4,
  secondary: 5,
};

const REQUIRED_LABEL = new Set<LabelPriority>([
  "selected",
  "hovered",
]);

export const LABEL_CANVAS_INSET = 8;
export const MAX_LABEL_LEADER_DISTANCE = 64;

export type GraphLabelBounds = {
  width: number;
  height: number;
  inset?: number;
};

export type GraphLabelOptions = {
  bounds?: GraphLabelBounds;
  maxLeaderDistance?: number;
};

export function placeGraphLabels(
  requests: readonly LabelRequest[],
  options?: GraphLabelOptions,
): PlacedLabel[] {
  const accepted: PlacedLabel[] = [];
  const byId = new Map<string, PlacedLabel>();
  const bounds = options?.bounds;
  const inset = bounds?.inset ?? LABEL_CANVAS_INSET;
  const maxLeader = options?.maxLeaderDistance ?? MAX_LABEL_LEADER_DISTANCE;
  const ordered = requests
    .map((item, index) => ({ item, index }))
    .sort((left, right) =>
      LABEL_PRIORITY_RANK[left.item.priority] -
        LABEL_PRIORITY_RANK[right.item.priority] ||
      left.item.id.localeCompare(right.item.id) ||
      left.index - right.index
    );

  for (const { item } of ordered) {
    const size = estimateLabelSize(item.text, item.fontSize);
    const required = REQUIRED_LABEL.has(item.priority);
    let placed: PlacedLabel | null = null;
    for (const slot of labelSlots(item.nodeRadius, required, maxLeader)) {
      const candidate = makeLabelCandidate(item, size, slot);
      if (
        !labelCandidateAllowed(
          candidate,
          item,
          accepted,
          bounds,
          inset,
          maxLeader,
        )
      ) continue;
      placed = candidate;
      break;
    }
    if (!placed && required) {
      placed = forceRequiredLabel(
        item,
        size,
        accepted,
        bounds,
        inset,
        maxLeader,
      );
    }
    if (!placed) {
      placed = {
        id: item.id,
        x: item.x + item.nodeRadius + 8,
        y: item.y,
        width: size.width,
        height: size.height,
        left: item.x + item.nodeRadius + 8,
        top: item.y - size.height / 2,
        visible: false,
        leader: false,
        textAnchor: "start",
      };
    }
    if (placed.visible) accepted.push(placed);
    byId.set(item.id, placed);
  }

  return requests.map((item) =>
    byId.get(item.id) ?? {
      id: item.id,
      x: item.x,
      y: item.y,
      width: 0,
      height: 0,
      left: item.x,
      top: item.y,
      visible: false,
      leader: false,
      textAnchor: "middle",
    }
  );
}

export type ExplorerGroup = {
  id: string;
  childIds: string[];
};

export function explorerGroups(
  graph: MarketInfluenceGraph,
  lens: GraphLens,
): ExplorerGroup[] {
  const grouping = groupGraph(graph, lens);
  const childrenByGroup = new Map<string, string[]>();
  for (const node of graph.nodes) {
    if (node.data.kind !== "family") continue;
    const groupId = grouping.groupByFamily.get(node.data.id) ?? "other";
    const children = childrenByGroup.get(groupId) ?? [];
    children.push(node.data.id);
    childrenByGroup.set(groupId, children);
  }
  const groupedDriverIds = new Set(grouping.groupByFamily.values());
  const roots = graph.nodes.filter((node) => {
    if (lens === "market") return node.data.kind === "market";
    if (lens === "domain") return node.data.kind === "domain";
    return node.data.kind === "driver" && groupedDriverIds.has(node.data.id);
  });
  const groups = roots.map((node) => ({
    id: node.data.id,
    childIds: childrenByGroup.get(node.data.id) ?? [],
  }));
  const otherChildren = childrenByGroup.get("other") ?? [];
  if (otherChildren.length) {
    groups.push({ id: "other", childIds: otherChildren });
  }
  return groups;
}

function estimateLabelSize(
  text: string,
  fontSize: number,
): { width: number; height: number } {
  const width = Math.max(12, Array.from(text).length * fontSize * 0.62) + 8;
  const height = fontSize * 1.25 + 4;
  return { width, height };
}

function labelBox(
  x: number,
  y: number,
  width: number,
  height: number,
  anchor: "start" | "middle" | "end",
): { left: number; top: number } {
  const left = anchor === "start"
    ? x
    : anchor === "end"
    ? x - width
    : x - width / 2;
  return { left, top: y - height / 2 };
}

function labelRectsOverlap(
  left: { left: number; top: number; width: number; height: number },
  right: { left: number; top: number; width: number; height: number },
): boolean {
  return left.left < right.left + right.width &&
    left.left + left.width > right.left &&
    left.top < right.top + right.height &&
    left.top + left.height > right.top;
}

type LabelSlot = {
  dx: number;
  dy: number;
  anchor: "start" | "middle" | "end";
  leader: boolean;
};

function makeLabelCandidate(
  item: LabelRequest,
  size: { width: number; height: number },
  slot: LabelSlot,
): PlacedLabel {
  const x = item.x + slot.dx;
  const y = item.y + slot.dy;
  const box = labelBox(x, y, size.width, size.height, slot.anchor);
  return {
    id: item.id,
    x,
    y,
    width: size.width,
    height: size.height,
    left: box.left,
    top: box.top,
    visible: true,
    leader: slot.leader,
    textAnchor: slot.anchor,
  };
}

function labelLeaderDistance(item: LabelRequest, placed: PlacedLabel): number {
  return Math.hypot(placed.x - item.x, placed.y - item.y);
}

function labelFitsBounds(
  label: { left: number; top: number; width: number; height: number },
  bounds: GraphLabelBounds,
  inset: number,
): boolean {
  return label.left >= inset - 1e-6 &&
    label.top >= inset - 1e-6 &&
    label.left + label.width <= bounds.width - inset + 1e-6 &&
    label.top + label.height <= bounds.height - inset + 1e-6;
}

function clampLabelToBounds(
  label: PlacedLabel,
  bounds: GraphLabelBounds,
  inset: number,
): PlacedLabel {
  const innerWidth = Math.max(0, bounds.width - 2 * inset);
  const innerHeight = Math.max(0, bounds.height - 2 * inset);
  let left = label.left;
  let top = label.top;
  if (label.width <= innerWidth) {
    left = Math.min(
      Math.max(left, inset),
      bounds.width - inset - label.width,
    );
  } else {
    left = inset;
  }
  if (label.height <= innerHeight) {
    top = Math.min(
      Math.max(top, inset),
      bounds.height - inset - label.height,
    );
  } else {
    top = inset;
  }
  const x = label.textAnchor === "start"
    ? left
    : label.textAnchor === "end"
    ? left + label.width
    : left + label.width / 2;
  return { ...label, x, y: top + label.height / 2, left, top, leader: true };
}

function labelCandidateAllowed(
  candidate: PlacedLabel,
  item: LabelRequest,
  accepted: readonly PlacedLabel[],
  bounds: GraphLabelBounds | undefined,
  inset: number,
  maxLeader: number,
): boolean {
  if (accepted.some((other) => labelRectsOverlap(candidate, other))) {
    return false;
  }
  if (labelLeaderDistance(item, candidate) > maxLeader + 1e-6) return false;
  if (bounds && !labelFitsBounds(candidate, bounds, inset)) return false;
  return true;
}

function forceRequiredLabel(
  item: LabelRequest,
  size: { width: number; height: number },
  accepted: readonly PlacedLabel[],
  bounds: GraphLabelBounds | undefined,
  inset: number,
  maxLeader: number,
): PlacedLabel | null {
  const minDist = item.nodeRadius + 8;
  for (let dist = minDist; dist <= maxLeader + 1e-6; dist += 6) {
    for (let step = 0; step < 16; step += 1) {
      const angle = (Math.PI * 2 * step) / 16;
      const dx = Math.cos(angle) * dist;
      const dy = Math.sin(angle) * dist;
      const candidate = makeLabelCandidate(item, size, {
        dx,
        dy,
        anchor: Math.abs(dx) >= Math.abs(dy)
          ? (dx >= 0 ? "start" : "end")
          : "middle",
        leader: dist > minDist + 1,
      });
      if (
        labelCandidateAllowed(
          candidate,
          item,
          accepted,
          bounds,
          inset,
          maxLeader,
        )
      ) {
        return candidate;
      }
    }
  }
  if (!bounds) return null;
  for (const slot of labelSlots(item.nodeRadius, true, maxLeader)) {
    const clamped = clampLabelToBounds(
      makeLabelCandidate(item, size, slot),
      bounds,
      inset,
    );
    if (
      labelCandidateAllowed(clamped, item, accepted, bounds, inset, maxLeader)
    ) {
      return clamped;
    }
  }
  const pinned = clampLabelToBounds(
    makeLabelCandidate(item, size, {
      dx: item.nodeRadius + 8,
      dy: 0,
      anchor: "start",
      leader: true,
    }),
    bounds,
    inset,
  );
  if (accepted.some((other) => labelRectsOverlap(pinned, other))) return null;
  if (labelLeaderDistance(item, pinned) > maxLeader + 1e-6) return null;
  if (!labelFitsBounds(pinned, bounds, inset)) return null;
  return pinned;
}

function labelSlots(
  radius: number,
  required: boolean,
  maxLeader: number,
): LabelSlot[] {
  const base: Array<
    { dx: number; dy: number; anchor: "start" | "middle" | "end" }
  > = [
    { dx: radius + 8, dy: 0, anchor: "start" },
    { dx: -(radius + 8), dy: 0, anchor: "end" },
    { dx: 0, dy: radius + 10, anchor: "middle" },
    { dx: 0, dy: -(radius + 10), anchor: "middle" },
    { dx: radius + 8, dy: radius + 8, anchor: "start" },
    { dx: -(radius + 8), dy: radius + 8, anchor: "end" },
    { dx: radius + 8, dy: -(radius + 8), anchor: "start" },
    { dx: -(radius + 8), dy: -(radius + 8), anchor: "end" },
  ];
  const scales = required ? [1, 1.35, 1.7, 2.05, 2.4] : [1];
  const slots: LabelSlot[] = [];
  for (const scale of scales) {
    for (const slot of base) {
      const dx = slot.dx * scale;
      const dy = slot.dy * scale;
      if (scale > 1 && Math.hypot(dx, dy) > maxLeader + 1e-6) continue;
      slots.push({
        dx,
        dy,
        anchor: slot.anchor,
        leader: scale > 1,
      });
    }
  }
  return slots;
}

function hash01(value: string): number {
  let hash = 2166136261;
  for (let index = 0; index < value.length; index += 1) {
    hash ^= value.charCodeAt(index);
    hash = Math.imul(hash, 16777619);
  }
  return (hash >>> 0) / 4294967296;
}
