import { useMemo, useState } from "react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardBody, CardHeader, CardTitle } from "@/components/ui/card";
import { PosturePanel } from "@/components/intelligence/posture-panel";
import { IntelligenceTimeline } from "@/components/intelligence/timeline";
import { WorldMap } from "@/components/intelligence/world-map";
import { useWorldIntelligence, useNewsFeed } from "@/hooks/use-intelligence";
import { formatAgo } from "@/lib/format";
import type { IntelligenceEvent } from "@/lib/types";
import { cn } from "@/lib/utils";

const FILTERS = ["all", "changes", "macro", "failures"] as const;
type Filter = (typeof FILTERS)[number];

export function WorldPage({ onReports }: { onReports: () => void }) {
  const query = useWorldIntelligence();
  const feedQuery = useNewsFeed({ limit: 200 });
  const [filter, setFilter] = useState<Filter>("all");
  const data = query.data;
  const events = useMemo(() => filterEvents(data?.events ?? [], filter), [data?.events, filter]);
  const posture = data?.current.posture;
  const digest = data?.current.digest;
  const latestFailure = data?.current.latest_failure;

  // Comptage des événements géopolitiques par pays pour la carte
  const countryCounts = useMemo(() => {
    const counts: Record<string, number> = {};
    for (const item of feedQuery.data?.items ?? []) {
      if (item.kind !== "geo" || !item.country) continue;
      counts[item.country] = (counts[item.country] ?? 0) + 1;
    }
    return counts;
  }, [feedQuery.data?.items]);

  // Posture par venue (TW / EU / US)
  // Record<string, string> est assignable à Record<string, string | null>
  const venuePosture = useMemo(
    (): Record<string, string | null> => posture?.venue_posture ?? {},
    [posture],
  );

  return (
    <div className="grid gap-6">
      {query.error ? <QueryError value={query.error} /> : null}
      {!data && query.isPending ? <p className="text-sm text-faint">Building the 30-day world timeline…</p> : null}
      {data ? <PosturePanel posture={posture} digest={digest} /> : null}

      {/* ── Carte du monde ─────────────────────────────────────────────── */}
      {data ? (
        <Card>
          <CardHeader>
            <CardTitle>The world right now</CardTitle>
            {feedQuery.isPending ? (
              <span className="font-mono text-[9px] text-faint">loading news…</span>
            ) : feedQuery.data ? (
              <span className="font-mono text-[9px] text-faint">
                {feedQuery.data.counts.geo} geo events · {feedQuery.data.counts.company} company
              </span>
            ) : null}
          </CardHeader>
          <CardBody className="px-3 pb-4 pt-3">
            {feedQuery.data && feedQuery.data.counts.geo === 0 && Object.keys(countryCounts).length === 0 ? (
              <p className="py-4 text-center text-sm text-faint">No geopolitical feed data yet.</p>
            ) : (
              <WorldMap countryCounts={countryCounts} venues={venuePosture} />
            )}
          </CardBody>
        </Card>
      ) : null}

      {data ? (
        <section className="grid gap-4 xl:grid-cols-[minmax(0,1.45fr)_minmax(280px,0.55fr)]">
          <Card>
            <CardHeader className="flex-wrap">
              <div>
                <CardTitle>Historical intelligence</CardTitle>
                <p className="mt-1 text-xs text-dim">{events.length} visible events · newest first</p>
              </div>
              <div className="flex flex-wrap gap-1">
                {FILTERS.map((item) => (
                  <button
                    key={item}
                    type="button"
                    onClick={() => setFilter(item)}
                    aria-pressed={filter === item}
                    className={cn(
                      "rounded-sm px-2 py-1 font-mono text-[9px] uppercase tracking-[0.14em]",
                      filter === item ? "bg-accent/15 text-accent" : "text-faint hover:bg-hairline hover:text-muted",
                    )}
                  >
                    {item}
                  </button>
                ))}
                <Button variant="ghost" size="sm" onClick={onReports}>
                  Raw reports
                </Button>
              </div>
            </CardHeader>
            <CardBody className="px-3 py-2">
              <IntelligenceTimeline events={events} />
            </CardBody>
          </Card>

          <aside className="space-y-4 xl:sticky xl:top-0 xl:self-start">
            <Card>
              <CardHeader>
                <CardTitle>Signal ledger</CardTitle>
                <Badge tone={latestFailure ? "warn" : "gain"}>{latestFailure ? "degraded" : "current"}</Badge>
              </CardHeader>
              <CardBody className="space-y-4">
                <Signal
                  label="Regime"
                  value={label(digest?.regime) || "unknown"}
                  meta={digest?.as_of ? formatAgo(digest.as_of) : "no digest"}
                />
                <Signal label="Rates" value={label(digest?.rates_bias) || "unknown"} />
                <Signal label="USD" value={label(digest?.usd_bias) || "unknown"} />
                <Signal
                  label="Family board"
                  value={label(data.current.family_board?.status) || "missing"}
                  meta={data.current.family_board?.as_of ? formatAgo(data.current.family_board.as_of) : undefined}
                />
              </CardBody>
            </Card>

            <Card>
              <CardHeader>
                <CardTitle>Current evidence</CardTitle>
              </CardHeader>
              <CardBody className="space-y-3">
                {(digest?.points ?? []).slice(0, 5).map((point, index) => (
                  <div key={`${point.point}-${index}`} className="border-b border-hairline pb-3 last:border-0 last:pb-0">
                    <p className="text-sm leading-relaxed text-muted">{point.point || "Unlabelled point"}</p>
                    <p className="mt-1 font-mono text-[9px] text-faint">
                      {[point.direction, point.signal, point.severity].filter(Boolean).join(" · ")}
                      {point.source_refs?.length ? ` · ${point.source_refs.length} sources` : ""}
                    </p>
                  </div>
                ))}
                {!digest?.points?.length ? <p className="text-sm text-faint">No global evidence points.</p> : null}
              </CardBody>
            </Card>

            {latestFailure ? (
              <Card className="border-loss/35">
                <CardHeader>
                  <CardTitle className="text-loss">Latest refresh failure</CardTitle>
                </CardHeader>
                <CardBody>
                  <p className="text-sm leading-relaxed text-muted">
                    {pick(latestFailure, ["error_message", "message", "error_code"]) || "Refresh failed."}
                  </p>
                  <p className="mt-2 font-mono text-[9px] text-faint">
                    {formatAgo(pick(latestFailure, ["as_of", "failed_at"]))}
                  </p>
                </CardBody>
              </Card>
            ) : null}
          </aside>
        </section>
      ) : null}
    </div>
  );
}

function filterEvents(events: IntelligenceEvent[], filter: Filter): IntelligenceEvent[] {
  if (filter === "changes") return events.filter((event) => event.changes.length);
  if (filter === "macro") return events.filter((event) => event.kind === "macro_brief" || event.kind === "global_digest");
  if (filter === "failures") return events.filter((event) => event.status === "error" || event.kind.includes("failure"));
  return events;
}

function Signal({ label: name, value, meta }: { label: string; value: string; meta?: string }) {
  return (
    <div className="flex items-start justify-between gap-4">
      <p className="font-mono text-[9px] uppercase tracking-[0.16em] text-faint">{name}</p>
      <div className="text-right">
        <p className="text-sm text-muted">{value}</p>
        {meta ? <p className="font-mono text-[9px] text-faint">{meta}</p> : null}
      </div>
    </div>
  );
}

function QueryError({ value }: { value: unknown }) {
  return <p className="text-sm text-loss">{value instanceof Error ? value.message : String(value)}</p>;
}

function label(value: string | null | undefined): string {
  return (value ?? "").replaceAll("_", " ");
}

function pick(payload: Record<string, unknown>, keys: string[]): string | undefined {
  for (const key of keys) {
    const value = payload[key];
    if (typeof value === "string" && value.trim()) return value;
  }
  return undefined;
}
