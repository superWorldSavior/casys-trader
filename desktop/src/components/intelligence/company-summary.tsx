import { ArrowUpRight } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { formatAgo } from "@/lib/format";
import type { CompanyBrief, CompanyEvidencePoint, CompanyIntelligence } from "@/lib/types";
import { cn } from "@/lib/utils";

export function CompanyRadarRow({
  company,
  selected,
  onSelect,
}: {
  company: CompanyIntelligence;
  selected: boolean;
  onSelect: () => void;
}) {
  return (
    <button
      type="button"
      onClick={onSelect}
      aria-pressed={selected}
      className={cn(
        "w-full border-b border-hairline px-3 py-3 text-left transition-colors last:border-0",
        selected ? "bg-panel-hover" : "hover:bg-panel/60",
      )}
    >
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0">
          <div className="flex flex-wrap items-center gap-1.5">
            <span className="text-sm font-semibold">{company.symbol}</span>
            <span className="truncate text-xs text-dim">{company.name}</span>
            {company.thesis_changed ? <span className="size-1.5 rounded-full bg-accent" title="Thesis changed" /> : null}
          </div>
          <p className="mt-1 line-clamp-2 text-xs leading-relaxed text-muted">{company.summary || "No thesis summary."}</p>
        </div>
        <div className="shrink-0 text-right">
          <Badge tone={thesisTone(company.thesis_status)}>{label(company.thesis_status)}</Badge>
          <p className="mt-1 font-mono text-[9px] text-faint">{company.venue}</p>
        </div>
      </div>
      <div className="mt-2 flex flex-wrap items-center gap-x-2 gap-y-1 font-mono text-[9px] text-faint">
        <span>{formatAgo(company.as_of)}</span>
        <span>{company.depth || "unknown depth"}</span>
        <span>{company.source_count} sources</span>
        {company.on_book ? <span className="text-accent">on book {company.side}</span> : null}
        {company.stale_market ? <span className="text-warn">stale marks</span> : null}
      </div>
    </button>
  );
}

export function CompanyIntelligenceDetail({
  company,
  onSymbol,
}: {
  company: CompanyIntelligence;
  onSymbol: (symbol: string) => void;
}) {
  const brief = company.brief;
  const selection = brief.selection_view;
  return (
    <article className="space-y-6">
      <header className="flex flex-wrap items-start justify-between gap-4 border-b border-line pb-5">
        <div>
          <div className="flex flex-wrap items-center gap-2">
            <p className="font-mono text-[10px] uppercase tracking-[0.18em] text-accent">{company.venue}</p>
            <Badge>{company.depth || "unknown"}</Badge>
            <Badge tone={company.coverage && ["complete", "full"].includes(company.coverage) ? "gain" : "warn"}>
              {company.coverage || "coverage unknown"}
            </Badge>
          </div>
          <h3 className="mt-2 text-3xl font-semibold tracking-[-0.04em]">{company.symbol}</h3>
          <p className="mt-1 text-sm text-dim">{company.name || "Issuer name unavailable"}</p>
        </div>
        <Button variant="outline" size="sm" onClick={() => onSymbol(company.symbol)}>
          Symbol sheet
          <ArrowUpRight className="size-3.5" />
        </Button>
      </header>

      <section>
        <div className="flex flex-wrap items-center gap-2">
          <p className="font-mono text-[9px] uppercase tracking-[0.2em] text-faint">Company thesis</p>
          <Badge tone={thesisTone(company.thesis_status)}>{label(company.thesis_status)}</Badge>
          {company.thesis_changed ? (
            <Badge tone="warn">
              {label(company.previous_thesis_status) || "prior"} → {label(company.thesis_status)}
            </Badge>
          ) : null}
        </div>
        <p className="mt-3 max-w-4xl text-lg leading-relaxed text-fg">{company.summary || "No current thesis summary."}</p>
        {company.business_summary ? (
          <p className="mt-3 max-w-4xl text-sm leading-relaxed text-dim">{company.business_summary}</p>
        ) : null}
      </section>

      <div className="grid gap-5 lg:grid-cols-2">
        <EvidenceSection title="Catalysts" items={brief.catalysts ?? []} tone="gain" empty="No evidenced catalyst." />
        <EvidenceSection title="Risks" items={brief.risks ?? []} tone="loss" empty="No evidenced risk." />
      </div>

      {(brief.open_questions ?? []).length ? (
        <EvidenceSection
          title="Open questions"
          items={brief.open_questions ?? []}
          tone="warn"
          empty="No open question."
        />
      ) : null}

      <section className="rounded-lg border border-line bg-panel/55 p-4">
        <div className="flex flex-wrap items-center justify-between gap-3">
          <div>
            <p className="font-mono text-[9px] uppercase tracking-[0.2em] text-faint">Selection view</p>
            <p className="mt-1 text-sm text-muted">{label(selection?.posture) || "No selection posture."}</p>
          </div>
          {typeof selection?.confidence === "number" ? (
            <Badge tone="accent">confidence {Math.round(selection.confidence * 100)}%</Badge>
          ) : company.selection_confidence_label ? (
            <Badge tone="accent">confidence {company.selection_confidence_label}</Badge>
          ) : null}
        </div>
        {selection?.summary ? <p className="mt-3 text-sm leading-relaxed text-dim">{selection.summary}</p> : null}
        {(selection?.reasons ?? []).length ? (
          <ul className="mt-3 list-disc space-y-1 pl-5 text-xs leading-relaxed text-dim">
            {selection?.reasons?.map((reason) => <li key={reason}>{reason}</li>)}
          </ul>
        ) : null}
      </section>

      <section className="border-t border-line pt-5">
        <div className="flex flex-wrap items-end justify-between gap-3">
          <div>
            <p className="font-mono text-[9px] uppercase tracking-[0.2em] text-faint">Agent expression</p>
          </div>
          <div className="flex flex-wrap gap-1.5">
            <Badge tone={company.on_book ? "accent" : "muted"}>
              {company.on_book ? `on book ${company.side}` : "not on book"}
            </Badge>
            {company.latest_action ? <Badge>{company.latest_action}</Badge> : null}
          </div>
        </div>
        <div className="mt-4 grid grid-cols-3 gap-2">
          <EvidenceStat label="Decisions" value={company.decision_count} />
          <EvidenceStat label="Linked" value={company.linked_decision_count} />
          <EvidenceStat label="History" value={company.history_count} />
        </div>
      </section>

      <FinancialSnapshotSection brief={brief} />
      <EarningsSection brief={brief} />

      <details className="rounded-md border border-hairline px-3 py-2">
        <summary className="cursor-pointer font-mono text-[9px] uppercase tracking-[0.18em] text-faint">
          Raw company brief
        </summary>
        <pre className="mt-3 max-h-96 overflow-auto whitespace-pre-wrap break-words font-mono text-[10px] leading-relaxed text-dim">
          {JSON.stringify(brief, null, 2)}
        </pre>
      </details>
    </article>
  );
}

// ── Financial snapshot ───────────────────────────────────────────────────────

type EvidencePointRaw = {
  point?: string;
  period?: string | null;
  horizon?: string | null;
  confidence?: string | null;
  source_refs?: string[];
  [key: string]: unknown;
};

function isEvidencePointArray(value: unknown): value is EvidencePointRaw[] {
  return Array.isArray(value) && (value.length === 0 || (typeof value[0] === "object" && value[0] !== null));
}

function FinancialSnapshotSection({ brief }: { brief: CompanyBrief }) {
  const section = brief.financial_snapshot;
  if (!section || typeof section !== "object") return null;

  const points = isEvidencePointArray((section as Record<string, unknown>).points)
    ? ((section as Record<string, unknown>).points as EvidencePointRaw[])
    : [];
  const summary = typeof (section as Record<string, unknown>).summary === "string"
    ? (section as Record<string, unknown>).summary as string
    : "";

  const scalars = extractScalars(section as Record<string, unknown>, ["points", "metrics", "source_refs", "freshness", "summary"]);
  const complexKeys = extractComplexKeys(section as Record<string, unknown>, ["points", "metrics", "source_refs", "freshness", "summary"]);

  if (!points.length && !summary && !scalars.length && !complexKeys.length) return null;

  return (
    <section>
      <p className="mb-3 font-mono text-[9px] uppercase tracking-[0.2em] text-faint">Financials</p>
      {summary ? <p className="mb-3 text-sm leading-relaxed text-dim">{summary}</p> : null}
      {points.length > 0 ? (
        <div className="space-y-2">
          {points.map((item, index) => (
            <EvidencePointRow key={`fin-${index}`} item={item} />
          ))}
        </div>
      ) : null}
      {scalars.length > 0 ? <ScalarGrid pairs={scalars} /> : null}
      {complexKeys.length > 0 ? <ComplexDetails obj={section as Record<string, unknown>} keys={complexKeys} /> : null}
    </section>
  );
}

function EarningsSection({ brief }: { brief: CompanyBrief }) {
  const section = brief.earnings_and_guidance;
  if (!section || typeof section !== "object") return null;

  const points = isEvidencePointArray((section as Record<string, unknown>).points)
    ? ((section as Record<string, unknown>).points as EvidencePointRaw[])
    : [];
  const summary = typeof (section as Record<string, unknown>).summary === "string"
    ? (section as Record<string, unknown>).summary as string
    : "";

  const scalars = extractScalars(section as Record<string, unknown>, ["points", "metrics", "source_refs", "freshness", "summary"]);
  const complexKeys = extractComplexKeys(section as Record<string, unknown>, ["points", "metrics", "source_refs", "freshness", "summary"]);

  if (!points.length && !summary && !scalars.length && !complexKeys.length) return null;

  return (
    <section>
      <p className="mb-3 font-mono text-[9px] uppercase tracking-[0.2em] text-faint">Earnings &amp; guidance</p>
      {summary ? <p className="mb-3 text-sm leading-relaxed text-dim">{summary}</p> : null}
      {points.length > 0 ? (
        <div className="space-y-2">
          {points.map((item, index) => (
            <EvidencePointRow key={`earn-${index}`} item={item} />
          ))}
        </div>
      ) : null}
      {scalars.length > 0 ? <ScalarGrid pairs={scalars} /> : null}
      {complexKeys.length > 0 ? <ComplexDetails obj={section as Record<string, unknown>} keys={complexKeys} /> : null}
    </section>
  );
}

function EvidencePointRow({ item }: { item: EvidencePointRaw }) {
  const meta = [item.period, item.horizon, item.confidence].filter(Boolean).join(" · ");
  const refs = item.source_refs?.length ? `${item.source_refs.length} source${item.source_refs.length > 1 ? "s" : ""}` : null;
  return (
    <div className="rounded-md border border-hairline bg-panel/35 px-3 py-2.5">
      <p className="text-sm leading-relaxed text-muted">{item.point || "—"}</p>
      {(meta || refs) ? (
        <p className="mt-1 font-mono text-[9px] text-faint">
          {[meta, refs].filter(Boolean).join(" · ")}
        </p>
      ) : null}
    </div>
  );
}

// Render flat scalar key-value pairs (string | number | boolean)
function ScalarGrid({ pairs }: { pairs: [string, string][] }) {
  return (
    <dl className="mt-3 grid grid-cols-2 gap-x-6 gap-y-1.5 sm:grid-cols-3">
      {pairs.map(([key, val]) => (
        <div key={key} className="min-w-0">
          <dt className="font-mono text-[9px] uppercase tracking-[0.14em] text-faint">{humanize(key)}</dt>
          <dd className="mt-0.5 truncate font-mono text-xs text-dim">{val}</dd>
        </div>
      ))}
    </dl>
  );
}

function ComplexDetails({ obj, keys }: { obj: Record<string, unknown>; keys: string[] }) {
  return (
    <>
      {keys.map((key) => (
        <details key={key} className="mt-2 rounded-md border border-hairline px-3 py-2">
          <summary className="cursor-pointer font-mono text-[9px] uppercase tracking-[0.14em] text-faint">
            {humanize(key)}
          </summary>
          <pre className="mt-2 max-h-48 overflow-auto whitespace-pre-wrap break-words font-mono text-[9px] leading-relaxed text-dim">
            {JSON.stringify(obj[key], null, 2)}
          </pre>
        </details>
      ))}
    </>
  );
}

function extractScalars(
  obj: Record<string, unknown>,
  exclude: string[],
): [string, string][] {
  return Object.entries(obj)
    .filter(([k, v]) => !exclude.includes(k) && (typeof v === "string" || typeof v === "number" || typeof v === "boolean"))
    .map(([k, v]) => [k, String(v)] as [string, string]);
}

function extractComplexKeys(
  obj: Record<string, unknown>,
  exclude: string[],
): string[] {
  return Object.keys(obj).filter(
    (k) => !exclude.includes(k) && typeof obj[k] === "object" && obj[k] !== null,
  );
}

function humanize(key: string): string {
  return key.replaceAll("_", " ");
}

function EvidenceSection({
  title,
  items,
  tone,
  empty,
}: {
  title: string;
  items: CompanyEvidencePoint[];
  tone: "gain" | "loss" | "warn";
  empty: string;
}) {
  return (
    <section>
      <div className="mb-2 flex items-center gap-2">
        <span className={cn("size-1.5 rounded-full", tone === "gain" ? "bg-gain" : tone === "loss" ? "bg-loss" : "bg-warn")} />
        <p className="font-mono text-[9px] uppercase tracking-[0.2em] text-faint">{title}</p>
      </div>
      <div className="space-y-2">
        {items.map((item, index) => (
          <div key={`${evidenceText(item)}-${index}`} className="rounded-md border border-hairline bg-panel/35 px-3 py-2.5">
            <p className="text-sm leading-relaxed text-muted">{evidenceText(item)}</p>
            <p className="mt-1 font-mono text-[9px] text-faint">
              {[item.direction, item.signal, item.severity].filter(Boolean).join(" · ")}
              {item.source_refs?.length ? ` · ${item.source_refs.length} sources` : ""}
            </p>
          </div>
        ))}
        {!items.length ? <p className="text-sm text-faint">{empty}</p> : null}
      </div>
    </section>
  );
}

function EvidenceStat({ label: name, value }: { label: string; value: number }) {
  return (
    <div className="rounded-md border border-hairline px-3 py-2">
      <p className="font-mono text-[9px] uppercase tracking-[0.16em] text-faint">{name}</p>
      <p className="mt-1 font-mono text-lg text-muted">{value}</p>
    </div>
  );
}

function thesisTone(status: string): "gain" | "loss" | "warn" | "accent" | "muted" {
  const value = status.toLowerCase();
  if (value.includes("construct") || value.includes("positive") || value.includes("bull")) return "gain";
  if (value.includes("caution") || value.includes("negative") || value.includes("bear")) return "loss";
  if (value.includes("watch") || value.includes("mixed")) return "warn";
  return value === "unknown" ? "muted" : "accent";
}

function label(value: string | null | undefined): string {
  return (value ?? "").replaceAll("_", " ");
}

function evidenceText(item: CompanyEvidencePoint): string {
  return item.point || item.label || item.detail || "Evidence recorded without a display label.";
}
