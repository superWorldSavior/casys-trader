import {
  Area,
  AreaChart,
  CartesianGrid,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import type { PriceBar } from "@/lib/types";

type Props = {
  bars: PriceBar[];
};

function fmtDate(ts: string): string {
  // Accepts ISO date strings like "2026-08-18T00:00:00"
  const d = new Date(ts);
  if (Number.isNaN(d.getTime())) return ts.slice(0, 10);
  return d.toLocaleDateString("en-GB", { day: "2-digit", month: "short" });
}

function fmtPrice(value: number): string {
  return value.toLocaleString("en-US", {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  });
}

export function PriceChart({ bars }: Props) {
  if (!bars.length) return null;

  const closes = bars.map((b) => b.close).filter(Number.isFinite);
  if (!closes.length) return null;

  const min = Math.min(...closes);
  const max = Math.max(...closes);
  const pad = Math.max((max - min) * 0.1, 0.01);

  return (
    <div style={{ height: 180, width: "100%" }}>
      <ResponsiveContainer width="100%" height="100%">
        <AreaChart data={bars} margin={{ top: 6, right: 8, left: 0, bottom: 0 }}>
          <defs>
            <linearGradient id="priceFill" x1="0" y1="0" x2="0" y2="1">
              <stop offset="0%" stopColor="var(--color-accent)" stopOpacity={0.22} />
              <stop offset="100%" stopColor="var(--color-accent)" stopOpacity={0} />
            </linearGradient>
          </defs>
          <CartesianGrid stroke="var(--color-hairline)" vertical={false} />
          <XAxis
            dataKey="ts"
            tickFormatter={(v) => fmtDate(String(v))}
            tick={{ fill: "var(--color-dim)", fontSize: 10, fontFamily: "IBM Plex Mono" }}
            minTickGap={40}
            axisLine={false}
            tickLine={false}
          />
          <YAxis
            domain={[min - pad, max + pad]}
            tickFormatter={fmtPrice}
            width={58}
            tick={{ fill: "var(--color-dim)", fontSize: 10, fontFamily: "IBM Plex Mono" }}
            axisLine={false}
            tickLine={false}
          />
          <Tooltip
            contentStyle={{
              background: "var(--color-panel)",
              border: "1px solid var(--color-line)",
              borderRadius: 8,
              fontSize: 12,
              color: "var(--color-fg)",
            }}
            labelFormatter={(label) => fmtDate(String(label))}
            formatter={(value, name) => {
              if (name === "close") return [fmtPrice(Number(value)), "close"];
              if (name === "volume")
                return [
                  Number(value).toLocaleString("en-US", { maximumFractionDigits: 0 }),
                  "vol",
                ];
              return [value, name];
            }}
            itemStyle={{ color: "var(--color-muted)" }}
          />
          <Area
            type="monotone"
            dataKey="close"
            stroke="var(--color-accent)"
            strokeWidth={2}
            fill="url(#priceFill)"
            dot={false}
            isAnimationActive={false}
          />
        </AreaChart>
      </ResponsiveContainer>
    </div>
  );
}
