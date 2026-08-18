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
  const buckets = new Map<number, { t: number; exec: number; hold: number; quiet: number; stale: number; risk: number }>();

  for (let t = start; t <= now; t += BUCKET_MS) {
    const key = Math.floor(t / BUCKET_MS) * BUCKET_MS;
    buckets.set(key, { t: key, exec: 0, hold: 0, quiet: 0, stale: 0, risk: 0 });
  }

  for (const row of mergeDecisions(report, recent)) {
    const date = parseTs(row.cycle_ts || row.ts);
    if (!date) continue;
    const key = Math.floor(date.getTime() / BUCKET_MS) * BUCKET_MS;
    const bucket = buckets.get(key);
    if (!bucket) continue;
    const kind = decisionKind(row);
    if (kind === "exec") bucket.exec += 1;
    else if (kind === "risk") bucket.risk += 1;
    else if (kind === "stale") bucket.stale += 1;
    else if (kind === "quiet") bucket.quiet += 1;
    else bucket.hold += 1;
  }

  const data = [...buckets.values()].map((bucket) => ({
    ...bucket,
    label: new Date(bucket.t).toLocaleTimeString("en-GB", { hour: "2-digit", minute: "2-digit" }),
  }));

  return (
    <ResponsiveContainer width="100%" height="100%" minHeight={160}>
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
        <Bar dataKey="exec" stackId="a" fill="var(--color-gain)" />
        <Bar dataKey="risk" stackId="a" fill="var(--color-loss)" />
        <Bar dataKey="stale" stackId="a" fill="var(--color-warn)" />
        <Bar dataKey="quiet" stackId="a" fill="var(--color-faint)" />
        <Bar dataKey="hold" stackId="a" fill="var(--color-dim)" radius={[2, 2, 0, 0]} />
      </BarChart>
    </ResponsiveContainer>
  );
}
