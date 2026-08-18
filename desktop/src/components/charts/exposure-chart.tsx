import { Cell, Pie, PieChart, ResponsiveContainer, Tooltip } from "recharts";
import type { Holding } from "@/lib/types";

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
  holdings: Holding[];
};

export function ExposureChart({ holdings }: Props) {
  const data = holdings
    .map((holding) => ({
      name: holding.symbol,
      value: Math.abs((holding.last_price ?? holding.avg_price ?? 0) * holding.quantity),
    }))
    .filter((row) => row.value > 0)
    .sort((a, b) => b.value - a.value);

  if (!data.length) {
    return <p className="px-2 py-8 text-center text-sm text-faint">No open exposure</p>;
  }

  return (
    <ResponsiveContainer width="100%" height="100%" minHeight={160}>
      <PieChart>
        <Pie
          data={data}
          dataKey="value"
          nameKey="name"
          innerRadius={48}
          outerRadius={78}
          paddingAngle={2}
          stroke="var(--color-panel)"
        >
          {data.map((entry, index) => (
            <Cell key={entry.name} fill={PALETTE[index % PALETTE.length]} />
          ))}
        </Pie>
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
      </PieChart>
    </ResponsiveContainer>
  );
}
