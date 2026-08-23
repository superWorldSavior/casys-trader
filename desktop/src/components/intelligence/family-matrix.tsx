import { ArrowDown, ArrowUp } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { FamilySparkline } from "@/components/intelligence/family-history-chart";
import { companyDisplayName, familyLabel } from "@/lib/humanize";
import type { FamilyIntelligence } from "@/lib/types";
import { cn } from "@/lib/utils";

export function FamilyMatrix({
  rows,
  companyMap = {},
  onSymbol,
}: {
  rows: FamilyIntelligence[];
  companyMap?: Record<string, string>;
  onSymbol: (symbol: string) => void;
}) {
  return (
    <div className="min-w-0 max-w-full overflow-x-auto">
      <div className="min-w-[760px]">
        <div className="grid grid-cols-[48px_minmax(160px,1fr)_120px_110px_130px_minmax(180px,1.35fr)] gap-3 border-b border-line px-3 py-2 font-mono text-[9px] uppercase tracking-[0.18em] text-faint">
          <span>Rank</span>
          <span>Theme</span>
          <span>30-day movement</span>
          <span>Opportunity</span>
          <span>Evidence</span>
          <span>Companies</span>
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
                  {row.rank_delta > 0 ? <ArrowUp className="inline size-3" aria-label="Moved up" /> : <ArrowDown className="inline size-3" aria-label="Moved down" />} {Math.abs(row.rank_delta)}
                </p>
              ) : null}
            </div>
            <div>
              <div className="flex flex-wrap items-center gap-1.5">
                <p className="text-sm font-medium">{familyLabel(row.family)}</p>
                {row.priority !== "neutral" ? (
                  <Badge tone={row.priority === "favored" ? "gain" : "muted"}>{priorityLabel(row.priority)}</Badge>
                ) : null}
              </div>
              <p className="mt-1 font-mono text-[10px] text-faint">
                {row.candidate_count ?? 0} companies considered · {biases(row.bias_counts)}
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
                {evidenceStatusLabel(row.situation_status)}
              </Badge>
              <p className="mt-1 font-mono text-[10px] text-faint">
                {directionCounts(row.direction_counts)}
              </p>
            </div>
            <div>
              <div className="flex flex-wrap gap-1">
                {Array.from(new Set([...row.symbols, ...row.sticky_symbols])).slice(0, 8).map((symbol) => (
                  <button
                    key={symbol}
                    type="button"
                    onClick={() => onSymbol(symbol)}
                    className="max-w-44 truncate rounded-sm bg-hairline px-1.5 py-0.5 text-[10px] text-muted hover:text-accent focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent/50"
                    title={`${companyDisplayName(companyMap, symbol)} (${symbol})`}
                  >
                    {companyDisplayName(companyMap, symbol)} <span className="font-mono text-[9px] text-faint">{symbol}</span>
                  </button>
                ))}
              </div>
              <p className="mt-1.5 line-clamp-2 text-xs leading-relaxed text-dim">
                {row.summary || observation(row.observations) || "No explanation is recorded for this theme."}
              </p>
            </div>
          </div>
        ))}
        {!rows.length ? <p className="px-3 py-8 text-sm text-faint">No theme evidence is recorded for this region.</p> : null}
      </div>
    </div>
  );
}

function biases(value: Record<string, number>): string {
  return (
    Object.entries(value)
      .filter(([, count]) => count)
      .map(([key, count]) => `${biasLabel(key)} ${count}`)
      .join(" · ") || "No directional preference"
  );
}

function priorityLabel(value: string): string {
  if (value === "favored") return "Preferred";
  if (value === "deprioritized") return "Lower priority";
  return "Balanced";
}

function biasLabel(value: string): string {
  const key = value.toLowerCase();
  if (["long", "bullish", "up", "positive"].includes(key)) return "Positive";
  if (["short", "bearish", "down", "negative"].includes(key)) return "Cautious";
  if (key === "neutral") return "Balanced";
  return value.replaceAll("_", " ");
}

function evidenceStatusLabel(value?: string | null): string {
  const key = String(value ?? "").toLowerCase();
  if (!key || ["not_reported", "missing"].includes(key)) return "No supporting brief";
  if (["complete", "full", "current", "active"].includes(key)) return "Evidence available";
  if (["partial", "incomplete"].includes(key)) return "Partial evidence";
  return value?.replaceAll("_", " ") || "No supporting brief";
}

function directionCounts(value: Record<string, number>): string {
  return Object.entries(value)
    .filter(([, count]) => count)
    .map(([key, count]) => `${biasLabel(key)} ${count}`)
    .join(" · ") || "No directional evidence";
}

function observation(rows: Array<Record<string, unknown>>): string {
  for (const row of rows) {
    const point = row.point;
    if (typeof point === "string" && point.trim()) return point;
  }
  return "";
}
