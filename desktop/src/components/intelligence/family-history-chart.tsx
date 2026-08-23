import { Circle } from "lucide-react";
import {
  CartesianGrid,
  Line,
  LineChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import { familyLabel } from "@/lib/humanize";
import type { FamilyIntelligence } from "@/lib/types";

const COLORS = [
  "var(--color-accent)",
  "var(--color-gain)",
  "var(--color-warn)",
  "var(--color-dim)",
  "var(--color-loss)",
  "var(--color-accent-dim)",
];

export function FamilyPositioningChart({ rows }: { rows: FamilyIntelligence[] }) {
  const series = rows
    .filter((row) => row.history.filter((point) => typeof point.rank === "number").length > 1)
    .slice(0, 6);
  const byTime = new Map<number, Record<string, number>>();
  for (const row of series) {
    for (const point of row.history) {
      const timestamp = Date.parse(point.as_of || "");
      if (!Number.isFinite(timestamp)) continue;
      const current = byTime.get(timestamp) ?? { timestamp };
      byTime.set(timestamp, current);
      if (typeof point.rank === "number") current[row.family] = point.rank;
    }
  }
  const data = Array.from(byTime.values()).sort((left, right) => left.timestamp - right.timestamp);
  const maxRank = Math.max(
    1,
    ...series.flatMap((row) =>
      row.history.flatMap((point) => (typeof point.rank === "number" ? [point.rank] : [])),
    ),
  );

  if (!series.length || data.length < 2) {
    return <p className="px-4 py-10 text-sm text-faint">Not enough historical boards to draw positioning.</p>;
  }

  return (
    <div>
      <div className="mb-3 flex flex-wrap gap-x-4 gap-y-1 px-1">
        {series.map((row, index) => (
          <div key={row.family} className="flex items-center gap-1.5">
            <Circle className="size-2 fill-current" style={{ color: COLORS[index % COLORS.length] }} aria-hidden="true" />
            <span className="font-mono text-[9px] text-dim">{familyLabel(row.family)}</span>
          </div>
        ))}
      </div>
      <div className="h-52">
        <ResponsiveContainer width="100%" height="100%">
          <LineChart data={data} margin={{ top: 4, right: 10, bottom: 0, left: -14 }}>
            <CartesianGrid stroke="var(--color-hairline)" vertical={false} />
            <XAxis
              dataKey="timestamp"
              type="number"
              domain={["dataMin", "dataMax"]}
              tickFormatter={(value) => shortDate(Number(value))}
              tick={{ fill: "var(--color-dim)", fontFamily: "IBM Plex Mono", fontSize: 9 }}
              axisLine={{ stroke: "var(--color-line)" }}
              tickLine={false}
              minTickGap={36}
            />
            <YAxis
              reversed
              domain={[1, maxRank]}
              allowDecimals={false}
              tick={{ fill: "var(--color-dim)", fontFamily: "IBM Plex Mono", fontSize: 9 }}
              axisLine={false}
              tickLine={false}
              width={34}
            />
            <Tooltip
              labelFormatter={(value) => new Date(Number(value)).toLocaleString(undefined, {
                day: "2-digit",
                month: "short",
                hour: "2-digit",
                minute: "2-digit",
              })}
              formatter={(value, name) => [`priority ${String(value)}`, familyLabel(String(name))]}
              contentStyle={{
                background: "var(--color-panel)",
                border: "1px solid var(--color-line)",
                borderRadius: 7,
                fontSize: 11,
                color: "var(--color-fg)",
              }}
            />
            {series.map((row, index) => (
              <Line
                key={row.family}
                dataKey={row.family}
                type="stepAfter"
                stroke={COLORS[index % COLORS.length]}
                strokeWidth={1.6}
                dot={false}
                activeDot={{ r: 3, strokeWidth: 0 }}
                isAnimationActive={false}
              />
            ))}
          </LineChart>
        </ResponsiveContainer>
      </div>
      <p className="mt-1 px-1 font-mono text-[9px] text-faint">
        Priority 1 is highest. Lines use only recorded theme priorities; gaps are not inferred.
      </p>
    </div>
  );
}

export function FamilySparkline({ row }: { row: FamilyIntelligence }) {
  const history = row.history.slice(-30);
  const observed = history.filter(
    (point): point is typeof point & { rank: number } => typeof point.rank === "number",
  );
  if (observed.length < 2) {
    return <span className="font-mono text-[10px] text-faint">one observation</span>;
  }
  const values = observed.map((point) => point.rank);
  const low = Math.min(...values);
  const high = Math.max(...values);
  const color = row.rank_delta && row.rank_delta > 0 ? "var(--color-gain)" : row.rank_delta && row.rank_delta < 0 ? "var(--color-loss)" : "var(--color-accent)";

  return (
    <div
      title={`${familyLabel(row.family)} priority history: ${history
        .map((point) => (typeof point.rank === "number" ? point.rank : "gap"))
        .join(", ")}`}
    >
      <div className="h-8 w-[104px]" role="img" aria-label={`${familyLabel(row.family)} priority movement`}>
        <ResponsiveContainer width="100%" height="100%">
          <LineChart data={history}>
            <YAxis hide reversed domain={[Math.max(1, low - 1), high + 1]} />
            <Line
              type="stepAfter"
              dataKey="rank"
              stroke={color}
              strokeWidth={1.7}
              dot={false}
              activeDot={{ r: 2.2, strokeWidth: 0 }}
              connectNulls={false}
              isAnimationActive={false}
            />
          </LineChart>
        </ResponsiveContainer>
      </div>
      <p className="font-mono text-[9px] text-faint">
        priority {values[0]} → {values.at(-1)} · {observed.length} reviews
      </p>
    </div>
  );
}

function shortDate(value: number): string {
  return new Date(value).toLocaleDateString(undefined, { day: "2-digit", month: "short" });
}
