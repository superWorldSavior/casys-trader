import { useState } from "react";
import { Badge } from "@/components/ui/badge";
import type { FamilyComparison, FamilyComparisonCell } from "@/lib/types";
import { cn } from "@/lib/utils";

const VENUES = ["TW", "EU", "US"] as const;

export function RegionalFamilyComparison({ rows }: { rows: FamilyComparison[] }) {
  const [selected, setSelected] = useState<string | null>(null);
  const active = rows.find((row) => row.group === selected) ?? rows[0];

  return (
    <div>
      <div className="overflow-x-auto">
        <div className="min-w-[720px]">
          <div className="grid grid-cols-[minmax(190px,1fr)_repeat(3,minmax(150px,0.8fr))] border-b border-line bg-ink/55 px-3 py-2 font-mono text-[9px] uppercase tracking-[0.16em] text-faint">
            <span>Macro family</span>
            {VENUES.map((venue) => (
              <span key={venue}>{venue}</span>
            ))}
          </div>
          {rows.map((row) => (
            <button
              key={row.group}
              type="button"
              onClick={() => setSelected(row.group)}
              aria-pressed={active?.group === row.group}
              className={cn(
                "grid w-full grid-cols-[minmax(190px,1fr)_repeat(3,minmax(150px,0.8fr))] border-b border-hairline px-3 py-2.5 text-left last:border-0",
                active?.group === row.group ? "bg-accent/5" : "hover:bg-ink/55",
              )}
            >
              <div className="pr-4">
                <p className="text-sm font-medium text-fg">{row.label}</p>
              </div>
              {VENUES.map((venue) => (
                <ComparisonCell key={venue} cell={row.venues[venue]} />
              ))}
            </button>
          ))}
        </div>
      </div>

      {active ? (
        <div className="border-t border-line bg-ink/35 p-4">
          <div className="mb-3 flex flex-wrap items-center justify-between gap-2">
            <div>
              <p className="font-mono text-[9px] uppercase tracking-[0.18em] text-accent">Why it differs</p>
              <p className="mt-1 text-sm font-medium">{active.label} across regions</p>
            </div>
          </div>
          <div className="grid gap-3 md:grid-cols-3">
            {VENUES.map((venue) => (
              <VenueExplanation key={venue} venue={venue} cell={active.venues[venue]} />
            ))}
          </div>
        </div>
      ) : null}
    </div>
  );
}

function ComparisonCell({ cell }: { cell: FamilyComparisonCell }) {
  if (cell.status === "not_in_taxonomy") {
    return (
      <div className="mr-2 rounded-md border border-dashed border-line px-2.5 py-2">
        <p className="font-mono text-[10px] text-faint">not mapped</p>
      </div>
    );
  }
  return (
    <div
      className={cn(
        "mr-2 rounded-md border px-2.5 py-2",
        cell.status === "active"
          ? "border-accent/25 bg-accent/7"
          : cell.status === "observed"
            ? "border-line bg-panel"
            : "border-dashed border-line bg-ink/40",
      )}
    >
      <div className="flex items-baseline justify-between gap-2">
        <span className="font-mono text-sm font-medium text-fg">
          {cell.best_rank != null ? `#${cell.best_rank}` : "—"}
        </span>
        <span className="font-mono text-[9px] text-faint">{cell.candidate_count} cand.</span>
      </div>
      <p className="mt-0.5 truncate text-[11px] text-dim">
        {cell.leader?.replaceAll("_", " ") || "not observed"}
      </p>
    </div>
  );
}

function VenueExplanation({
  venue,
  cell,
}: {
  venue: string;
  cell: FamilyComparisonCell;
}) {
  return (
    <section className="rounded-md border border-line bg-panel p-3">
      <div className="flex items-center justify-between gap-2">
        <p className="font-mono text-[10px] font-medium text-fg">{venue}</p>
        <Badge tone={cell.status === "active" ? "gain" : cell.status === "observed" ? "accent" : "muted"}>
          {cell.status.replaceAll("_", " ")}
        </Badge>
      </div>
      <p className="mt-2 min-h-10 text-xs leading-relaxed text-dim">{cell.reason}</p>
      <div className="mt-3 space-y-1.5">
        {cell.families.map((family) => (
          <div key={family.family} className="flex items-center justify-between gap-3 border-t border-hairline pt-1.5">
            <span className="truncate text-[11px] text-muted">{family.family.replaceAll("_", " ")}</span>
            <span className="shrink-0 font-mono text-[9px] text-faint">
              {family.rank != null ? `#${family.rank}` : "—"}
              {typeof family.attractiveness === "number" ? ` · ${family.attractiveness.toFixed(2)}` : ""}
            </span>
          </div>
        ))}
        {!cell.families.length ? <p className="font-mono text-[9px] text-faint">No mapped family.</p> : null}
      </div>
    </section>
  );
}
