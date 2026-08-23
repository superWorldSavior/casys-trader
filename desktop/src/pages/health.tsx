import type { ReactNode } from "react";
import type { PageKey } from "@/components/layout/app-shell";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardBody, CardHeader, CardTitle } from "@/components/ui/card";
import { useHealth } from "@/hooks/use-desk-api";
import { formatPct } from "@/lib/format";
import { companyDisplayName, decisionActionLabel, humanToken, venueLabel } from "@/lib/humanize";
import type { HealthPayload, Snapshot } from "@/lib/types";
import { cn } from "@/lib/utils";

export function HealthPage({
  snapshot,
  onPage,
}: {
  snapshot?: Snapshot;
  onPage: (page: PageKey) => void;
}) {
  const query = useHealth();
  const data = query.data;
  const verdict = trustVerdict(snapshot, data, Boolean(query.error));
  const openVenues = snapshot?.open_venues_list;

  return (
    <div className="grid gap-4">
      <section className="flex flex-wrap items-start justify-between gap-3">
        <div className="min-w-0 max-w-3xl">
          <div className="flex flex-wrap items-center gap-2">
            <Badge tone={verdict.tone}>{verdict.badge}</Badge>
            <p className={cn("text-lg font-semibold tracking-[-0.025em]", verdictTitleClass(verdict.tone))}>
              {verdict.title}
            </p>
          </div>
          <p className="mt-2 text-sm leading-6 text-muted">{verdict.detail}</p>
        </div>
        <div className="flex flex-wrap justify-end gap-1.5">
          <Button variant="ghost" size="sm" onClick={() => onPage("settings")}>
            Settings
          </Button>
          <Button variant="ghost" size="sm" onClick={() => onPage("reports")}>
            Technical reports
          </Button>
          <Button variant="ghost" size="sm" onClick={() => onPage("logs")}>
            System events
          </Button>
        </div>
      </section>
      {!data && !query.error ? (
        <p className="text-sm text-faint">Loading health…</p>
      ) : null}
      {data ? (
        <div className="grid grid-cols-1 gap-4 min-[1000px]:grid-cols-3">
          <div className="grid gap-4">
            <Card>
              <CardHeader>
                <CardTitle>Data freshness</CardTitle>
                {data.kill_active ? <Badge tone="loss">Emergency stop active</Badge> : null}
              </CardHeader>
              <CardBody className="space-y-3">
                {data.freshness.venues.map((venue) => {
                  const freshness = freshnessBadge(venue, openVenues);
                  return (
                    <div key={venue.venue}>
                      <div className="flex items-center justify-between">
                        <p className="text-sm font-medium">{regionName(venue.venue) || regionName(venue.display_name)}</p>
                        <Badge tone={freshness.tone}>
                          {freshness.label} · {venue.symbol_count}
                        </Badge>
                      </div>
                      {venue.stale_symbols.slice(0, 6).map((row) => (
                        <p key={row.symbol} className="text-xs text-dim">
                          {companyDisplayName(snapshot?.company_map, row.symbol)}{" "}
                          <span className="font-mono text-[9px] text-faint">{row.symbol}</span>
                          {row.age_minutes != null ? ` · ${humanAge(row.age_minutes)}` : ""}
                        </p>
                      ))}
                    </div>
                  );
                })}
                {data.freshness.venues.length === 0 ? (
                  <p className="text-sm text-faint">No freshness check is reported.</p>
                ) : null}
              </CardBody>
            </Card>
            <Card>
              <CardHeader>
                <CardTitle>Currency rates</CardTitle>
                <Badge tone={data.fx.source_available ? "gain" : "warn"}>
                  {data.fx.source_available ? "Available" : "Unavailable"}
                </Badge>
              </CardHeader>
              <CardBody className="space-y-1.5">
                {data.fx.rows.map((row) => (
                  <div key={row.currency} className="flex justify-between text-sm">
                    <span className="text-dim">{row.currency}</span>
                    <span>{row.rate.toFixed(4)}</span>
                  </div>
                ))}
                {data.fx.rows.length === 0 ? (
                  <p className="text-sm text-faint">No currency rate is reported.</p>
                ) : null}
              </CardBody>
            </Card>
          </div>

          <div className="grid gap-4">
            <Card>
              <CardHeader>
                <CardTitle>Sources</CardTitle>
              </CardHeader>
              <CardBody className="space-y-2">
                {data.sources.rows.map((row) => (
                  <p key={row.name} className="text-sm font-medium">{sourceName(row.name)}</p>
                ))}
                {data.sources.rows.length === 0 ? (
                  <p className="text-sm text-faint">No source status is reported.</p>
                ) : null}
                {data.sources.rows.length ? (
                  <HiddenDetails label="Source details">
                    {data.sources.rows.map((row) => (
                      <p key={row.name} className="text-xs text-faint">{row.name} · {row.detail}</p>
                    ))}
                  </HiddenDetails>
                ) : null}
              </CardBody>
            </Card>
            <Card>
              <CardHeader>
                <CardTitle>Safety limits</CardTitle>
                <Badge tone={data.risk.reject_count ? "loss" : "gain"}>
                  {data.risk.reject_count ? `${data.risk.reject_count} blocked` : "No blocks"}
                </Badge>
              </CardHeader>
              <CardBody className="space-y-2">
                {data.risk.recent_rejects.map((row) => (
                  <p key={`${row.symbol}-${row.cycle_ts}`} className="text-sm text-muted">
                    {companyDisplayName(snapshot?.company_map, row.symbol)}{" "}
                    <span className="font-mono text-[9px] text-faint">{row.symbol}</span>
                    {" · "}{decisionActionLabel(row.action)} · {humanToken(row.reason)}
                  </p>
                ))}
                {Object.keys(data.risk.caps).length ? (
                  <HiddenDetails label="Configured limits">
                    {Object.entries(data.risk.caps).slice(0, 10).map(([key, value]) => (
                      <div key={key} className="flex justify-between gap-3 font-mono text-[11px]">
                        <span className="text-faint">{key.replaceAll("_", " ")}</span>
                        <span className="text-muted">{String(value)}</span>
                      </div>
                    ))}
                  </HiddenDetails>
                ) : null}
                {data.risk.recent_rejects.length === 0 ? (
                  <p className="text-sm text-faint">No safety-limit block is recorded.</p>
                ) : null}
              </CardBody>
            </Card>
          </div>

          <div className="grid gap-4">
            <Card>
              <CardHeader>
                <CardTitle>Current cycle</CardTitle>
              </CardHeader>
              <CardBody className="space-y-2">
                <SystemFact
                  label="AI reviews"
                  value={typeof data.llm.calls_this_cycle === "number" ? String(data.llm.calls_this_cycle) : "Not recorded"}
                />
                <p className="text-xs leading-5 text-dim">
                  Activity reported for the latest recorded cycle.
                </p>
              </CardBody>
            </Card>
            <Card>
              <CardHeader>
                <CardTitle>Company coverage</CardTitle>
              </CardHeader>
              <CardBody className="space-y-1 text-sm text-muted">
                <p>{data.universe.total_symbols} in the current trading set</p>
                <p>{data.universe.hot_total} in regional research lists</p>
                {data.universe.venue_counts.map(([venue, count]) => (
                  <p key={venue} className="text-xs text-faint">
                    {regionName(venue)} · {count}
                  </p>
                ))}
              </CardBody>
            </Card>
          </div>

          <Card className="min-[1000px]:col-span-3">
            <details className="group">
              <summary className="flex cursor-pointer list-none items-center justify-between gap-4 px-4 py-3 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-accent/50 [&::-webkit-details-marker]:hidden">
                <div>
                  <p className="text-sm font-medium text-fg">Recorded system details</p>
                  <p className="mt-0.5 text-xs text-faint">Models, cumulative activity, learning and stored memory</p>
                </div>
                <span className="shrink-0 text-xs font-medium text-dim group-open:hidden">Show</span>
                <span className="hidden shrink-0 text-xs font-medium text-dim group-open:inline">Hide</span>
              </summary>
              <div className="grid gap-6 border-t border-hairline px-4 py-4 min-[760px]:grid-cols-2 min-[1180px]:grid-cols-4">
                <section className="space-y-2">
                  <p className="font-mono text-[10px] uppercase tracking-[0.18em] text-faint">AI models</p>
                  <p className="text-sm text-muted">{modelOverview(data.model)}</p>
                  {hasModelDetails(data.model) ? (
                    <div className="space-y-2">
                      {data.model.model_performance.map((row, index) => (
                        <p key={index} className="font-mono text-[11px] text-muted">
                          {String(row.provider ?? "")} · {String(row.model ?? "")} · confidence{" "}
                          {typeof row.avg_confidence === "number" ? formatPct(row.avg_confidence * 100) : "—"}
                        </p>
                      ))}
                      {Object.entries(data.model.providers ?? {}).map(([key, count]) => (
                        <p key={key} className="font-mono text-[11px] text-muted">
                          {key} · {count}
                        </p>
                      ))}
                    </div>
                  ) : null}
                </section>

                <section className="space-y-2">
                  <p className="font-mono text-[10px] uppercase tracking-[0.18em] text-faint">Cumulative activity</p>
                  <SystemFact label="Recorded fills" value={String(data.llm.total_fills)} />
                  <SystemFact label="Safe fallbacks" value={String(data.llm.total_fallbacks)} />
                  {data.llm.total_fallbacks ? (
                    <p className="text-xs leading-5 text-dim">If an AI review fails, Casys makes no portfolio change.</p>
                  ) : null}
                </section>

                <section className="space-y-2 text-sm text-muted">
                  <p className="font-mono text-[10px] uppercase tracking-[0.18em] text-faint">Learning review</p>
                  <p>{pendingLearningLabel(data.learnings.pending_label)}</p>
                  <p>{consolidationLabel(data.learnings.consolidation_label)}</p>
                  {data.learnings.last_run_label ? <p className="text-xs text-faint">Last update · {data.learnings.last_run_label}</p> : null}
                  {data.learnings.notes.length ? (
                    <HiddenDetails label="Pending notes">
                      {data.learnings.notes.slice(0, 6).map((note) => (
                        <p key={`${note.symbol}-${note.note}`} className="text-xs">
                          <span className="font-medium">{companyDisplayName(snapshot?.company_map, note.symbol)}</span>{" "}
                          <span className="font-mono text-[9px] text-faint">{note.symbol}</span>{" "}
                          {note.note}
                        </p>
                      ))}
                    </HiddenDetails>
                  ) : null}
                </section>

                <section className="space-y-2 text-sm text-muted">
                  <div className="flex flex-wrap items-center justify-between gap-2">
                    <p className="font-mono text-[10px] uppercase tracking-[0.18em] text-faint">Stored memory</p>
                    <Badge tone={data.memory.available ? "muted" : "warn"}>
                      {data.memory.available ? "Store present" : "Store unavailable"}
                    </Badge>
                  </div>
                  <p>{data.memory.available ? "A stored-memory record is present." : "No stored-memory record is available."}</p>
                  <p>{memoryCountLabel(data.memory.notes_label, data.memory.missing_label)}</p>
                  <p className={data.memory.sync_is_error ? "text-loss" : "text-faint"}>{memorySyncLabel(data.memory.sync_label)}</p>
                  <HiddenDetails label="Memory internals">
                    <p>{data.memory.lift_label}</p>
                    <p>{data.memory.useful_label}</p>
                    <p>{data.memory.rules_label}</p>
                    <p className="font-mono text-[11px] text-faint">{data.memory.situation_label}</p>
                  </HiddenDetails>
                </section>
              </div>
            </details>
          </Card>
        </div>
      ) : null}
    </div>
  );
}

function HiddenDetails({ label, children }: { label: string; children: ReactNode }) {
  return (
    <details className="mt-1">
      <summary className="cursor-pointer text-xs font-medium text-dim hover:text-fg focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent/50">
        {label}
      </summary>
      <div className="mt-2 space-y-2">{children}</div>
    </details>
  );
}

function SystemFact({ label, value }: { label: string; value: string }) {
  return (
    <div className="flex items-baseline justify-between gap-4">
      <p className="text-xs text-dim">{label}</p>
      <p className="text-sm font-medium text-muted">{value}</p>
    </div>
  );
}

function regionName(value?: string | null): string {
  const key = String(value ?? "").trim().toUpperCase();
  if (key === "TPE" || key === "TW") return "Taiwan";
  if (key === "EU") return "Europe";
  if (key === "US") return "United States";
  return venueLabel(value);
}

function humanAge(minutes: number): string {
  const total = Math.max(0, Math.round(minutes));
  if (total < 1) return "just now";
  if (total === 1) return "1 minute ago";
  if (total < 60) return `${total} minutes ago`;
  const hours = Math.round(total / 60);
  if (hours === 1) return "1 hour ago";
  if (hours < 24) return `${hours} hours ago`;
  const days = Math.round(total / (60 * 24));
  return days === 1 ? "1 day ago" : `${days} days ago`;
}

function sourceName(value: string): string {
  const key = value.toLowerCase();
  if (key.includes("ib")) return "Market and broker data";
  if (key.includes("finance")) return "Price history";
  if (key.includes("news")) return "News";
  return value;
}

function pendingLearningLabel(value: string): string {
  const count = Number.parseInt(value, 10);
  if (!Number.isFinite(count)) return value;
  if (count === 0) return "No observation is waiting to be consolidated.";
  return `${count.toLocaleString()} observation${count === 1 ? " is" : "s are"} waiting to be consolidated.`;
}

function consolidationLabel(value: string): string {
  if (value.toLowerCase().includes("idle")) return "No learning update is running.";
  return value;
}

function memoryCountLabel(notes?: string, missing?: string): string {
  const count = Number.parseInt(String(notes ?? ""), 10);
  if (Number.isFinite(count)) return `${count.toLocaleString()} recorded memories`;
  return notes || missing || "Memory count unavailable";
}

function memorySyncLabel(value?: string): string {
  const match = String(value ?? "").match(/^(\d+)([mhd]) ago\s*·\s*ok$/i);
  if (match) {
    const count = Number(match[1]);
    const unit = match[2].toLowerCase();
    if (unit === "m") return `Synced ${humanAge(count)}`;
    if (unit === "h") return `Synced ${count} hour${count === 1 ? "" : "s"} ago`;
    return `Synced ${count} day${count === 1 ? "" : "s"} ago`;
  }
  return value || "Sync time unavailable";
}

function hasModelDetails(model: HealthPayload["model"]): boolean {
  return model.model_performance.length > 0 || Object.keys(model.providers ?? {}).length > 0;
}

function modelOverview(model: HealthPayload["model"]): string {
  const recorded = model.model_performance.length;
  if (recorded > 0) {
    return recorded === 1 ? "1 model has recorded stats." : `${recorded} models have recorded stats.`;
  }
  const named = Object.keys(model.providers ?? {}).length;
  if (named > 0) {
    return named === 1 ? "1 recorded model is available." : `${named} recorded models are available.`;
  }
  return "No model stats yet.";
}

type VerdictTone = "muted" | "gain" | "warn" | "loss";

type TrustVerdict = {
  badge: string;
  title: string;
  detail: string;
  tone: VerdictTone;
};

function trustVerdict(snapshot?: Snapshot, health?: HealthPayload, statusLoadFailed = false): TrustVerdict {
  if (snapshot?.kill_active || health?.kill_active) {
    return {
      badge: "Stop",
      title: "Kill switch is on",
      detail: "Emergency stop is active. The paper portfolio will not change until it is turned off.",
      tone: "loss",
    };
  }
  if (snapshot?.daemon.alive === false) {
    return {
      badge: "Process",
      title: "Casys process not detected",
      detail: "The paper portfolio will not change until the process is running again.",
      tone: "warn",
    };
  }
  if (statusLoadFailed) {
    return {
      badge: "Status",
      title: "Current checks could not be refreshed",
      detail: health
        ? "The last recorded checks are shown below, but they do not prove the current system state."
        : "System checks could not be loaded. The portfolio snapshot alone does not prove the current system state.",
      tone: "warn",
    };
  }
  if (!snapshot || !health) {
    return {
      badge: "Checking",
      title: "Current status is incomplete",
      detail: !snapshot
        ? "Waiting for the latest portfolio snapshot before reporting current status."
        : "Waiting for the latest system checks before reporting current status.",
      tone: "muted",
    };
  }
  if (snapshot.daemon.alive !== true) {
    return {
      badge: "Process",
      title: "Process status is not reported",
      detail: "The current snapshot does not confirm that the Casys process is running.",
      tone: "warn",
    };
  }
  const openVenues = snapshot?.open_venues_list ?? [];
  const anyOpen = openVenues.some(Boolean);
  const stale = hasStaleQuotes(snapshot, health);
  if (anyOpen && stale) {
    return {
      badge: "Quotes",
      title: "Quotes need a refresh",
      detail: "Casys will not trade an affected company until its quote is fresh.",
      tone: "warn",
    };
  }
  if (health.fx.source_available === false) {
    return {
      badge: "FX",
      title: "Currency rates unavailable",
      detail: "FX rates are missing, so cross-market values may be incomplete.",
      tone: "warn",
    };
  }
  if (health.memory.sync_is_error) {
    return {
      badge: "Memory",
      title: "Stored memory sync needs attention",
      detail: health.memory.available
        ? "The memory store is present, but its latest synchronization is reported as failed."
        : "The latest stored-memory synchronization is reported as failed, and no memory store is currently reported as available.",
      tone: "warn",
    };
  }
  if (!health.memory.available) {
    return {
      badge: "Memory",
      title: "Stored memory is unavailable",
      detail: "The current system checks do not report a stored-memory record as available.",
      tone: "warn",
    };
  }
  if (health.freshness.venues.length === 0 || health.fx.rows.length === 0 || health.sources.rows.length === 0) {
    return {
      badge: "Checks",
      title: "Some current checks are not reported",
      detail: health.freshness.venues.length === 0
        ? "No market-data freshness check is included in the current system response."
        : health.fx.rows.length === 0
        ? "No currency rate is included in the current system response."
        : "No source status is included in the current system response.",
      tone: "warn",
    };
  }
  if (!anyOpen && stale) {
    return {
      badge: "Markets closed",
      title: "Quotes reflect the last session",
      detail: "Markets are closed and the quotes are from the last session. That is expected until a market reopens.",
      tone: "muted",
    };
  }
  return {
    badge: "Current checks",
    title: "No reported issue",
    detail: "The available process, freshness, currency and memory-sync checks do not report a blocking issue.",
    tone: "gain",
  };
}

function hasStaleQuotes(snapshot?: Snapshot, health?: HealthPayload): boolean {
  if (health?.freshness.venues.some((venue) => venue.is_stale)) return true;
  if (health) return false;
  return Object.keys(snapshot?.stale_market_data ?? {}).length > 0;
}

function freshnessBadge(
  venue: HealthPayload["freshness"]["venues"][number],
  openVenues?: string[],
): { label: string; tone: VerdictTone } {
  if (!venue.is_stale) return { label: "Current", tone: "gain" };
  if (openVenues == null) return { label: "Needs refresh", tone: "warn" };
  if (venueIsOpen(venue.venue, openVenues) || venueIsOpen(venue.display_name, openVenues)) {
    return { label: "Needs refresh", tone: "warn" };
  }
  return { label: "Last market close", tone: "muted" };
}

function venueIsOpen(venue: string, openVenues: string[]): boolean {
  const open = new Set(openVenues.map((item) => item.trim().toUpperCase()).filter(Boolean));
  const key = venue.trim().toUpperCase();
  if (open.has(key)) return true;
  if (key === "TPE") return open.has("TW");
  if (key === "TW") return open.has("TPE");
  return false;
}

function verdictTitleClass(tone: VerdictTone): string {
  if (tone === "loss") return "text-loss";
  if (tone === "warn") return "text-warn";
  if (tone === "gain") return "text-fg";
  return "text-fg";
}
