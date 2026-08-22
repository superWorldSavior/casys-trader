/** AI only when decision_source is llm AND model_called is exactly true. */

export type ProvenanceKind = "ai" | "system" | "planned" | "recorded";

export type ProvenanceFields = {
  decision_source?: string | null;
  model_called?: boolean | null;
};

export function decisionProvenance(row: ProvenanceFields): ProvenanceKind {
  const source = String(row.decision_source ?? "").trim().toLowerCase();
  if (source === "llm" && row.model_called === true) return "ai";
  if (source === "infra" || source.startsWith("infra_")) return "system";
  if (
    row.model_called === false &&
    ["armed_plan", "armed-plan", "planned_execution", "plan_execution"].includes(source)
  ) {
    return "planned";
  }
  return "recorded";
}

export function provenanceLabel(kind: ProvenanceKind): string {
  if (kind === "ai") return "Casys AI";
  if (kind === "system") return "Automatic check";
  if (kind === "planned") return "Planned execution";
  return "Source unconfirmed";
}

export function provenanceTone(kind: ProvenanceKind): "accent" | "muted" | "warn" {
  if (kind === "ai") return "accent";
  if (kind === "system" || kind === "planned") return "muted";
  return "warn";
}

export function provenanceDetail(kind: ProvenanceKind): string {
  if (kind === "ai") return "Casys AI wrote this decision after reviewing the available information.";
  if (kind === "system") {
    return "An automatic safety or data check recorded this outcome. It is not an AI opinion.";
  }
  if (kind === "planned") {
    return "This action followed a plan Casys had already prepared. No new AI review was made at execution time.";
  }
  return "This record does not confirm who produced it, so Casys does not present it as an AI decision.";
}
