import { Badge } from "@/components/ui/badge";
import { formatAgo } from "@/lib/format";
import type { GlobalIntelligencePosture, GlobalSituationDigest } from "@/lib/types";
import { cn } from "@/lib/utils";

export function PosturePanel({
  posture,
  digest,
}: {
  posture?: GlobalIntelligencePosture | null;
  digest?: GlobalSituationDigest | null;
}) {
  const priorities = posture?.family_priority;
  return (
    <section className="relative overflow-hidden rounded-lg border border-line bg-panel/75 p-5">
      <div className="pointer-events-none absolute -right-10 -top-20 size-56 rounded-full border border-accent/10" />
      <div className="pointer-events-none absolute -right-2 -top-12 size-36 rounded-full border border-accent/15" />
      <div className="relative">
        <div className="flex flex-wrap items-start justify-between gap-3">
          <div className="min-w-0 flex-1">
            <p className="font-mono text-[9px] uppercase tracking-[0.22em] text-accent">Current world posture</p>
            <div className="mt-2 space-y-2">
              <ModeGauge
                label="Gross mode"
                value={posture?.gross_mode}
                levels={GROSS_MODES}
              />
              <ModeGauge
                label="Net bias"
                value={posture?.net_bias}
                levels={NET_BIASES}
              />
            </div>
          </div>
          <div className="flex items-center gap-2">
            {digest?.regime ? <Badge tone="warn">{label(digest.regime)}</Badge> : null}
            {posture?.persistence_status ? <Badge>{label(posture.persistence_status)}</Badge> : null}
          </div>
        </div>
        <p className="mt-4 max-w-4xl text-[15px] leading-relaxed text-muted">
          {posture?.rationale || digest?.points?.[0]?.point || "No current global rationale is available."}
        </p>
        <div className="mt-4 flex flex-wrap gap-1.5">
          {(priorities?.favored ?? []).map((family) => (
            <Badge key={family} tone="gain">
              {label(family)}
            </Badge>
          ))}
          {(priorities?.deprioritized ?? []).map((family) => (
            <Badge key={family}>{label(family)}</Badge>
          ))}
        </div>
        <div className="mt-5 grid grid-cols-3 gap-2 border-t border-hairline pt-4">
          {(["TW", "EU", "US"] as const).map((venue) => (
            <div key={venue}>
              <p className="font-mono text-[9px] uppercase tracking-[0.18em] text-faint">{venue}</p>
              <p className="mt-1 text-sm text-muted">{label(posture?.venue_posture?.[venue]) || "—"}</p>
            </div>
          ))}
        </div>
        <p className="mt-4 font-mono text-[9px] uppercase tracking-[0.14em] text-faint">
          {posture?.as_of ? `Updated ${formatAgo(posture.as_of)}` : "Projection not available"}
          {digest?.rates_bias ? ` · rates ${label(digest.rates_bias)}` : ""}
          {digest?.usd_bias ? ` · USD ${label(digest.usd_bias)}` : ""}
        </p>
      </div>
    </section>
  );
}

function label(value: string | null | undefined): string {
  return (value ?? "").replaceAll("_", " ");
}

// Ordinal scales for segmented gauges (defensive → normal left → right).
// gross_mode domain: risk_off | cautious | normal  (verified in live data)
// net_bias  domain: short     | neutral  | long
const GROSS_MODES = ["risk_off", "cautious", "normal"] as const;
const NET_BIASES = ["short", "neutral", "long"] as const;

type Levels = readonly string[];

function ModeGauge({
  label: labelText,
  value,
  levels,
}: {
  label: string;
  value: string | null | undefined;
  levels: Levels;
}) {
  const idx = value ? levels.indexOf(value) : -1;

  // Unknown value: fall back to the current badge-text style.
  if (idx < 0) {
    return (
      <div>
        <p className="font-mono text-[9px] uppercase tracking-[0.14em] text-faint">{labelText}</p>
        {value ? (
          <Badge className="mt-1">{label(value)}</Badge>
        ) : (
          <p className="mt-1 text-sm text-faint">—</p>
        )}
      </div>
    );
  }

  return (
    <div>
      <p className="font-mono text-[9px] uppercase tracking-[0.14em] text-faint">
        {labelText}
        <span className="ml-1.5 normal-case text-muted">{label(value)}</span>
      </p>
      <div className="mt-1 flex gap-0.5">
        {levels.map((lv, i) => (
          <div
            key={lv}
            title={lv.replaceAll("_", " ")}
            className={cn(
              "h-1.5 flex-1 rounded-sm transition-colors",
              i === idx ? "bg-accent" : "bg-hairline",
            )}
          />
        ))}
      </div>
    </div>
  );
}
