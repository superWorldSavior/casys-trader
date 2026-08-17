import { useMemo, useState } from "react";
import { ActionChip } from "@/components/action-chip";
import { ActivityChart } from "@/components/charts/activity-chart";
import { Badge } from "@/components/ui/badge";
import { Card, CardBody, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { KIND_LABEL, decisionKind, mergeDecisions } from "@/lib/classify";
import { formatDayTime } from "@/lib/format";
import type { DecisionKind, Snapshot } from "@/lib/types";
import { cn } from "@/lib/utils";

const FILTERS: Array<DecisionKind | "all"> = ["all", "exec", "quiet", "stale", "risk", "hold", "watch", "plan"];

type Props = {
  snapshot: Snapshot;
  onSymbol: (symbol: string) => void;
};

export function DecisionsPage({ snapshot, onSymbol }: Props) {
  const [query, setQuery] = useState("");
  const [filter, setFilter] = useState<DecisionKind | "all">("all");
  const rows = useMemo(() => mergeDecisions(snapshot.decisions, snapshot.recent_decisions), [snapshot]);
  const visible = rows.filter((row) => {
    const kind = decisionKind(row);
    if (filter !== "all" && kind !== filter) return false;
    if (!query.trim()) return true;
    const hay = `${row.symbol} ${row.action} ${row.rationale} ${row.reason} ${row.llm_model}`.toLowerCase();
    return hay.includes(query.trim().toLowerCase());
  });

  return (
    <div className="grid gap-4">
      <Card className="h-[220px]">
        <CardHeader>
          <CardTitle>Activity</CardTitle>
          <span className="font-mono text-[10px] text-faint">{rows.length} in ledger tail</span>
        </CardHeader>
        <CardBody className="h-[170px] pt-1">
          <ActivityChart report={snapshot.decisions} recent={snapshot.recent_decisions} />
        </CardBody>
      </Card>

      <div className="flex flex-wrap items-center gap-2">
        <Input
          value={query}
          onChange={(event) => setQuery(event.target.value)}
          placeholder="Filter symbol, rationale, model…"
          className="max-w-sm"
        />
        {FILTERS.map((item) => (
          <button
            key={item}
            type="button"
            onClick={() => setFilter(item)}
            className={cn(
              "rounded-sm px-2 py-1 font-mono text-[10px] uppercase tracking-[0.14em]",
              filter === item ? "bg-accent/15 text-accent" : "text-faint hover:text-muted",
            )}
          >
            {item}
          </button>
        ))}
      </div>

      <Card>
        <div className="grid grid-cols-[88px_110px_72px_88px_1fr_90px] gap-3 border-b border-hairline px-4 py-2 font-mono text-[10px] uppercase tracking-[0.14em] text-faint">
          <span>When</span>
          <span>Symbol</span>
          <span>Side</span>
          <span>Kind</span>
          <span>Rationale</span>
          <span>Model</span>
        </div>
        <div>
          {visible.map((row) => {
            const kind = decisionKind(row);
            return (
              <button
                key={row.decision_id ?? `${row.symbol}-${row.cycle_ts}-${row.sequence}`}
                type="button"
                onClick={() => row.symbol && onSymbol(row.symbol)}
                className="grid w-full grid-cols-[88px_110px_72px_88px_1fr_90px] items-start gap-3 border-b border-hairline px-4 py-2.5 text-left last:border-0 hover:bg-panel-hover"
              >
                <span className="font-mono text-[11px] text-faint">{formatDayTime(row.cycle_ts || row.ts)}</span>
                <span className="text-sm font-medium">{row.symbol}</span>
                <ActionChip action={row.action} />
                <Badge tone={kind === "exec" ? "gain" : kind === "risk" ? "loss" : kind === "stale" ? "warn" : "muted"}>
                  {KIND_LABEL[kind]}
                </Badge>
                <p className="line-clamp-2 text-sm text-muted">{row.rationale || row.reason || "—"}</p>
                <span className="truncate font-mono text-[10px] text-faint">
                  {row.model_called === false ? "infra" : row.llm_model || "llm"}
                </span>
              </button>
            );
          })}
          {visible.length === 0 ? <p className="px-4 py-8 text-sm text-faint">Nothing matches.</p> : null}
        </div>
      </Card>
    </div>
  );
}
