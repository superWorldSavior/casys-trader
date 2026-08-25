export type AtlasPriority = "favored" | "neutral" | "deprioritized";
export type AtlasMovement = "improving" | "weakening" | "stable" | "unknown";

export type AtlasFamilyInput = {
  venue: string;
  family: string;
  priority?: string | null;
  rank?: number | null;
  rank_delta?: number | null;
  candidate_count?: number | null;
  persistence?: string | null;
  situation_status?: string | null;
  summary?: string | null;
  symbols?: readonly string[] | null;
  sticky_symbols?: readonly string[] | null;
};

export type AtlasBuildInput = {
  current?: Record<string, unknown> | null;
  families?: readonly AtlasFamilyInput[] | null;
  comparisons?: readonly AtlasFamilyThreadInput[] | null;
};

export type AtlasNode = {
  id: string;
  scopeKey: string;
  family: string;
  rank: number | null;
  ranked: boolean;
  candidateCount: number;
  priority: AtlasPriority;
  movement: AtlasMovement;
  evidenceUnavailable: boolean;
  persistence: string;
  isNew: boolean;
  radius: number;
  x: number;
  y: number;
  localX: number;
  localY: number;
  angle: number;
  distance: number;
  summary: string;
  symbols: string[];
};

export type AtlasScope = {
  key: string;
  x: number;
  y: number;
  radius: number;
  rankedInner: number;
  rankedOuter: number;
  unrankedRadius: number;
  rankedCount: number;
  unrankedCount: number;
};

export type AtlasLink = {
  fromX: number;
  fromY: number;
  toX: number;
  toY: number;
};

export type AtlasFamilyThreadInput = {
  group: string;
  label: string;
  venues: Record<
    string,
    {
      leader?: string | null;
      families?: Array<{ family?: string | null }> | null;
    } | undefined
  >;
};

export type AtlasFamilyThread = {
  key: string;
  label: string;
  possibleInfluence: string;
  nodeIds: string[];
  leaderIds: string[];
};

export type AtlasThreadRelation = {
  nodeId: string;
  scopeKey: string;
  crossScope: boolean;
};

export type AtlasObservationInput = {
  point?: string | null;
  direction?: string | null;
  signal?: string | null;
  source_refs?: string[] | null;
  symbols?: string[] | null;
  is_operational?: boolean | null;
};

export type AtlasMacroBriefInput = {
  zones?: Record<string, AtlasObservationInput[] | undefined> | null;
  families?: Record<string, AtlasObservationInput[] | undefined> | null;
};

export type AtlasEvidenceLink = {
  driverKey: string;
  driverLabel: string;
  driverKind: AtlasDriverKind;
  kind: "shared_source" | "shared_symbol" | "topic_match";
  tone: "supportive" | "headwind" | "mixed" | "context";
  driverPoint: string;
  familyPoint: string;
};

export type AtlasDriverKind =
  | "macro_indicator"
  | "event"
  | "observed_signal";

export type AtlasLayout = {
  viewBox: { width: number; height: number };
  core: { x: number; y: number; radius: number };
  scopes: AtlasScope[];
  nodes: AtlasNode[];
  links: AtlasLink[];
};

const CORE_RADIUS = 54;
const SCOPE_WELL_RADIUS = 104;
const SCOPE_PITCH = 238;
const RANKED_INNER = 28;
const RANKED_OUTER = 72;
const UNRANKED_RADIUS = 88;
const MIN_NODE_RADIUS = 4.5;
const UNRANKED_NODE_RADIUS = 4.5;

export function collectScopeKeys(
  current?: Record<string, unknown> | null,
  families?: readonly { venue?: string | null }[] | null,
  comparisons?: readonly AtlasFamilyThreadInput[] | null,
): string[] {
  const keys: string[] = [];
  const seen = new Set<string>();
  const push = (raw: unknown) => {
    const key = String(raw ?? "").trim();
    if (!key || seen.has(key)) return;
    seen.add(key);
    keys.push(key);
  };
  for (const key of Object.keys(current ?? {})) push(key);
  for (const row of families ?? []) push(row?.venue);
  for (const comparison of comparisons ?? []) {
    for (const [scopeKey, cell] of Object.entries(comparison.venues ?? {})) {
      if (cell) push(scopeKey);
    }
  }
  return keys;
}

export function normalizePriority(value: unknown): AtlasPriority {
  const key = String(value ?? "").trim().toLowerCase();
  if (key === "favored") return "favored";
  if (key === "deprioritized") return "deprioritized";
  return "neutral";
}

export function rankDeltaSemantics(delta: unknown): AtlasMovement {
  if (typeof delta !== "number" || !Number.isFinite(delta)) return "unknown";
  if (delta > 0) return "improving";
  if (delta < 0) return "weakening";
  return "stable";
}

export function isEvidenceUnavailable(status: unknown): boolean {
  if (status == null) return true;
  const key = String(status).trim().toLowerCase();
  if (!key) return true;
  return key === "missing" || key === "not_available" || key === "not_reported";
}

export function rankedNodeRadius(candidateCount: number): number {
  const count =
    typeof candidateCount === "number" && Number.isFinite(candidateCount)
      ? Math.max(0, candidateCount)
      : 0;
  return MIN_NODE_RADIUS + Math.min(9, Math.sqrt(count) * 1.6);
}

export function familyNodeId(scopeKey: string, family: string): string {
  return `${scopeKey}::${family}`;
}

/**
 * Project the governed comparison taxonomy onto the nodes that are actually
 * visible in this atlas. The thread is an association, never a causal claim.
 */
export function buildAtlasFamilyThreads(
  nodes: readonly AtlasNode[],
  comparisons?: readonly AtlasFamilyThreadInput[] | null,
): AtlasFamilyThread[] {
  const visible = new Set(nodes.map((node) => node.id));
  const threads: AtlasFamilyThread[] = [];

  for (const comparison of comparisons ?? []) {
    const nodeIds: string[] = [];
    const leaderIds: string[] = [];
    const seen = new Set<string>();

    for (const [scopeKey, cell] of Object.entries(comparison.venues ?? {})) {
      if (!cell) continue;
      const leaderId = cell.leader ? familyNodeId(scopeKey, cell.leader) : null;
      if (leaderId && visible.has(leaderId)) leaderIds.push(leaderId);
      for (const row of cell.families ?? []) {
        const family = String(row?.family ?? "").trim();
        if (!family) continue;
        const id = familyNodeId(scopeKey, family);
        if (!visible.has(id) || seen.has(id)) continue;
        seen.add(id);
        nodeIds.push(id);
      }
    }

    if (!nodeIds.length) continue;
    threads.push({
      key: String(comparison.group || comparison.label).trim(),
      label: String(comparison.label || comparison.group).trim() ||
        "Related themes",
      possibleInfluence: possibleInfluenceLabel(
        comparison.group,
        comparison.label,
      ),
      nodeIds,
      leaderIds: Array.from(new Set(leaderIds)),
    });
  }

  return threads;
}

export function threadForNode(
  threads: readonly AtlasFamilyThread[],
  nodeId?: string | null,
): AtlasFamilyThread | null {
  if (!nodeId) return null;
  return threads.find((thread) => thread.nodeIds.includes(nodeId)) ?? null;
}

/**
 * Keep the visual thread legible: show up to three close family peers in the
 * current market and one governed leader in every other market.
 */
export function relatedThreadNodes(
  thread: AtlasFamilyThread | null,
  nodes: readonly AtlasNode[],
  selected: AtlasNode | null,
  limit = 6,
): AtlasThreadRelation[] {
  if (!thread || !selected || limit <= 0) return [];
  const byId = new Map(nodes.map((node) => [node.id, node]));
  const related = thread.nodeIds
    .filter((id) => id !== selected.id)
    .map((id) => byId.get(id))
    .filter((node): node is AtlasNode => Boolean(node));
  const compareNodes = (left: AtlasNode, right: AtlasNode) => {
    if (left.rank != null && right.rank != null && left.rank !== right.rank) {
      return left.rank - right.rank;
    }
    if (left.rank != null && right.rank == null) return -1;
    if (left.rank == null && right.rank != null) return 1;
    return left.id.localeCompare(right.id);
  };

  const sameScope = related
    .filter((node) => node.scopeKey === selected.scopeKey)
    .sort(compareNodes)
    .slice(0, 3);
  const otherScopes = new Map<string, AtlasNode[]>();
  for (const node of related) {
    if (node.scopeKey === selected.scopeKey) continue;
    const list = otherScopes.get(node.scopeKey) ?? [];
    list.push(node);
    otherScopes.set(node.scopeKey, list);
  }
  const scopeOrder = Array.from(new Set(nodes.map((node) => node.scopeKey)));
  const crossScope = scopeOrder.flatMap((scopeKey) => {
    const candidates = (otherScopes.get(scopeKey) ?? []).sort(compareNodes);
    if (!candidates.length) return [];
    const leader = candidates.find((node) =>
      thread.leaderIds.includes(node.id)
    ) ?? candidates[0];
    return [leader];
  });

  return [...sameScope, ...crossScope].slice(0, limit).map((node) => ({
    nodeId: node.id,
    scopeKey: node.scopeKey,
    crossScope: node.scopeKey !== selected.scopeKey,
  }));
}

export function possibleInfluenceLabel(
  group?: string | null,
  label?: string | null,
): string {
  const key = `${String(group ?? "")} ${String(label ?? "")}`.toLowerCase();
  if (/technolog|semiconductor/.test(key)) return "AI and compute investment";
  if (/financial/.test(key)) return "Rates and funding costs";
  if (/industrial|defen[cs]e/.test(key)) return "Orders and public spending";
  if (/material/.test(key)) return "Commodity and input prices";
  if (/consumer/.test(key)) return "Household demand and inflation";
  if (/health/.test(key)) return "Product and policy events";
  if (/energy/.test(key)) return "Oil and gas prices";
  if (/communication|telecom/.test(key)) return "Network and media demand";
  if (/real[ _-]?estate|property/.test(key)) return "Rates and financing";
  if (/utilit/.test(key)) return "Rates and energy costs";
  return "Broader market conditions";
}

/**
 * Find the strongest shared-evidence associations in one market brief.
 * Shared evidence and shared symbols are factual associations. Text matching
 * is deliberately returned as `topic_match` so the UI can mark it heuristic.
 */
export function buildAtlasEvidenceLinks(
  node: Pick<AtlasNode, "family" | "summary"> | null,
  brief?: AtlasMacroBriefInput | null,
): AtlasEvidenceLink[] {
  if (!node || !brief) return [];
  const familyObservations = (brief.families?.[node.family] ?? []).filter(
    Boolean,
  );
  if (!familyObservations.length) return [];
  const links: Array<AtlasEvidenceLink & { strength: number }> = [];

  for (const [driverKey, rawDrivers] of Object.entries(brief.zones ?? {})) {
    const drivers = (rawDrivers ?? []).filter((item) =>
      item && !item.is_operational
    );
    if (!drivers.length) continue;
    const matches: Array<{
      driver: AtlasObservationInput;
      family: AtlasObservationInput;
      kind: AtlasEvidenceLink["kind"];
      strength: number;
    }> = [];

    for (const driver of drivers) {
      for (const family of familyObservations) {
        const sharedRefs = intersectionCount(
          driver.source_refs,
          family.source_refs,
        );
        const sharedSymbols = intersectionCount(driver.symbols, family.symbols);
        if (sharedRefs > 0) {
          matches.push({
            driver,
            family,
            kind: "shared_source",
            strength: 300 + sharedRefs,
          });
        } else if (sharedSymbols > 0) {
          matches.push({
            driver,
            family,
            kind: "shared_symbol",
            strength: 200 + sharedSymbols,
          });
        } else if (
          driverTopicMatches(
            driverKey,
            driver.point,
            family.point,
            node.summary,
          )
        ) {
          matches.push({ driver, family, kind: "topic_match", strength: 100 });
        }
      }
    }

    if (!matches.length) continue;
    matches.sort((left, right) =>
      right.strength - left.strength ||
      observationKey(left).localeCompare(observationKey(right))
    );
    const strongest = matches[0];
    const sameStrength = matches.filter((match) =>
      match.strength >= Math.floor(strongest.strength / 100) * 100
    );
    links.push({
      driverKey,
      driverLabel: readableDriverLabel(driverKey),
      driverKind: driverKind(driverKey, strongest.driver.signal),
      kind: strongest.kind,
      tone: directionTone(sameStrength.map((match) => match.family.direction)),
      driverPoint: String(strongest.driver.point ?? "").trim(),
      familyPoint: String(strongest.family.point ?? "").trim(),
      strength: strongest.strength,
    });
  }

  return links
    .sort((left, right) =>
      right.strength - left.strength ||
      left.driverKey.localeCompare(right.driverKey)
    )
    .map(({ strength: _strength, ...link }) => link);
}

export function driverKind(
  driverKey?: string | null,
  signal?: string | null,
): AtlasDriverKind {
  const signalKey = String(signal ?? "").trim().toLowerCase();
  if (signalKey === "event") return "event";
  const key = String(driverKey ?? "").trim().toLowerCase();
  if (
    /(?:^|[_ -])(oil|brent|crude|gas|gold|rate|rates|yield|inflation|cpi|unemployment|usd|dollar|fx)(?:$|[_ -])/
      .test(
        ` ${key} `,
      )
  ) {
    return "macro_indicator";
  }
  return "observed_signal";
}

function intersectionCount(
  left?: string[] | null,
  right?: string[] | null,
): number {
  const rightSet = new Set(
    (right ?? []).map((item) => String(item).trim()).filter(Boolean),
  );
  let count = 0;
  for (
    const item of new Set(
      (left ?? []).map((value) => String(value).trim()).filter(Boolean),
    )
  ) {
    if (rightSet.has(item)) count += 1;
  }
  return count;
}

function driverTopicMatches(
  driverKey: string,
  driverPoint?: string | null,
  familyPoint?: string | null,
  familySummary?: string | null,
): boolean {
  const cues: Record<string, string[]> = {
    oil: ["oil", "crude", "brent", "hydrocarbon", "refiner"],
    gold: ["gold", "miner", "newmont"],
    rates: ["rate", "rates", "yield", "fed", "ecb", "financing"],
    inflation: ["inflation", "input cost", "prices"],
  };
  const haystack = normaliseMatchText(
    `${familyPoint ?? ""} ${familySummary ?? ""}`,
  );
  const driverText = normaliseMatchText(`${driverKey} ${driverPoint ?? ""}`);
  const terms = cues[normaliseMatchText(driverKey).trim()] ??
    normaliseMatchText(driverKey).trim().split(" ");
  return terms.some((term) =>
    term.length > 2 && haystack.includes(` ${term} `) &&
    driverText.includes(` ${term} `)
  );
}

function normaliseMatchText(value: string): string {
  return ` ${value.toLowerCase().replace(/[^a-z0-9]+/g, " ").trim()} `;
}

function readableDriverLabel(value: string): string {
  const words = String(value).trim().replace(/[_-]+/g, " ");
  if (!words) return "Market conditions";
  return words.replace(/\b\w/g, (letter) => letter.toUpperCase());
}

function directionTone(
  values: Array<string | null | undefined>,
): AtlasEvidenceLink["tone"] {
  const keys = values.map((value) => String(value ?? "").toLowerCase());
  const positive = keys.some((key) =>
    ["bullish", "up", "positive", "risk_on", "supportive"].includes(key)
  );
  const negative = keys.some((key) =>
    ["bearish", "down", "negative", "risk_off", "headwind"].includes(key)
  );
  const mixed = keys.some((key) => key === "mixed");
  if (mixed || (positive && negative)) return "mixed";
  if (positive) return "supportive";
  if (negative) return "headwind";
  return "context";
}

function observationKey(
  match: { driver: AtlasObservationInput; family: AtlasObservationInput },
): string {
  return `${String(match.driver.point ?? "")}::${
    String(match.family.point ?? "")
  }`;
}

export function buildMarketAtlas(input: AtlasBuildInput = {}): AtlasLayout {
  const scopeKeys = collectScopeKeys(
    input.current,
    input.families,
    input.comparisons,
  );
  const grouped = groupFamilies(scopeKeys, [
    ...(input.families ?? []),
    ...comparisonFamilyFallbacks(input.comparisons),
  ]);
  const core = placeCore(scopeKeys.length);
  const viewBox = placeViewBox(scopeKeys.length, core);
  const scopes: AtlasScope[] = [];
  const nodes: AtlasNode[] = [];
  const links: AtlasLink[] = [];

  for (let index = 0; index < scopeKeys.length; index += 1) {
    const key = scopeKeys[index];
    const center = placeScopeCenter(index, scopeKeys.length, core);
    const rows = grouped.get(key) ?? [];
    const rankedRows = rows.filter((row) => row.rank != null);
    const unrankedRows = rows.filter((row) => row.rank == null);
    const ranks = rankedRows.map((row) => row.rank as number);
    const scope: AtlasScope = {
      key,
      x: center.x,
      y: center.y,
      radius: SCOPE_WELL_RADIUS,
      rankedInner: RANKED_INNER,
      rankedOuter: RANKED_OUTER,
      unrankedRadius: UNRANKED_RADIUS,
      rankedCount: rankedRows.length,
      unrankedCount: unrankedRows.length,
    };
    scopes.push(scope);
    links.push({
      fromX: core.x,
      fromY: core.y,
      toX: center.x,
      toY: center.y,
    });

    rankedRows.forEach((row, familyIndex) => {
      const rank = row.rank as number;
      const distance = rankDistance(rank, ranks);
      const angle = orbitAngle(
        familyIndex,
        rankedRows.length,
        -Math.PI / 2 + index * 0.31,
      );
      nodes.push(placeNode(scope, row, { angle, distance, ranked: true }));
    });
    unrankedRows.forEach((row, familyIndex) => {
      const angle = orbitAngle(
        familyIndex,
        unrankedRows.length,
        Math.PI / 2 + index * 0.17,
      );
      nodes.push(
        placeNode(scope, row, {
          angle,
          distance: UNRANKED_RADIUS,
          ranked: false,
        }),
      );
    });
  }

  return { viewBox, core, scopes, nodes, links };
}

function comparisonFamilyFallbacks(
  comparisons?: readonly AtlasFamilyThreadInput[] | null,
): AtlasFamilyInput[] {
  const rows: AtlasFamilyInput[] = [];
  const seen = new Set<string>();
  for (const comparison of comparisons ?? []) {
    for (const [scopeKey, cell] of Object.entries(comparison.venues ?? {})) {
      if (!cell) continue;
      const families = [
        cell.leader,
        ...(cell.families ?? []).map((row) => row?.family),
      ];
      for (const rawFamily of families) {
        const family = String(rawFamily ?? "").trim();
        const id = familyNodeId(scopeKey, family);
        if (!scopeKey || !family || seen.has(id)) continue;
        seen.add(id);
        rows.push({ venue: scopeKey, family });
      }
    }
  }
  return rows;
}

type PreparedFamily = {
  family: string;
  rank: number | null;
  candidateCount: number;
  priority: AtlasPriority;
  movement: AtlasMovement;
  evidenceUnavailable: boolean;
  persistence: string;
  isNew: boolean;
  summary: string;
  symbols: string[];
};

function groupFamilies(
  scopeKeys: string[],
  families: readonly AtlasFamilyInput[],
): Map<string, PreparedFamily[]> {
  const known = new Set(scopeKeys);
  const grouped = new Map<string, PreparedFamily[]>();
  const seen = new Set<string>();
  for (const key of scopeKeys) grouped.set(key, []);

  for (const row of families) {
    const scopeKey = String(row?.venue ?? "").trim();
    const family = String(row?.family ?? "").trim();
    if (!scopeKey || !family || !known.has(scopeKey)) continue;
    const id = familyNodeId(scopeKey, family);
    if (seen.has(id)) continue;
    seen.add(id);
    grouped.get(scopeKey)?.push(prepareFamily(row, family));
  }

  for (const rows of grouped.values()) {
    rows.sort(comparePrepared);
  }
  return grouped;
}

function prepareFamily(row: AtlasFamilyInput, family: string): PreparedFamily {
  const persistence = String(row.persistence ?? "").trim();
  return {
    family,
    rank: numericRank(row.rank),
    candidateCount: numericCount(row.candidate_count),
    priority: normalizePriority(row.priority),
    movement: rankDeltaSemantics(row.rank_delta),
    evidenceUnavailable: isEvidenceUnavailable(row.situation_status),
    persistence,
    isNew: persistence.toLowerCase() === "new",
    summary: String(row.summary ?? "").trim(),
    symbols: unionFamilySymbols(row.symbols, row.sticky_symbols),
  };
}

export function unionFamilySymbols(
  symbols?: readonly string[] | null,
  stickySymbols?: readonly string[] | null,
): string[] {
  const seen = new Set<string>();
  const merged: string[] = [];
  for (const raw of [...(symbols ?? []), ...(stickySymbols ?? [])]) {
    const symbol = String(raw ?? "").trim();
    if (!symbol) continue;
    const key = symbol.toUpperCase();
    if (seen.has(key)) continue;
    seen.add(key);
    merged.push(symbol);
  }
  return merged;
}

function comparePrepared(left: PreparedFamily, right: PreparedFamily): number {
  if (left.rank != null && right.rank != null && left.rank !== right.rank) {
    return left.rank - right.rank;
  }
  if (left.rank != null && right.rank == null) return -1;
  if (left.rank == null && right.rank != null) return 1;
  return left.family.localeCompare(right.family);
}

function placeNode(
  scope: AtlasScope,
  row: PreparedFamily,
  polar: { angle: number; distance: number; ranked: boolean },
): AtlasNode {
  const localX = polar.distance * Math.cos(polar.angle);
  const localY = polar.distance * Math.sin(polar.angle);
  return {
    id: familyNodeId(scope.key, row.family),
    scopeKey: scope.key,
    family: row.family,
    rank: row.rank,
    ranked: polar.ranked,
    candidateCount: row.candidateCount,
    priority: row.priority,
    movement: row.movement,
    evidenceUnavailable: row.evidenceUnavailable,
    persistence: row.persistence,
    isNew: row.isNew,
    radius: polar.ranked
      ? rankedNodeRadius(row.candidateCount)
      : UNRANKED_NODE_RADIUS,
    x: scope.x + localX,
    y: scope.y + localY,
    localX,
    localY,
    angle: polar.angle,
    distance: polar.distance,
    summary: row.summary,
    symbols: row.symbols,
  };
}

function placeCore(
  scopeCount: number,
): { x: number; y: number; radius: number } {
  if (scopeCount === 0) return { x: 180, y: 140, radius: CORE_RADIUS };
  return { x: 120, y: 168, radius: CORE_RADIUS };
}

function placeViewBox(
  scopeCount: number,
  core: { x: number; y: number; radius: number },
): { width: number; height: number } {
  if (scopeCount === 0) return { width: 360, height: 280 };
  const last = placeScopeCenter(scopeCount - 1, scopeCount, core);
  return {
    width: last.x + SCOPE_WELL_RADIUS + 56,
    height: Math.max(core.y, last.y) + SCOPE_WELL_RADIUS + 64,
  };
}

function placeScopeCenter(
  index: number,
  total: number,
  core: { x: number; y: number; radius: number },
): { x: number; y: number } {
  const x = core.x + core.radius + 132 + index * SCOPE_PITCH;
  const y = total === 1 ? core.y : core.y + (index % 2 === 0 ? -14 : 18);
  return { x, y };
}

function rankDistance(rank: number, ranks: number[]): number {
  if (!ranks.length) return RANKED_INNER;
  let min = ranks[0];
  let max = ranks[0];
  for (const value of ranks) {
    if (value < min) min = value;
    if (value > max) max = value;
  }
  if (max === min) return RANKED_INNER;
  return RANKED_INNER +
    ((rank - min) / (max - min)) * (RANKED_OUTER - RANKED_INNER);
}

function orbitAngle(index: number, total: number, seed: number): number {
  if (total <= 0) return seed;
  return seed + ((index + 0.5) / total) * Math.PI * 2;
}

function numericRank(value: unknown): number | null {
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

function numericCount(value: unknown): number {
  if (typeof value !== "number" || !Number.isFinite(value) || value < 0) {
    return 0;
  }
  return value;
}
