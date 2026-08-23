import { ArrowLeft } from "lucide-react";
import { ActionChip } from "@/components/action-chip";
import { PriceChart } from "@/components/charts/price-chart";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardBody, CardHeader, CardTitle } from "@/components/ui/card";
import { useSymbol, useSymbolBars } from "@/hooks/use-desk-api";
import { useCompanyIntelligence } from "@/hooks/use-intelligence";
import { formatAgo, formatDayTime, formatMarketPrice, formatQty, formatUsd, signedClass } from "@/lib/format";
import { conditionLabel, decisionActionLabel, humanToken, venueLabel } from "@/lib/humanize";
import { decisionProvenance, provenanceDetail, provenanceLabel, provenanceTone } from "@/lib/provenance";
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
  const pnl = num(
    holding.unrealized_pnl_after_broker_fees ??
      holding.unrealized_pnl_net ??
      holding.unrealized_pnl,
  );
  const stale = Boolean(data?.stale);
  const whyProvenance = data?.why ? decisionProvenance(data.why) : null;

  if (query.isPending && !data) {
    return <SymbolDetailLoading symbol={symbol} onBack={onBack} />;
  }
  if (query.error && !data) {
    return <SymbolDetailError symbol={symbol} error={query.error} onBack={onBack} />;
  }

  return (
    <div className="grid gap-4">
      <div className="flex items-start justify-between gap-4">
        <div>
          <Button variant="ghost" size="sm" onClick={onBack} className="-ml-2 mb-2">
            <ArrowLeft className="size-3.5" />
            Back
          </Button>
          <h2 className="text-3xl font-semibold tracking-tight">{data?.name || symbol}</h2>
          <p className="mt-1 font-mono text-[11px] text-dim">{symbol}</p>
        </div>
        <div className="flex gap-2">
          {stale ? <Badge tone="warn">Stale market data</Badge> : null}
          {qty != null && qty !== 0 ? (
            <Badge tone={qty < 0 ? "loss" : "gain"}>{qty < 0 ? "Short position" : "Long position"}</Badge>
          ) : (
            <Badge>No position</Badge>
          )}
        </div>
      </div>
      {query.error ? (
        <p className="text-sm text-loss">{query.error instanceof Error ? query.error.message : String(query.error)}</p>
      ) : null}

      <Card>
        <CardHeader>
          <CardTitle>
            {whyProvenance === "ai"
              ? "Why Casys made its latest call"
              : whyProvenance === "system"
                ? "Why the system recorded this outcome"
                : "Latest recorded outcome"}
          </CardTitle>
          {data?.why?.cycle_ts ? (
            <span className="font-mono text-[10px] text-faint">{formatDayTime(data.why.cycle_ts)}</span>
          ) : null}
        </CardHeader>
        <CardBody>
          {data?.why ? (
            <div>
              <div className="mb-2 flex items-center gap-2">
                <ActionChip action={data.why.action} />
                {whyProvenance ? <Badge tone={provenanceTone(whyProvenance)}>{provenanceLabel(whyProvenance)}</Badge> : null}
                {whyProvenance === "ai" && data.why.confidence != null ? (
                  <span className="text-xs text-dim">{Math.round(Number(data.why.confidence) * 100)}% confidence</span>
                ) : null}
              </div>
              <p className="border-l-2 border-accent/70 pl-3 text-sm leading-relaxed text-muted">
                {data.why.rationale || "—"}
              </p>
              {whyProvenance && whyProvenance !== "ai" ? (
                <p className="mt-2 text-xs leading-5 text-dim">{provenanceDetail(whyProvenance)}</p>
              ) : null}
            </div>
          ) : (
            <p className="text-sm italic text-faint">No reasoning recorded.</p>
          )}
        </CardBody>
      </Card>

      <section className="grid grid-cols-2 gap-3 min-[900px]:grid-cols-4">
        <Mini label="Shares held" value={qty != null ? formatQty(qty) : "—"} />
        <Mini label="Average purchase price" value={formatMarketPrice(num(holding.avg_price), 2)} />
        <Mini label="Latest market price" value={formatMarketPrice(data?.last_price ?? num(holding.last_price), 2)} />
        <Mini label="Unrealised gain/loss" value={formatUsd(pnl, 0)} className={signedClass(pnl)} />
      </section>

      <PriceCard bars={bars} asOf={barsAsOf} n={bars.length} pending={barsQuery.isPending} error={barsQuery.error} />

      <AboutBlock symbol={symbol} intelQuery={intelQuery} />

      <section className="grid grid-cols-1 gap-4 min-[900px]:grid-cols-2">
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
                  {positionSideLabel(row.side)} · protection price {formatMarketPrice(row.stop_price, 2)} · profit target{" "}
                  {row.take_profit_label ?? "—"}
                </p>
                <p className="font-mono text-[11px] text-faint">{row.protect_label || row.review_label || ""}</p>
              </div>
            ))}
            {(data?.armed ?? []).map((row) => (
              <div key={`${row.symbol}-armed`} className="rounded-md border border-hairline px-3 py-2">
                <p className="text-sm text-muted">
                  Ready condition: {decisionActionLabel(row.action)} · {row.countdown}
                </p>
              </div>
            ))}
          </CardBody>
        </Card>
        <Card>
          <CardHeader>
            <CardTitle>Watch</CardTitle>
          </CardHeader>
          <CardBody className="space-y-2">
            {(data?.watches ?? []).concat(data?.exit_watches ?? []).length === 0 ? (
              <p className="text-sm text-faint">No active watch on this symbol.</p>
            ) : (
              (data?.watches ?? []).concat(data?.exit_watches ?? []).map((watch) => (
                <div key={`${watch.symbol}-${watch.condition_label}`} className="rounded-md border border-hairline px-3 py-2">
                  <p className="text-sm text-muted">{conditionLabel(watch.condition_label)}</p>
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
          <CardTitle>Decisions for {data?.name || symbol}</CardTitle>
          <span className="font-mono text-[10px] text-faint">{data?.decisions.length ?? 0}</span>
        </CardHeader>
        <div>
          {(data?.decisions ?? []).map((row, index) => (
            <div key={`${row.cycle_ts}-${index}`} className="border-b border-hairline px-4 py-3 last:border-0">
              <div className="flex flex-wrap items-center gap-2">
                <span className="font-mono text-[11px] text-faint">{formatDayTime(row.cycle_ts)}</span>
                <ActionChip action={row.action} />
                <Badge tone={provenanceTone(decisionProvenance(row))}>{provenanceLabel(decisionProvenance(row))}</Badge>
                {decisionProvenance(row) === "ai" && row.confidence != null ? (
                  <span className="text-xs text-dim">{Math.round(Number(row.confidence) * 100)}% confidence</span>
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

function PriceCard({
  bars,
  asOf,
  n,
  pending,
  error,
}: {
  bars: PriceBar[];
  asOf: string | null;
  n: number;
  pending: boolean;
  error: unknown;
}) {
  return (
    <Card>
      <CardHeader>
        <CardTitle>{n > 0 ? `Price — last ${n} sessions` : "Price history"}</CardTitle>
        {asOf ? (
          <span className="font-mono text-[10px] text-faint">as of {asOf}</span>
        ) : null}
      </CardHeader>
      <CardBody>
        {error && !bars.length ? (
          <div className="py-4">
            <p className="text-sm text-warn">Price history is temporarily unavailable.</p>
            <details className="mt-2 text-xs text-dim">
              <summary className="cursor-pointer">Technical details</summary>
              <p className="mt-1 break-words font-mono text-[10px] text-faint">{error instanceof Error ? error.message : String(error)}</p>
            </details>
          </div>
        ) : pending && !bars.length ? (
          <div className="h-64 animate-pulse rounded-md bg-hairline motion-reduce:animate-none" role="status" aria-label="Loading price history">
            <span className="sr-only">Loading price history</span>
          </div>
        ) : bars.length > 0 ? (
          <PriceChart bars={bars} />
        ) : (
          <p className="py-4 text-sm text-faint">No price history available.</p>
        )}
      </CardBody>
    </Card>
  );
}

function SymbolDetailError({ symbol, error, onBack }: { symbol: string; error: unknown; onBack: () => void }) {
  return (
    <div className="grid gap-4">
      <Button variant="ghost" size="sm" onClick={onBack} className="w-fit -ml-2">
        <ArrowLeft className="size-3.5" />
        Back
      </Button>
      <Card>
        <CardHeader>
          <CardTitle>Company details unavailable</CardTitle>
          <span className="font-mono text-[10px] text-faint">{symbol}</span>
        </CardHeader>
        <CardBody>
          <p className="text-sm text-muted">Casys could not load this company record. No portfolio state is inferred from that failure.</p>
          <details className="mt-3 text-xs text-dim">
            <summary className="cursor-pointer">Technical details</summary>
            <p className="mt-1 break-words font-mono text-[10px] text-faint">{error instanceof Error ? error.message : String(error)}</p>
          </details>
        </CardBody>
      </Card>
    </div>
  );
}

function SymbolDetailLoading({ symbol, onBack }: { symbol: string; onBack: () => void }) {
  return (
    <div className="grid gap-4" role="status" aria-label={`Loading ${symbol}`}>
      <span className="sr-only">Loading {symbol}</span>
      <div>
        <Button variant="ghost" size="sm" onClick={onBack} className="-ml-2 mb-2">
          <ArrowLeft className="size-3.5" />
          Back
        </Button>
        <div className="h-8 w-64 animate-pulse rounded bg-line motion-reduce:animate-none" />
        <p className="mt-2 font-mono text-[11px] text-dim">{symbol}</p>
      </div>
      <Card className="h-40 animate-pulse bg-panel/60 motion-reduce:animate-none" />
      <section className="grid grid-cols-2 gap-3 min-[1180px]:grid-cols-4">
        {Array.from({ length: 4 }, (_, index) => (
          <Card key={index} className="h-24 animate-pulse bg-panel/60 motion-reduce:animate-none" />
        ))}
      </section>
      <Card className="h-80 animate-pulse bg-panel/60 motion-reduce:animate-none" />
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

function num(value: unknown): number | undefined {
  return typeof value === "number" && Number.isFinite(value) ? value : undefined;
}

function positionSideLabel(value: string | null | undefined): string {
  const key = String(value ?? "").toUpperCase();
  if (key === "L" || key === "LONG") return "Long position";
  if (key === "S" || key === "SHORT") return "Short position";
  return "Position";
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

  if (!company && intelQuery.error) {
    return (
      <Card>
        <CardHeader><CardTitle>Company research unavailable</CardTitle></CardHeader>
        <CardBody>
          <p className="text-sm text-muted">Casys could not load the research record for this company.</p>
          <details className="mt-3 text-xs text-dim">
            <summary className="cursor-pointer">Technical details</summary>
            <p className="mt-1 break-words font-mono text-[10px] text-faint">
              {intelQuery.error instanceof Error ? intelQuery.error.message : String(intelQuery.error)}
            </p>
          </details>
        </CardBody>
      </Card>
    );
  }
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
        <span className="font-mono text-[10px] text-faint">{venueLabel(company.venue)} · {analysisDepthLabel(company.depth)}</span>
      </CardHeader>
      <CardBody className="space-y-3">
        {intelQuery.error ? (
          <p className="text-xs text-warn">Showing the last recorded research; the latest refresh was unavailable.</p>
        ) : null}
        {company.business_summary ? (
          <p className="max-w-3xl text-sm leading-relaxed text-muted">{company.business_summary}</p>
        ) : null}

        {(thesisStatus || brief.company_thesis?.summary) ? (
          <div className="rounded-md border border-hairline bg-panel/35 px-3 py-2.5">
            <div className="mb-1.5 flex flex-wrap items-center gap-2">
              <span className="font-mono text-[9px] uppercase tracking-[0.14em] text-faint">Company view</span>
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
          {company.coverage ? <span>{sourceCoverageLabel(company.coverage)}</span> : null}
          {company.catalyst_count > 0 ? <span className="text-gain">{company.catalyst_count} catalyst{company.catalyst_count > 1 ? "s" : ""}</span> : null}
          {company.risk_count > 0 ? <span className="text-loss">{company.risk_count} risk{company.risk_count > 1 ? "s" : ""}</span> : null}
          {company.stale_market ? <span className="text-warn">stale market data</span> : null}
        </div>
      </CardBody>
    </Card>
  );
}

function analysisDepthLabel(value?: string | null): string {
  const key = String(value ?? "").toLowerCase();
  if (key === "deep") return "In-depth review";
  if (key === "screen") return "Quick review";
  return value ? humanToken(value) : "Review type unknown";
}

function sourceCoverageLabel(value?: string | null): string {
  const key = String(value ?? "").toLowerCase();
  if (["complete", "full"].includes(key)) return "Complete sources";
  if (["partial", "incomplete"].includes(key)) return "Partial sources";
  if (["missing", "none"].includes(key)) return "Sources missing";
  return value ? humanToken(value) : "Source coverage unknown";
}

function thesisBadgeTone(status: string): "gain" | "loss" | "warn" | "accent" | "muted" {
  const v = (status ?? "").toLowerCase();
  if (v.includes("construct") || v.includes("positive") || v.includes("bull")) return "gain";
  if (v.includes("caution") || v.includes("negative") || v.includes("bear")) return "loss";
  if (v.includes("watch") || v.includes("mixed")) return "warn";
  return v === "unknown" ? "muted" : "accent";
}
