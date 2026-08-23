import {
  ArrowRight,
  Globe2,
  Map,
  Route,
  Wallet,
} from "lucide-react";
import { useMemo } from "react";
import { ActionChip } from "@/components/action-chip";
import { EquityChart } from "@/components/charts/equity-chart";
import { PosturePanel } from "@/components/intelligence/posture-panel";
import type { PageKey } from "@/components/layout/app-shell";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardBody, CardHeader, CardTitle } from "@/components/ui/card";
import { usePlans } from "@/hooks/use-desk-api";
import { useWorldIntelligence } from "@/hooks/use-intelligence";
import { decisionKind, mergeDecisions } from "@/lib/classify";
import { formatAgo, formatPct, formatUsd, signedClass } from "@/lib/format";
import {
  companyDisplayName,
  conditionLabel,
  grossModeLabel,
  humanToken,
  netBiasLabel,
  plainMarketLanguage,
  venueLabel,
} from "@/lib/humanize";
import {
  decisionProvenance,
  provenanceLabel,
  provenanceTone,
} from "@/lib/provenance";
import type {
  DecisionRow,
  IntelligenceEvent,
  PlansPayload,
  Snapshot,
} from "@/lib/types";
import { cn } from "@/lib/utils";

type Props = {
  snapshot: Snapshot;
  onSymbol: (symbol: string) => void;
  onPage: (page: PageKey) => void;
};

export function OverviewPage({ snapshot, onSymbol, onPage }: Props) {
  const worldQuery = useWorldIntelligence(30);
  const plansQuery = usePlans();
  const world = worldQuery.data;
  const change = useMemo(
    () =>
      (world?.events ?? []).find((event) => event.changes.length || event.status === "error") ?? null,
    [world?.events],
  );
  const latestAction = useMemo(() => latestMaterialDecision(snapshot), [snapshot]);
  const holdings = (snapshot.portfolio.holdings ?? []).filter(
    (holding) => Math.abs(holding.quantity) > 1e-9,
  );

  return (
    <div className="grid gap-4 pb-8 xl:gap-5">
      {worldQuery.isPending && !world ? (
        <PostureLoading />
      ) : worldQuery.error && !world ? (
        <Card><CardBody><p className="text-sm text-warn">The current global outlook is temporarily unavailable.</p></CardBody></Card>
      ) : (
        <PosturePanel posture={world?.current.posture} digest={world?.current.digest} />
      )}

      <OperatingAlert snapshot={snapshot} />

      <section className="grid items-start gap-4 min-[1180px]:grid-cols-[minmax(0,1.35fr)_minmax(320px,0.65fr)]">
        <div className="grid min-w-0 gap-4">
          <DecisionPanel decision={latestAction} companyMap={snapshot.company_map} onSymbol={onSymbol} onPage={onPage} />
          <Card>
            <CardHeader>
              <CardTitle>What changed</CardTitle>
              <Button variant="ghost" size="sm" onClick={() => onPage("today")}>
                Markets <ArrowRight className="size-3.5" />
              </Button>
            </CardHeader>
            <CardBody className="p-0">
              {worldQuery.isPending && !world ? (
                <p className="px-4 py-6 text-sm text-faint">Loading recent changes…</p>
              ) : worldQuery.error && !world ? (
                <p className="px-4 py-6 text-sm text-warn">Recent world updates are unavailable.</p>
              ) : change ? (
                <ChangeRow event={change} />
              ) : (
                <p className="px-4 py-6 text-sm text-faint">No observed change in the current window.</p>
              )}
            </CardBody>
          </Card>
        </div>
        <div className="grid min-w-0 gap-4">
          <MoneyPulse snapshot={snapshot} holdings={holdings.length} onOpen={() => onPage("portfolio")} />
          <PositionPlansPulse
            plans={plansQuery.data}
            pending={plansQuery.isPending}
            error={plansQuery.error}
            companyMap={snapshot.company_map}
            onOpen={() => onPage("portfolio")}
          />
        </div>
      </section>
    </div>
  );
}

function PostureLoading() {
  return (
    <Card className="min-h-56" role="status" aria-label="Loading current outlook">
      <span className="sr-only">Loading current outlook</span>
      <CardBody className="grid gap-5 p-6 min-[1180px]:grid-cols-[minmax(0,1fr)_280px]">
        <div className="space-y-4">
          <div className="h-2 w-44 animate-pulse rounded bg-hairline motion-reduce:animate-none" />
          <div className="h-8 w-72 animate-pulse rounded bg-line motion-reduce:animate-none" />
          <div className="h-16 max-w-3xl animate-pulse rounded bg-hairline motion-reduce:animate-none" />
        </div>
        <div className="h-36 animate-pulse rounded-lg bg-hairline motion-reduce:animate-none" />
      </CardBody>
    </Card>
  );
}

function OperatingAlert({ snapshot }: { snapshot: Snapshot }) {
  const alerts = operatingAlerts(snapshot);
  if (!alerts.length) return null;
  const tone = alerts.some((item) => item.tone === "loss") ? "loss" : "warn";
  return (
    <section
      className={cn(
        "rounded-lg border px-4 py-3",
        tone === "loss" ? "border-loss/30 bg-loss/5" : "border-warn/30 bg-warn/5",
      )}
    >
      <div className="grid gap-2">
        {alerts.map((item) => (
          <div key={item.title}>
            <p className={cn("text-sm font-semibold", item.tone === "loss" ? "text-loss" : "text-warn")}>
              {item.title}
            </p>
            <p className="mt-0.5 text-sm text-muted">{item.detail}</p>
          </div>
        ))}
      </div>
    </section>
  );
}

function operatingAlerts(snapshot: Snapshot): Array<{ title: string; detail: string; tone: "loss" | "warn" }> {
  const alerts: Array<{ title: string; detail: string; tone: "loss" | "warn" }> = [];
  if (snapshot.kill_active) {
    alerts.push({
      title: "Kill switch is on",
      detail: "The paper portfolio will not change until it is turned off.",
      tone: "loss",
    });
  }
  if (snapshot.daemon.alive === false) {
    alerts.push({
      title: "Casys process not detected",
      detail: "The paper portfolio will not change until the process is running again.",
      tone: "warn",
    });
  }
  const marketOpen = snapshot.open_venues_list.some(Boolean);
  const stale = Object.keys(snapshot.stale_market_data ?? {}).length > 0;
  if (marketOpen && stale) {
    alerts.push({
      title: "Quotes need a refresh",
      detail: "A market is open, but Casys will not trade an affected company until its quote is fresh.",
      tone: "warn",
    });
  }
  return alerts;
}

function latestMaterialDecision(snapshot: Snapshot): DecisionRow | undefined {
  const merged = mergeDecisions(snapshot.decisions, snapshot.recent_decisions);
  const material = merged.filter((row) => {
    const kind = decisionKind(row);
    return kind !== "hold" && kind !== "quiet";
  });
  return material[0] ?? merged[0];
}

function DecisionPanel({
  decision,
  companyMap,
  onSymbol,
  onPage,
}: {
  decision?: DecisionRow;
  companyMap: Record<string, string>;
  onSymbol: (symbol: string) => void;
  onPage: (page: PageKey) => void;
}) {
  return (
    <Card>
      <CardHeader>
        <CardTitle>Latest action</CardTitle>
        <Button variant="ghost" size="sm" onClick={() => onPage("decisions")}>
          Activity <ArrowRight className="size-3.5" />
        </Button>
      </CardHeader>
      <CardBody className="p-0">
        {decision ? (
          <DecisionRowButton row={decision} companyMap={companyMap} onSymbol={onSymbol} />
        ) : (
          <p className="px-4 py-8 text-sm text-faint">No recent decision has been recorded.</p>
        )}
      </CardBody>
    </Card>
  );
}

function DecisionRowButton({
  row,
  companyMap,
  onSymbol,
}: {
  row: DecisionRow;
  companyMap: Record<string, string>;
  onSymbol: (symbol: string) => void;
}) {
  const provenance = decisionProvenance(row);
  const kind = decisionKind(row);
  return (
    <button
      type="button"
      onClick={() => row.symbol && onSymbol(row.symbol)}
      className="group grid w-full grid-cols-[minmax(0,1fr)_auto] gap-4 px-4 py-3 text-left transition-colors hover:bg-panel-hover focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-accent/50"
    >
      <div className="min-w-0">
        <div className="flex flex-wrap items-center gap-2">
          <span className="text-sm font-semibold text-fg">{companyDisplayName(companyMap, row.symbol)}</span>
          {row.symbol ? <span className="font-mono text-[10px] text-faint">{row.symbol}</span> : null}
          <ActionChip action={row.action} />
          <Badge tone={provenanceTone(provenance)}>{provenanceLabel(provenance)}</Badge>
          {kind === "hold" ? null : (
            <Badge tone={kind === "exec" ? "gain" : kind === "risk" ? "loss" : "muted"}>
              {outcomeLabel(kind)}
            </Badge>
          )}
        </div>
        <p className="mt-1.5 text-sm leading-6 text-muted">
          {decisionSummary(row)}
        </p>
      </div>
      <span className="pt-0.5 font-mono text-[10px] text-faint">{formatAgo(row.cycle_ts || row.ts)}</span>
    </button>
  );
}

function decisionSummary(row: DecisionRow): string {
  const action = String(row.action ?? "").toUpperCase();
  const provenance = decisionProvenance(row);
  if (provenance === "planned") {
    if (row.executed && action === "BUY") {
      return "A previously recorded entry plan executed; no new AI review was made at execution time.";
    }
    if (row.executed && (action === "SELL" || action === "CLOSE")) {
      return "A previously recorded exit plan executed; no new AI review was made at execution time.";
    }
    if (action === "BUY") {
      return "A previously recorded entry plan produced this action; no new AI review was made.";
    }
    if (action === "SELL" || action === "CLOSE") {
      return "A previously recorded exit plan produced this action; no new AI review was made.";
    }
    return "A previously recorded plan produced this action; no new AI review was made.";
  }
  if (provenance === "recorded") {
    if (row.executed && action === "BUY") {
      return "A purchase is recorded. This record does not confirm who authored the decision.";
    }
    if (row.executed && (action === "SELL" || action === "CLOSE")) {
      return "A position reduction is recorded. This record does not confirm who authored the decision.";
    }
    if (action === "HOLD") {
      return "No portfolio change was recorded. This record does not confirm who authored the decision.";
    }
    if (action === "BUY") return "A buy decision was recorded, without confirmed authorship or execution.";
    if (action === "SELL" || action === "CLOSE") {
      return "A sell decision was recorded, without confirmed authorship or execution.";
    }
    return "A portfolio decision was recorded, but its author is not confirmed.";
  }
  if (row.executed && action === "BUY") return "Casys recorded a purchase after its entry conditions were met.";
  if (row.executed && (action === "SELL" || action === "CLOSE")) return "Casys recorded a reduction in the position.";
  if (action === "HOLD" && provenance === "system") return "An automatic check recorded no portfolio action; this is not an AI opinion.";
  if (action === "HOLD") return "Casys reviewed this company and kept the portfolio unchanged.";
  if (action === "BUY") return "Casys recorded a buy decision; no execution is shown here.";
  if (action === "SELL" || action === "CLOSE") return "Casys recorded a sell decision; no execution is shown here.";
  return "Casys recorded a portfolio decision for this company.";
}

function outcomeLabel(kind: ReturnType<typeof decisionKind>): string {
  if (kind === "exec") return "Execution recorded";
  if (kind === "risk") return "Blocked by risk";
  if (kind === "stale") return "Stale data";
  if (kind === "armed") return "Ready condition";
  if (kind === "plan") return "Plan created";
  if (kind === "watch") return "Watch created";
  if (kind === "quiet") return "System pause";
  return "Decision recorded";
}

function MoneyPulse({
  snapshot,
  holdings,
  onOpen,
}: {
  snapshot: Snapshot;
  holdings: number;
  onOpen: () => void;
}) {
  const portfolio = snapshot.portfolio;
  return (
    <Card>
      <CardHeader>
        <div className="flex items-center gap-2">
          <Wallet className="size-4 text-accent" aria-hidden="true" />
          <CardTitle>Your money now</CardTitle>
        </div>
        <Button variant="ghost" size="sm" onClick={onOpen}>Open</Button>
      </CardHeader>
      <CardBody className="pb-3">
        <div className="flex items-end justify-between gap-3">
          <div>
            <p className="text-2xl font-semibold tracking-[-0.03em] text-fg">
              {formatUsd(portfolio.equity, 0)}
            </p>
            <p className={cn("mt-1 font-mono text-[11px]", signedClass(portfolio.total_return_pct))}>
              {formatPct(portfolio.total_return_pct)} reported return
            </p>
          </div>
          <p className="text-right text-xs text-dim">{holdings} open position{holdings === 1 ? "" : "s"}</p>
        </div>
        <div className="mt-3 h-20">
          <EquityChart points={snapshot.equity_series} minHeight={72} className="h-20" compact />
        </div>
      </CardBody>
    </Card>
  );
}

function PositionPlansPulse({
  plans,
  pending,
  error,
  companyMap,
  onOpen,
}: {
  plans?: PlansPayload;
  pending: boolean;
  error: unknown;
  companyMap: Record<string, string>;
  onOpen: () => void;
}) {
  const exits = new Set(
    plans?.exit_plans.rows.map((row) => row.symbol).filter(Boolean) ?? [],
  ).size;
  const armed = plans?.armed.rows.length ?? 0;
  const watches = new Set([
    ...(plans?.watches.rows.map((row) => row.symbol).filter(Boolean) ?? []),
    ...(plans?.exit_watches.rows.map((row) => row.symbol).filter(Boolean) ?? []),
  ]).size;
  const next = plans?.next_to_fire[0];
  const nextIsPortfolioReview = next?.kind === "wake";
  return (
    <Card>
      <CardHeader>
        <div className="flex items-center gap-2">
          <Route className="size-4 text-accent" aria-hidden="true" />
          <CardTitle>Position plans</CardTitle>
        </div>
        <Button variant="ghost" size="sm" onClick={onOpen}>Open</Button>
      </CardHeader>
      <CardBody>
        {error ? (
          <p className="text-sm text-warn">Position plans are temporarily unavailable.</p>
        ) : pending && !plans ? (
          <p className="text-sm text-faint">Checking current plans…</p>
        ) : (
          <div className="grid grid-cols-3 gap-3">
            <PlanFact value={exits} label="positions with an exit plan" />
            <PlanFact value={armed} label="ready conditions" />
            <PlanFact value={watches} label="companies watched" />
          </div>
        )}
        {next ? (
          <div className="mt-3 border-t border-hairline pt-3">
            <p className="font-mono text-[9px] uppercase tracking-[0.14em] text-faint">
              {nextIsPortfolioReview ? "Next scheduled check" : "Next watched condition"}
            </p>
            <p className="mt-1 text-sm font-medium text-fg">
              {nextIsPortfolioReview ? "Casys cycle" : companyDisplayName(companyMap, next.label)} · {timeUntil(next.countdown)}
            </p>
            <p className="mt-0.5 truncate text-xs text-dim">
              {nextIsPortfolioReview ? (
                "Casys will run its next scheduled check then."
              ) : (
                <>
                  <span className="font-mono text-[10px] text-faint">{next.label}</span>
                  <span className="mx-1.5 text-faint">·</span>
                  {conditionLabel(next.detail)}
                </>
              )}
            </p>
          </div>
        ) : null}
      </CardBody>
    </Card>
  );
}

function timeUntil(countdown: string): string {
  const value = countdown.trim();
  if (!value || value === "—") return "time not recorded";
  if (value.toLowerCase() === "expired") return "overdue";
  if (value.toLowerCase() === "now") return "now";
  return /^in\b/i.test(value) ? value : `in ${value}`;
}

function PlanFact({ value, label }: { value: number; label: string }) {
  return (
    <div>
      <p className="text-xl font-semibold tabular text-fg">{value}</p>
      <p className="mt-0.5 text-[11px] leading-tight text-dim">{label}</p>
    </div>
  );
}

function ChangeRow({ event }: { event: IntelligenceEvent }) {
  const failure = event.status === "error";
  return (
    <div className="px-4 py-3">
      <div className="flex flex-wrap items-center gap-2">
        {event.layer === "world" ? <Globe2 className="size-3.5 text-accent" /> : <Map className="size-3.5 text-accent" />}
        <p className="text-sm font-medium">{changeTitle(event.title)}</p>
        {event.venue ? <Badge>{venueLabel(event.venue)}</Badge> : null}
        {failure ? <Badge tone="loss">Refresh issue</Badge> : <Badge tone="warn">{event.changes.length} update{event.changes.length === 1 ? "" : "s"}</Badge>}
        <span className="ml-auto font-mono text-[9px] text-faint">{formatAgo(event.as_of)}</span>
      </div>
      {event.summary ? <p className="mt-1.5 line-clamp-2 text-xs leading-5 text-dim">{plainMarketLanguage(event.summary)}</p> : null}
    </div>
  );
}

function changeTitle(value: string): string {
  return value
    .split("·")
    .map((part) => {
      const token = part.trim().toLowerCase();
      if (["risk_off", "cautious", "normal", "watch", "selective", "defensive"].includes(token)) {
        return grossModeLabel(token);
      }
      if (["short", "neutral", "long"].includes(token)) return netBiasLabel(token);
      return humanToken(part.trim());
    })
    .join(" · ");
}
