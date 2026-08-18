import { Badge } from "@/components/ui/badge";
import { FamilySparkline } from "@/components/intelligence/family-history-chart";
import type { FamilyIntelligence } from "@/lib/types";
import { cn } from "@/lib/utils";

export function FamilyMatrix({
  rows,
  onSymbol,
}: {
  rows: FamilyIntelligence[];
  onSymbol: (symbol: string) => void;
}) {
  return (
    <div className="min-w-0 max-w-full overflow-x-auto">
      <div className="min-w-[760px]">
        <div className="grid grid-cols-[48px_minmax(160px,1fr)_120px_110px_130px_minmax(180px,1.35fr)] gap-3 border-b border-line px-3 py-2 font-mono text-[9px] uppercase tracking-[0.18em] text-faint">
          <span>Rank</span>
          <span>Family</span>
          <span>30d trajectory</span>
          <span>Attract.</span>
          <span>Evidence</span>
          <span>Names / reading</span>
        </div>
        {rows.map((row) => (
          <div
            key={`${row.venue}-${row.family}`}
            className="grid grid-cols-[48px_minmax(160px,1fr)_120px_110px_130px_minmax(180px,1.35fr)] items-start gap-3 border-b border-hairline px-3 py-3 last:border-0 hover:bg-panel/45"
          >
            <div>
              <p className="font-mono text-sm text-fg">{row.rank ?? "—"}</p>
              {row.rank_delta ? (
                <p className={cn("font-mono text-[10px]", row.rank_delta > 0 ? "text-gain" : "text-loss")}>
                  {row.rank_delta > 0 ? "↑" : "↓"} {Math.abs(row.rank_delta)}
                </p>
              ) : null}
            </div>
            <div>
              <div className="flex flex-wrap items-center gap-1.5">
                <p className="text-sm font-medium">{label(row.family)}</p>
                {row.priority !== "neutral" ? (
                  <Badge tone={row.priority === "favored" ? "gain" : "muted"}>{row.priority}</Badge>
                ) : null}
              </div>
              <p className="mt-1 font-mono text-[10px] text-faint">
                {row.candidate_count ?? 0} candidates · {biases(row.bias_counts)}
              </p>
            </div>
            <div>
              <FamilySparkline row={row} />
            </div>
            <div>
              <p className="font-mono text-sm tabular text-muted">
                {typeof row.attractiveness === "number" ? row.attractiveness.toFixed(3) : "—"}
              </p>
              {typeof row.attractiveness_delta === "number" && row.attractiveness_delta !== 0 ? (
                <p className={cn("font-mono text-[10px]", row.attractiveness_delta > 0 ? "text-gain" : "text-loss")}>
                  {row.attractiveness_delta > 0 ? "+" : ""}
                  {row.attractiveness_delta.toFixed(3)}
                </p>
              ) : null}
            </div>
            <div>
              <Badge
                tone={
                  row.situation_status && !["not_reported", "missing"].includes(row.situation_status)
                    ? "accent"
                    : "muted"
                }
              >
                {label(row.situation_status) || "not reported"}
              </Badge>
              <p className="mt-1 font-mono text-[10px] text-faint">
                {Object.entries(row.direction_counts)
                  .map(([key, value]) => `${key} ${value}`)
                  .join(" · ") || "no directional evidence"}
              </p>
            </div>
            <div>
              <div className="flex flex-wrap gap-1">
                {Array.from(new Set([...row.symbols, ...row.sticky_symbols])).slice(0, 8).map((symbol) => (
                  <button
                    key={symbol}
                    type="button"
                    onClick={() => onSymbol(symbol)}
                    className="rounded-sm bg-hairline px-1.5 py-0.5 font-mono text-[10px] text-muted hover:text-accent"
                  >
                    {symbol}
                  </button>
                ))}
              </div>
              <p className="mt-1.5 line-clamp-2 text-xs leading-relaxed text-dim">
                {row.summary || observation(row.observations) || "No family commentary."}
              </p>
            </div>
          </div>
        ))}
        {!rows.length ? <p className="px-3 py-8 text-sm text-faint">No family evidence for this venue.</p> : null}
      </div>
    </div>
  );
}

function label(value: string | null | undefined): string {
  return (value ?? "").replaceAll("_", " ");
}

function biases(value: Record<string, number>): string {
  return (
    Object.entries(value)
      .filter(([, count]) => count)
      .map(([key, count]) => `${key} ${count}`)
      .join(" · ") || "no bias"
  );
}

function observation(rows: Array<Record<string, unknown>>): string {
  for (const row of rows) {
    const point = row.point;
    if (typeof point === "string" && point.trim()) return point;
  }
  return "";
}
