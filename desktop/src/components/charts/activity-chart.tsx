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
    <ResponsiveContainer width="100%" height="100%">
      <BarChart data={data} barCategoryGap={2}>
        <CartesianGrid stroke="#26211b" vertical={false} />
        <XAxis dataKey="label" tick={{ fill: "#6b6157", fontSize: 10, fontFamily: "IBM Plex Mono" }} interval={3} axisLine={false} tickLine={false} />
        <YAxis allowDecimals={false} width={24} tick={{ fill: "#6b6157", fontSize: 10, fontFamily: "IBM Plex Mono" }} axisLine={false} tickLine={false} />
        <Tooltip
          cursor={{ fill: "#211e19" }}
          contentStyle={{ background: "#1a1815", border: "1px solid #332c23", borderRadius: 8, fontSize: 12 }}
          labelStyle={{ color: "#d5c3b5" }}
        />
        <Bar dataKey="exec" stackId="a" fill="#a5c98c" />
        <Bar dataKey="risk" stackId="a" fill="#e87f66" />
        <Bar dataKey="stale" stackId="a" fill="#e5c07b" />
        <Bar dataKey="quiet" stackId="a" fill="#6b6157" />
        <Bar dataKey="hold" stackId="a" fill="#8d8177" radius={[2, 2, 0, 0]} />
      </BarChart>
    </ResponsiveContainer>
  );
}
