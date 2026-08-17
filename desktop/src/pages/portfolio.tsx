import { EquityChart } from "@/components/charts/equity-chart";
import { ExposureChart } from "@/components/charts/exposure-chart";
import { Card, CardBody, CardHeader, CardTitle } from "@/components/ui/card";
import { formatPct, formatQty, formatUsd, signedClass } from "@/lib/format";
import type { Snapshot } from "@/lib/types";
import { cn } from "@/lib/utils";

type Props = {
  snapshot: Snapshot;
  onSymbol: (symbol: string) => void;
};

export function PortfolioPage({ snapshot, onSymbol }: Props) {
  const portfolio = snapshot.portfolio;
  const holdings = (portfolio.holdings ?? [])
    .filter((row) => Math.abs(row.quantity) > 1e-9)
    .sort((a, b) => Math.abs(b.unrealized_pnl ?? 0) - Math.abs(a.unrealized_pnl ?? 0));

  return (
    <div className="grid gap-4">
      <section className="grid grid-cols-4 gap-3">
        <Stat label="Equity" value={formatUsd(portfolio.equity, 0)} />
        <Stat label="Cash available" value={formatUsd(portfolio.cash_available ?? portfolio.cash, 0)} />
        <Stat label="Long" value={formatUsd(portfolio.long_exposure_usd, 0)} className="text-gain" />
        <Stat label="Short" value={formatUsd(portfolio.short_exposure_usd, 0)} className="text-loss" />
      </section>

      <section className="grid grid-cols-[1.5fr_1fr] gap-4">
        <Card className="min-h-[300px]">
          <CardHeader>
            <CardTitle>Equity curve</CardTitle>
            <span className="font-mono text-[10px] text-faint">{formatPct(portfolio.total_return_pct)}</span>
          </CardHeader>
          <CardBody className="h-[260px]">
            <EquityChart points={snapshot.equity_series} className="h-full w-full" />
          </CardBody>
        </Card>
        <Card className="min-h-[300px]">
          <CardHeader>
            <CardTitle>Gross by name</CardTitle>
          </CardHeader>
          <CardBody className="h-[260px]">
            <ExposureChart holdings={holdings} />
          </CardBody>
        </Card>
      </section>

      <Card>
        <div className="grid grid-cols-[1.2fr_70px_90px_90px_90px_90px] gap-3 border-b border-hairline px-4 py-2 font-mono text-[10px] uppercase tracking-[0.14em] text-faint">
          <span>Symbol</span>
          <span>Side</span>
          <span className="text-right">Qty</span>
          <span className="text-right">Avg</span>
          <span className="text-right">Last</span>
          <span className="text-right">uPnL</span>
        </div>
        {holdings.map((holding) => (
          <button
            key={holding.symbol}
            type="button"
            onClick={() => onSymbol(holding.symbol)}
            className="grid w-full grid-cols-[1.2fr_70px_90px_90px_90px_90px] items-center gap-3 border-b border-hairline px-4 py-2.5 text-left last:border-0 hover:bg-panel-hover"
          >
            <div>
              <p className="text-sm font-medium">{holding.symbol}</p>
              <p className="text-xs text-faint">{snapshot.company_map[holding.symbol] ?? ""}</p>
            </div>
            <span className={cn("font-mono text-[10px] uppercase", holding.quantity < 0 ? "text-loss" : "text-gain")}>
              {holding.quantity < 0 ? "short" : "long"}
            </span>
            <span className="text-right font-mono text-sm tabular">{formatQty(holding.quantity)}</span>
            <span className="text-right font-mono text-sm tabular text-muted">{formatUsd(holding.avg_price, 2)}</span>
            <span className="text-right font-mono text-sm tabular">{formatUsd(holding.last_price, 2)}</span>
            <span className={cn("text-right font-mono text-sm tabular", signedClass(holding.unrealized_pnl))}>
              {formatUsd(holding.unrealized_pnl, 0)}
            </span>
          </button>
        ))}
      </Card>
    </div>
  );
}

function Stat({ label, value, className }: { label: string; value: string; className?: string }) {
  return (
    <Card>
      <CardBody>
        <p className="font-mono text-[10px] uppercase tracking-[0.18em] text-faint">{label}</p>
        <p className={cn("mt-1 text-2xl font-semibold tabular", className)}>{value}</p>
      </CardBody>
    </Card>
  );
}
