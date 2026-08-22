function withQuery(url: URL, resource: string, keys: string[]): string[] {
  const args = [resource];
  for (const key of keys) {
    const value = url.searchParams.get(key);
    if (value) args.push(`--${key}`, value);
  }
  return args;
}

/** Maps a desk GET URL to `desktop/bridge/api.py` argv, or null if unknown. */
export function mapApi(url: URL): string[] | null {
  const pathname = url.pathname;

  if (pathname === "/api/intelligence/briefing") {
    return ["intelligence-briefing"];
  }
  if (pathname === "/api/intelligence/news") {
    return withQuery(url, "intelligence-news", ["venue", "symbol", "limit"]);
  }
  if (pathname === "/api/snapshot") return ["snapshot"];
  if (pathname === "/api/overview") return ["overview"];
  if (pathname === "/api/intelligence/world") {
    return withQuery(url, "intelligence-world", ["limit"]);
  }
  if (pathname === "/api/intelligence/regions") {
    return withQuery(url, "intelligence-regions", ["venue", "limit"]);
  }
  if (pathname === "/api/intelligence/companies") {
    return withQuery(url, "intelligence-companies", [
      "symbol",
      "venue",
      "limit",
    ]);
  }
  if (pathname === "/api/decisions") {
    return withQuery(url, "decisions", ["limit", "symbol", "filter"]);
  }
  if (pathname === "/api/plans") return ["plans"];
  if (pathname === "/api/universe") return ["universe"];
  if (pathname === "/api/health") return ["health"];
  if (pathname === "/api/reports") return ["reports"];
  const report = pathname.match(/^\/api\/reports\/(.+)$/);
  if (report) return ["report", "--key", decodeURIComponent(report[1])];
  if (pathname === "/api/logs/events") {
    return withQuery(url, "logs-events", ["cursor", "limit"]);
  }
  if (pathname === "/api/logs/trace") {
    return withQuery(url, "logs-trace", ["limit"]);
  }
  if (pathname === "/api/settings") return ["settings"];
  if (pathname === "/api/portfolio") {
    return withQuery(url, "portfolio", ["sort"]);
  }
  const symbolBars = pathname.match(/^\/api\/symbols\/([^/]+)\/bars$/);
  if (symbolBars) {
    return ["symbol-bars", "--symbol", decodeURIComponent(symbolBars[1])];
  }
  const symbol = pathname.match(/^\/api\/symbols\/([^/]+)$/);
  if (symbol) return ["symbol", "--symbol", decodeURIComponent(symbol[1])];
  return null;
}
