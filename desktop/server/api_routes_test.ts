import { assertEquals } from "@std/assert/equals";
import { mapApi } from "./api_routes.ts";

function url(path: string): URL {
  return new URL(path, "http://127.0.0.1");
}

Deno.test("mapApi maps exact GET resources", () => {
  const cases: Array<[string, string[]]> = [
    ["/api/intelligence/briefing", ["intelligence-briefing"]],
    ["/api/snapshot", ["snapshot"]],
    ["/api/overview", ["overview"]],
    ["/api/plans", ["plans"]],
    ["/api/universe", ["universe"]],
    ["/api/health", ["health"]],
    ["/api/reports", ["reports"]],
    ["/api/settings", ["settings"]],
  ];
  for (const [path, expected] of cases) {
    assertEquals(mapApi(url(path)), expected, path);
  }
});

Deno.test("mapApi forwards allowed query keys in declaration order", () => {
  assertEquals(
    mapApi(url("/api/intelligence/news?venue=TW&symbol=2330.TW&limit=10")),
    [
      "intelligence-news",
      "--venue",
      "TW",
      "--symbol",
      "2330.TW",
      "--limit",
      "10",
    ],
  );
  assertEquals(
    mapApi(url("/api/intelligence/world?limit=240")),
    ["intelligence-world", "--limit", "240"],
  );
  assertEquals(
    mapApi(url("/api/intelligence/regions?venue=US&limit=12")),
    ["intelligence-regions", "--venue", "US", "--limit", "12"],
  );
  assertEquals(
    mapApi(url("/api/intelligence/companies?symbol=AAPL&venue=US&limit=5")),
    [
      "intelligence-companies",
      "--symbol",
      "AAPL",
      "--venue",
      "US",
      "--limit",
      "5",
    ],
  );
  assertEquals(
    mapApi(url("/api/decisions?limit=80&symbol=AAPL&filter=llm")),
    [
      "decisions",
      "--limit",
      "80",
      "--symbol",
      "AAPL",
      "--filter",
      "llm",
    ],
  );
  assertEquals(
    mapApi(url("/api/logs/events?cursor=12&limit=50")),
    ["logs-events", "--cursor", "12", "--limit", "50"],
  );
  assertEquals(
    mapApi(url("/api/logs/trace?limit=200")),
    ["logs-trace", "--limit", "200"],
  );
  assertEquals(
    mapApi(url("/api/portfolio?sort=1")),
    ["portfolio", "--sort", "1"],
  );
});

Deno.test("mapApi omits missing or empty query values", () => {
  assertEquals(mapApi(url("/api/decisions")), ["decisions"]);
  assertEquals(mapApi(url("/api/decisions?symbol=")), ["decisions"]);
  assertEquals(
    mapApi(url("/api/intelligence/news?ignored=1")),
    ["intelligence-news"],
  );
});

Deno.test("mapApi maps parameterized report and symbol routes", () => {
  assertEquals(
    mapApi(url("/api/reports/global:current")),
    ["report", "--key", "global:current"],
  );
  assertEquals(
    mapApi(url("/api/reports/foo%2Fbar")),
    ["report", "--key", "foo/bar"],
  );
  assertEquals(
    mapApi(url("/api/symbols/2330.TW/bars")),
    ["symbol-bars", "--symbol", "2330.TW"],
  );
  assertEquals(
    mapApi(url("/api/symbols/AAPL")),
    ["symbol", "--symbol", "AAPL"],
  );
  assertEquals(
    mapApi(url("/api/symbols/foo%2Fbar")),
    ["symbol", "--symbol", "foo/bar"],
  );
});

Deno.test("mapApi prefers bars over symbol detail", () => {
  assertEquals(
    mapApi(url("/api/symbols/AAPL/bars")),
    ["symbol-bars", "--symbol", "AAPL"],
  );
});

Deno.test("mapApi returns null for unknown or non-api paths", () => {
  assertEquals(mapApi(url("/api/unknown")), null);
  assertEquals(mapApi(url("/api/snapshot/extra")), null);
  assertEquals(mapApi(url("/api/symbols/AAPL/extra")), null);
  assertEquals(mapApi(url("/")), null);
  assertEquals(mapApi(url("/index.html")), null);
});
