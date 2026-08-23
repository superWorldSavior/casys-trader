import { formatAgo } from "./format.ts";
import {
  familyLabel,
  plainMarketLanguage,
  projectionFreshness,
  venueLabel,
  venuePostureLabel,
} from "./humanize.ts";
import {
  type InfluenceGraphNode,
  type MarketInfluenceGraph,
  recordedEvidenceFreshness,
  recordedEvidencePoints,
} from "./market-influence-network.ts";
import type {
  CompanyEvidencePoint,
  CompanyIntelligence,
  FamilyComparison,
  FamilyComparisonCell,
  MacroBrief,
  RegionCurrentIntelligence,
} from "./types.ts";

export type CompanyDigestReading = {
  state: "pending" | "unavailable" | "empty" | "ready";
  summary: string | null;
  catalyst: string | null;
  risk: string | null;
  openQuestion: string | null;
  freshnessLabel: string | null;
  evidenceLimits: string[];
  refreshNote: string | null;
};

export type ThemeEvidenceReading = {
  point: string | null;
  freshnessLabel: string | null;
};

export type MarketReading = {
  summary: string | null;
  posture: string | null;
  points: string[];
  freshnessLabel: string | null;
};

export type DomainVenueReading = {
  venueKey: string;
  venue: string;
  statusLabel: string;
  leader: string | null;
  rankLabel: string | null;
};

export type DomainReading = {
  venues: DomainVenueReading[];
};

export type FactorRecordedReading = {
  targetId: string;
  driverPoint: string | null;
  familyPoint: string | null;
  freshnessLabel: string | null;
};

export type FactorReading = {
  recorded: FactorRecordedReading[];
  possibleTargetIds: string[];
};

export type MarketIntelligenceContext = {
  current?: Record<string, RegionCurrentIntelligence | undefined> | null;
  comparisons?: readonly FamilyComparison[] | null;
};

const STALE_COMPANY_HOURS = 72;
const REFRESH_FAILED_NOTE =
  "Could not refresh · Showing the last recorded view";

const CONTEXT_LANGUAGE_REPLACEMENTS: Array<
  readonly [RegExp, string | ((match: string, ...groups: string[]) => string)]
> = [
  [
    /\bno sourced operating or financial pillars can be established from the supplied company[- ]news headlines\b/gi,
    "available company research does not yet support a clear operating or financial view",
  ],
  [
    /\bthe (?:company )?outlook is therefore untested\b/gi,
    "the outlook still needs supporting evidence",
  ],
  [/\bis therefore untested\b/gi, "still needs supporting evidence"],
  [
    /\bthe news archive includes a headline reporting installation of the ([^,]+), but the supplied evidence does not establish its financial significance for ([^.]+)\b/gi,
    "a report covers the installation of the $1, but the available information does not show what it could mean financially for $2",
  ],
  [
    /\ba news headline states that a hybrid bond issue reshaped ([^,]+), but no terms or quantified effects were supplied\b/gi,
    "a report says the hybrid bond issue changed $1, but the available information does not include its terms or measurable effects",
  ],
  [
    /\bthe news archive includes a headline reporting\b/gi,
    "a report covers",
  ],
  [
    /\bbut the supplied evidence does not establish its financial significance\b/gi,
    "but the available information does not show what it could mean financially",
  ],
  [/\ba news headline states that\b/gi, "a report says"],
  [
    /\bbut no terms or quantified effects were supplied\b/gi,
    "but the available information does not include its terms or measurable effects",
  ],
  [/\bthe news archive\b/gi, "available reports"],
  [/\bthe supplied evidence\b/gi, "the available information"],
  [
    /\bno sourced operating or financial pillars\b/gi,
    "no clear operating or financial view",
  ],
  [
    /\ban untested ([^.!?]+)/gi,
    (_match, rest: string) =>
      articlePhrase(rest, "that still needs supporting evidence"),
  ],
  [
    /\ba untested ([^.!?]+)/gi,
    (_match, rest: string) =>
      articlePhrase(rest, "that still needs supporting evidence"),
  ],
  [/\bremains untested\b/gi, "still needs supporting evidence"],
  [/\bis untested\b/gi, "still needs supporting evidence"],
  [/\buntested\b/gi, "still needs supporting evidence"],
  [
    /\b(?:the current read )?still leans on supplied company-news headlines rather than operating or financial pillars\b/gi,
    "available company research does not yet support a clear operating or financial view",
  ],
  [
    /\bleans on supplied company-news headlines rather than operating or financial pillars\b/gi,
    "available company research does not yet support a clear operating or financial view",
  ],
  [/\bsupplied company-news headlines\b/gi, "available company news headlines"],
  [
    /\boperating or financial pillars\b/gi,
    "a clear operating or financial view",
  ],
  [/\b25-name fallback\b/gi, "broader company list"],
  [/\bEMS\/ODM names\b/g, "electronics manufacturers"],
  [/\bEMS\/ODM\b/g, "electronics manufacturers"],
  [
    /\bhydrocarbon-linked EU energy stays\b/gi,
    "European oil and gas companies stay",
  ],
  [/\bhydrocarbon-linked EU energy\b/gi, "European oil and gas companies"],
  [/\bhydrocarbon-linked\b/gi, "oil-and-gas-related"],
  [/\benergy names\b/gi, "energy companies"],
  [/\bgeo risk\b/gi, "geopolitical uncertainty"],
  [/\ban inflation cooldown\b/gi, "slower inflation"],
  [/\binflation cooldown\b/gi, "slower inflation"],
  [/\bscaled producers\b/gi, "large producers"],
  [/\bfallback\b/gi, "broader company list"],
];

export function matchingCompany(
  companies?: readonly CompanyIntelligence[] | null,
  symbol?: string | null,
): CompanyIntelligence | null {
  const wanted = String(symbol ?? "").trim().toUpperCase();
  if (!wanted) return null;
  return (companies ?? []).find((item) =>
    String(item.symbol ?? "").trim().toUpperCase() === wanted
  ) ?? null;
}

export function graphNodeLabel(
  data: Pick<
    InfluenceGraphNode["data"],
    "kind" | "label" | "family" | "scopeKey"
  >,
): string {
  if (data.kind === "market") return venueLabel(data.scopeKey || data.label);
  if (data.kind === "family") {
    return startCasedLabel(
      compactThemeLabel(data.family, data.scopeKey) ?? data.label,
    );
  }
  return data.label;
}

export function interactiveGraphLabel(
  data: Pick<
    InfluenceGraphNode["data"],
    "kind" | "label" | "family" | "scopeKey"
  >,
  peers: readonly Pick<
    InfluenceGraphNode["data"],
    "kind" | "label" | "family" | "scopeKey"
  >[],
): string {
  const base = graphNodeLabel(data);
  if (data.kind !== "family") return base;
  const collisions = peers.filter((peer) =>
    peer.kind === "family" && graphNodeLabel(peer) === base
  );
  if (collisions.length <= 1) return base;
  const market = data.scopeKey ? venueLabel(data.scopeKey) : "";
  return market ? `${base} · ${market}` : base;
}

export function projectCompanyDigest(
  input: {
    pending?: boolean;
    error?: boolean;
    company?: CompanyIntelligence | null;
    companies?: readonly CompanyIntelligence[] | null;
    symbol?: string | null;
  } = {},
  now = Date.now(),
): CompanyDigestReading {
  const empty: CompanyDigestReading = {
    state: "empty",
    summary: null,
    catalyst: null,
    risk: null,
    openQuestion: null,
    freshnessLabel: null,
    evidenceLimits: [],
    refreshNote: null,
  };
  const resolved = input.company ??
    matchingCompany(input.companies, input.symbol);
  if (resolved) {
    return {
      ...projectReadyCompany(resolved, now),
      refreshNote: input.error ? REFRESH_FAILED_NOTE : null,
    };
  }
  if (input.pending) return { ...empty, state: "pending" };
  if (Array.isArray(input.companies)) return empty;
  if (input.error) return { ...empty, state: "unavailable" };
  return empty;
}

export function projectThemeEvidence(input: {
  family?: string | null;
  scopeKey?: string | null;
  summary?: string | null;
  current?: Record<string, RegionCurrentIntelligence | undefined> | null;
} = {}): ThemeEvidenceReading {
  const brief = scopeBrief(input.current, input.scopeKey);
  const skip = new Set<string>();
  const summary = humanizeLine(input.summary);
  if (summary) skip.add(normalizeText(summary));
  const familyKey = String(input.family ?? "").trim();
  const observations = familyKey ? brief?.families?.[familyKey] ?? [] : [];
  return {
    point: firstMeaningfulPoints(observations, 1, skip)[0] ?? null,
    freshnessLabel: briefFreshnessLabel(brief),
  };
}

export function projectMarketReading(input: {
  scopeKey?: string | null;
  current?: Record<string, RegionCurrentIntelligence | undefined> | null;
} = {}): MarketReading {
  const region = scopeRegion(input.current, input.scopeKey);
  const brief = region?.macro_brief ?? null;
  const rawSummary = stringField(region?.regional_run, "summary");
  const summary = humanizeLine(rawSummary);
  const postureRaw = String(region?.posture ?? "").trim();
  const posture = postureRaw ? venuePostureLabel(postureRaw) : "";
  return {
    summary,
    posture: posture && posture !== "—" ? posture : null,
    points: marketPoints(brief, [summary, rawSummary]),
    freshnessLabel: briefFreshnessLabel(brief),
  };
}

export function projectDomainReading(input: {
  domainKey?: string | null;
  label?: string | null;
  comparisons?: readonly FamilyComparison[] | null;
} = {}): DomainReading | null {
  const comparison = matchComparison(
    input.domainKey,
    input.label,
    input.comparisons,
  );
  if (!comparison) return null;
  const venues: DomainVenueReading[] = [];
  for (const [venueKey, cell] of Object.entries(comparison.venues ?? {})) {
    if (!cell) continue;
    venues.push(projectDomainVenue(venueKey, cell));
  }
  return venues.length ? { venues } : null;
}

export function projectFactorReading(
  graph: MarketInfluenceGraph,
  driverId: string,
): FactorReading {
  const recorded: FactorRecordedReading[] = [];
  const recordedIds = new Set<string>();
  for (const edge of graph.edges) {
    if (edge.data.source !== driverId) continue;
    const points = recordedEvidencePoints(edge);
    if (!points || recordedIds.has(edge.data.target)) continue;
    const driverPoint = humanizeLine(points.driverPoint);
    const familyPoint = humanizeLine(points.familyPoint);
    const freshness = recordedEvidenceFreshness(edge);
    recorded.push({
      targetId: edge.data.target,
      driverPoint,
      familyPoint: familyPoint && familyPoint !== driverPoint
        ? familyPoint
        : null,
      freshnessLabel: freshness
        ? briefFreshnessLabel({
          as_of: freshness.as_of,
          valid_until: freshness.valid_until,
          status: freshness.status,
          coverage: freshness.coverage,
        })
        : null,
    });
    recordedIds.add(edge.data.target);
  }
  const possibleTargetIds: string[] = [];
  const possibleSeen = new Set<string>();
  for (const edge of graph.edges) {
    if (edge.data.source !== driverId) continue;
    if (recordedEvidencePoints(edge)) continue;
    if (
      edge.data.kind !== "possible" &&
      edge.data.basis !== "heuristic_domain" &&
      edge.data.basis !== "topic_match"
    ) continue;
    if (
      recordedIds.has(edge.data.target) || possibleSeen.has(edge.data.target)
    ) continue;
    possibleSeen.add(edge.data.target);
    possibleTargetIds.push(edge.data.target);
  }
  return { recorded, possibleTargetIds };
}

function projectReadyCompany(
  company: CompanyIntelligence,
  now: number,
): CompanyDigestReading {
  const catalyst = firstEvidenceLine(company.brief?.catalysts);
  const risk = firstEvidenceLine(company.brief?.risks);
  return {
    state: "ready",
    summary: companySummary(company),
    catalyst,
    risk,
    openQuestion: risk
      ? null
      : firstEvidenceLine(company.brief?.open_questions),
    freshnessLabel: companyStale(company, now)
      ? "Needs refreshing"
      : companyFreshness(company),
    evidenceLimits: projectEvidenceLimits(company),
    refreshNote: null,
  };
}

function companySummary(company: CompanyIntelligence): string | null {
  const primary = safeHumanize(company.summary);
  if (primary) return primary;
  const thesis = safeHumanize(company.brief?.company_thesis?.summary);
  if (thesis) return thesis;
  return safeHumanize(company.brief?.selection_view?.summary);
}

function firstEvidenceLine(
  items?: CompanyEvidencePoint[] | null,
): string | null {
  for (const item of items ?? []) {
    const line = safeHumanize(item.point || item.label || item.detail);
    if (line) return line;
  }
  return null;
}

function depthLimit(value?: string | null): string | null {
  const key = String(value ?? "").trim().toLowerCase();
  if (key === "deep") return "In-depth review";
  if (key === "screen") return "Quick review";
  return null;
}

function projectEvidenceLimits(company: CompanyIntelligence): string[] {
  const hasSources = typeof company.source_count === "number" &&
    Number.isFinite(company.source_count) &&
    company.source_count > 0;
  const coverageKey = String(company.coverage ?? "").trim().toLowerCase();
  const limited = coverageKey === "unsupported" || !hasSources;
  const limits: string[] = [];
  const depth = depthLimit(company.depth);
  if (depth) limits.push(depth);
  const coverage = coverageLimit(coverageKey, hasSources);
  if (coverage) limits.push(coverage);
  if (limited && !limits.includes("Limited evidence")) {
    limits.push("Limited evidence");
  }
  const sources = sourceCountLimit(company.source_count);
  if (sources) limits.push(sources);
  return limits;
}

function coverageLimit(key: string, hasSources: boolean): string | null {
  if (
    key === "unsupported" ||
    ((key === "complete" || key === "full") && !hasSources)
  ) {
    return "Limited evidence";
  }
  if (key === "complete" || key === "full") return "Complete sources";
  if (key === "partial" || key === "incomplete") return "Partial sources";
  if (key === "missing" || key === "none") return "Sources missing";
  return null;
}

function sourceCountLimit(value: number | null | undefined): string | null {
  if (typeof value !== "number" || !Number.isFinite(value) || value <= 0) {
    return null;
  }
  const count = Math.round(value);
  return count === 1 ? "1 source" : `${count} sources`;
}

function companyStale(company: CompanyIntelligence, now: number): boolean {
  if (
    typeof company.age_hours === "number" && Number.isFinite(company.age_hours)
  ) {
    return company.age_hours > STALE_COMPANY_HOURS;
  }
  if (!company.as_of) return false;
  const ts = Date.parse(company.as_of);
  if (!Number.isFinite(ts)) return false;
  return (now - ts) / 3_600_000 > STALE_COMPANY_HOURS;
}

function companyFreshness(company: CompanyIntelligence): string | null {
  const ago = formatAgo(company.as_of);
  if (ago !== "—") return ago;
  if (
    typeof company.age_hours === "number" && Number.isFinite(company.age_hours)
  ) {
    const hours = Math.max(0, Math.round(company.age_hours));
    return `${hours}h ago`;
  }
  return null;
}

function marketPoints(
  brief?: MacroBrief | null,
  seeds: Array<string | null | undefined> = [],
): string[] {
  const skip = new Set<string>();
  for (const seed of seeds) {
    const line = humanizeLine(seed) ?? String(seed ?? "").trim();
    if (line) skip.add(normalizeText(line));
    const raw = String(seed ?? "").trim();
    if (raw) skip.add(normalizeText(raw));
  }
  const fromAlerts = firstMeaningfulPoints(brief?.alerts, 2, skip);
  if (fromAlerts.length) return fromAlerts;
  const fromFamilies = firstMeaningfulPoints(
    nestedObservations(brief?.families),
    2,
    skip,
  );
  if (fromFamilies.length) return fromFamilies;
  return firstMeaningfulPoints(nestedObservations(brief?.zones), 2, skip);
}

function firstMeaningfulPoints(
  items: unknown,
  limit: number,
  skip = new Set<string>(),
): string[] {
  const points: string[] = [];
  for (const item of asList(items)) {
    if (isOperational(item)) continue;
    const line = humanizeLine(observationPoint(item));
    if (!line) continue;
    const key = normalizeText(line);
    if (!key || skip.has(key)) continue;
    skip.add(key);
    points.push(line);
    if (points.length >= limit) break;
  }
  return points;
}

function matchComparison(
  domainKey?: string | null,
  label?: string | null,
  comparisons?: readonly FamilyComparison[] | null,
): FamilyComparison | null {
  const keys = [domainKey, label]
    .map((value) => String(value ?? "").trim().toLowerCase())
    .filter(Boolean);
  if (!keys.length) return null;
  for (const comparison of comparisons ?? []) {
    const group = String(comparison.group ?? "").trim().toLowerCase();
    const comparisonLabel = String(comparison.label ?? "").trim().toLowerCase();
    if (keys.includes(group) || keys.includes(comparisonLabel)) {
      return comparison;
    }
  }
  return null;
}

function projectDomainVenue(
  venueKey: string,
  cell: FamilyComparisonCell,
): DomainVenueReading {
  return {
    venueKey,
    venue: venueLabel(venueKey),
    statusLabel: comparisonStatusLabel(cell.status),
    leader: compactThemeLabel(cell.leader, venueKey),
    rankLabel: rankLabel(cell.best_rank),
  };
}

function comparisonStatusLabel(status: FamilyComparisonCell["status"]): string {
  if (status === "active") return "Actively considered";
  if (status === "observed") return "Observed";
  if (status === "not_observed") return "No current reading";
  if (status === "not_in_taxonomy") return "Not covered";
  return "Status not recorded";
}

function compactThemeLabel(
  family?: string | null,
  scopeKey?: string | null,
): string | null {
  const raw = String(family ?? "").trim();
  if (!raw) return null;
  const full = familyLabel(raw);
  const scope = String(scopeKey ?? "").trim();
  let compact = full;
  if (
    scope && raw.toLowerCase().startsWith(`${scope.toLowerCase()}_`)
  ) {
    const withoutFirstWord = full.split(/\s+/).slice(1).join(" ").trim();
    if (withoutFirstWord) compact = withoutFirstWord;
  } else {
    const market = venueLabel(scope);
    for (const prefix of [market, `${market.replace(/e$/, "")}n`, scope]) {
      const candidate = String(prefix ?? "").trim();
      if (
        candidate &&
        full.toLowerCase().startsWith(`${candidate.toLowerCase()} `)
      ) {
        compact = full.slice(candidate.length + 1).trim() || full;
        break;
      }
    }
  }
  if (!compact || compact === "—") return null;
  return startCasedLabel(compact);
}

function rankLabel(value?: number | null): string | null {
  if (typeof value !== "number" || !Number.isFinite(value)) return null;
  const n = Math.trunc(value);
  const mod100 = n % 100;
  if (mod100 >= 11 && mod100 <= 13) return `${n}th`;
  if (n % 10 === 1) return `${n}st`;
  if (n % 10 === 2) return `${n}nd`;
  if (n % 10 === 3) return `${n}rd`;
  return `${n}th`;
}

function briefFreshnessLabel(
  brief?: {
    as_of?: string | null;
    valid_until?: string | null;
    status?: string | null;
    coverage?: string | Record<string, unknown> | null;
  } | null,
): string | null {
  if (
    !brief ||
    (!brief.as_of && !brief.valid_until && !brief.status &&
      brief.coverage == null)
  ) {
    return null;
  }
  const coverage = coverageStatus(brief.coverage);
  const status = ["partial", "incomplete", "degraded"].includes(coverage)
    ? coverage
    : (String(brief.status ?? "").trim() || coverage);
  const fresh = projectionFreshness({
    as_of: brief.as_of,
    valid_until: brief.valid_until,
    status,
  });
  if (fresh.label === "Needs refreshing") return fresh.label;
  const ago = formatAgo(brief.as_of);
  if (fresh.label === "Partial coverage") {
    return ago !== "—" ? `Partial coverage · ${ago}` : "Partial coverage";
  }
  if (ago !== "—") return ago;
  if (
    fresh.label === "Update unavailable" ||
    fresh.label === "Update time unavailable"
  ) {
    return null;
  }
  return fresh.label;
}

function coverageStatus(
  coverage?: string | Record<string, unknown> | null,
): string {
  if (typeof coverage === "string") return coverage.trim().toLowerCase();
  if (coverage && typeof coverage === "object") {
    const status = coverage.status;
    if (typeof status === "string") return status.trim().toLowerCase();
  }
  return "";
}

function scopeRegion(
  current?: Record<string, RegionCurrentIntelligence | undefined> | null,
  scopeKey?: string | null,
): RegionCurrentIntelligence | undefined {
  const key = String(scopeKey ?? "").trim();
  if (!key) return undefined;
  return current?.[key];
}

function scopeBrief(
  current?: Record<string, RegionCurrentIntelligence | undefined> | null,
  scopeKey?: string | null,
): MacroBrief | null {
  return scopeRegion(current, scopeKey)?.macro_brief ?? null;
}

function nestedObservations(value: unknown): unknown[] {
  if (!value || typeof value !== "object") return [];
  return Object.values(value as Record<string, unknown>).flatMap((item) =>
    Array.isArray(item) ? item : []
  );
}

function asList(value: unknown): unknown[] {
  return Array.isArray(value) ? value : [];
}

function isOperational(value: unknown): boolean {
  if (!value || typeof value !== "object") return false;
  const flag = (value as { is_operational?: unknown }).is_operational;
  return flag === true || flag === "true" || flag === "True";
}

function observationPoint(value: unknown): string {
  if (!value || typeof value !== "object") return "";
  const point = (value as { point?: unknown }).point;
  return typeof point === "string" ? point : "";
}

function stringField(value: unknown, key: string): string {
  if (!value || typeof value !== "object" || Array.isArray(value)) return "";
  const found = (value as Record<string, unknown>)[key];
  return typeof found === "string" ? found : "";
}

function humanizeLine(value?: string | null): string | null {
  const text = contextualMarketLanguage(String(value ?? "").trim()).trim();
  return text && text !== "—" ? text : null;
}

function safeHumanize(value?: string | null): string | null {
  const raw = String(value ?? "").trim();
  if (!raw || looksStructured(raw)) return null;
  return humanizeLine(raw);
}

function looksStructured(value: string): boolean {
  const first = value.trim().charAt(0);
  return first === "{" || first === "[";
}

function contextualMarketLanguage(value: string): string {
  let text = plainMarketLanguage(value);
  for (const [pattern, replacement] of CONTEXT_LANGUAGE_REPLACEMENTS) {
    text = typeof replacement === "function"
      ? text.replace(pattern, replacement)
      : text.replace(pattern, replacement);
  }
  return recapitalizeSentences(text);
}

function articlePhrase(rest: string, clause: string): string {
  const body = String(rest ?? "").trim();
  if (!body) return clause;
  const article = /^[aeiou]/i.test(body) ? "an" : "a";
  return `${article} ${body} ${clause}`;
}

function recapitalizeSentences(text: string): string {
  return text.replace(
    /(^|[.!?]\s+)([a-z])/g,
    (_match, prefix: string, letter: string) =>
      `${prefix}${letter.toUpperCase()}`,
  );
}

function startCasedLabel(value?: string | null): string {
  const text = String(value ?? "").trim();
  if (!text || text === "—") return text;
  const first = text.charAt(0);
  if (first === first.toUpperCase()) return text;
  return `${first.toUpperCase()}${text.slice(1)}`;
}

function normalizeText(value: string): string {
  return value.toLowerCase().replace(/\s+/g, " ").trim();
}
