import type { IntelligenceActivity } from "@/lib/types";
import { cn } from "@/lib/utils";

const LANES = [
  { key: "context", label: "News", wideLabel: "Markets & news" },
  { key: "companies", label: "Companies" },
  { key: "decisions", label: "Decisions" },
] as const;

function sentenceFor(activity: IntelligenceActivity): string {
  if (activity.decisions.active) return "Casys is deciding what to do next";
  if (activity.companies.active) return "Casys is reviewing the companies that matter";
  return "Casys is reading what changed";
}

function accessibleSentenceFor(activity: IntelligenceActivity): string {
  const work = [
    activity.context.active ? "reading what changed" : null,
    activity.companies.active ? "reviewing the companies that matter" : null,
    activity.decisions.active ? "deciding what to do next" : null,
  ].filter((item): item is string => Boolean(item));
  if (work.length === 1) return `Casys is ${work[0]}.`;
  if (work.length === 2) return `Casys is ${work[0]} and ${work[1]}.`;
  return `Casys is ${work.slice(0, -1).join(", ")}, and ${work.at(-1)}.`;
}

export function LiveWorkStatus({ activity }: { activity?: IntelligenceActivity }) {
  if (!activity) return null;
  const lanes = LANES.map((lane) => ({
    ...lane,
    active: Boolean(activity[lane.key]?.active),
  }));
  if (!lanes.some((lane) => lane.active)) return null;

  return (
    <div
      role="status"
      aria-live="polite"
      aria-atomic="true"
      className="live-work-surface relative overflow-hidden rounded-lg border border-accent/15 px-4 py-3"
    >
      <div className="relative grid gap-3 min-[900px]:grid-cols-[minmax(15rem,0.72fr)_minmax(26rem,1.28fr)] min-[900px]:items-center">
        <div className="flex min-w-0 items-center gap-3">
          <span className="h-7 w-px shrink-0 bg-accent/35" aria-hidden="true" />
          <p className="text-sm font-medium tracking-tight text-fg" aria-hidden="true">{sentenceFor(activity)}</p>
          <p className="sr-only">{accessibleSentenceFor(activity)}</p>
        </div>
        <div aria-hidden="true">
          <ol className="grid grid-cols-3 gap-3">
            {lanes.map((lane, index) => (
              <li
                key={lane.key}
                className="flex min-w-0 items-center gap-2"
              >
                <span
                  className={cn(
                    "size-2 shrink-0 rounded-full border",
                    lane.active ? "live-work-halo border-accent bg-accent" : "border-line bg-meter",
                  )}
                  style={lane.active ? { animationDelay: `${index * 160}ms` } : undefined}
                />
                <span
                  className={cn(
                    "font-mono text-[9px] uppercase tracking-[0.16em]",
                    lane.active ? "text-accent" : "text-dim",
                  )}
                >
                  {"wideLabel" in lane ? (
                    <>
                      <span className="min-[480px]:hidden">{lane.label}</span>
                      <span className="hidden min-[480px]:inline">{lane.wideLabel}</span>
                    </>
                  ) : lane.label}
                </span>
                <span
                  className={cn(
                    "relative h-px min-w-3 flex-1 overflow-hidden bg-line",
                    lane.active && "live-work-lane-sheen",
                  )}
                />
              </li>
            ))}
          </ol>
        </div>
      </div>
    </div>
  );
}
