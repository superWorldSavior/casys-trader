import { useEffect, useMemo, useState } from "react";
import {
  CompanyIntelligenceDetail,
  CompanyRadarRow,
} from "@/components/intelligence/company-summary";
import { IntelligenceTimeline } from "@/components/intelligence/timeline";
import { Badge } from "@/components/ui/badge";
import { Input } from "@/components/ui/input";
import { useCompanyIntelligence } from "@/hooks/use-intelligence";
import { cn } from "@/lib/utils";

const VENUES = ["ALL", "TW", "EU", "US"] as const;
const DEPTHS = ["all", "deep", "screen"] as const;
const FRESHNESS = ["all", "fresh", "aging", "stale"] as const;

export function CompaniesPage({
  initialVenue,
  onSymbol,
}: {
  initialVenue?: string | null;
  onSymbol: (symbol: string) => void;
}) {
  const query = useCompanyIntelligence({ limit: 400 });
  const [search, setSearch] = useState("");
  const [venue, setVenue] = useState(initialVenue || "ALL");
  const [depth, setDepth] = useState("all");
  const [freshness, setFreshness] = useState("all");
  const [status, setStatus] = useState("all");
  const [onBook, setOnBook] = useState(false);
  const [selected, setSelected] = useState<string | null>(null);

  useEffect(() => {
    if (initialVenue) setVenue(initialVenue);
  }, [initialVenue]);

  const companies = query.data?.companies ?? [];
  const statuses = useMemo(
    () => Array.from(new Set(companies.map((item) => item.thesis_status))).sort(),
    [companies],
  );
  const filtered = useMemo(() => {
    const needle = search.trim().toLowerCase();
    return companies.filter((company) => {
      if (venue !== "ALL" && company.venue !== venue) return false;
      if (depth !== "all" && company.depth !== depth) return false;
      if (status !== "all" && company.thesis_status !== status) return false;
      if (onBook && !company.on_book) return false;
      if (freshness === "fresh" && (company.age_hours == null || company.age_hours > 24)) return false;
      if (
        freshness === "aging" &&
        (company.age_hours == null || company.age_hours <= 24 || company.age_hours > 72)
      )
        return false;
      if (freshness === "stale" && (company.age_hours == null || company.age_hours <= 72)) return false;
      if (
        needle &&
        !company.symbol.toLowerCase().includes(needle) &&
        !company.name.toLowerCase().includes(needle) &&
        !company.summary.toLowerCase().includes(needle)
      )
        return false;
      return true;
    });
  }, [companies, depth, freshness, onBook, search, status, venue]);
  const chosen = filtered.find((company) => company.symbol === selected) ?? filtered[0];
  const detailQuery = useCompanyIntelligence(
    { symbol: chosen?.symbol, limit: 240 },
    Boolean(chosen?.symbol),
  );
  const detailCompany =
    detailQuery.data?.companies.find((company) => company.symbol === chosen?.symbol) ?? chosen;
  const lineageEvents = (detailQuery.data?.events ?? query.data?.events ?? [])
    .filter((event) => event.symbol === chosen?.symbol)
    .slice(0, 12);

  return (
    <div className="grid gap-6">
      {query.error ? (
        <p className="text-sm text-loss">{query.error instanceof Error ? query.error.message : String(query.error)}</p>
      ) : null}
      {!query.data && query.isPending ? <p className="text-sm text-faint">Loading company intelligence histories…</p> : null}

      {query.data ? (
        <>
          <section className="flex flex-wrap items-center gap-2 rounded-lg border border-line bg-panel/45 p-3">
            <Input
              name="company-filter"
              value={search}
              onChange={(event) => setSearch(event.target.value)}
              placeholder="Symbol, issuer or thesis…"
              className="w-full sm:w-64"
              aria-label="Filter companies"
            />
            <div className="flex gap-1">
              {VENUES.map((item) => (
                <FilterButton key={item} active={venue === item} onClick={() => setVenue(item)}>
                  {item}
                </FilterButton>
              ))}
            </div>
            <select
              name="company-depth"
              value={depth}
              onChange={(event) => setDepth(event.target.value)}
              className="h-8 rounded-md border border-line bg-ink px-2 font-mono text-[10px] uppercase text-muted"
              aria-label="Brief depth"
            >
              {DEPTHS.map((item) => (
                <option key={item} value={item}>
                  {item === "all" ? "all depths" : item}
                </option>
              ))}
            </select>
            <select
              name="company-freshness"
              value={freshness}
              onChange={(event) => setFreshness(event.target.value)}
              className="h-8 rounded-md border border-line bg-ink px-2 font-mono text-[10px] uppercase text-muted"
              aria-label="Freshness"
            >
              {FRESHNESS.map((item) => (
                <option key={item} value={item}>
                  {item === "fresh" ? "fresh ≤24h" : item === "aging" ? "aging 24–72h" : item === "stale" ? "stale >72h" : "all ages"}
                </option>
              ))}
            </select>
            <select
              name="company-thesis-status"
              value={status}
              onChange={(event) => setStatus(event.target.value)}
              className="h-8 max-w-48 rounded-md border border-line bg-ink px-2 font-mono text-[10px] uppercase text-muted"
              aria-label="Thesis status"
            >
              <option value="all">all theses</option>
              {statuses.map((item) => (
                <option key={item} value={item}>
                  {item.replaceAll("_", " ")}
                </option>
              ))}
            </select>
            <FilterButton active={onBook} onClick={() => setOnBook((value) => !value)}>
              on book
            </FilterButton>
            <Badge className="ml-auto">{filtered.length} visible</Badge>
          </section>

          <section className="grid gap-4 xl:grid-cols-[minmax(310px,0.72fr)_minmax(0,1.45fr)]">
            <div className="overflow-hidden rounded-lg border border-line bg-panel/35">
              <div className="flex items-center justify-between border-b border-line px-3 py-2.5">
                <p className="font-mono text-[9px] uppercase tracking-[0.2em] text-faint">Radar</p>
                <p className="font-mono text-[9px] text-faint">changed · current · evidenced</p>
              </div>
              <div className="max-h-[520px] overflow-auto xl:max-h-[calc(100vh-14rem)]">
                {filtered.map((company) => (
                  <CompanyRadarRow
                    key={company.symbol}
                    company={company}
                    selected={company.symbol === chosen?.symbol}
                    onSelect={() => setSelected(company.symbol)}
                  />
                ))}
                {!filtered.length ? <p className="px-4 py-10 text-sm text-faint">No company matches these filters.</p> : null}
              </div>
            </div>

            <div className="rounded-lg border border-line bg-panel/25 p-5">
              {detailCompany ? (
                <>
                  <CompanyIntelligenceDetail company={detailCompany} onSymbol={onSymbol} />
                  <section className="mt-6 border-t border-line pt-5">
                    <p className="mb-2 font-mono text-[9px] uppercase tracking-[0.2em] text-faint">
                      Brief & decision lineage
                    </p>
                    {detailQuery.error ? (
                      <p className="mb-2 text-xs text-loss">
                        {detailQuery.error instanceof Error
                          ? detailQuery.error.message
                          : String(detailQuery.error)}
                      </p>
                    ) : null}
                    <IntelligenceTimeline events={lineageEvents} />
                  </section>
                </>
              ) : (
                <p className="text-sm text-faint">Select a company to read its thesis.</p>
              )}
            </div>
          </section>
        </>
      ) : null}
    </div>
  );
}

function FilterButton({
  active,
  onClick,
  children,
}: {
  active: boolean;
  onClick: () => void;
  children: React.ReactNode;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      aria-pressed={active}
      className={cn(
        "h-8 rounded-md px-2.5 font-mono text-[10px] uppercase tracking-[0.12em] transition-colors",
        active ? "bg-accent/15 text-accent" : "bg-hairline text-dim hover:text-muted",
      )}
    >
      {children}
    </button>
  );
}
