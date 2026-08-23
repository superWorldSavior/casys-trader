import { useState } from "react";
import { EquityChart } from "@/components/charts/equity-chart";
import { ExposureChart } from "@/components/charts/exposure-chart";
import { Badge } from "@/components/ui/badge";
import { Card, CardBody, CardHeader, CardTitle } from "@/components/ui/card";
import { usePlans, usePortfolio } from "@/hooks/use-desk-api";
import { formatAgo, formatDayTime, formatMarketPrice, formatPct, formatQty, formatUsd, signedClass } from "@/lib/format";
import { companyDisplayName, conditionLabel, plainMarketLanguage, venueLabel } from "@/lib/humanize";
import type { PlansPayload, PortfolioStory, Snapshot } from "@/lib/types";
import { cn } from "@/lib/utils";

const SORTS = [
  { id: 0, label: "Gain/loss $" },
  { id: 1, label: "Notional" },
  { id: 2, label: "Gain/loss %" },
];

type Props = {
  snapshot?: Snapshot;
  onSymbol: (symbol: string) => void;
};

export function PortfolioPage({ snapshot, onSymbol }: Props) {
  const [sort, setSort] = useState(0);
  const query = usePortfolio(sort);
  const plansQuery = usePlans();
  const data = query.data;
  const rows = data?.positions.rows ?? [];
  const equity = data?.equity;
  const exposure = data?.exposure;
  const stories = data?.stories ?? [];
  const chartExposures = rows.map((row) => ({
    symbol: row.symbol,
    value: row.notional,
  }));

  return (
    <div className="grid gap-5 pb-8">
      {query.error ? (
        <div className="rounded-lg border border-loss/30 bg-loss/5 px-4 py-3 text-sm text-loss">
          <p>The portfolio could not be refreshed. Casys does not infer an empty portfolio from this failure.</p>
          <details className="mt-2 text-xs text-dim">
            <summary className="cursor-pointer">Technical details</summary>
            <p className="mt-1 break-words font-mono text-[10px] text-faint">{query.error instanceof Error ? query.error.message : String(query.error)}</p>
          </details>
        </div>
      ) : null}

      {query.isPending && !data ? <PortfolioLoading /> : null}

      {data ? (
        <>
          <section className="grid grid-cols-2 gap-3 min-[1180px]:grid-cols-4">
            <Stat label="Total value" value={formatUsd(equity?.equity, 0)} />
            <Stat label="Available cash" value={formatUsd(equity?.cash_available ?? equity?.cash, 0)} />
            <Stat label="Long exposure" value={formatUsd(exposure?.long_usd, 0)} className="text-gain" />
            <Stat label="Short exposure" value={formatUsd(exposure?.short_usd, 0)} className="text-loss" />
          </section>

          <section className="grid gap-4 min-[1180px]:grid-cols-[1.5fr_1fr]">
        <Card className="min-h-[300px]">
          <CardHeader>
            <CardTitle>Portfolio value</CardTitle>
            <span className={cn("font-mono text-[10px]", signedClass(equity?.return_pct))}>{formatPct(equity?.return_pct)}</span>
          </CardHeader>
          <CardBody className="h-[260px]">
            <EquityChart points={snapshot?.equity_series ?? []} className="h-full w-full" />
          </CardBody>
        </Card>
        <Card className="min-h-[300px]">
          <CardHeader>
            <CardTitle>Exposure by company</CardTitle>
          </CardHeader>
          <CardBody className="min-h-[340px] min-[600px]:h-[260px] min-[600px]:min-h-0">
            <ExposureChart exposures={chartExposures} companyMap={snapshot?.company_map} />
          </CardBody>
        </Card>
          </section>

          <section>
            <div className="mb-3 flex items-end justify-between gap-4">
              <h2 className="font-mono text-[10px] uppercase tracking-[0.2em] text-faint">Open positions</h2>
              <span className="text-xs text-dim">{rows.length} position{rows.length === 1 ? "" : "s"}</span>
            </div>
            {stories.length ? (
              <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
                {stories.map((story) => (
                  <StoryCard
                    key={story.symbol}
                    story={story}
                    onSymbol={onSymbol}
                    plans={plansQuery.data}
                    plansPending={plansQuery.isPending}
                    plansError={plansQuery.error}
                  />
                ))}
              </div>
            ) : rows.length ? (
              <Card>
                <CardBody>
                  <p className="text-sm text-muted">
                    {rows.length} positions are recorded. Their company narratives are not available yet; the technical record remains below.
                  </p>
                </CardBody>
              </Card>
            ) : (
              <Card>
                <CardBody>
                  <p className="text-sm text-muted">
                    No open position is recorded right now. Casys will keep observing until its conditions are met.
                  </p>
                </CardBody>
              </Card>
            )}
          </section>

      {/* ── Details (collapsible) ── */}
      <details className="group">
        <summary className="mb-3 cursor-pointer list-none text-sm font-medium text-dim hover:text-fg focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent/50">
          <span className="group-open:hidden">Show technical portfolio record</span>
          <span className="hidden group-open:inline">Hide technical portfolio record</span>
        </summary>

        <div className="grid gap-4">
          {/* Sort controls */}
          <div className="flex flex-wrap items-center gap-2">
            {SORTS.map((item) => (
              <button
                key={item.id}
                type="button"
                onClick={() => setSort(item.id)}
                aria-pressed={sort === item.id}
                className={cn(
                  "rounded-sm px-2 py-1 font-mono text-[10px] uppercase tracking-[0.14em]",
                  sort === item.id ? "bg-accent/15 text-accent" : "text-faint hover:text-muted",
                )}
              >
                {item.label}
              </button>
            ))}
            <span className="font-mono text-[10px] text-faint">
              unrealised gain/loss {formatUsd(data?.positions.unrealized_total, 0)} · net exposure{" "}
              {formatUsd(exposure?.net, 0)}
            </span>
            {query.isPlaceholderData ? <span className="text-xs text-accent" aria-live="polite">Updating order…</span> : null}
          </div>

          {/* Technical table */}
          <Card className="overflow-x-auto">
            <div className="min-w-[900px]">
            <div className="grid grid-cols-[minmax(210px,1.5fr)_50px_80px_80px_80px_80px_70px_70px] gap-3 border-b border-hairline px-4 py-2 font-mono text-[10px] uppercase tracking-[0.14em] text-faint">
              <span>Company</span>
              <span>Side</span>
              <span className="text-right">Qty</span>
              <span className="text-right">Avg</span>
              <span className="text-right">Last</span>
              <span className="text-right">uPnL</span>
              <span className="text-right">%</span>
              <span className="text-right">Stop</span>
            </div>
            {rows.map((row) => {
              const companyName = companyDisplayName(snapshot?.company_map, row.symbol);
              return (
                <button
                  key={row.symbol}
                  type="button"
                  onClick={() => onSymbol(row.symbol)}
                  className="grid w-full grid-cols-[minmax(210px,1.5fr)_50px_80px_80px_80px_80px_70px_70px] items-center gap-3 border-b border-hairline px-4 py-2.5 text-left last:border-0 hover:bg-panel-hover"
                >
                  <div className="min-w-0">
                    <p className="truncate text-sm font-medium" title={companyName}>
                      {companyName}
                      {row.is_stale ? (
                        <span className="ml-2 font-mono text-[10px] text-warn">stale</span>
                      ) : null}
                    </p>
                    <p className="font-mono text-[10px] text-faint">
                      {row.symbol} · {formatUsd(row.notional, 0)}
                    </p>
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
                    {formatMarketPrice(row.avg, 2)}
                  </span>
                  <span className="text-right font-mono text-sm tabular">{formatMarketPrice(row.last, 2)}</span>
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
              );
            })}
            </div>
          </Card>

          {/* Currency rates and completed trades */}
          <section className="grid grid-cols-1 gap-4 min-[900px]:grid-cols-2">
            <Card>
              <CardHeader>
                <CardTitle>Currency rates</CardTitle>
                <Badge tone={data?.fx.source_available ? "gain" : "warn"}>
                  {data?.fx.source_available ? "rates available" : "rates unavailable"}
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
                  <p className="text-sm text-faint">No currency rates available.</p>
                ) : null}
              </CardBody>
            </Card>
            <Card>
              <CardHeader>
                <CardTitle>Closed trades</CardTitle>
                <span className="font-mono text-[10px] text-faint">
                  {data?.closed_trips.length ?? 0}
                </span>
              </CardHeader>
              <CardBody className="space-y-2 p-0">
                {(data?.closed_trips ?? []).map((trip, index) => {
                  const pnl = typeof trip.pnl === "number" ? trip.pnl : null;
                  const symbol = typeof trip.symbol === "string" ? trip.symbol : "";
                  const companyName = symbol
                    ? companyDisplayName(snapshot?.company_map, symbol)
                    : "—";
                  return (
                    <button
                      key={`${String(trip.symbol)}-${String(trip.exit_ts)}-${index}`}
                      type="button"
                      onClick={() => symbol && onSymbol(symbol)}
                      className="flex w-full items-center justify-between border-b border-hairline px-4 py-2 last:border-0 hover:bg-panel-hover"
                    >
                      <div className="min-w-0 text-left">
                        <p className="truncate text-sm font-medium" title={companyName}>{companyName}</p>
                        <p className="font-mono text-[10px] text-faint">
                          {symbol ? `${symbol} · ` : ""}{formatDayTime(
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
        </>
      ) : null}
    </div>
  );
}

function PortfolioLoading() {
  return (
    <div className="grid gap-4" role="status" aria-label="Loading portfolio">
      <span className="sr-only">Loading portfolio</span>
      <div className="grid grid-cols-2 gap-3 min-[1180px]:grid-cols-4">
        {Array.from({ length: 4 }, (_, index) => (
          <Card key={index}>
            <CardBody className="space-y-3">
              <div className="h-2 w-24 animate-pulse rounded bg-hairline motion-reduce:animate-none" />
              <div className="h-7 w-32 animate-pulse rounded bg-line motion-reduce:animate-none" />
            </CardBody>
          </Card>
        ))}
      </div>
      <div className="grid gap-4 min-[1180px]:grid-cols-[1.5fr_1fr]">
        <Card className="h-[300px] animate-pulse bg-panel/60 motion-reduce:animate-none" />
        <Card className="h-[300px] animate-pulse bg-panel/60 motion-reduce:animate-none" />
      </div>
    </div>
  );
}

function StoryCard({
  story,
  onSymbol,
  plans,
  plansPending,
  plansError,
}: {
  story: PortfolioStory;
  onSymbol: (symbol: string) => void;
  plans?: PlansPayload;
  plansPending: boolean;
  plansError: unknown;
}) {
  const usingDecisionReason = !story.thesis?.trim() && !story.business?.trim() && Boolean(story.why?.trim());
  const phrase = plainMarketLanguage(
    story.thesis?.trim() ||
      story.business?.trim() ||
      (story.why?.trim()
        ? "Casys has a recorded AI review on file. Open the company to inspect the full reasoning."
        : "Casys hasn't written a company narrative for this position yet."),
  );

  const isPlaceholder = !story.why && !story.thesis && !story.business;
  const management = currentManagementLine(story.symbol, plans, plansPending, plansError);

  return (
    <button
      type="button"
      onClick={() => onSymbol(story.symbol)}
      className="flex min-w-0 w-full flex-col gap-2 rounded-lg border border-line bg-panel/90 p-4 text-left shadow-[0_1px_2px_rgba(23,43,54,0.04)] hover:bg-panel-hover focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent/50"
    >
      {/* Header row */}
      <div className="flex items-start justify-between gap-2">
        <div className="min-w-0">
          <p className="line-clamp-2 text-base font-semibold leading-tight">
            {story.name ?? story.symbol}
          </p>
          <p className="font-mono text-[10px] text-faint">
            {story.symbol}
            {story.venue ? ` · ${venueLabel(story.venue)}` : ""}
          </p>
        </div>
        <Badge tone={story.side === "S" ? "loss" : "muted"}>
          {story.side === "L" ? "Long position" : "Short position"}
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

      {management ? (
        <p className="text-xs leading-5 text-dim">{management}</p>
      ) : null}

      <p
        className={cn(
          "line-clamp-2 text-sm leading-snug",
          isPlaceholder ? "text-faint" : "text-muted",
        )}
      >
        {phrase}
      </p>

      {story.why_at && usingDecisionReason ? (
        <p className="font-mono text-[10px] text-faint">{formatAgo(story.why_at)}</p>
      ) : null}
    </button>
  );
}

function currentManagementLine(
  symbol: string,
  plans: PlansPayload | undefined,
  pending: boolean,
  error: unknown,
): string | null {
  if (error && !plans) return "Position plans are unavailable.";
  if (pending && !plans) return null;
  const exits = plans?.exit_plans.rows.filter((row) => row.symbol === symbol) ?? [];
  const exit = exits.find((row) => isResolvedPrice(row.stop_price)) ?? exits[0];
  const watches = [...(plans?.watches.rows ?? []), ...(plans?.exit_watches.rows ?? [])].filter(
    (row) => row.symbol === symbol,
  );
  const parts: string[] = [];
  if (exit) {
    const hasStop = isResolvedPrice(exit.stop_price);
    const facts = [hasStop ? "Protected" : "Exit plan"];
    if (hasStop) facts.push(`stop ${formatMarketPrice(exit.stop_price, 2)}`);
    if (hasStop && isFiniteNumber(exit.stop_left_pct)) facts.push(`${exit.stop_left_pct.toFixed(1)}% from recorded stop`);
    if (exit.take_profit_label && exit.take_profit_label !== "—") facts.push(`target ${exit.take_profit_label}`);
    parts.push(facts.join(" · "));
  }
  const seenWatches = new Set<string>();
  for (const watch of watches) {
    const detail = `${conditionLabel(watch.condition_label)} · ${expiryLabel(watch.countdown)}`;
    if (seenWatches.has(detail)) continue;
    seenWatches.add(detail);
    parts.push(`Watch: ${detail}`);
  }
  if (parts.length) return parts.join(" · ");
  return "No exit plan or watch recorded";
}

function expiryLabel(countdown: string): string {
  const value = countdown.trim();
  if (!value || value === "—") return "expiry not recorded";
  if (value.toLowerCase() === "expired") return "expired";
  if (value.toLowerCase() === "now") return "expires now";
  return /^in\b/i.test(value) ? `expires ${value}` : `expires in ${value}`;
}

function isResolvedPrice(value: number | null | undefined): value is number {
  return typeof value === "number" && Number.isFinite(value) && value > 0;
}

function isFiniteNumber(value: number | null | undefined): value is number {
  return typeof value === "number" && Number.isFinite(value);
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
