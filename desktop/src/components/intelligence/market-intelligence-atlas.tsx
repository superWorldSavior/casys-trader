import { useMemo, useState } from "react";
import { MarketInfluenceGraphView } from "@/components/intelligence/market-influence-graph";
import { useWorldGraph, useWorldIntelligence } from "@/hooks/use-intelligence";
import { formatAgo } from "@/lib/format";
import {
  familyLabel,
  grossModeLabel,
  marketRegimeLabel,
  venueLabel,
} from "@/lib/humanize";
import {
  type AtlasMacroBriefInput,
  type AtlasNode,
  type AtlasScope,
  buildAtlasEvidenceLinks,
  buildAtlasFamilyThreads,
  buildMarketAtlas,
} from "@/lib/market-intelligence-atlas";
import { buildMarketInfluenceGraph } from "@/lib/market-influence-network";
import type { MarketIntelligenceContext } from "@/lib/market-intelligence-reading";
import {
  bindWorldGraphProjection,
  worldGraphIsAvailable,
} from "@/lib/world-graph-explorer";
import type {
  FamilyComparison,
  FamilyIntelligence,
  Holding,
  RegionCurrentIntelligence,
} from "@/lib/types";

type Props = {
  current?: Record<string, RegionCurrentIntelligence | undefined> | null;
  families?: readonly FamilyIntelligence[] | null;
  comparison?: readonly FamilyComparison[] | null;
  activeScope?: string;
  companyMap?: Readonly<Record<string, string>>;
  holdings?: readonly Holding[] | null;
  onSelectScope?: (scope: string) => void;
};

const PRIORITY_FILL: Record<AtlasNode["priority"], string> = {
  favored: "#26735a",
  neutral: "#7695a3",
  deprioritized: "#b45149",
};

export function MarketIntelligenceAtlas({
  current,
  families,
  comparison,
  activeScope,
  companyMap,
  holdings,
  onSelectScope,
}: Props) {
  const worldQuery = useWorldIntelligence(30);
  const worldGraphQuery = useWorldGraph();
  const layout = useMemo(
    () =>
      buildMarketAtlas({
        current: current ?? {},
        families: families ?? [],
        comparisons: comparison ?? [],
      }),
    [comparison, current, families],
  );
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const selected =
    layout.nodes.find((node) =>
      node.id === selectedId && (!activeScope || node.scopeKey === activeScope)
    ) ??
      layout.nodes.find((node) => node.scopeKey === activeScope) ??
      layout.nodes[0] ??
      null;
  const familyThreads = useMemo(
    () => buildAtlasFamilyThreads(layout.nodes, comparison),
    [layout.nodes, comparison],
  );
  const evidenceByNode = useMemo(
    () =>
      layout.nodes.map((node) => {
        const brief = current?.[node.scopeKey]?.macro_brief;
        return {
          nodeId: node.id,
          links: buildAtlasEvidenceLinks(
            node,
            brief as AtlasMacroBriefInput | null | undefined,
          ),
          as_of: brief?.as_of ?? null,
          valid_until: brief?.valid_until ?? null,
          status: brief?.status ?? null,
          coverage: brief?.coverage ?? null,
        };
      }),
    [current, layout.nodes],
  );
  const businessGraph = useMemo(
    () =>
      buildMarketInfluenceGraph({
        nodes: layout.nodes,
        threads: familyThreads,
        evidenceByNode,
        companyNames: companyMap,
        holdings,
      }),
    [companyMap, evidenceByNode, familyThreads, holdings, layout.nodes],
  );
  const influenceGraph = useMemo(
    () =>
      worldGraphIsAvailable(worldGraphQuery.data)
        ? bindWorldGraphProjection(businessGraph, worldGraphQuery.data)
        : businessGraph,
    [businessGraph, worldGraphQuery.data],
  );
  const intelligenceContext = useMemo<MarketIntelligenceContext>(
    () => ({
      current: current ?? {},
      comparisons: comparison ?? [],
    }),
    [comparison, current],
  );

  function selectNode(node: AtlasNode) {
    setSelectedId(node.id);
    onSelectScope?.(node.scopeKey);
  }

  const posture = worldQuery.data?.current.posture;
  const digest = worldQuery.data?.current.digest;
  const board = worldQuery.data?.current.family_board;
  const limitedView = isLimitedBoard(board?.status);
  const worldLine = worldContextLine({
    pending: worldQuery.isPending && !worldQuery.data,
    error: Boolean(worldQuery.error && !worldQuery.data),
    regime: digest?.regime,
    grossMode: posture?.gross_mode,
  });
  const boardLine = board
    ? board.as_of
      ? `Market view ${formatAgo(board.as_of)}`
      : "Market view time not recorded"
    : "Market view not recorded";
  return (
    <section
      aria-label="Market intelligence map"
      className="atlas-mesh min-w-0 max-w-full overflow-hidden rounded-lg border border-line text-fg shadow-[0_8px_24px_rgba(23,43,54,0.06)]"
    >
      <header className="flex flex-wrap items-start justify-between gap-3 border-b border-hairline px-4 py-3.5 sm:px-5">
        <div className="min-w-0">
          <h2 className="max-w-3xl text-[15px] font-semibold leading-snug tracking-tight text-fg sm:text-base">
            {atlasHeadline(layout.scopes, layout.nodes)}
          </h2>
          <p className="mt-1.5 flex flex-wrap items-center gap-x-2 gap-y-1 text-[11px] text-dim">
            <span>{worldLine}</span>
            <span aria-hidden="true" className="text-line">·</span>
            <span>{boardLine}</span>
            {limitedView
              ? <span className="font-medium text-warn">Limited view</span>
              : null}
          </p>
        </div>
        <div className="flex flex-wrap items-center justify-end gap-2">
          <WorldGraphBindingStatus
            pending={worldGraphQuery.isPending && !worldGraphQuery.data}
            failed={Boolean(worldGraphQuery.error && !worldGraphQuery.data)}
            graph={influenceGraph}
          />
          <PriorityLegend />
        </div>
      </header>

      <p className="sr-only">
        A complete directed map of markets, domains, families, companies and
        drivers. Selecting an item highlights its path without removing any
        other item.
      </p>
      <ul className="sr-only">
        {layout.nodes.map((node) => (
          <li key={`summary-${node.id}`}>
            {familyLabel(node.family)} in {venueLabel(node.scopeKey)}.{" "}
            {priorityPhrase(node.priority)}.
            {node.ranked && node.rank != null
              ? ` ${ordinal(node.rank)} in this market.`
              : " No current read."}
            {` ${node.candidateCount} ${
              node.candidateCount === 1
                ? "company reviewed"
                : "companies reviewed"
            }.`}
          </li>
        ))}
      </ul>

      {selected
        ? (
          <MarketInfluenceGraphView
            graph={influenceGraph}
            atlasNodes={layout.nodes}
            selectedAtlasNodeId={selected.id}
            onSelect={selectNode}
            intelligenceContext={intelligenceContext}
          />
        )
        : (
          <div className="px-4 py-12 text-center text-sm text-faint sm:px-5">
            Casys is building the first market relationships.
          </div>
        )}
    </section>
  );
}

function WorldGraphBindingStatus({
  pending,
  failed,
  graph,
}: {
  pending: boolean;
  failed: boolean;
  graph: ReturnType<typeof buildMarketInfluenceGraph>;
}) {
  const projection = graph.projection;
  const label = projection
    ? `${projection.mappedCompanyCount}/${projection.companyCount} linked`
    : pending
    ? "Linking World Graph"
    : failed
    ? "World Graph unavailable"
    : "Business graph only";
  return (
    <span
      className={`inline-flex items-center gap-1.5 rounded-full border px-2 py-1 font-mono text-[8px] uppercase tracking-[0.12em] ${
        projection
          ? "border-[#b7ccd5] bg-[#eef5f7] text-[#365865]"
          : "border-line bg-white/70 text-faint"
      }`}
      title={projection
        ? "Linked to the current published World Graph"
        : undefined}
    >
      <span
        aria-hidden="true"
        className={`size-1.5 rounded-full ${
          projection
            ? "bg-[#26735a]"
            : pending
            ? "bg-[#b08a46]"
            : "bg-[#9eafb7]"
        }`}
      />
      {label}
    </span>
  );
}

function PriorityLegend() {
  return (
    <div
      aria-label="Family priority legend"
      className="flex flex-wrap items-center gap-x-3 gap-y-1 text-[10px] text-dim"
    >
      <LegendItem color={PRIORITY_FILL.favored}>Casys prefers</LegendItem>
      <LegendItem color={PRIORITY_FILL.neutral}>Under review</LegendItem>
      <LegendItem color={PRIORITY_FILL.deprioritized}>
        Lower priority
      </LegendItem>
    </div>
  );
}

function LegendItem(
  { color, children }: { color: string; children: React.ReactNode },
) {
  return (
    <span className="inline-flex items-center gap-1.5">
      <span
        className="size-1.5 rounded-sm"
        style={{ backgroundColor: color }}
        aria-hidden="true"
      />
      {children}
    </span>
  );
}

function isLimitedBoard(status?: string | null): boolean {
  const key = String(status ?? "").toLowerCase();
  return key === "partial" || key === "incomplete" || key === "degraded";
}

function atlasHeadline(scopes: AtlasScope[], nodes: AtlasNode[]): string {
  const leaders = scopes.flatMap((scope) => {
    const leader = nodes
      .filter((node) => node.scopeKey === scope.key && node.rank != null)
      .sort((left, right) =>
        (left.rank ?? Number.POSITIVE_INFINITY) -
        (right.rank ?? Number.POSITIVE_INFINITY)
      )[0];
    return leader ? [{ scope: scope.key, family: leader.family }] : [];
  });
  if (!leaders.length) return "Casys is building the current market view.";

  const shown = leaders.slice(0, 4).map(({ scope, family }) => (
    `${headlineThemeLabel(family, scope)} in ${scopeSentenceLabel(scope)}`
  ));
  const remaining = leaders.length - shown.length;
  return `Leading families: ${shown.join(" · ")}${
    remaining > 0 ? ` · ${remaining} more market scopes` : ""
  }`;
}

function scopeSentenceLabel(scope: string): string {
  const label = venueLabel(scope);
  return label === "United States" ? "the United States" : label;
}

function headlineThemeLabel(family: string, scope: string): string {
  const label = compactFamilyLabel(family, scope);
  if (!label) return "family";
  return `${label.charAt(0).toLowerCase()}${label.slice(1)}`;
}

function compactFamilyLabel(family: string, scope: string): string {
  const label = familyLabel(family);
  if (family.toLowerCase().startsWith(`${scope.toLowerCase()}_`)) {
    const withoutFirstWord = label.split(/\s+/).slice(1).join(" ").trim();
    if (withoutFirstWord) return withoutFirstWord;
  }
  const market = venueLabel(scope);
  const prefixes = [market, market.replace(/e$/, "") + "n", scope];
  for (const prefix of prefixes) {
    if (label.toLowerCase().startsWith(`${prefix.toLowerCase()} `)) {
      const compact = label.slice(prefix.length + 1).trim();
      if (compact) return compact;
    }
  }
  return label;
}

function worldContextLine(input: {
  pending: boolean;
  error: boolean;
  regime?: string | null;
  grossMode?: string | null;
}): string {
  if (input.pending) return "Reading world context";
  if (input.error) return "World context is temporarily unavailable";
  const regime = input.regime
    ? marketRegimeLabel(input.regime)
    : "Market mood not yet assessed";
  const risk = input.grossMode
    ? grossModeLabel(input.grossMode)
    : "Risk posture not yet assessed";
  return `${regime} · ${risk}`;
}

function priorityPhrase(priority: AtlasNode["priority"]): string {
  if (priority === "favored") return "Preferred by Casys";
  if (priority === "deprioritized") return "Lower priority for Casys";
  return "Under review";
}

function ordinal(value: number): string {
  const mod100 = value % 100;
  if (mod100 >= 11 && mod100 <= 13) return `${value}th`;
  if (value % 10 === 1) return `${value}st`;
  if (value % 10 === 2) return `${value}nd`;
  if (value % 10 === 3) return `${value}rd`;
  return `${value}th`;
}
