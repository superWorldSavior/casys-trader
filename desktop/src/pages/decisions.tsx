import { useMemo, useState } from "react";
import { ActionChip } from "@/components/action-chip";
import { Badge } from "@/components/ui/badge";
import { Card, CardBody, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { useDecisions, usePlans } from "@/hooks/use-desk-api";
import { formatUsd } from "@/lib/format";
import type { LedgerRow } from "@/lib/types";
import { cn } from "@/lib/utils";

const FILTERS = ["all", "buy", "sell", "hold", "risk", "stale"] as const;
const VIEW_CHIPS = ["all", "exec", "infra", "llm"] as const;

type Props = {
  onSymbol: (symbol: string) => void;
};

export function DecisionsPage({ onSymbol }: Props) {
  const [filter, setFilter] = useState<(typeof FILTERS)[number]>("all");
  const [view, setView] = useState<(typeof VIEW_CHIPS)[number]>("all");
  const [query, setQuery] = useState("");
  const [selected, setSelected] = useState<string | null>(null);
  const decisions = useDecisions(filter);
  const plans = usePlans();
  const rows = decisions.data?.rows ?? [];
  const visible = useMemo(() => {
    return rows.filter((row) => {
      if (view === "exec" && row.executed !== true) return false;
      if (view === "infra" && row.model_called !== false) return false;
      if (view === "llm" && row.model_called === false) return false;
      if (!query.trim()) return true;
      const hay = `${row.symbol} ${row.action} ${row.rationale} ${row.effect_text} ${row.llm_model}`.toLowerCase();
      return hay.includes(query.trim().toLowerCase());
    });
  }, [query, rows, view]);
  const active = visible.find((row) => row.row_key === selected) ?? visible[0];

  return (
    <div className="grid grid-cols-[1.7fr_1fr] gap-4">
      <div className="grid gap-4">
        <div className="flex flex-wrap items-center gap-2">
          <Input
            value={query}
            onChange={(event) => setQuery(event.target.value)}
            placeholder="Filter symbol, rationale, model…"
            className="max-w-sm"
          />
          {FILTERS.map((item) => (
            <Chip
              key={item}
              active={filter === item}
              onClick={() => setFilter(item)}
              label={`${item}${decisions.data?.counts[item] != null ? ` ${decisions.data.counts[item]}` : ""}`}
            />
          ))}
          <span className="mx-1 text-faint">·</span>
          {VIEW_CHIPS.map((item) => (
            <Chip key={item} active={view === item} onClick={() => setView(item)} label={item} />
          ))}
        </div>
        {decisions.error ? <ErrorText error={decisions.error} /> : null}
        <Card>
          <div className="grid grid-cols-[72px_88px_64px_72px_1fr_80px] gap-3 border-b border-hairline px-4 py-2 font-mono text-[10px] uppercase tracking-[0.14em] text-faint">
            <span>When</span>
            <span>Symbol</span>
            <span>Side</span>
            <span>Src</span>
            <span>Effect</span>
            <span>Conf</span>
          </div>
          <div>
            {visible.map((row) => (
              <button
                key={row.row_key}
                type="button"
                onClick={() => setSelected(row.row_key)}
                onDoubleClick={() => row.symbol && row.symbol !== "—" && onSymbol(row.symbol)}
                className={cn(
                  "grid w-full grid-cols-[72px_88px_64px_72px_1fr_80px] items-start gap-3 border-b border-hairline px-4 py-2.5 text-left last:border-0 hover:bg-panel-hover",
                  active?.row_key === row.row_key && "bg-panel-hover",
                )}
              >
                <span className="font-mono text-[11px] text-faint">{row.utc_text}</span>
                <span className="text-sm font-medium">{row.symbol}</span>
                <ActionChip action={row.action_text} />
                <Badge tone={row.model_called === false ? "muted" : "accent"}>{row.source_text}</Badge>
                <p className="line-clamp-2 text-sm text-muted">{row.effect_text || "—"}</p>
                <span className="font-mono text-[11px] text-faint">{row.confidence_text || "—"}</span>
              </button>
            ))}
            {visible.length === 0 ? <p className="px-4 py-8 text-sm text-faint">Nothing matches.</p> : null}
          </div>
        </Card>
        {active ? <DetailPanel row={active} onSymbol={onSymbol} /> : null}
      </div>
      <Playbook plans={plans.data} onSymbol={onSymbol} error={plans.error} />
    </div>
  );
}

function DetailPanel({ row, onSymbol }: { row: LedgerRow; onSymbol: (symbol: string) => void }) {
  return (
    <Card>
      <CardHeader>
        <CardTitle>Detail</CardTitle>
        <button
          type="button"
          className="font-mono text-[11px] text-accent hover:underline"
          onClick={() => row.symbol && row.symbol !== "—" && onSymbol(row.symbol)}
        >
          open {row.symbol}
        </button>
      </CardHeader>
      <CardBody className="space-y-2 text-sm text-muted">
        <p className="leading-relaxed">{row.rationale || "No rationale on this row."}</p>
        <div className="flex flex-wrap gap-1.5">
          {row.executed ? <Badge tone="gain">executed</Badge> : <Badge>not filled</Badge>}
          {row.model_called === false ? <Badge>infra</Badge> : <Badge tone="accent">llm</Badge>}
          {row.decision_reason_code ? <Badge>{row.decision_reason_code}</Badge> : null}
          {row.price != null ? <Badge>{formatUsd(row.price, 2)}</Badge> : null}
        </div>
      </CardBody>
    </Card>
  );
}

function Playbook({
  plans,
  onSymbol,
  error,
}: {
  plans: ReturnType<typeof usePlans>["data"];
  onSymbol: (symbol: string) => void;
  error: unknown;
}) {
  const armed = plans?.armed.rows ?? [];
  const exits = plans?.exit_plans.rows ?? [];
  const watches = plans?.watches.rows ?? [];
  const exitWatches = plans?.exit_watches.rows ?? [];
  return (
    <div className="grid gap-4">
      {error ? <ErrorText error={error} /> : null}
      <Card>
        <CardHeader>
          <CardTitle>Armed</CardTitle>
          <span className="font-mono text-[10px] text-faint">{armed.length}</span>
        </CardHeader>
        <CardBody className="space-y-2">
          {armed.length === 0 ? <Empty>No armed orders.</Empty> : null}
          {armed.map((row) => (
            <button
              key={`${row.symbol}-${row.countdown}`}
              type="button"
              onClick={() => onSymbol(row.symbol)}
              className="w-full rounded-md border border-hairline px-3 py-2 text-left hover:bg-panel-hover"
            >
              <div className="flex items-center justify-between">
                <span className="text-sm font-medium">{row.symbol}</span>
                <ActionChip action={row.action} />
              </div>
              <p className="mt-1 font-mono text-[11px] text-faint">
                {row.countdown} · {(row.conditions ?? []).slice(0, 2).join(" · ")}
              </p>
            </button>
          ))}
        </CardBody>
      </Card>
      <Card>
        <CardHeader>
          <CardTitle>Exit plans</CardTitle>
          <span className="font-mono text-[10px] text-faint">{exits.length}</span>
        </CardHeader>
        <CardBody className="space-y-2">
          {exits.length === 0 ? <Empty>No exit plans.</Empty> : null}
          {exits.map((row) => (
            <button
              key={row.symbol}
              type="button"
              onClick={() => onSymbol(row.symbol)}
              className="w-full rounded-md border border-hairline px-3 py-2 text-left hover:bg-panel-hover"
            >
              <div className="flex items-center justify-between">
                <span className="text-sm font-medium">{row.symbol}</span>
                <Badge tone={row.side === "S" || row.side === "short" ? "loss" : "gain"}>{row.side}</Badge>
              </div>
              <p className="mt-1 font-mono text-[11px] text-faint">
                stop {row.stop_price != null ? formatUsd(row.stop_price, 2) : "—"} · tp {row.take_profit_label ?? "—"}
              </p>
            </button>
          ))}
        </CardBody>
      </Card>
      <Card>
        <CardHeader>
          <CardTitle>Watches</CardTitle>
          <span className="font-mono text-[10px] text-faint">{watches.length + exitWatches.length}</span>
        </CardHeader>
        <CardBody className="space-y-2">
          {watches.length + exitWatches.length === 0 ? <Empty>No active watches.</Empty> : null}
          {[...watches, ...exitWatches].map((row) => (
            <button
              key={`${row.symbol}-${row.condition_label}`}
              type="button"
              onClick={() => onSymbol(row.symbol)}
              className="w-full rounded-md border border-hairline px-3 py-2 text-left hover:bg-panel-hover"
            >
              <p className="text-sm font-medium">{row.symbol}</p>
              <p className="mt-1 font-mono text-[11px] text-faint">
                {row.countdown} · {row.condition_label}
              </p>
            </button>
          ))}
        </CardBody>
      </Card>
    </div>
  );
}

function Chip({ active, onClick, label }: { active: boolean; onClick: () => void; label: string }) {
  return (
    <button
      type="button"
      onClick={onClick}
      className={cn(
        "rounded-sm px-2 py-1 font-mono text-[10px] uppercase tracking-[0.14em]",
        active ? "bg-accent/15 text-accent" : "text-faint hover:text-muted",
      )}
    >
      {label}
    </button>
  );
}

function Empty({ children }: { children: React.ReactNode }) {
  return <p className="text-sm text-faint">{children}</p>;
}

function ErrorText({ error }: { error: unknown }) {
  return <p className="text-sm text-loss">{error instanceof Error ? error.message : String(error)}</p>;
}
