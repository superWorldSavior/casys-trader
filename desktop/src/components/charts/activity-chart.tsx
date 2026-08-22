import { Bar, BarChart, CartesianGrid, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";
import { decisionKind, mergeDecisions } from "@/lib/classify";
import { parseTs } from "@/lib/format";
import type { DecisionRow } from "@/lib/types";

const BUCKET_MS = 30 * 60 * 1000;

type Props = {
  report: DecisionRow[];
  recent: DecisionRow[];
};

export function ActivityChart({ report, recent }: Props) {
  const now = Date.now();
  const start = now - 12 * 60 * 60 * 1000;
  const buckets = new Map<
    number,
    {
      t: number;
      trades: number;
      decisions: number;
      attention: number;
      routine: number;
    }
  >();

  for (let t = start; t <= now; t += BUCKET_MS) {
    const key = Math.floor(t / BUCKET_MS) * BUCKET_MS;
    buckets.set(key, {
      t: key,
      trades: 0,
      decisions: 0,
      attention: 0,
      routine: 0,
    });
  }

  for (const row of mergeDecisions(report, recent)) {
    const date = parseTs(row.cycle_ts || row.ts);
    if (!date) continue;
    const timestamp = date.getTime();
    if (timestamp < start || timestamp > now) continue;
    const key = Math.floor(timestamp / BUCKET_MS) * BUCKET_MS;
    const bucket = buckets.get(key);
    if (!bucket) continue;
    const kind = decisionKind(row);
    if (kind === "exec") bucket.trades += 1;
    else if (kind === "risk" || kind === "stale") bucket.attention += 1;
    else if (kind === "quiet" || kind === "hold") bucket.routine += 1;
    else bucket.decisions += 1;
  }

  const data = [...buckets.values()].map((bucket) => ({
    ...bucket,
    label: new Date(bucket.t).toLocaleTimeString("en-GB", { hour: "2-digit", minute: "2-digit" }),
  }));
  const totals = data.reduce(
    (acc, bucket) => ({
      trades: acc.trades + bucket.trades,
      decisions: acc.decisions + bucket.decisions,
      attention: acc.attention + bucket.attention,
      routine: acc.routine + bucket.routine,
    }),
    { trades: 0, decisions: 0, attention: 0, routine: 0 },
  );
  const total = Object.values(totals).reduce((sum, value) => sum + value, 0);

  return (
    <div
      className="flex h-full w-full flex-col"
      role="group"
      aria-label={`${total} recorded events in the last 12 hours: ${totals.trades} completed trades, ${totals.decisions} decisions or plans, ${totals.attention} events needing attention and ${totals.routine} routine checks.`}
    >
      <dl className="mb-2 grid grid-cols-4 gap-2">
        <ActivityTotal label="Trades" value={totals.trades} tone="text-gain" />
        <ActivityTotal label="Decisions" value={totals.decisions} tone="text-accent" />
        <ActivityTotal label="Needs attention" value={totals.attention} tone="text-warn" />
        <ActivityTotal label="Routine" value={totals.routine} tone="text-faint" />
      </dl>
      {total === 0 ? (
        <div className="grid min-h-0 flex-1 place-items-center rounded-md border border-dashed border-hairline bg-ink/25 px-4 text-center text-xs text-faint">
          No activity was recorded in this 12-hour window.
        </div>
      ) : (
        <div className="min-h-0 flex-1" aria-hidden="true">
          <ResponsiveContainer width="100%" height="100%" minHeight={80}>
          <BarChart data={data} barCategoryGap={2}>
            <CartesianGrid stroke="var(--color-hairline)" vertical={false} />
            <XAxis
              dataKey="label"
              tick={{ fill: "var(--color-dim)", fontSize: 10, fontFamily: "IBM Plex Mono" }}
              interval={3}
              axisLine={false}
              tickLine={false}
            />
            <YAxis
              allowDecimals={false}
              width={24}
              tick={{ fill: "var(--color-dim)", fontSize: 10, fontFamily: "IBM Plex Mono" }}
              axisLine={false}
              tickLine={false}
            />
            <Tooltip
              cursor={{ fill: "var(--color-panel-hover)" }}
              contentStyle={{
                background: "var(--color-panel)",
                border: "1px solid var(--color-line)",
                borderRadius: 8,
                fontSize: 12,
              }}
              labelStyle={{ color: "var(--color-muted)" }}
            />
            <Bar dataKey="trades" name="Completed trades" stackId="a" fill="var(--color-gain)" />
            <Bar dataKey="decisions" name="Decisions and plans" stackId="a" fill="var(--color-accent)" />
            <Bar dataKey="attention" name="Needs attention" stackId="a" fill="var(--color-warn)" />
            <Bar dataKey="routine" name="Routine checks" stackId="a" fill="var(--color-faint)" radius={[2, 2, 0, 0]} />
          </BarChart>
          </ResponsiveContainer>
        </div>
      )}
    </div>
  );
}

function ActivityTotal({ label, value, tone }: { label: string; value: number; tone: string }) {
  return (
    <div className="min-w-0">
      <dt className="truncate text-[10px] text-dim">{label}</dt>
      <dd className={`font-mono text-sm font-semibold ${tone}`}>{value}</dd>
    </div>
  );
}
