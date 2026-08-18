import { ArrowRight, Building2, Globe2, Map } from "lucide-react";
import { useMemo } from "react";
import { ActionChip } from "@/components/action-chip";
import { EquityChart } from "@/components/charts/equity-chart";
import { PosturePanel } from "@/components/intelligence/posture-panel";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardBody, CardHeader, CardTitle } from "@/components/ui/card";
import type { PageKey } from "@/components/layout/app-shell";
import {
  useCompanyIntelligence,
  useRegionIntelligence,
  useWorldIntelligence,
} from "@/hooks/use-intelligence";
import { decisionKind, KIND_LABEL, mergeDecisions } from "@/lib/classify";
import { formatAgo, formatPct, formatUsd, signedClass } from "@/lib/format";
import type {
  CompanyIntelligence,
  FamilyIntelligence,
  IntelligenceEvent,
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
  const regionsQuery = useRegionIntelligence(undefined, 30);
  const companiesQuery = useCompanyIntelligence({ limit: 60 });
  const world = worldQuery.data;
  const regions = regionsQuery.data;
  const companies = companiesQuery.data?.companies ?? [];
  const changes = useMemo(
    () =>
      (world?.events ?? [])
        .filter((event) => event.changes.length || event.status === "error")
        .slice(0, 5),
    [world?.events],
  );
  const watch = useMemo(
    () =>
      [...companies]
        .sort(
          (left, right) =>
            Number(right.thesis_changed && right.source_count > 0) -
              Number(left.thesis_changed && left.source_count > 0) ||
            Number(right.thesis_changed) - Number(left.thesis_changed) ||
            right.source_count - left.source_count ||
            (right.selection_confidence ?? -1) - (left.selection_confidence ?? -1),
        )
        .slice(0, 6),
    [companies],
  );
  const journal = useMemo(
    () =>
      mergeDecisions(snapshot.decisions, snapshot.recent_decisions)
        .filter((row) => row.model_called !== false)
        .slice(0, 3),
    [snapshot.decisions, snapshot.recent_decisions],
  );
  const holdings = (snapshot.portfolio.holdings ?? []).filter((holding) => Math.abs(holding.quantity) > 1e-9);
  const queryError = worldQuery.error || regionsQuery.error || companiesQuery.error;

  return (
    <div className="grid gap-6">
      {queryError ? (
        <p className="text-sm text-loss">{queryError instanceof Error ? queryError.message : String(queryError)}</p>
      ) : null}

      <PosturePanel posture={world?.current.posture} digest={world?.current.digest} />

      <section className="grid gap-4 xl:grid-cols-[minmax(0,1.12fr)_minmax(300px,0.88fr)]">
        <Card>
          <CardHeader>
            <div>
              <CardTitle>What changed</CardTitle>
            </div>
            <Button variant="ghost" size="sm" onClick={() => onPage("world")}>
              World timeline
              <ArrowRight className="size-3.5" />
            </Button>
          </CardHeader>
          <CardBody className="p-0">
            {changes.map((event) => (
              <ChangeRow key={event.event_id} event={event} />
            ))}
            {!changes.length ? <p className="px-4 py-8 text-sm text-faint">No observed change in the current window.</p> : null}
          </CardBody>
        </Card>

        <Card>
          <CardHeader>
            <div>
              <CardTitle>Regional pulse</CardTitle>
            </div>
            <Button variant="ghost" size="sm" onClick={() => onPage("regions")}>
              Matrix
              <ArrowRight className="size-3.5" />
            </Button>
          </CardHeader>
          <CardBody className="grid gap-3 sm:grid-cols-3 xl:grid-cols-1">
            {(["TW", "EU", "US"] as const).map((venue) => (
              <RegionPulse
                key={venue}
                venue={venue}
                posture={regions?.current[venue]?.posture}
                freshness={regions?.current[venue]?.freshness_hours}
                families={(regions?.families ?? []).filter((row) => row.venue === venue).slice(0, 3)}
                onOpen={() => onPage("regions")}
              />
            ))}
          </CardBody>
        </Card>
      </section>

      <section>
        <div className="mb-3 flex flex-wrap items-end justify-between gap-3">
          <div>
            <p className="font-mono text-[9px] uppercase tracking-[0.22em] text-accent">Companies to watch</p>
          </div>
          <Button variant="ghost" size="sm" onClick={() => onPage("companies")}>
            Full company radar
            <ArrowRight className="size-3.5" />
          </Button>
        </div>
        <div className="grid gap-3 md:grid-cols-2 xl:grid-cols-3">
          {watch.map((company) => (
            <CompanyWatch key={company.symbol} company={company} onOpen={() => onSymbol(company.symbol)} />
          ))}
          {!watch.length ? (
            <p className="rounded-lg border border-dashed border-line px-4 py-8 text-sm text-faint">
              No company intelligence is available.
            </p>
          ) : null}
        </div>
      </section>

      <section className="border-t border-line pt-6">
        <div className="mb-3 flex flex-wrap items-end justify-between gap-3">
          <div>
            <p className="font-mono text-[9px] uppercase tracking-[0.22em] text-faint">Agent expression</p>
          </div>
          <div className="flex gap-1.5">
            <Button variant="ghost" size="sm" onClick={() => onPage("portfolio")}>Portfolio</Button>
            <Button variant="ghost" size="sm" onClick={() => onPage("decisions")}>Decisions</Button>
          </div>
        </div>

        <div className="grid gap-4 xl:grid-cols-[1fr_1.15fr_0.85fr]">
          <Card>
            <CardHeader>
              <CardTitle>Book expression</CardTitle>
              <span className="font-mono text-[10px] text-faint">{holdings.length} open</span>
            </CardHeader>
            <CardBody className="space-y-1.5">
              {holdings.map((holding) => {
                const company = companies.find((item) => item.symbol === holding.symbol);
                const pnl = holding.unrealized_pnl_net ?? holding.unrealized_pnl;
                return (
                  <button
                    key={holding.symbol}
                    type="button"
                    onClick={() => onSymbol(holding.symbol)}
                    className="flex w-full items-center justify-between rounded-md px-2 py-1.5 text-left hover:bg-panel-hover"
                  >
                    <div>
                      <p className="text-sm font-medium">{holding.symbol}</p>
                      <p className="font-mono text-[9px] text-faint">
                        {company?.venue || "—"} · {company?.thesis_status?.replaceAll("_", " ") || "no thesis"}
                      </p>
                    </div>
                    <span className={cn("font-mono text-xs tabular", signedClass(pnl))}>{formatUsd(pnl, 0)}</span>
                  </button>
                );
              })}
              {!holdings.length ? <p className="text-sm text-faint">Agent is flat.</p> : null}
            </CardBody>
          </Card>

          <Card>
            <CardHeader>
              <CardTitle>Recent decisions</CardTitle>
            </CardHeader>
            <CardBody className="p-0">
              {journal.map((row) => {
                const kind = decisionKind(row);
                return (
                  <button
                    key={row.decision_id ?? `${row.symbol}-${row.cycle_ts}`}
                    type="button"
                    onClick={() => row.symbol && onSymbol(row.symbol)}
                    className="w-full border-b border-hairline px-4 py-3 text-left last:border-0 hover:bg-panel-hover"
                  >
                    <div className="flex flex-wrap items-center gap-2">
                      <span className="font-mono text-[9px] text-faint">{formatAgo(row.cycle_ts || row.ts)}</span>
                      <span className="text-sm font-medium">{row.symbol}</span>
                      <ActionChip action={row.action} />
                      <Badge tone={kind === "exec" ? "gain" : kind === "risk" ? "loss" : "muted"}>
                        {KIND_LABEL[kind]}
                      </Badge>
                    </div>
                    <p className="mt-1.5 line-clamp-2 text-xs leading-relaxed text-dim">{row.rationale || row.reason || "—"}</p>
                  </button>
                );
              })}
            </CardBody>
          </Card>

          <Card>
            <CardHeader>
              <CardTitle>Equity · consequence</CardTitle>
              <span className={cn("font-mono text-[10px]", signedClass(snapshot.portfolio.total_return_pct))}>
                {formatPct(snapshot.portfolio.total_return_pct)}
              </span>
            </CardHeader>
            <CardBody className="h-44 pt-2">
              <EquityChart points={snapshot.equity_series} className="h-40" minHeight={140} />
            </CardBody>
          </Card>
        </div>
      </section>
    </div>
  );
}

function ChangeRow({ event }: { event: IntelligenceEvent }) {
  const failure = event.status === "error";
  return (
    <div className="border-b border-hairline px-4 py-3 last:border-0">
      <div className="flex flex-wrap items-center gap-2">
        {event.layer === "world" ? <Globe2 className="size-3.5 text-accent" /> : <Map className="size-3.5 text-accent" />}
        <p className="text-sm font-medium">{event.title}</p>
        {event.venue ? <Badge>{event.venue}</Badge> : null}
        {failure ? <Badge tone="loss">failure</Badge> : <Badge tone="warn">{event.changes.length} changed</Badge>}
        <span className="ml-auto font-mono text-[9px] text-faint">{formatAgo(event.as_of)}</span>
      </div>
      {event.summary ? <p className="mt-1.5 line-clamp-2 text-xs leading-relaxed text-dim">{event.summary}</p> : null}
      {event.changes.length ? (
        <p className="mt-1 font-mono text-[9px] text-faint">
          {event.changes.map((change) => change.field.replaceAll("_", " ")).join(" · ")}
        </p>
      ) : null}
    </div>
  );
}

function RegionPulse({
  venue,
  posture,
  freshness,
  families,
  onOpen,
}: {
  venue: string;
  posture?: string | null;
  freshness?: number | null;
  families: FamilyIntelligence[];
  onOpen: () => void;
}) {
  return (
    <button
      type="button"
      onClick={onOpen}
      className="rounded-md border border-hairline px-3 py-3 text-left transition-colors hover:bg-panel-hover"
    >
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-2">
          <Map className="size-3.5 text-accent" />
          <span className="text-sm font-semibold">{venue}</span>
        </div>
        <Badge tone={typeof freshness === "number" && freshness <= 24 ? "gain" : "warn"}>
          {typeof freshness === "number" ? `${Math.round(freshness)}h` : "unknown"}
        </Badge>
      </div>
      <p className="mt-1 font-mono text-[9px] text-faint">{posture?.replaceAll("_", " ") || "no posture"}</p>
      <div className="mt-3 flex flex-wrap gap-1">
        {families.map((family) => (
          <span key={family.family} className="rounded-sm bg-hairline px-1.5 py-0.5 font-mono text-[9px] text-muted">
            {family.rank ?? "—"} {family.family.replaceAll("_", " ")}
          </span>
        ))}
      </div>
    </button>
  );
}

function CompanyWatch({
  company,
  onOpen,
}: {
  company: CompanyIntelligence;
  onOpen: () => void;
}) {
  return (
    <button
      type="button"
      onClick={onOpen}
      className="rounded-lg border border-line bg-panel/55 p-4 text-left transition-colors hover:bg-panel-hover"
    >
      <div className="flex items-start justify-between gap-3">
        <div className="flex items-center gap-2">
          <Building2 className="size-3.5 text-accent" />
          <div>
            <p className="text-sm font-semibold">{company.symbol}</p>
            <p className="max-w-40 truncate text-[11px] text-dim">{company.name}</p>
          </div>
        </div>
        <Badge tone={company.thesis_changed ? "warn" : "accent"}>
          {company.thesis_changed ? "changed" : company.thesis_status.replaceAll("_", " ")}
        </Badge>
      </div>
      <p className="mt-3 line-clamp-3 text-sm leading-relaxed text-muted">{company.summary || "No thesis summary."}</p>
      <div className="mt-3 flex flex-wrap gap-x-3 gap-y-1 font-mono text-[9px] text-faint">
        <span>{company.venue}</span>
        <span>{company.catalyst_count} catalysts</span>
        <span>{company.risk_count} risks</span>
        <span>{company.source_count} sources</span>
        {company.on_book ? <span className="text-accent">on book {company.side}</span> : null}
      </div>
    </button>
  );
}
