import { ActionChip } from "@/components/action-chip";
import { ActivityChart } from "@/components/charts/activity-chart";
import { EquityChart } from "@/components/charts/equity-chart";
import { Badge } from "@/components/ui/badge";
import { Card, CardBody, CardHeader, CardTitle } from "@/components/ui/card";
import { KIND_LABEL, decisionKind, mergeDecisions } from "@/lib/classify";
import { formatAgo, formatPct, formatUsd, signedClass } from "@/lib/format";
import type { Snapshot } from "@/lib/types";
import { cn } from "@/lib/utils";

type Props = {
  snapshot: Snapshot;
  onSymbol: (symbol: string) => void;
};

export function OverviewPage({ snapshot, onSymbol }: Props) {
  const portfolio = snapshot.portfolio;
  const equity = portfolio.equity ?? 0;
  const start = snapshot.starting_cash ?? equity;
  const pnl = equity - start;
  const holdings = (portfolio.holdings ?? []).filter((row) => Math.abs(row.quantity) > 1e-9);
  const journal = mergeDecisions(snapshot.decisions, snapshot.recent_decisions).slice(0, 8);
  const staleCount = Object.keys(snapshot.stale_market_data ?? {}).length;
  const workers = snapshot.queue_worker_activity.active_workers ?? 0;

  return (
    <div className="grid gap-4">
      <section className="grid grid-cols-4 gap-3">
        <Kpi label="Equity" value={formatUsd(equity, 0)} hint={formatPct(portfolio.total_return_pct)} tone={signedClass(pnl)} />
        <Kpi label="Vs start" value={formatUsd(pnl, 0)} hint="starting cash" tone={signedClass(pnl)} />
        <Kpi label="Cash" value={formatUsd(portfolio.cash_available ?? portfolio.cash, 0)} hint={`${holdings.length} open`} />
        <Kpi
          label="Cycle"
          value={`${snapshot.daemon.decisions_done ?? 0}/${snapshot.daemon.symbols_total ?? 0}`}
          hint={`${workers} workers · ${staleCount} stale`}
        />
      </section>

      <section className="grid grid-cols-[1.6fr_1fr] gap-4">
        <Card className="min-h-[320px]">
          <CardHeader>
            <CardTitle>Equity</CardTitle>
            <span className="font-mono text-[10px] text-faint">{snapshot.equity_series.length} marks</span>
          </CardHeader>
          <CardBody className="h-[280px] pt-2">
            <EquityChart points={snapshot.equity_series} className="h-full w-full" />
          </CardBody>
        </Card>
        <Card className="min-h-[320px]">
          <CardHeader>
            <CardTitle>Decisions · 12h</CardTitle>
            <div className="flex gap-2 font-mono text-[10px] text-faint">
              <span className="text-gain">fill</span>
              <span className="text-loss">risk</span>
              <span className="text-warn">stale</span>
              <span>hold</span>
            </div>
          </CardHeader>
          <CardBody className="h-[280px] pt-2">
            <ActivityChart report={snapshot.decisions} recent={snapshot.recent_decisions} />
          </CardBody>
        </Card>
      </section>

      <section className="grid grid-cols-[1.4fr_1fr] gap-4">
        <Card>
          <CardHeader>
            <CardTitle>Journal</CardTitle>
            <span className="text-[11px] text-faint">LLM rationale first · infra holds labelled</span>
          </CardHeader>
          <CardBody className="space-y-0 p-0">
            {journal.length === 0 ? (
              <p className="px-4 py-8 text-sm text-faint">No decisions in the current window.</p>
            ) : (
              journal.map((row) => {
                const kind = decisionKind(row);
                return (
                  <button
                    key={row.decision_id ?? `${row.symbol}-${row.cycle_ts}`}
                    type="button"
                    onClick={() => row.symbol && onSymbol(row.symbol)}
                    className="w-full border-b border-hairline px-4 py-3 text-left last:border-0 hover:bg-panel-hover"
                  >
                    <div className="flex items-center gap-2">
                      <span className="font-mono text-[10px] text-faint">{formatAgo(row.cycle_ts || row.ts)}</span>
                      <span className="font-medium">{row.symbol}</span>
                      <ActionChip action={row.action} />
                      <Badge tone={kind === "exec" ? "gain" : kind === "risk" ? "loss" : kind === "quiet" ? "muted" : "accent"}>
                        {KIND_LABEL[kind]}
                      </Badge>
                      {row.model_called === false ? <Badge>infra</Badge> : <Badge tone="accent">llm</Badge>}
                    </div>
                    <p className="mt-1.5 line-clamp-2 text-sm leading-relaxed text-muted">
                      {row.rationale || row.reason || "—"}
                    </p>
                  </button>
                );
              })
            )}
          </CardBody>
        </Card>

        <div className="grid gap-4">
          <Card>
            <CardHeader>
              <CardTitle>Open book</CardTitle>
              <span className="font-mono text-[10px] text-faint">{holdings.length}</span>
            </CardHeader>
            <CardBody className="space-y-2 p-3">
              {holdings
                .slice()
                .sort((a, b) => Math.abs(b.unrealized_pnl ?? 0) - Math.abs(a.unrealized_pnl ?? 0))
                .map((holding) => (
                  <button
                    key={holding.symbol}
                    type="button"
                    onClick={() => onSymbol(holding.symbol)}
                    className="flex w-full items-center justify-between rounded-md px-2 py-1.5 hover:bg-panel-hover"
                  >
                    <div>
                      <p className="text-sm font-medium">{holding.symbol}</p>
                      <p className="font-mono text-[10px] text-faint">
                        {holding.quantity > 0 ? "long" : "short"} {Math.abs(holding.quantity)}
                      </p>
                    </div>
                    <p className={cn("font-mono text-sm tabular", signedClass(holding.unrealized_pnl))}>
                      {formatUsd(holding.unrealized_pnl, 0)}
                    </p>
                  </button>
                ))}
            </CardBody>
          </Card>
          <Card>
            <CardHeader>
              <CardTitle>Next wake</CardTitle>
            </CardHeader>
            <CardBody>
              <p className="font-mono text-sm text-accent">{snapshot.default_next_wake ? formatAgo(snapshot.default_next_wake) : "—"}</p>
              <p className="mt-1 text-xs text-dim">{snapshot.daemon.symbols_due ?? 0} symbols due this cycle</p>
              <p className="mt-3 text-xs text-faint">{snapshot.indicator_watches.length} watches · {snapshot.armed_plans.length} armed</p>
            </CardBody>
          </Card>
        </div>
      </section>
    </div>
  );
}

function Kpi({
  label,
  value,
  hint,
  tone,
}: {
  label: string;
  value: string;
  hint?: string;
  tone?: string;
}) {
  return (
    <Card>
      <CardBody className="space-y-1">
        <p className="font-mono text-[10px] uppercase tracking-[0.18em] text-faint">{label}</p>
        <p className={cn("text-2xl font-semibold tracking-tight tabular", tone)}>{value}</p>
        {hint ? <p className="text-xs text-dim">{hint}</p> : null}
      </CardBody>
    </Card>
  );
}
