import type {
  CompanyIntelligencePayload,
  DailyBriefingPayload,
  DecisionsPayload,
  HealthPayload,
  LogsEventsPayload,
  LogsTracePayload,
  NewsFeedPayload,
  OverviewPayload,
  PlansPayload,
  PortfolioPayload,
  ReportDetail,
  RegionIntelligencePayload,
  ReportsPayload,
  SettingsPayload,
  Snapshot,
  SymbolBarsPayload,
  SymbolDetail,
  UniversePayload,
  WorldIntelligencePayload,
} from "@/lib/types";
import type { WorldGraphExplorerPayload } from "@/lib/world-graph-explorer";

async function getJson<T>(path: string): Promise<T> {
  const response = await fetch(path);
  if (!response.ok) {
    throw new Error((await response.text()) || `${path} HTTP ${response.status}`);
  }
  return response.json() as Promise<T>;
}

export function readSnapshot(): Promise<Snapshot> {
  return getJson<Snapshot>("/api/snapshot");
}

export function readDecisions(params?: {
  limit?: number;
  symbol?: string;
  filter?: string;
}): Promise<DecisionsPayload> {
  const query = new URLSearchParams();
  if (params?.limit) query.set("limit", String(params.limit));
  if (params?.symbol) query.set("symbol", params.symbol);
  if (params?.filter && params.filter !== "all") query.set("filter", params.filter);
  const suffix = query.toString();
  return getJson<DecisionsPayload>(`/api/decisions${suffix ? `?${suffix}` : ""}`);
}

export function readPlans(): Promise<PlansPayload> {
  return getJson<PlansPayload>("/api/plans");
}

export function readOverview(): Promise<OverviewPayload> {
  return getJson<OverviewPayload>("/api/overview");
}

export function readWorldIntelligence(limit = 240): Promise<WorldIntelligencePayload> {
  return getJson<WorldIntelligencePayload>(`/api/intelligence/world?limit=${limit}`);
}

export function readWorldGraph(): Promise<WorldGraphExplorerPayload> {
  return getJson<WorldGraphExplorerPayload>("/api/world-graph");
}

export function readRegionIntelligence(params?: {
  venue?: string;
  limit?: number;
}): Promise<RegionIntelligencePayload> {
  const query = new URLSearchParams();
  if (params?.venue) query.set("venue", params.venue);
  if (params?.limit) query.set("limit", String(params.limit));
  const suffix = query.toString();
  return getJson<RegionIntelligencePayload>(
    `/api/intelligence/regions${suffix ? `?${suffix}` : ""}`,
  );
}

export function readCompanyIntelligence(params?: {
  symbol?: string;
  venue?: string;
  limit?: number;
}): Promise<CompanyIntelligencePayload> {
  const query = new URLSearchParams();
  if (params?.symbol) query.set("symbol", params.symbol);
  if (params?.venue) query.set("venue", params.venue);
  if (params?.limit) query.set("limit", String(params.limit));
  const suffix = query.toString();
  return getJson<CompanyIntelligencePayload>(
    `/api/intelligence/companies${suffix ? `?${suffix}` : ""}`,
  );
}

export function readUniverse(): Promise<UniversePayload> {
  return getJson<UniversePayload>("/api/universe");
}

export function readHealth(): Promise<HealthPayload> {
  return getJson<HealthPayload>("/api/health");
}

export function readReports(): Promise<ReportsPayload> {
  return getJson<ReportsPayload>("/api/reports");
}

export function readReport(key: string): Promise<ReportDetail> {
  return getJson<ReportDetail>(`/api/reports/${encodeURIComponent(key)}`);
}

export function readLogEvents(params?: {
  cursor?: number;
  limit?: number;
}): Promise<LogsEventsPayload> {
  const query = new URLSearchParams();
  if (params?.cursor != null) query.set("cursor", String(params.cursor));
  if (params?.limit) query.set("limit", String(params.limit));
  const suffix = query.toString();
  return getJson<LogsEventsPayload>(`/api/logs/events${suffix ? `?${suffix}` : ""}`);
}

export function readLogTrace(limit = 200): Promise<LogsTracePayload> {
  return getJson<LogsTracePayload>(`/api/logs/trace?limit=${limit}`);
}

export function readSettings(): Promise<SettingsPayload> {
  return getJson<SettingsPayload>("/api/settings");
}

export function readPortfolio(sort = 0): Promise<PortfolioPayload> {
  return getJson<PortfolioPayload>(`/api/portfolio?sort=${sort}`);
}

export function readSymbol(symbol: string): Promise<SymbolDetail> {
  return getJson<SymbolDetail>(`/api/symbols/${encodeURIComponent(symbol)}`);
}

export function readSymbolBars(symbol: string): Promise<SymbolBarsPayload> {
  return getJson<SymbolBarsPayload>(`/api/symbols/${encodeURIComponent(symbol)}/bars`);
}

export function readDailyBriefing(): Promise<DailyBriefingPayload> {
  return getJson<DailyBriefingPayload>("/api/intelligence/briefing");
}

export function readNewsFeed(params?: {
  venue?: string;
  symbol?: string;
  limit?: number;
}): Promise<NewsFeedPayload> {
  const query = new URLSearchParams();
  if (params?.venue) query.set("venue", params.venue);
  if (params?.symbol) query.set("symbol", params.symbol);
  if (params?.limit) query.set("limit", String(params.limit));
  const suffix = query.toString();
  return getJson<NewsFeedPayload>(`/api/intelligence/news${suffix ? `?${suffix}` : ""}`);
}
