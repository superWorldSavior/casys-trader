import type { DecisionKind, DecisionRow } from "@/lib/types";

export function decisionKind(row: DecisionRow): DecisionKind {
  const reason = String(row.reason || row.rationale || "").toLowerCase();
  const source = String(row.decision_source || "").toLowerCase();
  const action = String(row.action || "").toUpperCase();
  const runtime = row.runtime ?? {};

  if (row.executed === true) return "exec";
  if (reason.startsWith("risk:") || reason.includes("risk:")) return "risk";
  if (reason.includes("stale")) return "stale";
  if (source === "armed_plan" || runtime.armed_plan_id) return "armed";
  if (runtime.trade_plan_created) return "plan";
  if (runtime.indicator_watch_created) return "watch";
  if (source === "infra" || (row.model_called === false && reason.includes("quiet"))) {
    return "quiet";
  }
  return action === "HOLD" ? "hold" : "signal";
}

export function mergeDecisions(report: DecisionRow[], recent: DecisionRow[]): DecisionRow[] {
  const seen = new Set<string>();
  const rows: DecisionRow[] = [];
  for (const row of [...recent, ...report]) {
    const key = [
      row.cycle_ts ?? row.ts ?? "",
      String(row.sequence ?? ""),
      row.symbol ?? "",
      row.action ?? "",
      row.rationale ?? row.reason ?? "",
    ].join("|");
    if (seen.has(key)) continue;
    seen.add(key);
    rows.push(row);
  }
  return rows.sort((a, b) => {
    const left = Date.parse(a.cycle_ts || a.ts || "") || 0;
    const right = Date.parse(b.cycle_ts || b.ts || "") || 0;
    return right - left;
  });
}

export const KIND_LABEL: Record<DecisionKind, string> = {
  exec: "filled",
  risk: "risk",
  stale: "stale",
  armed: "armed",
  plan: "plan",
  watch: "watch",
  quiet: "quiet",
  hold: "hold",
  signal: "signal",
};
