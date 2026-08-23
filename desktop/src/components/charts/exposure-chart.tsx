import {
  Bar,
  BarChart,
  Cell,
  LabelList,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";

// Palette dérivée des design tokens — opacités décroissantes de l'accent + gain/loss/warn/dim
const PALETTE = [
  "var(--color-accent)",
  "var(--color-gain)",
  "var(--color-accent-dim)",
  "var(--color-warn)",
  "var(--color-loss)",
  "var(--color-dim)",
  "var(--color-muted)",
  "var(--color-faint)",
];

type Props = {
  exposures: Array<{ symbol: string; value: number }>;
  companyMap?: Record<string, string>;
};

export function ExposureChart({ exposures, companyMap = {} }: Props) {
  const data = exposures
    .map((exposure) => ({
      symbol: exposure.symbol,
      name: companyMap[exposure.symbol]?.trim() || exposure.symbol,
      shortName: chartCompanyName(companyMap[exposure.symbol]?.trim() || exposure.symbol),
      value: Math.abs(exposure.value),
    }))
    .filter((row) => row.value > 0)
    .sort((a, b) => b.value - a.value);

  if (!data.length) {
    return <p className="px-2 py-8 text-center text-sm text-faint">No open exposure</p>;
  }

  return (
    <div
      className="flex h-full min-h-40 flex-col"
      role="img"
      aria-label={`Current USD exposure by company: ${data
        .slice(0, 6)
        .map((entry) => `${entry.name}, ${entry.value.toLocaleString("en-US", { style: "currency", currency: "USD", maximumFractionDigits: 0 })}`)
        .join("; ")}${data.length > 6 ? `; and ${data.length - 6} smaller exposures` : ""}`}
    >
      <div
        className="min-h-0 flex-1"
        aria-hidden="true"
      >
        <ResponsiveContainer width="100%" height="100%" minHeight={160}>
          <BarChart
            data={data.slice(0, 6)}
            layout="vertical"
            margin={{ top: 2, right: 50, bottom: 2, left: 0 }}
          >
            <XAxis type="number" hide />
            <YAxis
              type="category"
              dataKey="shortName"
              width={152}
              tickLine={false}
              axisLine={false}
              tick={{ fill: "var(--color-dim)", fontSize: 11 }}
            />
            <Bar
              dataKey="value"
              name="Exposure"
              barSize={12}
              radius={[0, 4, 4, 0]}
              isAnimationActive={false}
            >
              {data.slice(0, 6).map((entry, index) => (
                <Cell key={entry.symbol} fill={PALETTE[index % PALETTE.length]} />
              ))}
              <LabelList
                dataKey="value"
                position="right"
                formatter={(value: unknown) => formatCompactUsd(Number(value))}
                style={{ fill: "var(--color-fg)", fontSize: 10, fontFamily: "var(--font-mono)" }}
              />
            </Bar>
            <Tooltip
              formatter={(value) =>
                typeof value === "number"
                  ? value.toLocaleString("en-US", { style: "currency", currency: "USD", maximumFractionDigits: 0 })
                  : value
              }
              contentStyle={{
                background: "var(--color-panel)",
                border: "1px solid var(--color-line)",
                borderRadius: 8,
                fontSize: 12,
                color: "var(--color-fg)",
              }}
            />
          </BarChart>
        </ResponsiveContainer>
      </div>
      {data.length > 6 ? <p className="pt-1 text-center text-[10px] text-faint">+{data.length - 6} smaller exposures</p> : null}
    </div>
  );
}

function chartCompanyName(value: string): string {
  const short = value
    .replace(/,?\s+(?:Co\.,?\s+Ltd\.?|Corporation|Company|plc|S\.A\.|SE|Aktiengesellschaft)$/i, "")
    .trim();
  return short.length > 24 ? `${short.slice(0, 23).trimEnd()}…` : short;
}

function formatCompactUsd(value: number): string {
  if (Math.abs(value) >= 1_000_000) return `$${(value / 1_000_000).toFixed(1)}m`;
  if (Math.abs(value) >= 1_000) return `$${(value / 1_000).toFixed(1)}k`;
  return `$${Math.round(value)}`;
}
