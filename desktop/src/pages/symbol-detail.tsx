import { ArrowLeft } from "lucide-react";
import { ActionChip } from "@/components/action-chip";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardBody, CardHeader, CardTitle } from "@/components/ui/card";
import { KIND_LABEL, decisionKind, mergeDecisions } from "@/lib/classify";
import { formatAgo, formatDayTime, formatQty, formatUsd, signedClass } from "@/lib/format";
import type { Snapshot } from "@/lib/types";
import { cn } from "@/lib/utils";

type Props = {
  symbol: string;
  snapshot: Snapshot;
  onBack: () => void;
};

export function SymbolDetailPage({ symbol, snapshot, onBack }: Props) {
  const holding = (snapshot.portfolio.holdings ?? []).find((row) => row.symbol === symbol);
  const name = snapshot.company_map[symbol];
  const rows = mergeDecisions(snapshot.decisions, snapshot.recent_decisions).filter((row) => row.symbol === symbol);
  const watches = snapshot.indicator_watches.filter((watch) => watch.symbol === symbol);
  const wake = snapshot.symbol_wakes[symbol];
  const stale = snapshot.stale_market_data[symbol];

  return (
    <div className="grid gap-4">
      <div className="flex items-start justify-between gap-4">
        <div>
          <Button variant="ghost" size="sm" onClick={onBack} className="-ml-2 mb-2">
            <ArrowLeft className="size-3.5" />
            Back
          </Button>
          <h2 className="text-3xl font-semibold tracking-tight">{symbol}</h2>
          <p className="mt-1 text-sm text-dim">{name || "No company name on file"}</p>
        </div>
        <div className="flex gap-2">
          {stale ? <Badge tone="warn">stale</Badge> : <Badge tone="gain">fresh-or-unknown</Badge>}
          {holding ? (
            <Badge tone={holding.quantity < 0 ? "loss" : "gain"}>{holding.quantity < 0 ? "short" : "long"}</Badge>
          ) : (
            <Badge>flat</Badge>
          )}
        </div>
      </div>

      <section className="grid grid-cols-4 gap-3">
        <Mini label="Qty" value={holding ? formatQty(holding.quantity) : "—"} />
        <Mini label="Avg" value={formatUsd(holding?.avg_price, 2)} />
        <Mini label="Last" value={formatUsd(holding?.last_price, 2)} />
        <Mini
          label="uPnL"
          value={formatUsd(holding?.unrealized_pnl, 0)}
          className={signedClass(holding?.unrealized_pnl)}
        />
      </section>

      <section className="grid grid-cols-2 gap-4">
        <Card>
          <CardHeader>
            <CardTitle>Watches</CardTitle>
            <span className="font-mono text-[10px] text-faint">{wake ? `wake ${formatAgo(wake)}` : "no wake"}</span>
          </CardHeader>
          <CardBody className="space-y-2">
            {watches.length === 0 ? (
              <p className="text-sm text-faint">No active watch on this symbol.</p>
            ) : (
              watches.map((watch) => (
                <div key={watch.watch_id ?? `${watch.purpose}-${watch.expires_at}`} className="rounded-md border border-hairline px-3 py-2">
                  <p className="text-sm text-muted">{watch.purpose || "watch"}</p>
                  <p className="font-mono text-[11px] text-faint">expires {formatAgo(watch.expires_at)}</p>
                </div>
              ))
            )}
          </CardBody>
        </Card>
        <Card>
          <CardHeader>
            <CardTitle>Notes</CardTitle>
          </CardHeader>
          <CardBody className="space-y-2 text-sm text-dim">
            <p>Source of last mark is the daemon report, not a live broker stream.</p>
            <p>This desk is read-only. Plans and orders stay with the Python runtime.</p>
          </CardBody>
        </Card>
      </section>

      <Card>
        <CardHeader>
          <CardTitle>Decisions for {symbol}</CardTitle>
          <span className="font-mono text-[10px] text-faint">{rows.length}</span>
        </CardHeader>
        <div>
          {rows.map((row) => {
            const kind = decisionKind(row);
            return (
              <div
                key={row.decision_id ?? `${row.cycle_ts}-${row.sequence}`}
                className="border-b border-hairline px-4 py-3 last:border-0"
              >
                <div className="flex flex-wrap items-center gap-2">
                  <span className="font-mono text-[11px] text-faint">{formatDayTime(row.cycle_ts || row.ts)}</span>
                  <ActionChip action={row.action} />
                  <Badge>{KIND_LABEL[kind]}</Badge>
                  {row.confidence != null ? (
                    <span className="font-mono text-[10px] text-dim">{Math.round(row.confidence * 100)}%</span>
                  ) : null}
                </div>
                <p className="mt-2 text-sm leading-relaxed text-muted">{row.rationale || row.reason || "—"}</p>
              </div>
            );
          })}
          {rows.length === 0 ? <p className="px-4 py-8 text-sm text-faint">No recent decisions for this symbol.</p> : null}
        </div>
      </Card>
    </div>
  );
}

function Mini({ label, value, className }: { label: string; value: string; className?: string }) {
  return (
    <Card>
      <CardBody>
        <p className="font-mono text-[10px] uppercase tracking-[0.18em] text-faint">{label}</p>
        <p className={cn("mt-1 text-xl font-semibold tabular", className)}>{value}</p>
      </CardBody>
    </Card>
  );
}
