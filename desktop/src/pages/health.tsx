import { Badge } from "@/components/ui/badge";
import { Card, CardBody, CardHeader, CardTitle } from "@/components/ui/card";
import { useHealth } from "@/hooks/use-desk-api";
import { formatPct } from "@/lib/format";

export function HealthPage() {
  const query = useHealth();
  const data = query.data;
  if (query.error) {
    return <p className="text-sm text-loss">{query.error instanceof Error ? query.error.message : String(query.error)}</p>;
  }
  if (!data) {
    return <p className="text-sm text-faint">Loading health…</p>;
  }

  return (
    <div className="grid grid-cols-3 gap-4">
      <div className="grid gap-4">
        <Card>
          <CardHeader>
            <CardTitle>Data freshness</CardTitle>
            {data.kill_active ? <Badge tone="loss">kill</Badge> : null}
          </CardHeader>
          <CardBody className="space-y-3">
            {data.freshness.venues.map((venue) => (
              <div key={venue.venue}>
                <div className="flex items-center justify-between">
                  <p className="text-sm font-medium">{venue.display_name}</p>
                  <Badge tone={venue.is_stale ? "warn" : "gain"}>
                    {venue.is_stale ? "stale" : "ok"} · {venue.symbol_count}
                  </Badge>
                </div>
                {venue.stale_symbols.slice(0, 6).map((row) => (
                  <p key={row.symbol} className="font-mono text-[11px] text-faint">
                    {row.symbol} {row.age_minutes != null ? `${Math.round(row.age_minutes)}m` : ""}
                  </p>
                ))}
              </div>
            ))}
          </CardBody>
        </Card>
        <Card>
          <CardHeader>
            <CardTitle>FX rates</CardTitle>
            <Badge tone={data.fx.source_available ? "gain" : "warn"}>
              {data.fx.source_available ? "live" : "missing"}
            </Badge>
          </CardHeader>
          <CardBody className="space-y-1.5">
            {data.fx.rows.map((row) => (
              <div key={row.currency} className="flex justify-between font-mono text-sm">
                <span className="text-dim">{row.currency}</span>
                <span>{row.rate.toFixed(4)}</span>
              </div>
            ))}
          </CardBody>
        </Card>
        <Card>
          <CardHeader>
            <CardTitle>Risk gate</CardTitle>
            <Badge tone={data.risk.reject_count ? "loss" : "gain"}>
              {data.risk.reject_count ? `${data.risk.reject_count} rejects` : "clear"}
            </Badge>
          </CardHeader>
          <CardBody className="space-y-2">
            {data.risk.recent_rejects.map((row) => (
              <p key={`${row.symbol}-${row.cycle_ts}`} className="text-sm text-muted">
                {row.symbol} {row.action} · {row.reason}
              </p>
            ))}
            {Object.entries(data.risk.caps).slice(0, 10).map(([key, value]) => (
              <div key={key} className="flex justify-between gap-3 font-mono text-[11px]">
                <span className="text-faint">{key.replaceAll("_", " ")}</span>
                <span className="text-muted">{String(value)}</span>
              </div>
            ))}
          </CardBody>
        </Card>
      </div>

      <div className="grid gap-4">
        <Card>
          <CardHeader>
            <CardTitle>Sources</CardTitle>
          </CardHeader>
          <CardBody className="space-y-2">
            {data.sources.rows.map((row) => (
              <div key={row.name}>
                <p className="text-sm font-medium">{row.name}</p>
                <p className="font-mono text-[11px] text-faint">{row.detail}</p>
              </div>
            ))}
          </CardBody>
        </Card>
        <Card>
          <CardHeader>
            <CardTitle>LLM</CardTitle>
          </CardHeader>
          <CardBody className="space-y-1 text-sm text-muted">
            <p>{data.llm.calls_label}</p>
            <p>{data.llm.fallbacks_label}</p>
            <p className="font-mono text-[11px] text-faint">
              fills {data.llm.total_fills} · fallbacks {data.llm.total_fallbacks}
            </p>
          </CardBody>
        </Card>
        <Card>
          <CardHeader>
            <CardTitle>Model</CardTitle>
          </CardHeader>
          <CardBody className="space-y-2">
            {data.model.model_performance.map((row, index) => (
              <p key={index} className="font-mono text-[11px] text-muted">
                {String(row.provider ?? "")} · {String(row.model ?? "")} · conf{" "}
                {typeof row.avg_confidence === "number" ? formatPct(row.avg_confidence * 100) : "—"}
              </p>
            ))}
            {Object.entries(data.model.providers ?? {}).map(([key, count]) => (
              <p key={key} className="font-mono text-[11px] text-muted">
                {key} · {count}
              </p>
            ))}
            {data.model.model_performance.length === 0 && !data.model.providers ? (
              <p className="text-sm text-faint">No model stats yet.</p>
            ) : null}
          </CardBody>
        </Card>
      </div>

      <div className="grid gap-4">
        <Card>
          <CardHeader>
            <CardTitle>Learnings</CardTitle>
          </CardHeader>
          <CardBody className="space-y-2 text-sm text-muted">
            <p>{data.learnings.pending_label}</p>
            <p>{data.learnings.consolidation_label}</p>
            <p className="font-mono text-[11px] text-faint">{data.learnings.last_run_label}</p>
            {data.learnings.notes.slice(0, 6).map((note) => (
              <p key={`${note.symbol}-${note.note}`} className="text-xs">
                <span className="font-medium">{note.symbol}</span> {note.note}
              </p>
            ))}
          </CardBody>
        </Card>
        <Card>
          <CardHeader>
            <CardTitle>Memory</CardTitle>
            <Badge tone={data.memory.available ? "gain" : "warn"}>{data.memory.available ? "on" : "off"}</Badge>
          </CardHeader>
          <CardBody className="space-y-1 text-sm text-muted">
            <p>{data.memory.notes_label || data.memory.missing_label}</p>
            <p>{data.memory.lift_label}</p>
            <p>{data.memory.useful_label}</p>
            <p>{data.memory.rules_label}</p>
            <p className={data.memory.sync_is_error ? "text-loss" : "text-faint"}>{data.memory.sync_label}</p>
            <p className="font-mono text-[11px] text-faint">{data.memory.situation_label}</p>
          </CardBody>
        </Card>
        <Card>
          <CardHeader>
            <CardTitle>Universe</CardTitle>
          </CardHeader>
          <CardBody className="space-y-1 text-sm text-muted">
            <p>{data.universe.symbols_label}</p>
            <p>{data.universe.hotset_label}</p>
            {data.universe.venue_counts.map(([venue, count]) => (
              <p key={venue} className="font-mono text-[11px] text-faint">
                {venue} {count}
              </p>
            ))}
          </CardBody>
        </Card>
      </div>
    </div>
  );
}
