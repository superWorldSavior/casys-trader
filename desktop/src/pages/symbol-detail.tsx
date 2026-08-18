import { ArrowLeft } from "lucide-react";
import { ActionChip } from "@/components/action-chip";
import { PriceChart } from "@/components/charts/price-chart";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardBody, CardHeader, CardTitle } from "@/components/ui/card";
import { useSymbol, useSymbolBars } from "@/hooks/use-desk-api";
import { useCompanyIntelligence } from "@/hooks/use-intelligence";
import { formatAgo, formatDayTime, formatQty, formatUsd, signedClass } from "@/lib/format";
import type { PriceBar } from "@/lib/types";
import { cn } from "@/lib/utils";

type Props = {
  symbol: string;
  onBack: () => void;
};

export function SymbolDetailPage({ symbol, onBack }: Props) {
  const query = useSymbol(symbol);
  const data = query.data;
  const barsQuery = useSymbolBars(symbol);
  const bars = barsQuery.data?.bars ?? [];
  const barsAsOf = barsQuery.data?.as_of ?? null;
  const intelQuery = useCompanyIntelligence({ symbol });
  const holding = data?.holding ?? {};
  const qty = num(holding.quantity ?? holding.qty);
  const pnl = num(holding.unrealized_pnl ?? holding.unrealized_pnl_net);
  const stale = Boolean(data?.stale);

  return (
    <div className="grid gap-4">
      <div className="flex items-start justify-between gap-4">
        <div>
          <Button variant="ghost" size="sm" onClick={onBack} className="-ml-2 mb-2">
            <ArrowLeft className="size-3.5" />
            Back
          </Button>
          <h2 className="text-3xl font-semibold tracking-tight">{symbol}</h2>
          <p className="mt-1 text-sm text-dim">{data?.name || "No company name on file"}</p>
        </div>
        <div className="flex gap-2">
          {stale ? <Badge tone="warn">stale</Badge> : <Badge tone="gain">fresh-or-unknown</Badge>}
          {qty != null && qty !== 0 ? (
            <Badge tone={qty < 0 ? "loss" : "gain"}>{qty < 0 ? "short" : "long"}</Badge>
          ) : (
            <Badge>flat</Badge>
          )}
        </div>
      </div>
      {query.error ? (
        <p className="text-sm text-loss">{query.error instanceof Error ? query.error.message : String(query.error)}</p>
      ) : null}

      <Card>
        <CardHeader>
          <CardTitle>Why</CardTitle>
          {data?.why?.cycle_ts ? (
            <span className="font-mono text-[10px] text-faint">{formatDayTime(data.why.cycle_ts)}</span>
          ) : null}
        </CardHeader>
        <CardBody>
          {data?.why ? (
            <div>
              <div className="mb-2 flex items-center gap-2">
                <ActionChip action={data.why.action} />
                {data.why.confidence != null ? (
                  <span className="font-mono text-[11px] text-faint">{Number(data.why.confidence).toFixed(2)}</span>
                ) : null}
              </div>
              <p className="border-l-2 border-accent/70 pl-3 text-sm leading-relaxed text-muted">
                {data.why.rationale || "—"}
              </p>
            </div>
          ) : (
            <p className="text-sm italic text-faint">No reasoning recorded.</p>
          )}
        </CardBody>
      </Card>

      <section className="grid grid-cols-4 gap-3">
        <Mini label="Qty" value={qty != null ? formatQty(qty) : "—"} />
        <Mini label="Avg" value={formatUsd(num(holding.avg_price), 2)} />
        <Mini label="Last" value={formatUsd(data?.last_price ?? num(holding.last_price), 2)} />
        <Mini label="uPnL" value={formatUsd(pnl, 0)} className={signedClass(pnl)} />
      </section>

      <PriceCard bars={bars} asOf={barsAsOf} n={bars.length} />

      <AboutBlock symbol={symbol} intelQuery={intelQuery} />

      <section className="grid grid-cols-2 gap-4">
        <Card>
          <CardHeader>
            <CardTitle>Exit plan</CardTitle>
            <span className="font-mono text-[10px] text-faint">
              {data?.wake ? `wake ${typeof data.wake === "string" ? formatAgo(data.wake) : String(data.wake)}` : "no wake"}
            </span>
          </CardHeader>
          <CardBody className="space-y-2">
            {(data?.exit_plans ?? []).length === 0 ? <p className="text-sm text-faint">No exit plan.</p> : null}
            {(data?.exit_plans ?? []).map((row) => (
              <div key={`${row.symbol}-${row.stop_price}`} className="rounded-md border border-hairline px-3 py-2">
                <p className="text-sm text-muted">
                  {row.side} · stop {row.stop_price != null ? formatUsd(row.stop_price, 2) : "—"} · tp{" "}
                  {row.take_profit_label ?? "—"}
                </p>
                <p className="font-mono text-[11px] text-faint">{row.protect_label || row.review_label || ""}</p>
              </div>
            ))}
            {(data?.armed ?? []).map((row) => (
              <div key={`${row.symbol}-armed`} className="rounded-md border border-hairline px-3 py-2">
                <p className="text-sm text-muted">
                  armed {row.action} · {row.countdown}
                </p>
              </div>
            ))}
          </CardBody>
        </Card>
        <Card>
          <CardHeader>
            <CardTitle>Watch / market</CardTitle>
          </CardHeader>
          <CardBody className="space-y-2">
            {(data?.watches ?? []).concat(data?.exit_watches ?? []).length === 0 ? (
              <p className="text-sm text-faint">No active watch on this symbol.</p>
            ) : (
              (data?.watches ?? []).concat(data?.exit_watches ?? []).map((watch) => (
                <div key={`${watch.symbol}-${watch.condition_label}`} className="rounded-md border border-hairline px-3 py-2">
                  <p className="text-sm text-muted">{watch.condition_label}</p>
                  <p className="font-mono text-[11px] text-faint">{watch.countdown}</p>
                </div>
              ))
            )}
            {stale ? <p className="text-xs text-warn">Market data marked stale for this symbol.</p> : null}
          </CardBody>
        </Card>
      </section>

      <Card>
        <CardHeader>
          <CardTitle>Decisions for {symbol}</CardTitle>
          <span className="font-mono text-[10px] text-faint">{data?.decisions.length ?? 0}</span>
        </CardHeader>
        <div>
          {(data?.decisions ?? []).map((row, index) => (
            <div key={`${row.cycle_ts}-${index}`} className="border-b border-hairline px-4 py-3 last:border-0">
              <div className="flex flex-wrap items-center gap-2">
                <span className="font-mono text-[11px] text-faint">{formatDayTime(row.cycle_ts)}</span>
                <ActionChip action={row.action} />
                {row.model_called === false ? <Badge>infra</Badge> : <Badge tone="accent">llm</Badge>}
                {row.confidence != null ? (
                  <span className="font-mono text-[10px] text-dim">{Number(row.confidence).toFixed(2)}</span>
                ) : null}
              </div>
              <p className="mt-2 text-sm leading-relaxed text-muted">{row.rationale || row.reason || "—"}</p>
            </div>
          ))}
          {(data?.decisions ?? []).length === 0 ? (
            <p className="px-4 py-8 text-sm text-faint">No recent decisions for this symbol.</p>
          ) : null}
        </div>
      </Card>
    </div>
  );
}

function PriceCard({ bars, asOf, n }: { bars: PriceBar[]; asOf: string | null; n: number }) {
  return (
    <Card>
      <CardHeader>
        <CardTitle>Price — last {n} sessions</CardTitle>
        {asOf ? (
          <span className="font-mono text-[10px] text-faint">as of {asOf}</span>
        ) : null}
      </CardHeader>
      <CardBody>
        {bars.length > 0 ? (
          <PriceChart bars={bars} />
        ) : (
          <p className="py-4 text-sm text-faint">No price history available.</p>
        )}
      </CardBody>
    </Card>
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

function num(value: unknown): number | undefined {
  return typeof value === "number" && Number.isFinite(value) ? value : undefined;
}

// ── About block ──────────────────────────────────────────────────────────────

// The daemon emits confidence either as a float or as a label (low/medium/high).
function convictionLabel(confidence: unknown): string | null {
  if (typeof confidence === "string" && confidence.trim()) {
    const level = confidence.trim().toLowerCase();
    if (level === "high") return "high conviction";
    if (level === "medium" || level === "moderate") return "moderate";
    if (level === "low") return "low";
    return level.replaceAll("_", " ");
  }
  if (typeof confidence === "number") {
    if (confidence >= 0.7) return "high conviction";
    if (confidence >= 0.45) return "moderate";
    return "low";
  }
  return null;
}

type IntelQuery = ReturnType<typeof useCompanyIntelligence>;

function AboutBlock({ symbol, intelQuery }: { symbol: string; intelQuery: IntelQuery }) {
  if (intelQuery.isLoading) {
    return (
      <Card>
        <CardHeader><CardTitle>About</CardTitle></CardHeader>
        <CardBody><p className="text-sm text-faint">Loading…</p></CardBody>
      </Card>
    );
  }

  const company = intelQuery.data?.companies?.find((c) => c.symbol === symbol);

  if (!company) return null;

  const brief = company.brief;
  const conviction = convictionLabel(
    company.selection_confidence_label ??
      brief.selection_view?.confidence ??
      company.selection_confidence,
  );
  const thesisStatus = company.thesis_status
    ? company.thesis_status.replaceAll("_", " ")
    : null;

  return (
    <Card>
      <CardHeader>
        <CardTitle>About</CardTitle>
        <span className="font-mono text-[10px] text-faint">{company.venue} · {company.depth || "unknown depth"}</span>
      </CardHeader>
      <CardBody className="space-y-3">
        {company.business_summary ? (
          <p className="max-w-3xl text-sm leading-relaxed text-muted">{company.business_summary}</p>
        ) : null}

        {(thesisStatus || brief.company_thesis?.summary) ? (
          <div className="rounded-md border border-hairline bg-panel/35 px-3 py-2.5">
            <div className="mb-1.5 flex flex-wrap items-center gap-2">
              <span className="font-mono text-[9px] uppercase tracking-[0.14em] text-faint">Thesis</span>
              {thesisStatus ? (
                <Badge tone={thesisBadgeTone(company.thesis_status)}>{thesisStatus}</Badge>
              ) : null}
              {conviction ? (
                <Badge tone="accent">{conviction}</Badge>
              ) : null}
            </div>
            {brief.company_thesis?.summary ? (
              <p className="text-sm leading-relaxed text-muted">{brief.company_thesis.summary}</p>
            ) : company.summary ? (
              <p className="text-sm leading-relaxed text-muted">{company.summary}</p>
            ) : null}
          </div>
        ) : null}

        <div className="flex flex-wrap items-center gap-x-4 gap-y-1 font-mono text-[9px] text-faint">
          <span>{company.source_count} sources</span>
          {company.coverage ? <span>coverage: {company.coverage}</span> : null}
          {company.catalyst_count > 0 ? <span className="text-gain">{company.catalyst_count} catalyst{company.catalyst_count > 1 ? "s" : ""}</span> : null}
          {company.risk_count > 0 ? <span className="text-loss">{company.risk_count} risk{company.risk_count > 1 ? "s" : ""}</span> : null}
          {company.stale_market ? <span className="text-warn">stale market data</span> : null}
        </div>
      </CardBody>
    </Card>
  );
}

function thesisBadgeTone(status: string): "gain" | "loss" | "warn" | "accent" | "muted" {
  const v = (status ?? "").toLowerCase();
  if (v.includes("construct") || v.includes("positive") || v.includes("bull")) return "gain";
  if (v.includes("caution") || v.includes("negative") || v.includes("bear")) return "loss";
  if (v.includes("watch") || v.includes("mixed")) return "warn";
  return v === "unknown" ? "muted" : "accent";
}
