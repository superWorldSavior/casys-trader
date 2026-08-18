import { useState } from "react";
import { EquityChart } from "@/components/charts/equity-chart";
import { ExposureChart } from "@/components/charts/exposure-chart";
import { Badge } from "@/components/ui/badge";
import { Card, CardBody, CardHeader, CardTitle } from "@/components/ui/card";
import { usePortfolio } from "@/hooks/use-desk-api";
import { formatAgo, formatDayTime, formatPct, formatQty, formatUsd, signedClass } from "@/lib/format";
import type { PortfolioStory, Snapshot } from "@/lib/types";
import { cn } from "@/lib/utils";

const SORTS = [
  { id: 0, label: "P&L $" },
  { id: 1, label: "Notional" },
  { id: 2, label: "P&L %" },
];

type Props = {
  snapshot?: Snapshot;
  onSymbol: (symbol: string) => void;
};

export function PortfolioPage({ snapshot, onSymbol }: Props) {
  const [sort, setSort] = useState(0);
  const query = usePortfolio(sort);
  const data = query.data;
  const rows = data?.positions.rows ?? [];
  const equity = data?.equity;
  const exposure = data?.exposure;
  const stories = data?.stories ?? [];
  const chartHoldings = rows.map((row) => ({
    symbol: row.symbol,
    quantity: row.qty,
    unrealized_pnl: row.pnl,
    last_price: row.last ?? undefined,
    avg_price: row.avg ?? undefined,
  }));

  return (
    <div className="grid gap-6">
      {/* ── Your Positions narrative ── */}
      <section>
        <h2 className="mb-3 font-mono text-[10px] uppercase tracking-[0.22em] text-faint">
          Your positions
        </h2>
        {query.error ? (
          <p className="text-sm text-loss">
            {query.error instanceof Error ? query.error.message : String(query.error)}
          </p>
        ) : stories.length === 0 ? (
          <Card>
            <CardBody>
              <p className="text-sm text-muted">
                No open positions right now. The agent will surface opportunities as the market develops.
              </p>
            </CardBody>
          </Card>
        ) : (
          <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
            {stories.map((story) => (
              <StoryCard key={story.symbol} story={story} onSymbol={onSymbol} />
            ))}
          </div>
        )}
      </section>

      {/* ── KPIs ── */}
      <section className="grid grid-cols-4 gap-3">
        <Stat label="Equity" value={formatUsd(equity?.equity, 0)} />
        <Stat label="Cash" value={formatUsd(equity?.cash_available ?? equity?.cash, 0)} />
        <Stat label="Long" value={formatUsd(exposure?.long_usd, 0)} className="text-gain" />
        <Stat label="Short" value={formatUsd(exposure?.short_usd, 0)} className="text-loss" />
      </section>

      {/* ── Charts ── */}
      <section className="grid grid-cols-[1.5fr_1fr] gap-4">
        <Card className="min-h-[300px]">
          <CardHeader>
            <CardTitle>Equity curve</CardTitle>
            <span className="font-mono text-[10px] text-faint">{formatPct(equity?.return_pct)}</span>
          </CardHeader>
          <CardBody className="h-[260px]">
            <EquityChart points={snapshot?.equity_series ?? []} className="h-full w-full" />
          </CardBody>
        </Card>
        <Card className="min-h-[300px]">
          <CardHeader>
            <CardTitle>Gross by name</CardTitle>
          </CardHeader>
          <CardBody className="h-[260px]">
            <ExposureChart holdings={chartHoldings} />
          </CardBody>
        </Card>
      </section>

      {/* ── Details (collapsible) ── */}
      <details className="group">
        <summary className="mb-3 cursor-pointer list-none font-mono text-[10px] uppercase tracking-[0.22em] text-faint hover:text-muted">
          <span className="group-open:hidden">▶ Details</span>
          <span className="hidden group-open:inline">▼ Details</span>
        </summary>

        <div className="grid gap-4">
          {/* Sort controls */}
          <div className="flex flex-wrap items-center gap-2">
            {SORTS.map((item) => (
              <button
                key={item.id}
                type="button"
                onClick={() => setSort(item.id)}
                className={cn(
                  "rounded-sm px-2 py-1 font-mono text-[10px] uppercase tracking-[0.14em]",
                  sort === item.id ? "bg-accent/15 text-accent" : "text-faint hover:text-muted",
                )}
              >
                {item.label}
              </button>
            ))}
            <span className="font-mono text-[10px] text-faint">
              uPnL {formatUsd(data?.positions.unrealized_total, 0)} · net{" "}
              {formatUsd(exposure?.net, 0)}
            </span>
          </div>

          {/* Technical table */}
          <Card>
            <div className="grid grid-cols-[1.1fr_50px_80px_80px_80px_80px_70px_70px] gap-3 border-b border-hairline px-4 py-2 font-mono text-[10px] uppercase tracking-[0.14em] text-faint">
              <span>Symbol</span>
              <span>Side</span>
              <span className="text-right">Qty</span>
              <span className="text-right">Avg</span>
              <span className="text-right">Last</span>
              <span className="text-right">uPnL</span>
              <span className="text-right">%</span>
              <span className="text-right">Stop</span>
            </div>
            {rows.map((row) => (
              <button
                key={row.symbol}
                type="button"
                onClick={() => onSymbol(row.symbol)}
                className="grid w-full grid-cols-[1.1fr_50px_80px_80px_80px_80px_70px_70px] items-center gap-3 border-b border-hairline px-4 py-2.5 text-left last:border-0 hover:bg-panel-hover"
              >
                <div>
                  <p className="text-sm font-medium">
                    {row.symbol}
                    {row.is_stale ? (
                      <span className="ml-2 font-mono text-[10px] text-warn">stale</span>
                    ) : null}
                  </p>
                  <p className="font-mono text-[10px] text-faint">{formatUsd(row.notional, 0)}</p>
                </div>
                <span
                  className={cn(
                    "font-mono text-[10px] uppercase",
                    row.side === "S" ? "text-loss" : "text-gain",
                  )}
                >
                  {row.side}
                </span>
                <span className="text-right font-mono text-sm tabular">{formatQty(row.qty)}</span>
                <span className="text-right font-mono text-sm tabular text-muted">
                  {formatUsd(row.avg, 2)}
                </span>
                <span className="text-right font-mono text-sm tabular">{formatUsd(row.last, 2)}</span>
                <span className={cn("text-right font-mono text-sm tabular", signedClass(row.pnl))}>
                  {formatUsd(row.pnl, 0)}
                </span>
                <span
                  className={cn("text-right font-mono text-sm tabular", signedClass(row.pnl_pct))}
                >
                  {formatPct(row.pnl_pct)}
                </span>
                <span className="text-right font-mono text-[11px] text-faint">
                  {row.stop_left_pct != null ? formatPct(row.stop_left_pct) : "—"}
                </span>
              </button>
            ))}
          </Card>

          {/* FX + Closed trips */}
          <section className="grid grid-cols-2 gap-4">
            <Card>
              <CardHeader>
                <CardTitle>FX</CardTitle>
                <Badge tone={data?.fx.source_available ? "gain" : "warn"}>
                  {data?.fx.source_available ? "live" : "missing"}
                </Badge>
              </CardHeader>
              <CardBody className="space-y-1.5">
                {(data?.fx.rows ?? []).map((row) => (
                  <div key={row.currency} className="flex justify-between font-mono text-sm">
                    <span className="text-dim">{row.currency}</span>
                    <span>{row.rate.toFixed(4)}</span>
                  </div>
                ))}
                {(data?.fx.rows ?? []).length === 0 ? (
                  <p className="text-sm text-faint">No FX rows.</p>
                ) : null}
              </CardBody>
            </Card>
            <Card>
              <CardHeader>
                <CardTitle>Closed trips</CardTitle>
                <span className="font-mono text-[10px] text-faint">
                  {data?.closed_trips.length ?? 0}
                </span>
              </CardHeader>
              <CardBody className="space-y-2 p-0">
                {(data?.closed_trips ?? []).map((trip, index) => {
                  const pnl = typeof trip.pnl === "number" ? trip.pnl : null;
                  return (
                    <button
                      key={`${String(trip.symbol)}-${String(trip.exit_ts)}-${index}`}
                      type="button"
                      onClick={() =>
                        typeof trip.symbol === "string" && onSymbol(trip.symbol)
                      }
                      className="flex w-full items-center justify-between border-b border-hairline px-4 py-2 last:border-0 hover:bg-panel-hover"
                    >
                      <div>
                        <p className="text-sm font-medium">{String(trip.symbol ?? "—")}</p>
                        <p className="font-mono text-[10px] text-faint">
                          {formatDayTime(
                            typeof trip.exit_ts === "string" ? trip.exit_ts : undefined,
                          )}{" "}
                          · {String(trip.exit_reason ?? trip.side ?? "")}
                        </p>
                      </div>
                      <p className={cn("font-mono text-sm tabular", signedClass(pnl))}>
                        {formatUsd(pnl, 0)}
                      </p>
                    </button>
                  );
                })}
                {(data?.closed_trips ?? []).length === 0 ? (
                  <p className="px-4 py-6 text-sm text-faint">No closed trades yet.</p>
                ) : null}
              </CardBody>
            </Card>
          </section>
        </div>
      </details>
    </div>
  );
}

function StoryCard({
  story,
  onSymbol,
}: {
  story: PortfolioStory;
  onSymbol: (symbol: string) => void;
}) {
  // Pick the best narrative phrase
  const phrase =
    story.thesis?.trim() ||
    story.why?.trim() ||
    story.business?.trim() ||
    "The agent hasn't written up this position yet.";

  const isPlaceholder = !story.thesis && !story.why && !story.business;

  return (
    <button
      type="button"
      onClick={() => onSymbol(story.symbol)}
      className="flex w-full flex-col gap-2 rounded-lg border border-line bg-panel/90 p-4 text-left shadow-[0_1px_2px_rgba(23,43,54,0.04)] hover:bg-panel-hover"
    >
      {/* Header row */}
      <div className="flex items-start justify-between gap-2">
        <div className="min-w-0">
          <p className="truncate text-base font-semibold leading-tight">
            {story.name ?? story.symbol}
          </p>
          <p className="font-mono text-[10px] text-faint">
            {story.symbol}
            {story.venue ? ` · ${story.venue}` : ""}
          </p>
        </div>
        <Badge tone={story.side === "S" ? "loss" : "muted"}>
          {story.side === "L" ? "holding" : "short bet"}
        </Badge>
      </div>

      {/* PnL */}
      <div className="flex items-baseline gap-2">
        <span className={cn("text-xl font-semibold tabular", signedClass(story.pnl))}>
          {formatUsd(story.pnl, 0)}
        </span>
        <span className={cn("font-mono text-sm tabular", signedClass(story.pnl_pct))}>
          {formatPct(story.pnl_pct)}
        </span>
      </div>

      {/* Narrative phrase */}
      <p
        className={cn(
          "line-clamp-3 text-sm leading-snug",
          isPlaceholder ? "text-faint" : "text-muted",
        )}
      >
        {phrase}
      </p>

      {/* Attribution timestamp (only for "why" sourced from a real decision) */}
      {story.why_at && story.why && !story.thesis ? (
        <p className="font-mono text-[10px] text-faint">{formatAgo(story.why_at)}</p>
      ) : null}
    </button>
  );
}

function Stat({
  label,
  value,
  className,
}: {
  label: string;
  value: string;
  className?: string;
}) {
  return (
    <Card>
      <CardBody>
        <p className="font-mono text-[10px] uppercase tracking-[0.18em] text-faint">{label}</p>
        <p className={cn("mt-1 text-2xl font-semibold tabular", className)}>{value}</p>
      </CardBody>
    </Card>
  );
}
