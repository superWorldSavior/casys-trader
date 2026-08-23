import { useEffect, useMemo, useState } from "react";
import {
  CompanyIntelligenceDetail,
  CompanyRadarRow,
} from "@/components/intelligence/company-summary";
import { IntelligenceTimeline } from "@/components/intelligence/timeline";
import { Badge } from "@/components/ui/badge";
import { Input } from "@/components/ui/input";
import { useCompanyIntelligence } from "@/hooks/use-intelligence";
import { venueLabel } from "@/lib/humanize";
import type { CompanyIntelligence } from "@/lib/types";
import { cn } from "@/lib/utils";

const VENUES = ["ALL", "TW", "EU", "US"] as const;
const DEPTHS = ["all", "deep", "screen"] as const;
const FRESHNESS = ["all", "fresh", "aging", "stale"] as const;
const SCOPES = ["relevant", "all"] as const;
type Scope = (typeof SCOPES)[number];

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
  const [scope, setScope] = useState<Scope>(initialVenue ? "all" : "relevant");
  const [onBook, setOnBook] = useState(false);
  const [selected, setSelected] = useState<string | null>(null);

  useEffect(() => {
    if (!initialVenue) return;
    setVenue(initialVenue);
    setScope("all");
  }, [initialVenue]);

  const companies = query.data?.companies ?? [];
  const companyMap = useMemo(
    () => Object.fromEntries(companies.map((company) => [company.symbol, company.name])),
    [companies],
  );
  const statuses = useMemo(
    () => Array.from(new Set(companies.map((item) => item.thesis_status))).sort(),
    [companies],
  );
  const filtered = useMemo(() => {
    const needle = search.trim().toLowerCase();
    return companies.filter((company) => {
      if (scope === "relevant" && !isRelevantNow(company)) return false;
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
    }).sort(compareCompanyPriority);
  }, [companies, depth, freshness, onBook, scope, search, status, venue]);
  const advancedFiltersActive = depth !== "all" || freshness !== "all" || status !== "all";
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
      {query.error && !query.data ? (
        <p className="text-sm text-warn">Company research could not be loaded.</p>
      ) : query.error ? (
        <p className="text-xs text-warn">Company research could not be refreshed. The last available view is still shown.</p>
      ) : null}
      {!query.data && query.isPending ? <p className="text-sm text-faint">Loading the companies Casys follows…</p> : null}

      {query.data ? (
        <>
          <section className="flex flex-wrap items-center gap-2 rounded-lg border border-line bg-panel/45 p-3">
            <Input
              name="company-filter"
              value={search}
              onChange={(event) => setSearch(event.target.value)}
              placeholder="Search companies or reasoning…"
              className="w-full sm:w-64"
              aria-label="Filter companies"
            />
            <div className="flex gap-1" role="group" aria-label="Research scope">
              {SCOPES.map((item) => (
                <FilterButton key={item} active={scope === item} onClick={() => setScope(item)}>
                  {item === "relevant" ? "Relevant now" : "All research"}
                </FilterButton>
              ))}
            </div>
            <div className="flex gap-1" role="group" aria-label="Region">
              {VENUES.map((item) => (
                <FilterButton key={item} active={venue === item} onClick={() => setVenue(item)}>
                  {venueLabel(item)}
                </FilterButton>
              ))}
            </div>
            <FilterButton active={onBook} onClick={() => setOnBook((value) => !value)}>
              In portfolio
            </FilterButton>
            <Badge className="ml-auto">
              {filtered.length} {search.trim() ? "matches" : scope === "relevant" ? "relevant" : "total"}
            </Badge>

            <details className="group w-full border-t border-hairline pt-2">
              <summary className="cursor-pointer list-none font-mono text-[9px] uppercase tracking-[0.16em] text-faint [&::-webkit-details-marker]:hidden">
                More filters{advancedFiltersActive ? " · active" : ""}
              </summary>
              <div className="mt-2 flex flex-wrap gap-2">
                <select
                  name="company-depth"
                  value={depth}
                  onChange={(event) => setDepth(event.target.value)}
                  className="h-8 rounded-md border border-line bg-ink px-2 font-mono text-[10px] uppercase text-muted"
                  aria-label="Review depth"
                >
                  {DEPTHS.map((item) => (
                    <option key={item} value={item}>
                      {item === "all" ? "Any review depth" : item === "deep" ? "In-depth reviews" : "Quick reviews"}
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
                      {item === "fresh" ? "Updated in the last day" : item === "aging" ? "Updated 1–3 days ago" : item === "stale" ? "Needs refreshing" : "Any update date"}
                    </option>
                  ))}
                </select>
                <select
                  name="company-thesis-status"
                  value={status}
                  onChange={(event) => setStatus(event.target.value)}
                  className="h-8 max-w-52 rounded-md border border-line bg-ink px-2 font-mono text-[10px] uppercase text-muted"
                  aria-label="Company view status"
                >
                  <option value="all">Any company view</option>
                  {statuses.map((item) => (
                    <option key={item} value={item}>
                      {thesisStatusLabel(item)}
                    </option>
                  ))}
                </select>
              </div>
            </details>
          </section>

          <section className="grid gap-4 lg:grid-cols-[minmax(310px,0.72fr)_minmax(0,1.45fr)]">
            <div className="overflow-hidden rounded-lg border border-line bg-panel/35">
              <div className="flex items-center justify-between border-b border-line px-3 py-2.5">
                <p className="font-mono text-[9px] uppercase tracking-[0.2em] text-faint">
                  {scope === "relevant" && !search.trim() ? "Relevant now" : "Research results"}
                </p>
                <p className="font-mono text-[9px] text-faint">{filtered.length}</p>
              </div>
              <div className="max-h-[520px] overflow-auto lg:max-h-[calc(100vh-14rem)]">
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
                  <details className="group mt-6 border-t border-line pt-5">
                    <summary className="cursor-pointer list-none font-mono text-[9px] uppercase tracking-[0.2em] text-faint [&::-webkit-details-marker]:hidden">
                      How this view changed
                    </summary>
                    {detailQuery.error ? (
                      <p className="mt-2 text-xs text-warn">Earlier reviews could not be refreshed.</p>
                    ) : null}
                    <div className="mt-3">
                      <IntelligenceTimeline events={lineageEvents} companyMap={companyMap} />
                    </div>
                  </details>
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

function isRelevantNow(company: CompanyIntelligence): boolean {
  if (company.on_book) return true;
  return company.thesis_changed && typeof company.age_hours === "number" && company.age_hours <= 24;
}

function compareCompanyPriority(left: CompanyIntelligence, right: CompanyIntelligence): number {
  const leftTier = left.on_book ? 0 : left.thesis_changed ? 1 : 2;
  const rightTier = right.on_book ? 0 : right.thesis_changed ? 1 : 2;
  if (leftTier !== rightTier) return leftTier - rightTier;
  return (left.age_hours ?? Number.POSITIVE_INFINITY) - (right.age_hours ?? Number.POSITIVE_INFINITY);
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

function thesisStatusLabel(value: string): string {
  const key = value.toLowerCase();
  if (key === "intact") return "Outlook intact";
  if (key === "untested" || key === "insufficient_evidence") return "Evidence incomplete";
  if (key.includes("construct") || key.includes("positive") || key.includes("bull")) return "Positive outlook";
  if (key.includes("caution") || key.includes("negative") || key.includes("bear")) return "Cautious outlook";
  if (key.includes("watch")) return "Watch closely";
  if (key.includes("mixed") || key.includes("neutral")) return "Mixed outlook";
  if (key === "unknown") return "Not yet assessed";
  return value.replaceAll("_", " ");
}
