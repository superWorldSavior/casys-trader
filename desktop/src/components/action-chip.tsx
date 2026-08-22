import { cn } from "@/lib/utils";
import { decisionActionLabel } from "@/lib/humanize";

export function ActionChip({ action }: { action?: string }) {
  const value = (action ?? "—").toUpperCase();
  return (
    <span
      className={cn(
        "inline-flex min-w-12 justify-center rounded-sm px-1.5 py-0.5 text-[10px] font-semibold uppercase tracking-[0.08em]",
        value === "BUY" && "bg-gain/15 text-gain",
        value === "SELL" && "bg-loss/15 text-loss",
        value !== "BUY" && value !== "SELL" && "bg-hairline text-dim",
      )}
    >
      <span>{decisionActionLabel(value)}</span>
    </span>
  );
}
