import { ArrowRight } from "lucide-react";
import { useMemo, useState } from "react";
import { FamilyPositioningChart } from "@/components/intelligence/family-history-chart";
import { FamilyMatrix } from "@/components/intelligence/family-matrix";
import { RegionalFamilyComparison } from "@/components/intelligence/regional-comparison";
import { IntelligenceTimeline } from "@/components/intelligence/timeline";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardBody, CardHeader, CardTitle } from "@/components/ui/card";
import { useRegionIntelligence } from "@/hooks/use-intelligence";
import type { MacroBrief, RegionCurrentIntelligence } from "@/lib/types";
import { cn } from "@/lib/utils";

const VENUES = ["TW", "EU", "US"] as const;

export function RegionsPage({
  onCompanies,
  onUniverse,
  onSymbol,
}: {
  onCompanies: (venue: string) => void;
  onUniverse: () => void;
  onSymbol: (symbol: string) => void;
}) {
  const query = useRegionIntelligence();
  const data = query.data;
  const [venue, setVenue] = useState<string>("TW");
  const current = data?.current[venue];
  const families = useMemo(
    () => (data?.families ?? []).filter((row) => row.venue === venue),
    [data?.families, venue],
  );
  const events = useMemo(
    () => (data?.events ?? []).filter((event) => event.venue === venue),
    [data?.events, venue],
  );
  const run = current?.regional_run ?? {};
  const hotlist = stringList(run.selected_hotlist);
  const rationales = record(run.symbol_rationales);

  return (
    <div className="grid gap-6">
      {query.error ? (
        <p className="text-sm text-loss">{query.error instanceof Error ? query.error.message : String(query.error)}</p>
      ) : null}
      {!data && query.isPending ? <p className="text-sm text-faint">Comparing regional family boards…</p> : null}

      {data ? (
        <>
          <Card>
            <CardHeader>
              <div>
                <CardTitle>Cross-region macro radar</CardTitle>
              </div>
              <Badge tone="accent">TW · EU · US · {data.families.length}</Badge>
            </CardHeader>
            <RegionalFamilyComparison rows={data.comparison} />
          </Card>

          <div className="flex flex-wrap items-end justify-between gap-3">
            <div>
              <p className="font-mono text-[9px] uppercase tracking-[0.2em] text-accent">Regional evolution</p>
            </div>
          </div>

          <section className="grid gap-3 md:grid-cols-3">
            {VENUES.map((key) => (
              <RegionCard
                key={key}
                venue={key}
                current={data.current[key]}
                familyCount={data.families.filter((row) => row.venue === key).length}
                active={venue === key}
                onClick={() => setVenue(key)}
              />
            ))}
          </section>

          <section className="grid gap-4 xl:grid-cols-[minmax(0,1.35fr)_minmax(270px,0.65fr)]">
            <div className="min-w-0 space-y-4">
              <Card>
                <CardHeader>
                  <div>
                    <CardTitle>{venue} family positioning · 30 days</CardTitle>
                  </div>
                  <Badge tone="accent">rank history</Badge>
                </CardHeader>
                <CardBody>
                  <FamilyPositioningChart rows={families} />
                </CardBody>
              </Card>
              <Card>
                <CardHeader>
                  <div>
                    <CardTitle>{venue} family matrix</CardTitle>
                  </div>
                  <Badge tone="accent">{families.length} families</Badge>
                </CardHeader>
                <FamilyMatrix rows={families} onSymbol={onSymbol} />
              </Card>
            </div>

            <aside className="space-y-4">
              <Card>
                <CardHeader>
                  <CardTitle>Regional brief · {venue}</CardTitle>
                  <Badge>{label(current?.posture) || "no posture"}</Badge>
                </CardHeader>
                <CardBody className="space-y-4">
                  <p className="text-sm leading-relaxed text-muted">
                    {recordString(run, "summary") || macroPoint(current?.macro_brief) || "No regional summary is available."}
                  </p>
                  <div className="grid grid-cols-2 gap-2">
                    <MiniStat
                      label="Freshness"
                      value={
                        typeof current?.freshness_hours === "number"
                          ? `${Math.round(current.freshness_hours)}h`
                          : "—"
                      }
                    />
                    <MiniStat label="Hot set" value={String(hotlist.length)} />
                  </div>
                  <div className="flex flex-wrap gap-2">
                    <Button size="sm" onClick={() => onCompanies(venue)}>
                      Company radar
                      <ArrowRight className="size-3.5" />
                    </Button>
                    <Button variant="outline" size="sm" onClick={onUniverse}>
                      Operator universe
                    </Button>
                  </div>
                </CardBody>
              </Card>

              <Card>
                <CardHeader>
                  <CardTitle>Hot set · {venue}</CardTitle>
                  <span className="font-mono text-[10px] text-faint">{hotlist.length}</span>
                </CardHeader>
                <CardBody className="space-y-2">
                  {hotlist.slice(0, 14).map((symbol) => (
                    <button
                      key={symbol}
                      type="button"
                      onClick={() => onSymbol(symbol)}
                      className="w-full rounded-md px-2 py-1.5 text-left hover:bg-panel-hover"
                    >
                      <div className="flex items-center justify-between">
                        <span className="text-sm font-medium">{symbol}</span>
                        <ArrowRight className="size-3 text-faint" />
                      </div>
                      <p className="mt-0.5 line-clamp-2 text-[11px] leading-relaxed text-dim">
                        {typeof rationales[symbol] === "string" ? rationales[symbol] : "No symbol rationale."}
                      </p>
                    </button>
                  ))}
                  {!hotlist.length ? <p className="text-sm text-faint">No current hot-set for this venue.</p> : null}
                </CardBody>
              </Card>
            </aside>
          </section>

          <Card>
            <CardHeader>
              <div>
                <CardTitle>{venue} intelligence history</CardTitle>
                <p className="mt-1 text-xs text-dim">Briefs, universe analyses and mandate transitions</p>
              </div>
              <span className="font-mono text-[10px] text-faint">{events.length} events</span>
            </CardHeader>
            <CardBody className="px-3 py-2">
              <IntelligenceTimeline events={events.slice(0, 80)} empty={`No ${venue} event in this window.`} />
            </CardBody>
          </Card>
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
          <p className="text-xl font-semibold">{venue}</p>
          <p className="mt-0.5 text-xs text-dim">{label(current?.posture) || "no posture"}</p>
        </div>
        <Badge tone={freshnessTone(current?.freshness_hours)}>
          {typeof current?.freshness_hours === "number" ? `${Math.round(current.freshness_hours)}h` : "unknown"}
        </Badge>
      </div>
      <p className="mt-4 font-mono text-[10px] text-faint">
        {familyCount} families · {hot} hot names
      </p>
      <p className="mt-2 line-clamp-2 text-xs leading-relaxed text-muted">
        {recordString(run, "summary") || macroPoint(current?.macro_brief) || "No regional brief."}
      </p>
    </button>
  );
}

function MiniStat({ label: name, value }: { label: string; value: string }) {
  return (
    <div className="rounded-md border border-hairline px-3 py-2">
      <p className="font-mono text-[9px] uppercase tracking-[0.16em] text-faint">{name}</p>
      <p className="mt-1 font-mono text-sm text-muted">{value}</p>
    </div>
  );
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

function label(value: string | null | undefined): string {
  return (value ?? "").replaceAll("_", " ");
}
