import { Area, AreaChart, CartesianGrid, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";
import { formatDayTime } from "@/lib/format";
import type { EquityPoint } from "@/lib/types";
import { cn } from "@/lib/utils";

type Props = {
  points: EquityPoint[];
  className?: string;
  minHeight?: number;
  compact?: boolean;
};

export function EquityChart({ points, className, minHeight = 240, compact = false }: Props) {
  if (!points.length) {
    return <p className="px-2 py-8 text-center text-sm text-faint">No equity history yet</p>;
  }

  const validPoints = points.filter((point) => Number.isFinite(point.equity));
  const values = validPoints.map((point) => point.equity);
  if (!values.length) {
    return <p className="px-2 py-8 text-center text-sm text-faint">No equity history yet</p>;
  }
  const min = Math.min(...values);
  const max = Math.max(...values);
  const pad = Math.max((max - min) * 0.08, 1);
  const first = values[0];
  const last = values.at(-1) ?? first;

  return (
    <div
      className={cn("h-full w-full", className)}
      style={{ minHeight }}
      role="img"
      aria-label={`Reported portfolio value moved from ${first.toLocaleString("en-US", { style: "currency", currency: "USD", maximumFractionDigits: 0 })} to ${last.toLocaleString("en-US", { style: "currency", currency: "USD", maximumFractionDigits: 0 })} across ${values.length} recorded points.`}
    >
      <ResponsiveContainer width="100%" height="100%" minHeight={minHeight}>
        <AreaChart data={validPoints} margin={compact ? { top: 5, right: 1, left: 1, bottom: 1 } : { top: 8, right: 8, left: 0, bottom: 0 }}>
          <defs>
            <linearGradient id="equityFill" x1="0" y1="0" x2="0" y2="1">
              <stop offset="0%" stopColor="var(--color-accent)" stopOpacity={0.24} />
              <stop offset="100%" stopColor="var(--color-accent)" stopOpacity={0} />
            </linearGradient>
          </defs>
          {compact ? null : <CartesianGrid stroke="var(--color-hairline)" vertical={false} />}
          {compact ? null : (
            <XAxis
              dataKey="ts"
              tickFormatter={(value) => formatDayTime(String(value))}
              tick={{ fill: "var(--color-dim)", fontSize: 10, fontFamily: "IBM Plex Mono" }}
              minTickGap={32}
              axisLine={false}
              tickLine={false}
            />
          )}
          {compact ? null : (
            <YAxis
              domain={[min - pad, max + pad]}
              tickFormatter={(value) =>
                Number(value).toLocaleString("en-US", { maximumFractionDigits: 0 })
              }
              width={64}
              tick={{ fill: "var(--color-dim)", fontSize: 10, fontFamily: "IBM Plex Mono" }}
              axisLine={false}
              tickLine={false}
            />
          )}
          <Tooltip
            contentStyle={{
              background: "var(--color-panel)",
              border: "1px solid var(--color-line)",
              borderRadius: 8,
              fontSize: 12,
              color: "var(--color-fg)",
            }}
            labelFormatter={(label) => formatDayTime(String(label))}
            formatter={(value) =>
              typeof value === "number"
                ? [value.toLocaleString("en-US", { style: "currency", currency: "USD" }), "equity"]
                : [value, "equity"]
            }
          />
          <Area
            type="monotone"
            dataKey="equity"
            stroke="var(--color-accent)"
            strokeWidth={compact ? 1.75 : 2}
            fill="url(#equityFill)"
            isAnimationActive={false}
          />
        </AreaChart>
      </ResponsiveContainer>
    </div>
  );
}
