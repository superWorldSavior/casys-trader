import { AlertTriangle, ChevronDown, Circle } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { formatAgo, formatDayTime } from "@/lib/format";
import {
  companyDisplayName,
  familyLabel,
  humanToken,
  marketRegimeLabel,
  plainMarketLanguage,
  venueLabel,
} from "@/lib/humanize";
import type { IntelligenceEvent } from "@/lib/types";
import { cn } from "@/lib/utils";

export function IntelligenceTimeline({
  events,
  companyMap = {},
  empty = "No intelligence event in this window.",
  openFirst = false,
}: {
  events: IntelligenceEvent[];
  companyMap?: Record<string, string>;
  empty?: string;
  openFirst?: boolean;
}) {
  if (!events.length) {
    return <p className="rounded-lg border border-dashed border-line px-4 py-8 text-sm text-faint">{empty}</p>;
  }

  return (
    <div className="relative">
      <div className="absolute bottom-4 left-[5px] top-3 w-px bg-line" />
      <div className="space-y-1">
        {events.map((event, index) => (
          <TimelineItem key={event.event_id} event={event} companyMap={companyMap} defaultOpen={openFirst && index === 0} />
        ))}
      </div>
    </div>
  );
}

function TimelineItem({
  event,
  companyMap,
  defaultOpen,
}: {
  event: IntelligenceEvent;
  companyMap: Record<string, string>;
  defaultOpen: boolean;
}) {
  const failure = event.status === "error" || event.kind.includes("failure");
  const changed = event.changes.length > 0;
  return (
    <details className="group relative pl-7" open={defaultOpen ? true : undefined}>
      <Circle
        aria-hidden="true"
        className={cn(
          "absolute left-0 top-[18px] z-10 size-[11px] fill-current stroke-ink stroke-[3px]",
          failure ? "text-loss" : changed ? "text-accent" : "text-faint",
        )}
      />
      <summary className="flex cursor-pointer list-none items-start gap-3 rounded-lg px-3 py-3 transition-colors hover:bg-panel/70 [&::-webkit-details-marker]:hidden">
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-center gap-2">
            <p className="text-sm font-medium text-fg">
              {event.symbol ? companyDisplayName(companyMap, event.symbol) : eventTitleLabel(event.title)}
            </p>
            {event.venue ? <Badge>{venueLabel(event.venue)}</Badge> : null}
            {event.symbol ? <Badge tone="accent">{event.symbol}</Badge> : null}
            <Badge>{eventKindLabel(event.kind)}</Badge>
            {failure ? <Badge tone="loss">Refresh failed</Badge> : null}
            {changed ? <Badge tone="warn">{event.changes.length} update{event.changes.length === 1 ? "" : "s"}</Badge> : null}
            {event.linkage === "unlinked" ? <Badge tone="warn">Evidence link missing</Badge> : null}
          </div>
          {event.summary ? <p className="mt-1 line-clamp-2 text-sm leading-relaxed text-dim group-open:line-clamp-none">{plainMarketLanguage(event.summary)}</p> : null}
          <p className="mt-1.5 font-mono text-[10px] text-faint">
            {formatDayTime(event.as_of)}
            <span className="mx-1.5">·</span>
            {formatAgo(event.as_of)}
            {event.source_count ? (
              <>
                <span className="mx-1.5">·</span>
                {event.source_count} source{event.source_count === 1 ? "" : "s"}
              </>
            ) : null}
          </p>
        </div>
        <ChevronDown className="mt-0.5 size-3.5 shrink-0 text-faint transition-transform group-open:rotate-180" />
      </summary>
      <div className="mb-3 ml-3 rounded-lg border border-hairline bg-panel/55 p-4">
        {failure ? (
          <div className="mb-3 flex items-center gap-2 text-xs text-loss">
            <AlertTriangle className="size-3.5" />
            Showing the last successful update.
          </div>
        ) : null}
        {event.changes.length ? (
          <div className="mb-4">
            <p className="mb-2 font-mono text-[9px] uppercase tracking-[0.2em] text-faint">Observed changes</p>
            <div className="space-y-1.5">
              {event.changes.map((change) => (
                <div
                  key={`${event.event_id}-${change.field}`}
                  className="grid grid-cols-[120px_1fr] gap-3 text-xs"
                >
                  <span className="font-mono text-faint">{changeFieldLabel(change.field)}</span>
                  <span className="text-muted">
                    {readable(change.from)} <span className="mx-1 text-accent">→</span> {readable(change.to)}
                  </span>
                </div>
              ))}
            </div>
          </div>
        ) : null}
        <div className="flex flex-wrap gap-1.5">
          <Badge>{scopeLabel(event.layer)}</Badge>
          {event.coverage ? <Badge tone="accent">{coverageLabel(event.coverage)}</Badge> : null}
          {event.linkage ? <Badge tone={event.linkage === "linked" ? "gain" : "warn"}>{linkageLabel(event.linkage)}</Badge> : null}
        </div>
        {Object.keys(event.refs).length ? (
          <details className="mt-4 rounded-md border border-hairline px-3 py-2">
            <summary className="cursor-pointer text-xs font-medium text-dim">Technical references</summary>
            <dl className="mt-3 grid gap-1.5">
              {Object.entries(event.refs).map(([key, value]) => (
                <div key={key} className="grid grid-cols-[140px_minmax(0,1fr)] gap-3 text-[10px]">
                  <dt className="font-mono text-faint">{humanToken(key)}</dt>
                  <dd className="break-all font-mono text-dim">{readable(value)}</dd>
                </div>
              ))}
            </dl>
          </details>
        ) : null}
        {Object.keys(event.payload).length ? (
          <details className="mt-4 rounded-md border border-hairline px-3 py-2">
            <summary className="cursor-pointer font-mono text-[9px] uppercase tracking-[0.18em] text-faint">
              Report detail
            </summary>
            <pre className="mt-3 max-h-80 overflow-auto whitespace-pre-wrap break-words font-mono text-[10px] leading-relaxed text-dim">
              {JSON.stringify(event.payload, null, 2)}
            </pre>
          </details>
        ) : null}
      </div>
    </details>
  );
}

function readable(value: unknown): string {
  if (value == null || value === "") return "—";
  if (typeof value === "string") {
    if (["risk_off", "risk_on"].includes(value.toLowerCase())) return marketRegimeLabel(value);
    if (/^(tw|eu|us)_/i.test(value)) return familyLabel(value);
    return humanToken(value);
  }
  if (Array.isArray(value)) return value.map((item) => readable(item)).join(", ");
  if (typeof value === "object") {
    return Object.entries(value as Record<string, unknown>)
      .map(([key, item]) => `${changeFieldLabel(key)}: ${readable(item)}`)
      .join(" · ");
  }
  return String(value);
}

function changeFieldLabel(value: string): string {
  const labels: Record<string, string> = {
    family_priority: "Theme priorities",
    gross_mode: "Risk appetite",
    net_bias: "Portfolio direction",
    venue_posture: "Regional outlook",
    status: "Coverage status",
  };
  if (labels[value]) return labels[value];
  if (["TW", "EU", "US"].includes(value.toUpperCase())) return venueLabel(value);
  return humanToken(value);
}

function eventKindLabel(value: string): string {
  const key = value.toLowerCase();
  if (key.includes("family") || key.includes("board")) return "Theme priorities";
  if (key.includes("company") || key.includes("micro")) return "Company review";
  if (key.includes("region")) return "Regional review";
  if (key.includes("macro")) return "Economic update";
  if (key.includes("global") || key.includes("world")) return "World briefing";
  if (key.includes("failure")) return "Refresh attempt";
  return humanToken(value);
}

function eventTitleLabel(value: string): string {
  const key = value.toLowerCase().replaceAll("_", " ");
  if (key.includes("family board")) return "Cross-region theme priorities";
  return plainMarketLanguage(humanToken(value));
}

function scopeLabel(value: string): string {
  const key = value.toLowerCase();
  if (key.includes("company") || key.includes("micro")) return "Company level";
  if (key.includes("region")) return "Regional level";
  if (key.includes("global") || key.includes("world") || key.includes("macro")) return "World level";
  return humanToken(value);
}

function coverageLabel(value: string): string {
  const key = value.toLowerCase();
  if (["complete", "full"].includes(key)) return "Complete sources";
  if (["partial", "incomplete"].includes(key)) return "Partial sources";
  if (["missing", "none"].includes(key)) return "Sources missing";
  return humanToken(value);
}

function linkageLabel(value: string): string {
  if (value.toLowerCase() === "linked") return "Evidence linked";
  if (value.toLowerCase() === "unlinked") return "Evidence link missing";
  return humanToken(value);
}
