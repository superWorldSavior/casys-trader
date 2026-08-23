import { useMemo, useState } from "react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardBody, CardHeader, CardTitle } from "@/components/ui/card";
import { IntelligenceTimeline } from "@/components/intelligence/timeline";
import { WorldMap } from "@/components/intelligence/world-map";
import { useWorldIntelligence, useNewsFeed } from "@/hooks/use-intelligence";
import { formatAgo } from "@/lib/format";
import {
  grossModeLabel,
  humanToken,
  marketRegimeLabel,
  netBiasLabel,
  plainMarketLanguage,
  projectionFreshness,
} from "@/lib/humanize";
import type { IntelligenceEvent } from "@/lib/types";
import { cn } from "@/lib/utils";

const FILTERS = ["all", "changes", "macro", "failures"] as const;
const FILTER_LABELS: Record<(typeof FILTERS)[number], string> = {
  all: "All updates",
  changes: "Changes",
  macro: "Economy",
  failures: "Refresh issues",
};
type Filter = (typeof FILTERS)[number];

export function WorldPage({ onReports }: { onReports: () => void }) {
  const query = useWorldIntelligence();
  const feedQuery = useNewsFeed({ limit: 200 });
  const [filter, setFilter] = useState<Filter>("changes");
  const [showOlder, setShowOlder] = useState(false);
  const data = query.data;
  const events = useMemo(() => filterEvents(data?.events ?? [], filter), [data?.events, filter]);
  const visibleEvents = showOlder ? events : events.slice(0, 20);
  const posture = data?.current.posture;
  const digest = data?.current.digest;
  const latestFailure = data?.current.latest_failure;
  const digestFreshness = projectionFreshness(digest);

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
  const hasWorldReporting = Object.keys(countryCounts).length > 0;

  return (
    <div className="grid gap-4">
      {query.error && !data ? <QueryError /> : null}
      {query.error && data ? (
        <p className="text-xs text-warn">The world view could not be refreshed. The last available update is still shown.</p>
      ) : null}
      {!data && query.isPending ? <p className="text-sm text-faint">Building the 30-day world timeline…</p> : null}

      {data ? (
        <section className="grid items-start gap-4 lg:grid-cols-[minmax(0,1.45fr)_minmax(280px,0.55fr)]">
          <Card>
            <CardHeader className="flex-wrap">
              <div>
                <CardTitle>What changed</CardTitle>
                <p className="mt-1 text-xs text-dim">{events.length} relevant updates · newest first</p>
              </div>
              <div className="flex flex-wrap gap-1">
                {FILTERS.map((item) => (
                  <button
                    key={item}
                    type="button"
                    onClick={() => {
                      setFilter(item);
                      setShowOlder(false);
                    }}
                    aria-pressed={filter === item}
                    className={cn(
                      "rounded-sm px-2 py-1 font-mono text-[9px] uppercase tracking-[0.14em]",
                      filter === item ? "bg-accent/15 text-accent" : "text-faint hover:bg-hairline hover:text-muted",
                    )}
                  >
                    {FILTER_LABELS[item]}
                  </button>
                ))}
                <Button variant="ghost" size="sm" onClick={onReports}>
                  Technical reports
                </Button>
              </div>
            </CardHeader>
            <CardBody className="px-3 py-2">
              <IntelligenceTimeline events={visibleEvents} openFirst={filter === "changes"} />
              {events.length > 20 ? (
                <div className="border-t border-hairline px-3 py-3">
                  <Button variant="ghost" size="sm" onClick={() => setShowOlder((value) => !value)} className="w-full">
                    {showOlder ? "Show first 20 updates" : `Show ${events.length - 20} older updates`}
                  </Button>
                </div>
              ) : null}
            </CardBody>
          </Card>

          <aside className="space-y-4 lg:sticky lg:top-0 lg:self-start">
            <Card>
              <CardHeader>
                <CardTitle>Current outlook</CardTitle>
                <Badge tone={latestFailure || !digestFreshness.current ? "warn" : "gain"}>
                  {latestFailure ? "Refresh issue" : digestFreshness.label}
                </Badge>
              </CardHeader>
              <CardBody className="space-y-4">
                <Signal label="Risk appetite" value={posture?.gross_mode ? grossModeLabel(posture.gross_mode) : "Not yet assessed"} />
                <Signal label="Portfolio direction" value={posture?.net_bias ? netBiasLabel(posture.net_bias) : "Not yet assessed"} />
                <Signal
                  label="Markets"
                  value={digest?.regime ? marketRegimeLabel(digest.regime) : "Not yet assessed"}
                  meta={digest?.as_of ? formatAgo(digest.as_of) : "no digest"}
                />
                <Signal label="Interest rates" value={ratesBiasLabel(digest?.rates_bias)} />
                <Signal label="US dollar" value={usdBiasLabel(digest?.usd_bias)} />
                <Signal
                  label="Theme priorities"
                  value={coverageStatusLabel(data.current.family_board?.status)}
                  meta={data.current.family_board?.as_of ? formatAgo(data.current.family_board.as_of) : undefined}
                />
              </CardBody>
            </Card>

            <Card>
              <CardHeader>
                <CardTitle>Evidence behind the current view</CardTitle>
              </CardHeader>
              <CardBody className="space-y-3">
                {(digest?.points ?? []).slice(0, 5).map((point, index) => (
                  <div key={`${point.point}-${index}`} className="border-b border-hairline pb-3 last:border-0 last:pb-0">
                    <p className="text-sm leading-relaxed text-muted">{plainMarketLanguage(point.point) || "Unlabelled point"}</p>
                    <p className="mt-1 font-mono text-[9px] text-faint">
                      {[point.direction, point.signal, point.severity].filter(Boolean).map((value) => plainMarketLanguage(humanToken(value))).join(" · ")}
                      {point.source_refs?.length ? ` · ${point.source_refs.length} sources` : ""}
                    </p>
                  </div>
                ))}
                {!digest?.points?.length ? <p className="text-sm text-faint">No supporting evidence is recorded for the current world view.</p> : null}
              </CardBody>
            </Card>

            {latestFailure ? (
              <Card className="border-loss/35">
                <CardHeader>
                  <CardTitle className="text-loss">Latest refresh issue</CardTitle>
                </CardHeader>
                <CardBody>
                  <p className="text-sm leading-relaxed text-muted">
                    {refreshFailureLabel(pick(latestFailure, ["error_message", "message", "error_code"]))}
                  </p>
                  <p className="mt-2 font-mono text-[9px] text-faint">
                    {formatAgo(pick(latestFailure, ["as_of", "failed_at"]))}
                  </p>
                  {pick(latestFailure, ["error_message", "message", "error_code"]) ? (
                    <details className="mt-3 text-xs text-dim">
                      <summary className="cursor-pointer">Technical details</summary>
                      <p className="mt-1 break-words font-mono text-[10px] text-faint">
                        {pick(latestFailure, ["error_message", "message", "error_code"])}
                      </p>
                    </details>
                  ) : null}
                </CardBody>
              </Card>
            ) : null}
          </aside>
        </section>
      ) : null}

      {/* Reporting density is context, not the primary world answer. */}
      {data && (feedQuery.isPending || Boolean(feedQuery.error) || hasWorldReporting) ? (
        <Card>
          <CardHeader>
            <CardTitle>Where reporting is concentrated</CardTitle>
            {feedQuery.isPending ? (
              <span className="font-mono text-[9px] text-faint">loading news…</span>
            ) : feedQuery.error ? (
              <Badge tone="warn">World feed unavailable</Badge>
            ) : feedQuery.data ? (
              <span className="font-mono text-[9px] text-faint">
                {feedQuery.data.counts.geo} geopolitical reports · {feedQuery.data.counts.company} company reports
              </span>
            ) : null}
          </CardHeader>
          <CardBody className="px-3 pb-4 pt-3">
            {feedQuery.error && !feedQuery.data ? (
              <div className="py-4 text-center">
                <p className="text-sm text-warn">The world-news layer could not be refreshed.</p>
                <p className="mt-1 text-xs text-dim">The latest successful world view remains available above.</p>
              </div>
            ) : (
              <WorldMap countryCounts={countryCounts} venues={venuePosture} />
            )}
          </CardBody>
        </Card>
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

function QueryError() {
  return <p className="text-sm text-warn">The world view could not be loaded.</p>;
}

function ratesBiasLabel(value?: string | null): string {
  const key = String(value ?? "").toLowerCase();
  if (key === "hawkish") return "Upward pressure";
  if (key === "dovish") return "Room to ease";
  if (key === "neutral") return "Broadly steady";
  return value ? humanToken(value) : "Not yet assessed";
}

function usdBiasLabel(value?: string | null): string {
  const key = String(value ?? "").toLowerCase();
  if (key === "strong") return "Strengthening";
  if (key === "weak") return "Weakening";
  if (key === "neutral") return "Broadly steady";
  return value ? humanToken(value) : "Not yet assessed";
}

function coverageStatusLabel(value?: string | null): string {
  const key = String(value ?? "").toLowerCase();
  if (["complete", "full", "current", "success"].includes(key)) return "Available";
  if (["partial", "incomplete", "degraded"].includes(key)) return "Partially available";
  if (["missing", "failed", "error"].includes(key)) return "Unavailable";
  return value ? humanToken(value) : "Not available";
}

function pick(payload: Record<string, unknown>, keys: string[]): string | undefined {
  for (const key of keys) {
    const value = payload[key];
    if (typeof value === "string" && value.trim()) return value;
  }
  return undefined;
}

function refreshFailureLabel(value?: string): string {
  const key = String(value ?? "").toLowerCase();
  if (!key) return "The latest intelligence refresh did not complete.";
  if (key.includes("startup") || key.includes("agent")) {
    return "The latest intelligence refresh could not start. The last successful view is still shown.";
  }
  return "The latest intelligence refresh did not complete. The last successful view is still shown.";
}
