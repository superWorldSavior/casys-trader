import { Cell, Pie, PieChart, ResponsiveContainer, Tooltip } from "recharts";
import type { Holding } from "@/lib/types";

const COLORS = ["#ffb86f", "#a5c98c", "#e5c07b", "#d5c3b5", "#e87f66", "#8d8177", "#c48a4a", "#6b6157"];

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
    <ResponsiveContainer width="100%" height="100%">
      <PieChart>
        <Pie data={data} dataKey="value" nameKey="name" innerRadius={48} outerRadius={78} paddingAngle={2} stroke="#0f0e0c">
          {data.map((entry, index) => (
            <Cell key={entry.name} fill={COLORS[index % COLORS.length]} />
          ))}
        </Pie>
        <Tooltip
          formatter={(value) =>
            typeof value === "number"
              ? value.toLocaleString("en-US", { style: "currency", currency: "USD", maximumFractionDigits: 0 })
              : value
          }
          contentStyle={{ background: "#1a1815", border: "1px solid #332c23", borderRadius: 8, fontSize: 12 }}
        />
      </PieChart>
    </ResponsiveContainer>
  );
}
