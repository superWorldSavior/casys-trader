import { useMemo, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { Badge } from "@/components/ui/badge";
import { Card, CardBody, CardHeader, CardTitle } from "@/components/ui/card";
import { useReports } from "@/hooks/use-desk-api";
import { readReport } from "@/lib/api";
import { formatAgo } from "@/lib/format";
import { grossModeLabel, humanToken, marketRegimeLabel, netBiasLabel } from "@/lib/humanize";
import { cn } from "@/lib/utils";

const KINDS = ["global", "macro", "regional", "micro"] as const;

export function ReportsPage() {
  const list = useReports();
  const [key, setKey] = useState<string | null>(null);
  const items = list.data?.items ?? [];
  const selected = key ?? items[0]?.key ?? null;
  const detail = useQuery({
    queryKey: ["report", selected],
    queryFn: () => readReport(selected ?? ""),
    enabled: Boolean(selected),
    staleTime: 30_000,
  });
  const grouped = useMemo(
    () => KINDS.map((kind) => ({ kind, items: items.filter((item) => item.kind === kind) })),
    [items],
  );
  const payload = detail.data?.payload ?? {};

  return (
    <div className="grid gap-4 min-[900px]:grid-cols-[320px_minmax(0,1fr)]">
      <Card className="max-h-[calc(100vh-8rem)] overflow-auto">
        <CardHeader>
          <CardTitle>Report history</CardTitle>
          <span className="font-mono text-[10px] text-faint">{items.length}</span>
        </CardHeader>
        <CardBody className="space-y-4">
          {list.error ? (
            <p className="text-sm text-loss">{list.error instanceof Error ? list.error.message : String(list.error)}</p>
          ) : null}
          {list.isPending && !list.data ? <p className="text-sm text-faint">Loading report history…</p> : null}
          {list.data && !items.length ? <p className="text-sm text-faint">No reports are available.</p> : null}
          {list.data ? grouped.map((group) => (
            <div key={group.kind}>
              <p className="mb-2 font-mono text-[10px] uppercase tracking-[0.16em] text-faint">{group.kind}</p>
              <div className="space-y-1">
                {group.items.map((item) => (
                  <button
                    key={item.key}
                    type="button"
                    onClick={() => setKey(item.key)}
                    aria-pressed={selected === item.key}
                    className={cn(
                      "w-full rounded-md px-2 py-1.5 text-left hover:bg-panel-hover",
                      selected === item.key && "bg-panel-hover",
                    )}
                  >
                    <p className="text-sm font-medium">{item.label}</p>
                    <p className="font-mono text-[10px] text-faint">
                      {item.as_of ? formatAgo(item.as_of) : "—"}
                      {item.depth ? ` · ${item.depth}` : ""}
                    </p>
                  </button>
                ))}
                {group.items.length === 0 ? <p className="text-xs text-faint">None</p> : null}
              </div>
            </div>
          )) : null}
        </CardBody>
      </Card>
      <Card className="min-h-[480px]">
        <CardHeader>
          <CardTitle>{detail.data?.label || "Report"}</CardTitle>
          {detail.data?.kind ? <Badge>{detail.data.kind}</Badge> : null}
        </CardHeader>
        <CardBody className="space-y-4">
          {selected && detail.isPending && !detail.data ? <p className="text-sm text-faint">Loading the selected report…</p> : null}
          {!selected && list.data ? <p className="text-sm text-faint">Select a report when one becomes available.</p> : null}
          {detail.error ? (
            <p className="text-sm text-loss">
              {detail.error instanceof Error ? detail.error.message : String(detail.error)}
            </p>
          ) : null}
          {detail.data && !detail.data.error ? (
            <>
              {detail.data.kind === "regional" ? (
                <RegionalDetail payload={payload} />
              ) : (
                <>
                  <Block
                    title="Posture"
                    value={pick(payload, ["posture", "stance", "regime", "gross_mode", "net_bias"])}
                  />
                  <List
                    title="Points"
                    values={listish(payload, ["points", "key_points", "bullets", "takeaways", "family_priority", "venue_posture"])}
                  />
                  <Block
                    title="Brief"
                    value={pick(payload, ["brief", "summary", "thesis", "narrative", "text", "rationale"])}
                  />
                </>
              )}
              <details className="rounded-md border border-hairline px-3 py-2">
                <summary className="cursor-pointer font-mono text-[10px] uppercase tracking-[0.16em] text-faint">
                  Raw report data
                </summary>
                <pre className="mt-2 max-h-[360px] overflow-auto text-xs text-dim">
                  {JSON.stringify(payload, null, 2)}
                </pre>
              </details>
            </>
          ) : detail.data?.error ? (
            <p className="text-sm text-loss">This report could not be opened.</p>
          ) : null}
        </CardBody>
      </Card>
    </div>
  );
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return Boolean(value) && typeof value === "object" && !Array.isArray(value);
}

function RegionalDetail({ payload }: { payload: Record<string, unknown> }) {
  const waiting =
    isRecord(payload.latest_waiting) && payload.latest_waiting.status === "waiting_brief"
      ? payload.latest_waiting
      : payload.status === "waiting_brief"
        ? payload
        : null;
  const status = String(waiting?.status ?? payload.status ?? "");
  const errorCode = String(waiting?.error_code ?? payload.error_code ?? "");
  const summary = typeof payload.summary === "string" ? payload.summary : null;
  const previous = Boolean(waiting && payload.status === "success");
  const postures = isRecord(payload.family_postures) ? payload.family_postures : {};
  const hotlist = Array.isArray(payload.selected_hotlist)
    ? payload.selected_hotlist.map((item) => String(item))
    : [];

  return (
    <>
      <div className="flex flex-wrap items-center gap-2">
        {status ? <Badge>{status}</Badge> : null}
        {errorCode ? <span className="font-mono text-[10px] text-faint">{errorCode}</span> : null}
      </div>
      {waiting ? (
        <div className="rounded-lg border border-hairline bg-panel/55 px-3 py-2 text-xs text-muted">
          {previous
            ? "Current pre-open waiting · last success kept."
            : "Current pre-open waiting · not an empty report."}
        </div>
      ) : null}
      {previous ? (
        <p className="font-mono text-[10px] uppercase tracking-[0.16em] text-faint">Previous generation</p>
      ) : null}
      <Block title="Summary" value={summary} />
      <List
        title="Theme outlooks"
        values={Object.entries(postures).map(([name, value]) =>
          typeof value === "string" ? `${name}: ${value}` : `${name}: ${JSON.stringify(value)}`,
        )}
      />
      <List title="Companies in focus" values={hotlist} />
    </>
  );
}

function pick(payload: Record<string, unknown>, keys: string[]): string | null {
  for (const key of keys) {
    const value = payload[key];
    if (typeof value === "string" && value.trim()) return value;
  }
  return null;
}

function listish(payload: Record<string, unknown>, keys: string[]): string[] {
  for (const key of keys) {
    const value = payload[key];
    if (Array.isArray(value)) {
      return value.map((item) => (typeof item === "string" ? item : JSON.stringify(item))).filter(Boolean);
    }
    if (value && typeof value === "object") {
      return Object.entries(value as Record<string, unknown>).map(([name, item]) =>
        typeof item === "string" ? `${name}: ${item}` : `${name}: ${JSON.stringify(item)}`,
      );
    }
  }
  return [];
}

function Block({ title, value }: { title: string; value: string | null }) {
  return (
    <section>
      <p className="mb-1 font-mono text-[10px] uppercase tracking-[0.16em] text-faint">{title}</p>
      <p className="text-sm leading-relaxed text-muted">{reportValueLabel(value)}</p>
    </section>
  );
}

function reportValueLabel(value: string | null): string {
  if (!value) return "—";
  const key = value.toLowerCase();
  if (["risk_off", "risk_on"].includes(key)) return marketRegimeLabel(value);
  if (["cautious", "normal"].includes(key)) return grossModeLabel(value);
  if (["short", "neutral", "long"].includes(key)) return netBiasLabel(value);
  return humanToken(value);
}

function List({ title, values }: { title: string; values: string[] }) {
  return (
    <section>
      <p className="mb-1 font-mono text-[10px] uppercase tracking-[0.16em] text-faint">{title}</p>
      {values.length === 0 ? <p className="text-sm text-faint">—</p> : null}
      <ul className="list-disc space-y-1 pl-5 text-sm text-muted">
        {values.map((item) => (
          <li key={item}>{item}</li>
        ))}
      </ul>
    </section>
  );
}
