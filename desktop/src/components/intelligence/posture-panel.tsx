import { Compass } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { formatAgo } from "@/lib/format";
import {
  grossModeLabel,
  marketRegimeLabel,
  netBiasLabel,
  plainMarketLanguage,
  projectionFreshness,
} from "@/lib/humanize";
import type { GlobalIntelligencePosture, GlobalSituationDigest } from "@/lib/types";

export function PosturePanel({
  posture,
  digest,
}: {
  posture?: GlobalIntelligencePosture | null;
  digest?: GlobalSituationDigest | null;
}) {
  const postureFreshness = projectionFreshness(posture);
  const digestFreshness = projectionFreshness(digest);
  const postureTitle = posture?.gross_mode
    ? outlookTitle(posture.gross_mode)
    : "Current outlook unavailable";
  const updateLabel = posture?.as_of
    ? `Recorded ${formatAgo(posture.as_of)}`
    : postureFreshness.label;

  return (
    <section className="rounded-xl border border-line bg-panel/90 p-4 shadow-[0_1px_2px_rgba(23,43,54,0.04)] xl:p-5">
      <div className="flex flex-wrap items-start justify-between gap-4">
        <div className="flex items-start gap-4">
          <div className="flex size-10 shrink-0 items-center justify-center rounded-lg bg-accent/10 text-accent">
            <Compass className="size-5" aria-hidden="true" />
          </div>
          <h1 className="text-[26px] font-semibold leading-none tracking-[-0.035em] text-fg">
            {postureTitle}
          </h1>
        </div>
        <Badge tone={postureFreshness.current ? "gain" : "warn"}>{updateLabel}</Badge>
      </div>

      <p className="mt-3 max-w-5xl text-sm leading-6 text-muted">
        {plainMarketLanguage(posture?.rationale || digest?.points?.[0]?.point) || "Casys has not recorded a global rationale."}
      </p>

      <dl className="mt-4 grid gap-3 border-t border-hairline pt-3 sm:grid-cols-2">
        <PositionFact label="Directional view" value={netBiasLabel(posture?.net_bias)} />
        <PositionFact
          label="Market context"
          value={digest?.regime ? marketRegimeLabel(digest.regime) : "Not recorded"}
          meta={digest?.regime && !digestFreshness.current ? digestFreshness.label : undefined}
        />
      </dl>
    </section>
  );
}

function outlookTitle(value: string): string {
  const key = value.trim().toLowerCase();
  if (key === "risk_off" || key === "defensive") return "Defensive outlook";
  if (key === "cautious" || key === "selective") return "Selective outlook";
  if (key === "normal") return "Opportunity-seeking outlook";
  if (key === "watch") return "Watchful outlook";
  const label = grossModeLabel(value);
  return label === "—" ? "Current outlook unavailable" : `${label} outlook`;
}

function PositionFact({ label, value, meta }: { label: string; value: string; meta?: string }) {
  return (
    <div className="min-w-0">
      <dt className="text-xs text-dim">{label}</dt>
      <dd className="mt-1 text-sm font-medium text-fg">
        {value}
        {meta ? <span className="ml-2 text-[10px] font-normal text-warn">{meta}</span> : null}
      </dd>
    </div>
  );
}
