import { AlertTriangle, ChevronDown } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { formatAgo, formatDayTime } from "@/lib/format";
import type { IntelligenceEvent } from "@/lib/types";
import { cn } from "@/lib/utils";

export function IntelligenceTimeline({
  events,
  empty = "No intelligence event in this window.",
}: {
  events: IntelligenceEvent[];
  empty?: string;
}) {
  if (!events.length) {
    return <p className="rounded-lg border border-dashed border-line px-4 py-8 text-sm text-faint">{empty}</p>;
  }

  return (
    <div className="relative">
      <div className="absolute bottom-4 left-[5px] top-3 w-px bg-line" />
      <div className="space-y-1">
        {events.map((event) => (
          <TimelineItem key={event.event_id} event={event} />
        ))}
      </div>
    </div>
  );
}

function TimelineItem({ event }: { event: IntelligenceEvent }) {
  const failure = event.status === "error" || event.kind.includes("failure");
  const changed = event.changes.length > 0;
  return (
    <details className="group relative pl-7">
      <span
        className={cn(
          "absolute left-0 top-[18px] z-10 size-[11px] rounded-full border-2 border-ink",
          failure ? "bg-loss" : changed ? "bg-accent" : "bg-faint",
        )}
      />
      <summary className="flex cursor-pointer list-none items-start gap-3 rounded-lg px-3 py-3 transition-colors hover:bg-panel/70 [&::-webkit-details-marker]:hidden">
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-center gap-2">
            <p className="text-sm font-medium text-fg">{event.title}</p>
            {event.venue ? <Badge>{event.venue}</Badge> : null}
            {event.symbol ? <Badge tone="accent">{event.symbol}</Badge> : null}
            {failure ? <Badge tone="loss">failure</Badge> : null}
            {changed ? <Badge tone="warn">{event.changes.length} changed</Badge> : null}
            {event.linkage === "unlinked" ? <Badge tone="warn">unlinked</Badge> : null}
          </div>
          {event.summary ? <p className="mt-1 line-clamp-2 text-sm leading-relaxed text-dim">{event.summary}</p> : null}
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
            Last successful projection retained.
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
                  <span className="font-mono text-faint">{change.field.replaceAll("_", " ")}</span>
                  <span className="text-muted">
                    {readable(change.from)} <span className="mx-1 text-accent">→</span> {readable(change.to)}
                  </span>
                </div>
              ))}
            </div>
          </div>
        ) : null}
        <div className="flex flex-wrap gap-1.5">
          <Badge>{event.layer}</Badge>
          <Badge>{event.kind.replaceAll("_", " ")}</Badge>
          {event.coverage ? <Badge tone="accent">{event.coverage}</Badge> : null}
          {event.linkage ? <Badge tone={event.linkage === "linked" ? "gain" : "warn"}>{event.linkage}</Badge> : null}
        </div>
        {Object.keys(event.refs).length ? (
          <dl className="mt-4 grid gap-1.5">
            {Object.entries(event.refs).map(([key, value]) => (
              <div key={key} className="grid grid-cols-[140px_minmax(0,1fr)] gap-3 text-[10px]">
                <dt className="font-mono text-faint">{key.replaceAll("_", " ")}</dt>
                <dd className="break-all font-mono text-dim">{readable(value)}</dd>
              </div>
            ))}
          </dl>
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
  if (typeof value === "string") return value.replaceAll("_", " ");
  if (typeof value === "object") return JSON.stringify(value);
  return String(value);
}
