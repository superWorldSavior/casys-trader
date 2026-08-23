import { ArrowRight, ChevronDown } from "lucide-react";
import { useMemo, useState } from "react";
import { FamilyPositioningChart } from "@/components/intelligence/family-history-chart";
import { FamilyMatrix } from "@/components/intelligence/family-matrix";
import { MarketIntelligenceAtlas } from "@/components/intelligence/market-intelligence-atlas";
import { IntelligenceTimeline } from "@/components/intelligence/timeline";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardBody, CardHeader, CardTitle } from "@/components/ui/card";
import { useRegionIntelligence } from "@/hooks/use-intelligence";
import { companyDisplayName, familyLabel, grossModeLabel, plainMarketLanguage, venueLabel } from "@/lib/humanize";
import { collectScopeKeys } from "@/lib/market-intelligence-atlas";
import type { FamilyIntelligence, MacroBrief, RegionCurrentIntelligence } from "@/lib/types";
import { cn } from "@/lib/utils";

export function RegionsPage({
  companyMap = {},
  onCompanies,
  onSymbol,
}: {
  companyMap?: Record<string, string>;
  onCompanies: (venue: string) => void;
  onSymbol: (symbol: string) => void;
}) {
  const query = useRegionIntelligence();
  const data = query.data;
  const scopes = useMemo(
    () => collectScopeKeys(data?.current, data?.families),
    [data?.current, data?.families],
  );
  const [scope, setScope] = useState("");
  const activeScope = scopes.includes(scope) ? scope : (scopes[0] ?? "");
  const current = activeScope ? data?.current[activeScope] : undefined;
  const families = useMemo(
    () => (data?.families ?? []).filter((row) => row.venue === activeScope),
    [data?.families, activeScope],
  );
  const events = useMemo(
    () => (data?.events ?? []).filter((event) => event.venue === activeScope),
    [data?.events, activeScope],
  );
  const run = current?.regional_run ?? {};
  const hotlist = stringList(run.selected_hotlist);
  const rationales = record(run.symbol_rationales);
  const regionSummary = regionNarrative(
    recordString(run, "summary") || macroPoint(current?.macro_brief),
    "No market summary is available.",
  );
  const topFamilies = families
    .filter((row) => typeof row.rank === "number")
    .sort((left, right) => (left.rank ?? 999) - (right.rank ?? 999))
    .slice(0, 4);

  return (
    <div className="grid gap-6">
      {query.error && !data ? (
        <p className="text-sm text-warn">Market research could not be loaded.</p>
      ) : query.error ? (
        <p className="text-xs text-warn">Market research could not be refreshed. The last available view is still shown.</p>
      ) : null}
      {!data && query.isPending ? <p className="text-sm text-faint">Building the current market map…</p> : null}

      {data ? (
        <>
          <section className="grid gap-3 [grid-template-columns:repeat(auto-fit,minmax(min(100%,16rem),1fr))]">
            {scopes.map((key) => (
              <RegionCard
                key={key}
                venue={key}
                current={data.current[key]}
                familyCount={data.families.filter((row) => row.venue === key).length}
                active={activeScope === key}
                onClick={() => setScope(key)}
              />
            ))}
          </section>

          <MarketIntelligenceAtlas
            current={data.current}
            families={data.families}
            comparison={data.comparison}
            activeScope={activeScope}
            onSelectScope={setScope}
          />

          <section className="grid items-start gap-4 lg:grid-cols-[minmax(0,1.15fr)_minmax(320px,0.85fr)]">
            <Card>
              <CardHeader>
                <CardTitle>{regionReasonTitle(current?.posture, activeScope)}</CardTitle>
              </CardHeader>
              <CardBody className="space-y-4">
                <p className="text-sm leading-relaxed text-muted">{regionSummary}</p>
                <ThemeFocus label="Leading themes" rows={topFamilies} tone="accent" empty="No ranked theme is recorded." />
                <div className="flex flex-wrap items-center justify-between gap-3">
                  <p className="font-mono text-[10px] uppercase tracking-[0.14em] text-faint">
                    {typeof current?.freshness_hours === "number"
                      ? `Updated ${Math.round(current.freshness_hours)}h ago`
                      : "Update time unavailable"}
                  </p>
                  <Button size="sm" onClick={() => onCompanies(activeScope)}>
                    View company research
                    <ArrowRight className="size-3.5" />
                  </Button>
                </div>
              </CardBody>
            </Card>

            <Card>
              <CardHeader>
                <CardTitle>Companies in focus</CardTitle>
                <div className="flex items-center gap-2">
                  <Badge>{venueLabel(activeScope)}</Badge>
                  <span className="font-mono text-[10px] text-faint">{hotlist.length}</span>
                </div>
              </CardHeader>
              <CardBody className="space-y-2">
                {hotlist.slice(0, 10).map((symbol) => (
                  <button
                    key={symbol}
                    type="button"
                    onClick={() => onSymbol(symbol)}
                    className="w-full rounded-md px-2 py-1.5 text-left hover:bg-panel-hover focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent/50"
                  >
                    <div className="flex items-center justify-between">
                      <span className="min-w-0 truncate text-sm font-medium">{companyDisplayName(companyMap, symbol)}</span>
                      <ArrowRight className="size-3 text-faint" />
                    </div>
                    <p className="mt-0.5 line-clamp-2 text-[11px] leading-relaxed text-dim">
                      <span className="font-mono text-[9px] text-faint">{symbol}</span>
                      <span className="mx-1 text-faint">·</span>
                      {typeof rationales[symbol] === "string"
                        ? regionNarrative(rationales[symbol] as string, "No explanation recorded.")
                        : "No explanation recorded."}
                    </p>
                  </button>
                ))}
                {!hotlist.length ? <p className="text-sm text-faint">No company is in focus for this market.</p> : null}
              </CardBody>
            </Card>
          </section>

          <details className="group overflow-hidden rounded-lg border border-line bg-panel/35">
            <summary className="flex cursor-pointer list-none items-center justify-between gap-4 px-4 py-3 [&::-webkit-details-marker]:hidden">
              <span className="font-mono text-[10px] uppercase tracking-[0.18em] text-faint">Research record</span>
              <span className="flex items-center gap-2 font-mono text-[9px] text-faint">
                {data.families.length} themes · 30 days
                <ChevronDown className="size-3.5 transition-transform group-open:rotate-180" />
              </span>
            </summary>
            <div className="space-y-4 border-t border-line p-4">
              <section className="grid gap-4 xl:grid-cols-[minmax(0,1fr)_minmax(0,1fr)]">
                <Card>
                  <CardHeader>
                    <CardTitle>How {venueLabel(activeScope)} priorities moved</CardTitle>
                  </CardHeader>
                  <CardBody>
                    <FamilyPositioningChart rows={families} />
                  </CardBody>
                </Card>
                <Card>
                  <CardHeader>
                    <CardTitle>All {venueLabel(activeScope)} themes</CardTitle>
                    <Badge tone="accent">{families.length}</Badge>
                  </CardHeader>
                  <FamilyMatrix rows={families} companyMap={companyMap} onSymbol={onSymbol} />
                </Card>
              </section>

              <Card>
                <CardHeader>
                  <CardTitle>{venueLabel(activeScope)} research history</CardTitle>
                  <span className="font-mono text-[10px] text-faint">{events.length} updates</span>
                </CardHeader>
                <CardBody className="px-3 py-2">
                  <IntelligenceTimeline
                    events={events.slice(0, 80)}
                    companyMap={companyMap}
                    empty={`No ${venueLabel(activeScope)} update in this window.`}
                  />
                </CardBody>
              </Card>
            </div>
          </details>
        </>
      ) : null}
    </div>
  );
}

function RegionCard({
  venue,
  current,
  familyCount,
  active,
  onClick,
}: {
  venue: string;
  current?: RegionCurrentIntelligence;
  familyCount: number;
  active: boolean;
  onClick: () => void;
}) {
  const run = current?.regional_run ?? {};
  const hot = stringList(run.selected_hotlist).length;
  return (
    <button
      type="button"
      onClick={onClick}
      aria-pressed={active}
      className={cn(
        "rounded-lg border px-4 py-4 text-left transition-colors",
        active ? "border-accent/45 bg-panel" : "border-line bg-panel/45 hover:bg-panel-hover",
      )}
    >
      <div className="flex items-start justify-between">
        <div>
          <p className="text-xl font-semibold">{venueLabel(venue)}</p>
          <p className="mt-0.5 text-xs text-dim">{current?.posture ? grossModeLabel(current.posture) : "No outlook"}</p>
        </div>
        <Badge tone={freshnessTone(current?.freshness_hours)}>
          {typeof current?.freshness_hours === "number" ? `${Math.round(current.freshness_hours)}h` : "unknown"}
        </Badge>
      </div>
      <p className="mt-4 font-mono text-[10px] text-faint">
        {familyCount} themes · {hot} companies in focus
      </p>
    </button>
  );
}

function regionReasonTitle(posture: string | null | undefined, venue: string): string {
  const place = venueLabel(venue);
  const key = String(posture ?? "").toLowerCase();
  if (key === "watch") return `Why ${place} is under close watch`;
  if (key === "selective" || key === "cautious") return `Why Casys is selective in ${place}`;
  if (key === "defensive" || key === "risk_off") return `Why Casys is defensive in ${place}`;
  if (key === "favor" || key === "favored") return `Why Casys prefers ${place}`;
  return `The current ${place} view`;
}

function ThemeFocus({
  label,
  rows,
  tone,
  empty,
}: {
  label: string;
  rows: FamilyIntelligence[];
  tone: "accent";
  empty: string;
}) {
  return (
    <div className="rounded-md border border-hairline px-3 py-3">
      <p className="font-mono text-[9px] uppercase tracking-[0.16em] text-faint">{label}</p>
      {rows.length ? (
        <div className="mt-2 flex flex-wrap gap-1.5">
          {rows.map((row) => (
            <Badge key={row.family} tone={tone}>{familyLabel(row.family)}</Badge>
          ))}
        </div>
      ) : (
        <p className="mt-2 text-xs text-faint">{empty}</p>
      )}
    </div>
  );
}

function regionNarrative(value?: string | null, fallback = ""): string {
  const text = String(value ?? "").trim() || fallback;
  const regional = text
    .replace(
      /\b(Europe|Taiwan|United States) is treated as a selective, cautious book\b/gi,
      (_match, place: string) => `Casys is selective and cautious in ${place}`,
    )
    .replace(/\bthe book concentrates\b/gi, "Casys's research focuses")
    .replace(/\bthe book focuses\b/gi, "Casys's research focuses")
    .replace(/\bthe portfolio concentrates\b/gi, "Casys's research focuses")
    .replace(/\bweaker cash[\s_-]*conversion names\b/gi, "companies with weaker cash generation")
    .replace(
      /\bidentity[\s_-]*unverified design paper\b/gi,
      "a chip-design company whose identity is not confirmed",
    )
    .replace(
      /(?:higher )?oil pressure on energy-importing Asia argue for a shorter list than the (?:deterministic fallback|default company list)/gi,
      "higher oil costs for energy-importing Asian economies lead Casys to review fewer companies than usual",
    )
    .replace(/\bthin[\s_-]*margin recovery or cooling names\b/gi, "low-margin recovery and cooling companies")
    .replace(
      /\bwere ranked below cleaner cash and catalyst cases\b/gi,
      "were ranked below companies with stronger cash generation and clearer upcoming events",
    )
    .replace(/\bthe quality memory anchor\b/gi, "the high-quality memory company in focus")
    .replace(/\bquality memory anchor\b/gi, "high-quality memory company in focus")
    .replace(/\bthe custom[\s_-]*silicon setup\b/gi, "the custom-chip opportunity")
    .replace(/\bcustom[\s_-]*silicon setup\b/gi, "custom-chip opportunity")
    .replace(/\bstill concentrates AI[\s_-]*board attention\b/gi, "remains a focus among AI circuit-board companies")
    .replace(/\bAI[\s_-]*board attention\b/gi, "attention among AI circuit-board companies")
    .replace(/\bthe connector candidate\b/gi, "this connector company")
    .replace(/\bconnector candidate\b/gi, "connector company")
    .replace(/\bcontradict earnings quality\b/gi, "raise doubts about the quality of those earnings")
    .replace(
      /\bthis compound[\s_-]*semiconductor name prints high reported margins\b/gi,
      "this compound-semiconductor company reports high margins",
    )
    .replace(/\bcompound[\s_-]*semiconductor name prints\b/gi, "compound-semiconductor company reports")
    .replace(/\bon the AI hardware watch\b/gi, "under review as an AI hardware company")
    .replace(/\bPrior previous selections lagged the deterministic list\b/gi, "The previous selection was less convincing")
    .replace(/\bthis pass\b/gi, "the current review")
    .replace(/\bthat 25-name fallback\b/gi, "the broader 25-company list");
  return plainMarketLanguage(regional)
    .replace(/\bthe portfolio concentrates\b/gi, "Casys's research focuses")
    .replace(/\bthe portfolio focuses\b/gi, "Casys's research focuses");
}

function record(value: unknown): Record<string, unknown> {
  return value && typeof value === "object" && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : {};
}

function recordString(value: unknown, key: string): string {
  const found = record(value)[key];
  return typeof found === "string" ? found : "";
}

function stringList(value: unknown): string[] {
  return Array.isArray(value) ? value.filter((item): item is string => typeof item === "string") : [];
}

function macroPoint(brief?: MacroBrief | null): string {
  return brief?.alerts?.find((item) => item.point)?.point ?? "";
}

function freshnessTone(value: number | null | undefined): "gain" | "warn" | "loss" | "muted" {
  if (typeof value !== "number") return "muted";
  if (value <= 24) return "gain";
  if (value <= 72) return "warn";
  return "loss";
}
