import { Activity } from "lucide-react";
import { useMemo, useState } from "react";
import { ActionChip } from "@/components/action-chip";
import { ActivityChart } from "@/components/charts/activity-chart";
import { LiveWorkStatus } from "@/components/live-work-status";
import { Badge } from "@/components/ui/badge";
import { Card, CardBody, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { useDecisions, usePlans } from "@/hooks/use-desk-api";
import { formatAgo, formatMarketPrice, formatQty } from "@/lib/format";
import {
  companyDisplayName,
  conditionLabel,
  decisionActionLabel,
} from "@/lib/humanize";
import {
  decisionProvenance,
  provenanceLabel,
  provenanceTone,
} from "@/lib/provenance";
import type { LedgerRow, PlansPayload, Snapshot } from "@/lib/types";
import { cn } from "@/lib/utils";

const FILTERS = ["all", "trades", "watches", "protections", "plans", "blocked"] as const;
const FILTER_LABELS: Record<(typeof FILTERS)[number], string> = {
  all: "All",
  trades: "Trades",
  watches: "Watches",
  protections: "Protections",
  plans: "Plans",
  blocked: "Blocked",
};

const SOURCE_VIEWS = ["all", "executed", "ai", "planned", "system", "recorded"] as const;
const SOURCE_LABELS: Record<(typeof SOURCE_VIEWS)[number], string> = {
  all: "All sources",
  executed: "Executed",
  ai: "Casys AI",
  planned: "Planned execution",
  system: "Automatic",
  recorded: "Unconfirmed",
};

const ACTIVITY_SCOPES = ["important", "all"] as const;
const ACTIVITY_SCOPE_LABELS: Record<(typeof ACTIVITY_SCOPES)[number], string> = {
  important: "Important",
  all: "Full history",
};

type Props = {
  snapshot?: Snapshot;
  onSymbol: (symbol: string) => void;
};

export function DecisionsPage({ snapshot, onSymbol }: Props) {
  const [filter, setFilter] = useState<(typeof FILTERS)[number]>("all");
  const [view, setView] = useState<(typeof SOURCE_VIEWS)[number]>("all");
  const [scope, setScope] = useState<(typeof ACTIVITY_SCOPES)[number]>("important");
  const [query, setQuery] = useState("");
  const [selected, setSelected] = useState<string | null>(null);
  const decisions = useDecisions("all");
  const plans = usePlans();
  const rows = decisions.data?.rows ?? [];
  const companyMap = snapshot?.company_map ?? {};
  const importantRows = useMemo(() => rows.filter(isMaterialActivity), [rows]);
  const visible = useMemo(() => {
    const scoped = scope === "important" ? importantRows : rows;
    return scoped
      .filter((row) => {
        if (!matchesDecisionFilter(row, filter)) return false;
        const provenance = decisionProvenance(row);
        if (view === "executed" && row.executed !== true) return false;
        if (view === "ai" && provenance !== "ai") return false;
        if (view === "planned" && provenance !== "planned") return false;
        if (view === "system" && provenance !== "system") return false;
        if (view === "recorded" && provenance !== "recorded") return false;
        if (!query.trim()) return true;
        const hay = `${decisionDisplayName(companyMap, row)} ${row.symbol} ${row.action} ${row.rationale} ${row.effect_text} ${row.llm_model}`.toLowerCase();
        return hay.includes(query.trim().toLowerCase());
      })
      .sort((a, b) => timestampOf(b) - timestampOf(a));
  }, [companyMap, filter, importantRows, query, rows, scope, view]);
  const active = visible.find((row) => row.row_key === selected) ?? visible[0];
  const counts = useMemo(() => {
    return {
      executed: rows.filter((row) => row.executed === true).length,
      watched: rows.filter((row) => row.effect_kind === "watch" || runtimeFlag(row, "indicator_watch_created")).length,
      protected: rows.filter((row) => runtimeFlag(row, "exit_update_applied")).length,
      attention: rows.filter((row) => isRiskDecision(row) || isStaleDecision(row)).length,
    };
  }, [rows]);
  const filterCounts = useMemo(() => {
    const scoped = scope === "important" ? importantRows : rows;
    return Object.fromEntries(FILTERS.map((item) => [item, scoped.filter((row) => matchesDecisionFilter(row, item)).length])) as Record<(typeof FILTERS)[number], number>;
  }, [importantRows, rows, scope]);

  return (
    <div className="grid gap-5 pb-8">
      <LiveWorkStatus activity={snapshot?.intelligence_activity} />
      <section className="grid items-start gap-4 min-[1180px]:grid-cols-[minmax(320px,0.72fr)_minmax(0,1.28fr)]">
        <Card className="min-w-0 overflow-hidden">
          <CardHeader className="items-start">
            <div>
              <h2 className="sr-only">Recorded activity</h2>
              {decisions.data ? (
                <div className="flex flex-wrap gap-1.5">
                  <SummaryBadge label="Trades" value={counts.executed} tone="gain" />
                  <SummaryBadge label="Watches" value={counts.watched} tone="accent" />
                  <SummaryBadge label="Protection updates" value={counts.protected} tone="accent" />
                  {counts.attention ? <SummaryBadge label="Attention" value={counts.attention} tone="warn" /> : null}
                </div>
              ) : decisions.isPending ? (
                <Badge>Loading activity…</Badge>
              ) : decisions.error ? (
                <Badge tone="warn">Activity unavailable</Badge>
              ) : null}
            </div>
            <fieldset className="flex shrink-0 gap-1">
              <legend className="sr-only">Choose activity scope</legend>
              {ACTIVITY_SCOPES.map((item) => (
                <Chip
                  key={item}
                  active={scope === item}
                  onClick={() => setScope(item)}
                  label={ACTIVITY_SCOPE_LABELS[item]}
                />
              ))}
            </fieldset>
          </CardHeader>
          <CardBody className="border-b border-hairline p-3">
            <div className="grid gap-2">
              <Input
                value={query}
                onChange={(event) => setQuery(event.target.value)}
                placeholder="Search a company or explanation…"
                aria-label="Search recorded decisions"
                className="w-full"
              />
              <fieldset className="flex flex-wrap gap-1">
                <legend className="sr-only">Filter by recorded consequence</legend>
                {FILTERS.map((item) => (
                  <Chip
                    key={item}
                    active={filter === item}
                    onClick={() => setFilter(item)}
                    label={`${FILTER_LABELS[item]} ${filterCounts[item]}`}
                  />
                ))}
              </fieldset>
              <details className="group">
                <summary className="cursor-pointer text-xs font-medium text-dim hover:text-fg focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent/50">
                  Filter by source{view !== "all" ? ` · ${SOURCE_LABELS[view]}` : ""}
                </summary>
                <fieldset className="mt-2 flex flex-wrap gap-1">
                  <legend className="sr-only">Filter by recorded source</legend>
                  {SOURCE_VIEWS.map((item) => (
                    <Chip key={item} active={view === item} onClick={() => setView(item)} label={SOURCE_LABELS[item]} />
                  ))}
                </fieldset>
              </details>
            </div>
          </CardBody>
          {decisions.error ? <div className="p-3"><ErrorText error={decisions.error} /></div> : null}
          <div
            className="max-h-[470px] overflow-y-auto"
            aria-label="Recorded decisions"
          >
            <p className="sr-only" aria-live="polite">{visible.length} decisions shown.</p>
            {decisions.isPending && !decisions.data ? (
              <p className="px-4 py-8 text-sm text-faint">Loading the decision record…</p>
            ) : null}
            {visible.map((row) => (
              <DecisionRecordRow
                key={row.row_key}
                row={row}
                companyName={decisionDisplayName(companyMap, row)}
                active={active?.row_key === row.row_key}
                onSelect={() => setSelected(row.row_key)}
              />
            ))}
            {!decisions.isPending && !decisions.error && visible.length === 0 ? (
              <div className="px-4 py-8 text-sm text-faint">
                <p>No recorded decision matches these filters.</p>
                {scope === "important" ? (
                  <button
                    type="button"
                    onClick={() => setScope("all")}
                    className="mt-2 font-medium text-accent hover:underline focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent/50"
                  >
                    Open full history
                  </button>
                ) : null}
              </div>
            ) : null}
          </div>
        </Card>

        {active ? (
          <DetailPanel
            row={active}
            companyName={decisionDisplayName(companyMap, active)}
            plans={plans.data}
            onSymbol={onSymbol}
          />
        ) : (
          <Card>
            <CardBody className="py-16 text-center text-sm text-faint">Select a decision to open its dossier.</CardBody>
          </Card>
        )}
      </section>

      <Card>
        <CardHeader>
          <div className="flex items-center gap-2">
            <Activity className="size-4 text-accent" aria-hidden="true" />
            <div>
              <CardTitle>Last 12 hours</CardTitle>
              <p className="mt-1 text-xs text-dim">Important outcomes stay prominent; routine checks are muted.</p>
            </div>
          </div>
        </CardHeader>
        <CardBody className="h-44 pt-3">
          {snapshot ? (
            <ActivityChart report={snapshot.decisions} recent={snapshot.recent_decisions} />
          ) : (
            <p className="py-12 text-center text-sm text-faint">Loading the recent activity timeline…</p>
          )}
        </CardBody>
      </Card>
    </div>
  );
}

function DecisionRecordRow({
  row,
  companyName,
  active,
  onSelect,
}: {
  row: LedgerRow;
  companyName: string;
  active: boolean;
  onSelect: () => void;
}) {
  const provenance = decisionProvenance(row);
  const cycleSummary = isCycleSummary(row);
  return (
    <button
      type="button"
      onClick={onSelect}
      aria-pressed={active}
      aria-controls="decision-dossier"
      className={cn(
        "grid w-full grid-cols-[minmax(0,1fr)_auto] gap-4 border-b border-hairline border-l-2 border-l-transparent px-4 py-3 text-left transition-colors last:border-b-0 hover:bg-panel-hover focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-accent/50",
        active && "border-l-accent bg-accent/[0.04]",
      )}
    >
      <div className="min-w-0">
        <div className="flex flex-wrap items-center gap-2">
          <span className="text-sm font-semibold text-fg">{companyName}</span>
          {!cycleSummary ? <span className="font-mono text-[10px] text-faint">{row.symbol}</span> : null}
          {cycleSummary ? <Badge>Cycle summary</Badge> : <ActionChip action={row.action_text} />}
          <Badge tone={provenanceTone(provenance)}>{provenanceLabel(provenance)}</Badge>
          {row.executed ? <Badge tone="gain">{isConfirmedExecution(row) ? "Trade confirmed" : "Trade recorded"}</Badge> : null}
        </div>
        <p className="mt-1.5 text-sm leading-6 text-muted">{recordSummary(row)}</p>
        {row.effect_text ? <p className="mt-1 text-xs text-dim">Outcome: {row.executed ? actionTaken(row) : plainEffect(row.effect_text, row.action_text || row.action)}</p> : null}
      </div>
      <div className="text-right">
        <p className="font-mono text-[10px] text-faint">{row.utc_text}</p>
        {confidenceLabel(row) ? <p className="mt-2 text-xs font-medium text-dim">{confidenceLabel(row)}</p> : null}
      </div>
    </button>
  );
}

function DetailPanel({
  row,
  companyName,
  plans,
  onSymbol,
}: {
  row: LedgerRow;
  companyName: string;
  plans?: PlansPayload;
  onSymbol: (symbol: string) => void;
}) {
  const provenance = decisionProvenance(row);
  const cycleSummary = isCycleSummary(row);
  const stages = decisionStages(row);
  const currentState = cycleSummary ? null : currentProtection(row, plans);
  return (
    <Card id="decision-dossier" className="h-fit min-w-0 min-[1180px]:sticky min-[1180px]:top-0">
      <CardHeader className="items-start">
        <div>
          <CardTitle>
            {companyName}
            {!cycleSummary ? <span className="ml-1 font-mono text-[10px] font-normal text-faint">{row.symbol}</span> : null}
          </CardTitle>
        </div>
        {!cycleSummary ? (
          <button
            type="button"
            className="text-xs font-medium text-accent hover:underline focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent/50"
            onClick={() => onSymbol(row.symbol)}
          >
            Open company
          </button>
        ) : null}
      </CardHeader>
      <CardBody>
        <div className="flex flex-wrap items-center justify-between gap-2">
          <div className="flex flex-wrap items-center gap-2">
            {cycleSummary ? <Badge>Cycle summary</Badge> : <ActionChip action={row.action_text} />}
            <Badge tone={provenanceTone(provenance)}>{provenanceLabel(provenance)}</Badge>
            {row.executed ? <Badge tone="gain">{isConfirmedExecution(row) ? "Trade confirmed" : "Trade recorded"}</Badge> : null}
          </div>
          <span className="font-mono text-[10px] text-faint">{row.cycle_ts ? formatAgo(row.cycle_ts) : row.utc_text}</span>
        </div>

        <ol className="mt-4 overflow-hidden rounded-lg border border-hairline bg-ink/35">
          {stages.map((stage, index) => (
            <DecisionStage key={stage.label} index={index + 1} label={stage.label} text={stage.text} last={index === stages.length - 1} />
          ))}
        </ol>

        {currentState ? (
          <div className="mt-4 rounded-lg border border-hairline bg-ink/35 px-4 py-3">
            <p className="font-mono text-[9px] uppercase tracking-[0.14em] text-faint">Currently recorded</p>
            <p className="mt-1 text-sm leading-5 text-muted">{currentState}</p>
            <p className="mt-1.5 text-[11px] leading-5 text-faint">
              Shown separately because this record does not confirm which decision created the current protection or watch.
            </p>
          </div>
        ) : null}

        <details className="mt-4 border-t border-hairline pt-3">
          <summary className="cursor-pointer text-xs font-medium text-dim hover:text-fg focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent/50">
            Technical record
          </summary>
          <dl className="mt-3 grid gap-2 rounded-md border border-hairline bg-ink/50 p-3 font-mono text-[10px] text-dim sm:grid-cols-2">
            <DetailFact label="Recorded source" value={row.source_text || "—"} compact />
            <DetailFact label="Decision source" value={row.decision_source || "—"} compact />
            <DetailFact label="Reason code" value={row.decision_reason_code || "—"} compact />
            {row.llm_model ? <DetailFact label="Model" value={row.llm_model} compact /> : null}
            <DetailFact label="Recorded at" value={row.cycle_ts ? formatAgo(row.cycle_ts) : row.utc_text} compact />
            {row.price != null ? <DetailFact label="Recorded market price" value={formatMarketPrice(row.price, 2)} compact /> : null}
            {row.qty != null ? <DetailFact label="Quantity" value={formatQty(row.qty)} compact /> : null}
            {row.rationale ? <DetailFact label="Original explanation" value={row.rationale} compact /> : null}
          </dl>
        </details>
      </CardBody>
    </Card>
  );
}

function DecisionStage({
  index,
  label,
  text,
  last,
}: {
  index: number;
  label: string;
  text: string;
  last: boolean;
}) {
  return (
    <li className="grid grid-cols-[28px_minmax(0,1fr)] gap-3 px-4 py-3">
      <div className="relative flex justify-center">
        <span className="relative z-10 grid size-5 place-items-center rounded-full border border-accent/35 bg-panel font-mono text-[9px] font-semibold text-accent">
          {index}
        </span>
        {!last ? <span className="absolute bottom-[-14px] top-5 w-px bg-hairline" aria-hidden="true" /> : null}
      </div>
      <div className="min-w-0 pb-0.5">
        <p className="font-mono text-[9px] uppercase tracking-[0.14em] text-faint">{label}</p>
        <p className="mt-1 text-sm leading-5 text-muted">{text}</p>
      </div>
    </li>
  );
}

function SummaryBadge({
  label,
  value,
  tone = "muted",
}: {
  label: string;
  value: number;
  tone?: "muted" | "accent" | "gain" | "warn";
}) {
  return <Badge tone={tone}>{value} {label}</Badge>;
}

function Chip({ active, onClick, label }: { active: boolean; onClick: () => void; label: string }) {
  return (
    <button
      type="button"
      onClick={onClick}
      aria-pressed={active}
      className={cn(
        "rounded-md px-2.5 py-1.5 text-xs font-medium transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent/50",
        active ? "bg-accent/10 text-accent" : "text-dim hover:bg-panel-hover hover:text-fg",
      )}
    >
      {label}
    </button>
  );
}

function DetailFact({
  label,
  value,
  compact = false,
}: {
  label: string;
  value: string;
  compact?: boolean;
}) {
  return (
    <div>
      <dt className={compact ? "text-[9px] uppercase tracking-[0.12em] text-faint" : "font-mono text-[9px] uppercase tracking-[0.14em] text-faint"}>{label}</dt>
      <dd className={compact ? "mt-1 break-words text-[10px] text-dim" : "mt-1 text-sm font-medium text-fg"}>{value}</dd>
    </div>
  );
}

function confidenceLabel(row: LedgerRow): string | null {
  if (decisionProvenance(row) !== "ai") return null;
  if (typeof row.confidence === "number") return `${Math.round(row.confidence * 100)}% confidence`;
  const parsed = Number(row.confidence_text);
  if (Number.isFinite(parsed) && parsed >= 0 && parsed <= 1) return `${Math.round(parsed * 100)}% confidence`;
  return row.confidence_text || null;
}

function recordSummary(row: LedgerRow): string {
  const action = String(row.action_text || row.action || "").toUpperCase();
  const provenance = decisionProvenance(row);
  if (isCycleSummary(row)) return "Routine automatic checks were grouped into one cycle summary.";
  if (row.executed && action === "BUY") {
    return isConfirmedExecution(row)
      ? "A purchase was completed after the entry conditions were met."
      : "A purchase was recorded.";
  }
  if (row.executed && (action === "SELL" || action === "CLOSE")) {
    return isConfirmedExecution(row)
      ? "A reduction in the position was completed."
      : "A reduction in the position was recorded.";
  }
  if (isRiskDecision(row)) return "Risk controls stopped a trade before execution.";
  if (isStaleDecision(row)) {
    if (provenance === "ai") return "Casys waited because the available market data was too old.";
    if (provenance === "system") return "An automatic check recorded no action because the available market data was too old.";
    if (provenance === "planned") return "A planned action stayed on hold because the available market data was too old.";
    return "The record shows the available market data was too old; it does not confirm an AI review.";
  }
  if (runtimeFlag(row, "exit_update_applied")) {
    if (provenance === "ai") return "Casys kept the position open and updated its protection.";
    if (provenance === "system") return "An automatic check kept the position open and updated its protection.";
    if (provenance === "planned") return "A previously prepared plan updated the position's protection.";
    return "The record shows the position stayed open with updated protection; it does not confirm who made that change.";
  }
  if (runtimeFlag(row, "indicator_watch_created") || row.effect_kind === "watch") {
    if (provenance === "ai") return "Casys kept the portfolio unchanged and created a watch for the next setup.";
    if (provenance === "system") return "An automatic check kept the portfolio unchanged and recorded a watch for the next setup.";
    if (provenance === "planned") return "A previously prepared plan kept the portfolio unchanged and recorded the next condition to watch.";
    return "The record shows a watch was created and the portfolio stayed unchanged; it does not confirm an AI review.";
  }
  if (runtimeFlag(row, "trade_plan_created") || Boolean(row.runtime?.armed_plan_id)) {
    if (provenance === "ai") return "Casys prepared a trade plan; no execution is claimed here.";
    if (provenance === "system") return "An automatic check recorded a trade plan; no execution is claimed here.";
    if (provenance === "planned") return "A previously prepared plan remained active; no execution is claimed here.";
    return "The record shows a trade plan; it does not confirm who prepared it.";
  }
  if (action === "HOLD" && provenance === "system") return "An automatic check recorded no portfolio action; this is not an AI opinion.";
  if (action === "HOLD" && provenance === "planned") return "A previously prepared plan did not trigger a portfolio action.";
  if (action === "HOLD" && provenance === "ai") return "Casys reviewed the company and kept the portfolio unchanged.";
  if (action === "HOLD") return "The record shows the portfolio stayed unchanged; it does not confirm an AI review.";
  if (action === "BUY") return "A buy decision was recorded; no execution is shown here.";
  if (action === "SELL" || action === "CLOSE") return "A sell decision was recorded; no execution is shown here.";
  if (provenance === "recorded") return "The record shows a portfolio decision; it does not confirm an AI review.";
  if (provenance === "planned") return "A previously prepared plan recorded this portfolio outcome.";
  return "A portfolio decision was recorded for this company.";
}

function plainEffect(value?: string | null, action?: string | null): string {
  const clean = String(value ?? "").replace(/\s*▾\s*$/u, "").trim();
  if (/^wake set$/i.test(clean)) return "Review scheduled";
  if (/^watch:\s*/i.test(clean)) return `Watch created: ${conditionLabel(clean.replace(/^watch:\s*/i, ""))}`;
  if (/^hold$/i.test(clean)) return "Portfolio unchanged";
  if (/^quiet[_ -]?gate$/i.test(clean)) return "Automatic safety check; no action";
  const fill = clean.match(/^filled\s+([0-9.,]+)\s*@\s*([0-9.,]+)$/i);
  if (fill) {
    const verb = String(action ?? "").toUpperCase() === "BUY" ? "Purchase" : "Sale";
    return `${verb} executed · ${fill[1]} shares at ${fill[2]}`;
  }
  return clean
    .replace(/\b([0-9]+)\s+due\b/gi, "$1 companies due")
    .replace(/\bLLM calls?\b/gi, "AI reviews")
    .replace(/\bquiet holds?\b/gi, "automatic no-change outcomes")
    .replaceAll("_", " ");
}

function timestampOf(row: LedgerRow): number {
  const value = Date.parse(String(row.cycle_ts ?? ""));
  return Number.isFinite(value) ? value : 0;
}

function isCycleSummary(row: LedgerRow): boolean {
  return row.is_batch || row.summary_kind === "automatic_cycle";
}

function isRiskDecision(row: LedgerRow): boolean {
  const reason = String(row.reason ?? row.decision_reason_code ?? "").toLowerCase();
  return reason.startsWith("risk:") || reason.startsWith("blocked_") || reason.includes("risk_block") || runtimeFlag(row, "blocked") || Boolean(row.runtime?.trade_evaluation_rejection);
}

function isStaleDecision(row: LedgerRow): boolean {
  const reason = String(row.reason ?? "").toLowerCase();
  return reason.startsWith("stale");
}

function hasProtectionChange(row: LedgerRow): boolean {
  return row.effect_kind === "watch" || runtimeFlag(row, "trade_plan_created") || runtimeFlag(row, "indicator_watch_created") || runtimeFlag(row, "exit_update_applied") || Boolean(row.runtime?.armed_plan_id);
}

function isMaterialActivity(row: LedgerRow): boolean {
  const action = String(row.action_text || row.action || "").toUpperCase();
  if (isCycleSummary(row)) return Boolean((row as LedgerRow & { has_material?: boolean }).has_material);
  return row.executed === true || ["BUY", "SELL", "CLOSE", "REDUCE", "FLIP"].includes(action) || hasProtectionChange(row) || isRiskDecision(row) || isStaleDecision(row);
}

function matchesDecisionFilter(row: LedgerRow, filter: (typeof FILTERS)[number]): boolean {
  if (filter === "trades") return row.executed === true;
  if (filter === "watches") return row.effect_kind === "watch" || runtimeFlag(row, "indicator_watch_created");
  if (filter === "protections") return runtimeFlag(row, "exit_update_applied");
  if (filter === "plans") return runtimeFlag(row, "trade_plan_created") || Boolean(row.runtime?.armed_plan_id);
  if (filter === "blocked") return isRiskDecision(row) || isStaleDecision(row);
  return true;
}

function runtimeFlag(row: LedgerRow, key: string): boolean {
  return row.runtime?.[key] === true;
}

function isConfirmedExecution(row: LedgerRow): boolean {
  return row.executed === true && row.execution_status === "confirmed";
}

function decisionStages(row: LedgerRow): Array<{ label: string; text: string }> {
  return [
    { label: "Context", text: decisionContext(row) },
    { label: "Decision", text: decisionChoice(row) },
    { label: "Action taken", text: actionTaken(row) },
    { label: "Outcome", text: decisionOutcome(row) },
  ];
}

function decisionContext(row: LedgerRow): string {
  const provenance = decisionProvenance(row);
  if (isCycleSummary(row)) return "Routine automatic checks were completed during the same cycle.";
  if (isStaleDecision(row)) return "The available market data was too old to support a fresh trade.";
  if (String(row.decision_reason_code ?? "").toUpperCase() === "MARKET_CLOSED") {
    if (provenance === "ai") return "The market was closed when Casys reviewed this company.";
    if (provenance === "system") return "The market was closed when this automatic check ran.";
    if (provenance === "planned") return "The market was closed when the prepared plan was evaluated.";
    return "The record shows the market was closed; it does not confirm an AI review.";
  }
  if (row.price != null) {
    const price = formatMarketPrice(row.price, 2);
    if (provenance === "ai") return `Casys reviewed the available market and portfolio context at a recorded price of ${price}.`;
    if (provenance === "system") return `An automatic check used the available market and portfolio context at a recorded price of ${price}.`;
    if (provenance === "planned") return `The prepared plan was evaluated at a recorded market price of ${price}.`;
    return `The record shows a market price of ${price}. It does not confirm who reviewed this company.`;
  }
  if (provenance === "ai") return "Casys reviewed the available market and portfolio context. More observation detail is not exposed in this desktop record.";
  if (provenance === "system") return "An automatic check used the available market and portfolio context. More observation detail is not exposed in this desktop record.";
  if (provenance === "planned") return "The action followed conditions already recorded in a prepared plan.";
  return "The record does not confirm who reviewed the available market and portfolio context.";
}

function decisionChoice(row: LedgerRow): string {
  if (isCycleSummary(row)) return "No AI opinion was produced; the automatic outcomes were kept together for readability.";
  const action = decisionActionLabel(row.action_text || row.action);
  const confidence = confidenceLabel(row);
  const provenance = decisionProvenance(row);
  if (provenance === "system") return `${action}. This outcome came from an automatic check, not an AI opinion.`;
  if (provenance === "planned") return `${action}. This outcome followed a previously prepared plan; no new AI review ran.`;
  if (provenance === "recorded") return `${action}. The record does not confirm who made this choice.`;
  return confidence ? `${action}. Casys recorded ${confidence.toLowerCase()} in this decision.` : `${action}.`;
}

function actionTaken(row: LedgerRow): string {
  const provenance = decisionProvenance(row);
  const effect = plainEffect(row.effect_text, row.action_text || row.action);
  if (row.executed) {
    if (isConfirmedExecution(row)) {
      return effect;
    }
    const action = String(row.action_text || row.action || "").toUpperCase();
    const verb = action === "BUY" ? "Purchase" : "Sale";
    const qty = row.qty != null ? formatQty(row.qty) : null;
    const price = row.price != null ? formatMarketPrice(row.price, 2) : null;
    if (qty != null && price != null) return `${verb} recorded · ${qty} shares at ${price}`;
    if (qty != null) return `${verb} recorded · ${qty} shares`;
    return `${verb} recorded`;
  }
  if (runtimeFlag(row, "exit_update_applied")) {
    if (provenance === "ai") return "Casys updated the position's exit protection.";
    if (provenance === "system") return "An automatic check updated the position's exit protection.";
    if (provenance === "planned") return "The prepared plan updated the position's exit protection.";
    return "The record shows the position's exit protection was updated.";
  }
  if (runtimeFlag(row, "indicator_watch_created") || row.effect_kind === "watch") {
    if (provenance === "ai") return effect || "Casys created a market watch.";
    if (provenance === "system") return effect || "An automatic check recorded a market watch.";
    if (provenance === "planned") return effect || "The prepared plan recorded a market watch.";
    return effect || "The record shows a market watch.";
  }
  if (runtimeFlag(row, "trade_plan_created") || Boolean(row.runtime?.armed_plan_id)) {
    if (provenance === "ai") return "Casys prepared a trade plan; no execution is claimed here.";
    if (provenance === "system") return "An automatic check recorded a trade plan; no execution is claimed here.";
    if (provenance === "planned") return "The prepared plan remained active; no execution is claimed here.";
    return "The record shows a trade plan; it does not confirm who prepared it.";
  }
  if (isRiskDecision(row)) return "Risk controls blocked the trade before execution.";
  if (row.effect_kind === "wake" || row.runtime?.next_wake_event) {
    if (provenance === "ai") return "Casys scheduled another review; no trade was placed.";
    if (provenance === "system") return "An automatic check scheduled another review; no trade was placed.";
    if (provenance === "planned") return "The prepared plan scheduled its next evaluation; no trade was placed.";
    return "The record shows another review was scheduled; no trade was placed.";
  }
  if (isCycleSummary(row)) return "No portfolio action was taken by these automatic checks.";
  return "No trade was placed.";
}

function currentProtection(row: LedgerRow, plans?: PlansPayload): string {
  const symbol = String(row.symbol ?? "");
  const exit = plans?.exit_plans.rows.find((item) => item.symbol === symbol);
  const watches = [...(plans?.watches.rows ?? []), ...(plans?.exit_watches.rows ?? [])].filter((item) => item.symbol === symbol);
  const armed = plans?.armed.rows.find((item) => item.symbol === symbol);
  const parts: string[] = [];
  if (exit) {
    const stop = exit.stop_price != null ? `stop ${formatMarketPrice(exit.stop_price, 2)}` : "an exit plan";
    const target = exit.take_profit_label ? `target ${exit.take_profit_label}` : null;
    parts.push(`Current position: ${[stop, target].filter(Boolean).join("; ")}.`);
  }
  if (watches[0]) parts.push(`Active watch: ${conditionLabel(watches[0].condition_label)}, ${expiryPhrase(watches[0].countdown)}.`);
  if (armed) parts.push(`Entry plan ready for ${decisionActionLabel(armed.action).toLowerCase()}, ${expiryPhrase(armed.countdown)}.`);
  if (parts.length) return parts.join(" ");
  const recorded = recordedExitProtection(row);
  if (recorded) return recorded;
  if (!plans) return "Current protection has not been loaded.";
  return "No current stop, target or watch is recorded for this company.";
}

function recordedExitProtection(row: LedgerRow): string | null {
  const exitPlan = recordValue(row.runtime?.exit_plan);
  const updateTrace = recordValue(row.runtime?.exit_update_trace);
  const stop = numberValue(exitPlan?.hard_stop) ?? numberValue(recordValue(updateTrace?.hard_stop)?.resolved_price);
  const targets = Array.isArray(exitPlan?.take_profits) ? exitPlan.take_profits : Array.isArray(updateTrace?.take_profits) ? updateTrace.take_profits : [];
  const target = numberValue(recordValue(targets[0])?.price) ?? numberValue(recordValue(targets[0])?.resolved_price);
  if (stop == null && target == null) return null;
  return `This record includes ${[stop != null ? `stop ${formatMarketPrice(stop, 2)}` : null, target != null ? `target ${formatMarketPrice(target, 2)}` : null].filter(Boolean).join(" and ")}.`;
}

function expiryPhrase(countdown: string): string {
  const value = countdown.trim();
  if (!value || value === "—") return "expiry not recorded";
  if (value.toLowerCase() === "expired") return "has expired";
  if (value.toLowerCase() === "now") return "expires now";
  return /^in\b/i.test(value) ? `expires ${value}` : `expires in ${value}`;
}

function decisionOutcome(row: LedgerRow): string {
  const provenance = decisionProvenance(row);
  const effect = plainEffect(row.effect_text, row.action_text || row.action);
  if (row.executed) {
    const action = String(row.action_text || row.action || "").toUpperCase();
    const quantity = row.qty != null ? `${formatQty(row.qty)} shares` : "the recorded quantity";
    if (isConfirmedExecution(row)) {
      if (action === "BUY") return `The portfolio confirms an increase of ${quantity}.`;
      if (["SELL", "CLOSE", "REDUCE"].includes(action)) return `The portfolio confirms a decrease of ${quantity}.`;
      return "The trade changed the portfolio position.";
    }
    if (action === "BUY") return `The recorded position increased by ${quantity}.`;
    if (["SELL", "CLOSE", "REDUCE"].includes(action)) return `The recorded position decreased by ${quantity}.`;
    return "The trade changed the recorded portfolio position.";
  }
  if (isRiskDecision(row)) return "The portfolio stayed unchanged because the trade did not pass its controls.";
  if (isStaleDecision(row)) {
    if (provenance === "ai") return "The portfolio stayed unchanged while Casys waited for fresher data.";
    if (provenance === "system") return "The portfolio stayed unchanged after an automatic stale-data check.";
    if (provenance === "planned") return "The portfolio stayed unchanged because the prepared plan could not use stale market data.";
    return "The record shows the portfolio stayed unchanged because market data was too old.";
  }
  if (runtimeFlag(row, "exit_update_applied")) {
    if (provenance === "recorded") {
      return "The record shows the position stayed open with a protection update; it does not confirm who made that change.";
    }
    return "The position stayed open with updated protection.";
  }
  if (runtimeFlag(row, "indicator_watch_created") || row.effect_kind === "watch") {
    const watchText = effect || (provenance === "recorded" ? "A market watch is recorded" : "A market watch is active");
    if (provenance === "ai") return `${watchText}. Casys will reassess when the condition is met.`;
    if (provenance === "system") return `${watchText}. An automatic check will run when the condition is met.`;
    if (provenance === "planned") return `${watchText}. The prepared plan will be evaluated again when the condition is met.`;
    return `${watchText}. The record does not confirm a later AI reassessment.`;
  }
  if (row.effect_kind === "wake" || row.runtime?.next_wake_event) return "The portfolio stayed unchanged and another review was scheduled.";
  if (isCycleSummary(row)) return plainEffect(row.effect_text) || "The portfolio stayed unchanged.";
  return effect && effect !== "Portfolio unchanged" ? effect : "No immediate portfolio change was recorded.";
}

function recordValue(value: unknown): Record<string, unknown> | null {
  return value && typeof value === "object" && !Array.isArray(value) ? value as Record<string, unknown> : null;
}

function numberValue(value: unknown): number | null {
  const parsed = typeof value === "number" ? value : Number(value);
  return Number.isFinite(parsed) ? parsed : null;
}

function decisionDisplayName(companyMap: Record<string, string>, row: LedgerRow): string {
  return isCycleSummary(row) ? "Automatic cycle summary" : companyDisplayName(companyMap, row.symbol);
}

function ErrorText({ error }: { error: unknown }) {
  return (
    <div className="rounded-lg border border-loss/30 bg-loss/5 px-4 py-3 text-sm text-loss">
      <p>The decision record could not be refreshed.</p>
      <details className="mt-2 text-xs text-dim">
        <summary className="cursor-pointer">Technical details</summary>
        <p className="mt-1 break-words font-mono text-[10px] text-faint">{error instanceof Error ? error.message : String(error)}</p>
      </details>
    </div>
  );
}
