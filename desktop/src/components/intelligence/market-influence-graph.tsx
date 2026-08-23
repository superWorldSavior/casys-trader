import { useEffect, useMemo, useRef, useState } from "react";
import { drag } from "d3-drag";
import {
  type Force,
  forceCenter,
  forceCollide,
  forceLink,
  forceManyBody,
  forceSimulation,
  forceX,
  forceY,
  type Simulation,
  type SimulationLinkDatum,
  type SimulationNodeDatum,
} from "d3-force";
import { polygonHull } from "d3-polygon";
import { select } from "d3-selection";
import { curveCatmullRomClosed, line } from "d3-shape";
import {
  zoom,
  type ZoomBehavior,
  zoomIdentity,
  type ZoomTransform,
} from "d3-zoom";
import { familyLabel, plainMarketLanguage, venueLabel } from "@/lib/humanize";
import type { AtlasNode } from "@/lib/market-intelligence-atlas";
import {
  clusterIdForNode,
  focusedPath,
  type GraphHull,
  graphHulls,
  type GraphLens,
  linkForceParams,
  nodeLabelVisible,
  presentGraph,
  seedNodePositions,
} from "@/lib/market-influence-layout";
import {
  familyGraphId,
  type InfluenceEdgeBasis,
  type InfluenceNodeKind,
  type MarketInfluenceGraph,
} from "@/lib/market-influence-network";

type Props = {
  graph: MarketInfluenceGraph;
  atlasNodes: readonly AtlasNode[];
  selectedAtlasNodeId: string;
  onSelect: (node: AtlasNode) => void;
};

type SimNode = SimulationNodeDatum & {
  id: string;
  kind: InfluenceNodeKind;
  label: string;
  classes: string;
  extraClass: string;
  radius: number;
  clusterId: string | null;
};

type SimLink = SimulationLinkDatum<SimNode> & {
  id: string;
  basis: InfluenceEdgeBasis;
  kind: string;
  tone: string;
  label: string;
  classes: string;
  strength: number;
  distance: number;
};

type ForceGraphController = {
  update: (input: {
    graph: MarketInfluenceGraph;
    lens: GraphLens;
    compact: boolean;
    focusId?: string | null;
  }) => void;
  setFocus: (nodeId: string | null, options?: { recenter?: boolean }) => void;
  fit: () => void;
  reorganize: () => void;
  zoomBy: (factor: number) => void;
  resize: () => void;
  destroy: () => void;
};

const FONT_FAMILY = "IBM Plex Sans, Segoe UI, sans-serif";
const CANVAS_FILL = "#fbfcfd";
const MIN_ZOOM = 0.12;
const MAX_ZOOM = 2.8;
const hullLine = line<[number, number]>()
  .x((point) => point[0])
  .y((point) => point[1])
  .curve(curveCatmullRomClosed.alpha(0.82));

export function MarketInfluenceGraphView({
  graph,
  atlasNodes,
  selectedAtlasNodeId,
  onSelect,
}: Props) {
  const graphContainerRef = useRef<HTMLDivElement | null>(null);
  const graphControllerRef = useRef<ForceGraphController | null>(null);
  const latestGraphRef = useRef(graph);
  const compactRef = useRef(false);
  const lensRef = useRef<GraphLens>("market");
  const focusIdRef = useRef<string | null>(
    familyGraphId(selectedAtlasNodeId),
  );
  const atlasNodesRef = useRef(
    new Map(atlasNodes.map((node) => [node.id, node])),
  );
  const onSelectRef = useRef(onSelect);
  const chooseNodeRef = useRef<(graphNodeId: string) => void>(() => {});
  const [compact, setCompact] = useState(false);
  const [loadFailed, setLoadFailed] = useState<string | null>(null);
  const [lens, setLens] = useState<GraphLens>("market");
  const [detailNodeId, setDetailNodeId] = useState(() =>
    familyGraphId(selectedAtlasNodeId)
  );
  const [explorerScope, setExplorerScope] = useState(() =>
    atlasNodes.find((node) => node.id === selectedAtlasNodeId)?.scopeKey ??
      atlasNodes[0]?.scopeKey ?? ""
  );

  const selectedGraphNodeId = useMemo(
    () => familyGraphId(selectedAtlasNodeId),
    [selectedAtlasNodeId],
  );
  const pathSummary = useMemo(() => {
    const selected = graph.nodes.find((node) =>
      node.data.id === selectedGraphNodeId
    );
    if (!selected) return "The complete market map is visible.";
    return `${
      displayNodeLabel(selected.data)
    } is selected. Its incoming market paths are highlighted while the complete map remains visible.`;
  }, [graph.nodes, selectedGraphNodeId]);

  latestGraphRef.current = graph;
  compactRef.current = compact;
  lensRef.current = lens;
  atlasNodesRef.current = new Map(atlasNodes.map((node) => [node.id, node]));
  onSelectRef.current = onSelect;
  chooseNodeRef.current = (graphNodeId: string) => {
    focusIdRef.current = graphNodeId;
    setDetailNodeId(graphNodeId);
    graphControllerRef.current?.setFocus(graphNodeId, { recenter: true });
    const graphNode = latestGraphRef.current.nodes.find((node) =>
      node.data.id === graphNodeId
    );
    const atlasNode = atlasNodesRef.current.get(
      String(graphNode?.data.atlasNodeId ?? ""),
    );
    if (graphNode?.data.scopeKey) {
      setExplorerScope(graphNode.data.scopeKey);
    } else if (atlasNode?.scopeKey) {
      setExplorerScope(atlasNode.scopeKey);
    }
    if (atlasNode) onSelectRef.current(atlasNode);
  };

  useEffect(() => {
    const container = graphContainerRef.current;
    if (!container) return;
    const observer = new ResizeObserver(([entry]) => {
      const nextCompact = entry.contentRect.width < 900;
      setCompact((current) => current === nextCompact ? current : nextCompact);
      requestAnimationFrame(() => graphControllerRef.current?.resize());
    });
    observer.observe(container);
    return () => observer.disconnect();
  }, []);

  useEffect(() => {
    const container = graphContainerRef.current;
    if (!container) return;
    let controller: ForceGraphController | null = null;
    try {
      controller = createMarketForceGraph(container, {
        onSelect: (graphNodeId) => chooseNodeRef.current(graphNodeId),
        onClearFocus: () => {
          focusIdRef.current = null;
          graphControllerRef.current?.setFocus(null);
        },
      });
      graphControllerRef.current = controller;
      setLoadFailed(null);
      controller.update({
        graph: latestGraphRef.current,
        lens: lensRef.current,
        compact: compactRef.current,
        focusId: focusIdRef.current,
      });
    } catch (error: unknown) {
      setLoadFailed(
        error instanceof Error ? error.message : "Unknown graph error",
      );
    }
    return () => {
      controller?.destroy();
      if (graphControllerRef.current === controller) {
        graphControllerRef.current = null;
      }
    };
  }, []);

  useEffect(() => {
    graphControllerRef.current?.update({
      graph,
      lens,
      compact,
      focusId: focusIdRef.current,
    });
  }, [compact, graph, lens]);

  useEffect(() => {
    focusIdRef.current = selectedGraphNodeId;
    setDetailNodeId(selectedGraphNodeId);
    const selectedAtlasNode = atlasNodesRef.current.get(selectedAtlasNodeId);
    if (selectedAtlasNode) setExplorerScope(selectedAtlasNode.scopeKey);
    graphControllerRef.current?.setFocus(selectedGraphNodeId, {
      recenter: true,
    });
  }, [selectedAtlasNodeId, selectedGraphNodeId]);

  const detailNode =
    graph.nodes.find((node) => node.data.id === detailNodeId) ??
      graph.nodes.find((node) => node.data.id === selectedGraphNodeId) ?? null;

  return (
    <div className="border-b border-hairline bg-[#fbfcfd]">
      <div className="flex flex-wrap items-center gap-x-4 gap-y-1 border-b border-hairline px-4 py-2.5 text-[10px] text-dim sm:px-5">
        <GraphLegend stroke="dashed" color="#7695a3" arrow>
          Possible influence
        </GraphLegend>
        <GraphLegend stroke="solid" color="#176887">
          Shared evidence
        </GraphLegend>
        <GraphLegend stroke="dotted" color="#b7c6cd">
          Market or domain
        </GraphLegend>
        <span className="sm:ml-auto">Select any item to light its path</span>
      </div>

      <div className="grid xl:grid-cols-[230px_minmax(0,1fr)_300px]">
        <GraphExplorerPanel
          graph={graph}
          activeNodeId={detailNode?.data.id ?? null}
          activeScope={explorerScope}
          onChoose={(nodeId) => chooseNodeRef.current(nodeId)}
        />
        <div className="relative min-w-0">
          <p className="pointer-events-none absolute bottom-3 left-4 z-10 rounded bg-[#fbfcfd]/90 px-2 py-1 text-[10px] text-faint">
            Drag to explore · select to follow a path
          </p>
          <GraphControls
            lens={lens}
            onLensChange={setLens}
            onFit={() => graphControllerRef.current?.fit()}
            onReorganize={() => graphControllerRef.current?.reorganize()}
            onZoomIn={() => graphControllerRef.current?.zoomBy(1.18)}
            onZoomOut={() => graphControllerRef.current?.zoomBy(1 / 1.18)}
          />
          <div
            ref={graphContainerRef}
            aria-hidden="true"
            className="market-influence-canvas w-full overflow-hidden"
            style={{
              height: compact ? 580 : "clamp(640px, 68vh, 780px)",
            }}
          />
          {loadFailed
            ? (
              <p className="absolute inset-0 grid place-items-center px-6 text-center text-sm text-faint">
                The relationship view is temporarily unavailable.
              </p>
            )
            : null}
        </div>
        <GraphDetailPanel
          node={detailNode}
          graph={graph}
          atlasNodes={atlasNodes}
          onChoose={(nodeId) => chooseNodeRef.current(nodeId)}
        />
      </div>

      <div className="flex flex-wrap items-center gap-x-3 gap-y-1 border-t border-hairline px-4 py-2.5 text-[10px] leading-relaxed text-faint sm:px-5">
        <span aria-live="polite" className="sr-only">{pathSummary}</span>
        <span>
          Every market, domain and theme stays in view. Connections show
          evidence, possible influence or structure — not measured correlation.
        </span>
        <span>
          Group bubbles show membership; Macro groups themes by a possible
          domain factor. Size shows relative reach within each node type;
          companies stay uniform.
        </span>
        <span>
          Colours reflect recorded theme priority or evidence tone. Freshness is
          shown above.
        </span>
        <span className="sm:ml-auto">
          {graph.scopeCount} markets · {graph.domainCount} domains ·{" "}
          {graph.familyCount} themes · {graph.companyCount} companies
        </span>
      </div>
    </div>
  );
}

function GraphExplorerPanel({
  graph,
  activeNodeId,
  activeScope,
  onChoose,
}: {
  graph: MarketInfluenceGraph;
  activeNodeId: string | null;
  activeScope: string;
  onChoose: (nodeId: string) => void;
}) {
  const [query, setQuery] = useState("");
  const markets = graph.nodes.filter((node) => node.data.kind === "market");
  const themes = graph.nodes.filter((node) =>
    node.data.kind === "family" && node.data.scopeKey === activeScope
  );
  const domains = graph.nodes.filter((node) => node.data.kind === "domain");
  const normalizedQuery = query.trim().toLowerCase();
  const results = normalizedQuery
    ? graph.nodes.filter((node) =>
      `${node.data.label} ${node.data.symbol ?? ""} ${
        displayNodeLabel(node.data)
      }`
        .toLowerCase()
        .includes(normalizedQuery)
    ).slice(0, 30)
    : [];

  return (
    <aside className="max-h-[520px] overflow-y-auto border-b border-hairline bg-white xl:h-[clamp(640px,68vh,780px)] xl:max-h-none xl:border-b-0 xl:border-r">
      <div className="sticky top-0 z-10 border-b border-hairline bg-white px-3 py-3">
        <label className="sr-only" htmlFor="market-map-search">
          Search the market map
        </label>
        <input
          id="market-map-search"
          type="search"
          value={query}
          onChange={(event) => setQuery(event.target.value)}
          placeholder="Search themes or companies"
          className="h-9 w-full rounded-md border border-line bg-[#fbfcfd] px-3 text-[11px] text-fg outline-none placeholder:text-faint focus:border-accent/50 focus:ring-2 focus:ring-accent/10"
        />
      </div>

      {normalizedQuery
        ? (
          <ExplorerSection title={`${results.length} results`}>
            {results.map((node) => (
              <ExplorerNodeButton
                key={`search-${node.data.id}`}
                node={node}
                active={activeNodeId === node.data.id}
                onChoose={onChoose}
              />
            ))}
            {!results.length
              ? (
                <p className="px-2 py-2 text-[11px] text-faint">
                  No matching item.
                </p>
              )
              : null}
          </ExplorerSection>
        )
        : (
          <>
            <ExplorerSection title="Markets">
              {markets.map((node) => (
                <ExplorerNodeButton
                  key={node.data.id}
                  node={node}
                  active={activeNodeId === node.data.id ||
                    node.data.scopeKey === activeScope}
                  onChoose={onChoose}
                />
              ))}
            </ExplorerSection>
            <ExplorerSection
              title={`Themes · ${venueLabel(activeScope)}`}
            >
              {themes.map((node) => (
                <ExplorerNodeButton
                  key={node.data.id}
                  node={node}
                  active={activeNodeId === node.data.id}
                  onChoose={onChoose}
                />
              ))}
            </ExplorerSection>
            <ExplorerSection title="Domains">
              {domains.map((node) => (
                <ExplorerNodeButton
                  key={node.data.id}
                  node={node}
                  active={activeNodeId === node.data.id}
                  onChoose={onChoose}
                />
              ))}
            </ExplorerSection>
          </>
        )}
    </aside>
  );
}

function ExplorerSection({
  title,
  children,
}: {
  title: string;
  children: React.ReactNode;
}) {
  return (
    <section className="border-b border-hairline px-2 py-3 last:border-b-0">
      <h3 className="px-2 font-mono text-[9px] uppercase tracking-[0.14em] text-faint">
        {title}
      </h3>
      <div className="mt-1.5 space-y-0.5">{children}</div>
    </section>
  );
}

function ExplorerNodeButton({
  node,
  active,
  onChoose,
}: {
  node: MarketInfluenceGraph["nodes"][number];
  active: boolean;
  onChoose: (nodeId: string) => void;
}) {
  return (
    <button
      type="button"
      onClick={() => onChoose(node.data.id)}
      aria-pressed={active}
      className={`flex min-h-8 w-full items-center gap-2 rounded px-2 py-1.5 text-left text-[11px] transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent/40 ${
        active
          ? "bg-accent/10 font-medium text-accent"
          : "text-dim hover:bg-panel-hover hover:text-fg"
      }`}
    >
      <NodeKindMark kind={node.data.kind} />
      <span className="min-w-0 flex-1 truncate">
        {displayNodeLabel(node.data)}
      </span>
      {node.data.symbol
        ? (
          <span className="shrink-0 font-mono text-[8px] text-faint">
            {node.data.symbol}
          </span>
        )
        : null}
    </button>
  );
}

function NodeKindMark({
  kind,
}: {
  kind: MarketInfluenceGraph["nodes"][number]["data"]["kind"];
}) {
  const className = kind === "market"
    ? "h-2.5 w-4 rounded-sm bg-[#284b5b]"
    : kind === "domain"
    ? "h-2.5 w-3.5 rounded-sm border border-[#7793a0] bg-[#dce8ed]"
    : kind === "family"
    ? "h-2.5 w-3.5 rounded-sm border border-[#aabcc5] bg-[#f4f7f8]"
    : kind === "driver"
    ? "h-2.5 w-3.5 rounded-sm border border-[#aa8c57] bg-[#f5f0e6]"
    : "size-2.5 rounded-full border border-[#718d99] bg-[#9eb4be]";
  return <span aria-hidden="true" className={`shrink-0 ${className}`} />;
}

function GraphDetailPanel({
  node,
  graph,
  atlasNodes,
  onChoose,
}: {
  node: MarketInfluenceGraph["nodes"][number] | null;
  graph: MarketInfluenceGraph;
  atlasNodes: readonly AtlasNode[];
  onChoose: (nodeId: string) => void;
}) {
  if (!node) {
    return (
      <aside className="border-t border-hairline bg-white px-4 py-5 text-sm text-faint xl:border-l xl:border-t-0">
        Select an item in the map.
      </aside>
    );
  }

  const atlasNode =
    atlasNodes.find((item) => item.id === node.data.atlasNodeId) ?? null;
  const incoming = connectedGraphNodes(graph, node.data.id, "incoming");
  const outgoing = connectedGraphNodes(graph, node.data.id, "outgoing");
  const companies = outgoing.filter((item) => item.data.kind === "company");
  const themes = (node.data.kind === "company" ? incoming : outgoing).filter(
    (item) => item.data.kind === "family",
  );
  const context = incoming.filter((item) =>
    item.data.kind !== "family" && item.data.kind !== "company"
  );
  const scope = node.data.scopeKey
    ? venueLabel(node.data.scopeKey)
    : atlasNode
    ? venueLabel(atlasNode.scopeKey)
    : null;

  return (
    <aside
      aria-live="polite"
      className="max-h-[520px] overflow-y-auto border-t border-hairline bg-white xl:h-[clamp(640px,68vh,780px)] xl:max-h-none xl:border-l xl:border-t-0"
    >
      <header className="border-b border-hairline px-4 py-4">
        <p className="font-mono text-[9px] uppercase tracking-[0.14em] text-faint">
          {graphKindLabel(node.data.kind)}
          {scope ? ` · ${scope}` : ""}
        </p>
        <h3 className="mt-2 text-[17px] font-semibold leading-snug tracking-tight text-fg">
          {displayNodeLabel(node.data)}
        </h3>
        {node.data.symbol
          ? (
            <p className="mt-1 font-mono text-[10px] text-faint">
              {node.data.symbol}
            </p>
          )
          : null}
      </header>

      {atlasNode
        ? (
          <div className="border-b border-hairline px-4 py-4">
            <p className="text-[12px] font-medium text-fg">
              {priorityLabel(atlasNode.priority)}
              {atlasNode.rank != null
                ? ` · ${ordinalLabel(atlasNode.rank)} in this market`
                : " · No current read"}
            </p>
            <p className="mt-1.5 text-[11px] text-dim">
              {atlasNode.symbols.length}{" "}
              {atlasNode.symbols.length === 1 ? "company" : "companies"}
              {atlasNode.movement !== "unknown"
                ? ` · ${movementLabel(atlasNode.movement)}`
                : ""}
            </p>
            <p className="mt-3 text-[11px] font-medium text-accent">
              Latest market review
            </p>
            <p className="mt-1.5 text-[12px] leading-relaxed text-fg">
              {plainMarketLanguage(atlasNode.summary) ||
                "No recent market review is recorded."}
            </p>
          </div>
        )
        : null}

      {node.data.kind === "company" && themes.length
        ? (
          <GraphDetailLinks
            title="Theme"
            nodes={themes}
            onChoose={onChoose}
          />
        )
        : null}
      {node.data.kind === "family" && companies.length
        ? (
          <GraphDetailLinks
            title="Companies"
            nodes={companies}
            onChoose={onChoose}
          />
        )
        : null}
      {(node.data.kind === "market" || node.data.kind === "domain") &&
          themes.length
        ? (
          <GraphDetailLinks
            title="Themes"
            nodes={themes}
            onChoose={onChoose}
          />
        )
        : null}
      {node.data.kind === "driver" && outgoing.length
        ? (
          <GraphDetailLinks
            title="Connected views"
            nodes={outgoing}
            onChoose={onChoose}
          />
        )
        : null}
      {context.length
        ? (
          <GraphDetailLinks
            title={node.data.kind === "family"
              ? "Connected through"
              : "Context"}
            nodes={context}
            onChoose={onChoose}
          />
        )
        : null}
    </aside>
  );
}

function GraphDetailLinks({
  title,
  nodes,
  onChoose,
}: {
  title: string;
  nodes: MarketInfluenceGraph["nodes"];
  onChoose: (nodeId: string) => void;
}) {
  return (
    <div className="border-b border-hairline px-4 py-3.5 last:border-b-0">
      <p className="font-mono text-[9px] uppercase tracking-[0.14em] text-faint">
        {title}
      </p>
      <ul className="mt-2 space-y-1">
        {nodes.map((item) => (
          <li key={`${title}-${item.data.id}`}>
            <button
              type="button"
              onClick={() => onChoose(item.data.id)}
              className="flex min-h-8 w-full items-center justify-between gap-3 rounded px-2 py-1.5 text-left text-[11px] text-dim transition-colors hover:bg-panel-hover hover:text-fg focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent/40"
            >
              <span className="min-w-0 truncate">
                {displayNodeLabel(item.data)}
              </span>
              <span className="shrink-0 font-mono text-[8px] uppercase tracking-[0.1em] text-faint">
                {graphKindLabel(item.data.kind)}
              </span>
            </button>
          </li>
        ))}
      </ul>
    </div>
  );
}

function connectedGraphNodes(
  graph: MarketInfluenceGraph,
  nodeId: string,
  direction: "incoming" | "outgoing",
): MarketInfluenceGraph["nodes"] {
  const ids = new Set(
    graph.edges
      .filter((edge) =>
        direction === "incoming"
          ? edge.data.target === nodeId
          : edge.data.source === nodeId
      )
      .map((edge) =>
        direction === "incoming" ? edge.data.source : edge.data.target
      ),
  );
  return graph.nodes
    .filter((item) => ids.has(item.data.id))
    .sort((left, right) =>
      displayNodeLabel(left.data).localeCompare(displayNodeLabel(right.data))
    );
}

function graphKindLabel(
  kind: MarketInfluenceGraph["nodes"][number]["data"]["kind"],
): string {
  if (kind === "driver") return "Market factor";
  if (kind === "family") return "Theme";
  if (kind === "market") return "Market";
  if (kind === "domain") return "Domain";
  return "Company";
}

function priorityLabel(priority: AtlasNode["priority"]): string {
  if (priority === "favored") return "Preferred by Casys";
  if (priority === "deprioritized") return "Lower priority for Casys";
  return "Under review";
}

function movementLabel(movement: AtlasNode["movement"]): string {
  if (movement === "improving") return "moving up";
  if (movement === "weakening") return "moving down";
  return "holding its place";
}

function ordinalLabel(value: number): string {
  const mod100 = value % 100;
  if (mod100 >= 11 && mod100 <= 13) return `${value}th`;
  if (value % 10 === 1) return `${value}st`;
  if (value % 10 === 2) return `${value}nd`;
  if (value % 10 === 3) return `${value}rd`;
  return `${value}th`;
}

function GraphControls({
  lens,
  onLensChange,
  onFit,
  onReorganize,
  onZoomIn,
  onZoomOut,
}: {
  lens: GraphLens;
  onLensChange: (lens: GraphLens) => void;
  onFit: () => void;
  onReorganize: () => void;
  onZoomIn: () => void;
  onZoomOut: () => void;
}) {
  const buttonClass =
    "grid size-8 place-items-center border-l border-hairline text-[13px] text-dim transition-colors first:border-l-0 hover:bg-panel-hover hover:text-fg focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-accent/40";
  return (
    <>
      <div className="absolute left-4 top-3 z-10 flex overflow-hidden rounded-full border border-line bg-[#fbfcfd]/95 p-0.5 shadow-sm">
        {(["market", "domain", "macro"] as const).map((value) => (
          <button
            key={value}
            type="button"
            onClick={() => onLensChange(value)}
            aria-pressed={lens === value}
            className={`rounded-full px-3 py-1.5 text-[10px] font-medium transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent/40 ${
              lens === value
                ? "bg-[#284b5b] text-white"
                : "text-dim hover:bg-panel-hover hover:text-fg"
            }`}
          >
            {value === "market"
              ? "Markets"
              : value === "domain"
              ? "Domains"
              : "Macro"}
          </button>
        ))}
      </div>
      <div className="absolute right-4 top-3 z-10 flex overflow-hidden rounded-md border border-line bg-[#fbfcfd]/95 shadow-sm">
        <button
          type="button"
          className={`${buttonClass} w-auto px-2.5 text-[10px] font-medium`}
          onClick={onFit}
          aria-label="Fit the complete market map"
        >
          Fit all
        </button>
        <button
          type="button"
          className={`${buttonClass} w-auto px-2.5 text-[10px] font-medium`}
          onClick={onReorganize}
          aria-label="Reorganize the market map"
        >
          Reorganize
        </button>
        <button
          type="button"
          className={buttonClass}
          onClick={onZoomOut}
          aria-label="Zoom out"
        >
          −
        </button>
        <button
          type="button"
          className={buttonClass}
          onClick={onZoomIn}
          aria-label="Zoom in"
        >
          +
        </button>
      </div>
    </>
  );
}

function GraphLegend({
  stroke,
  color,
  arrow = false,
  children,
}: {
  stroke: "dashed" | "dotted" | "solid";
  color: string;
  arrow?: boolean;
  children: React.ReactNode;
}) {
  return (
    <span className="inline-flex items-center gap-1.5">
      <span className="inline-flex items-center" aria-hidden="true">
        <span
          className="block w-5 border-t-2"
          style={{ borderColor: color, borderTopStyle: stroke }}
        />
        {arrow
          ? <span style={{ color }} className="-ml-0.5 text-[10px]">›</span>
          : null}
      </span>
      {children}
    </span>
  );
}

function createMarketForceGraph(
  container: HTMLDivElement,
  handlers: {
    onSelect: (graphNodeId: string) => void;
    onClearFocus: () => void;
  },
): ForceGraphController {
  let width = Math.max(container.clientWidth, 120);
  let height = Math.max(container.clientHeight, 120);
  let graph: MarketInfluenceGraph = {
    nodes: [],
    edges: [],
    scopeCount: 0,
    domainCount: 0,
    familyCount: 0,
    companyCount: 0,
    supportedPathCount: 0,
    possiblePathCount: 0,
  };
  let lens: GraphLens = "market";
  let compact = false;
  let focusId: string | null = null;
  let hoveredId: string | null = null;
  let topologyKey = "";
  let nodes: SimNode[] = [];
  let links: SimLink[] = [];
  let hulls: GraphHull[] = [];
  let transform: ZoomTransform = zoomIdentity;
  let userAdjustedView = false;
  let pendingFit = false;
  let pointerMoved = false;
  let frame = 0;
  let destroyed = false;
  let pathNodeIds = new Set<string>();
  let pathEdgeIds = new Set<string>();
  const nodeById = new Map<string, SimNode>();

  const svg = select(container)
    .append("svg")
    .attr("aria-hidden", "true")
    .attr("width", width)
    .attr("height", height)
    .style("display", "block")
    .style("width", "100%")
    .style("height", "100%")
    .style("cursor", "grab")
    .style("touch-action", "none")
    .style("user-select", "none")
    .style("background", CANVAS_FILL);

  svg.append("rect")
    .attr("class", "graph-background")
    .attr("width", width)
    .attr("height", height)
    .attr("fill", CANVAS_FILL);

  const defs = svg.append("defs");
  defs.append("marker")
    .attr("id", "casys-edge-arrow")
    .attr("viewBox", "0 0 10 10")
    .attr("refX", 9)
    .attr("refY", 5)
    .attr("markerWidth", 6)
    .attr("markerHeight", 6)
    .attr("orient", "auto")
    .append("path")
    .attr("d", "M 0 0 L 10 5 L 0 10 z")
    .attr("fill", "#7695a3");

  const viewport = svg.append("g").attr("class", "graph-viewport");
  const lensHullLayer = viewport.append("g").attr("class", "lens-hulls");
  const familyHullLayer = viewport.append("g").attr("class", "family-hulls");
  const edgeLayer = viewport.append("g").attr("class", "edges");
  const nodeLayer = viewport.append("g").attr("class", "nodes");
  const hullLabelLayer = viewport.append("g").attr("class", "hull-labels");
  const edgeLabelLayer = viewport.append("g").attr("class", "edge-labels");

  const zoomer: ZoomBehavior<SVGSVGElement, unknown> = zoom<
    SVGSVGElement,
    unknown
  >()
    .scaleExtent([MIN_ZOOM, MAX_ZOOM])
    .filter((event) => {
      if (event.type === "wheel") return true;
      if ((event.target as Element | null)?.closest?.(".graph-node")) {
        return false;
      }
      return !event.button;
    })
    .on("zoom", (event: { transform: ZoomTransform; sourceEvent?: Event }) => {
      transform = event.transform;
      viewport.attr("transform", transform.toString());
      if (event.sourceEvent) userAdjustedView = true;
      drawFrame();
    });

  svg.call(zoomer);
  svg.on("dblclick.zoom", null);
  svg.on("pointerdown.graph", () => {
    pointerMoved = false;
  });
  svg.on("pointermove.graph", (event: PointerEvent) => {
    if (event.buttons) pointerMoved = true;
  });
  svg.on("click.graph", (event: MouseEvent) => {
    if (pointerMoved) return;
    if ((event.target as Element | null)?.closest?.(".graph-node")) return;
    handlers.onClearFocus();
  });

  const nodeDrag = drag<SVGGElement, SimNode>()
    .on("start", (event, node) => {
      if (!event.active) simulation.alphaTarget(0.28).restart();
      node.fx = node.x ?? 0;
      node.fy = node.y ?? 0;
    })
    .on("drag", (event, node) => {
      node.fx = event.x;
      node.fy = event.y;
    })
    .on("end", (event, node) => {
      if (!event.active) simulation.alphaTarget(0);
      node.fx = null;
      node.fy = null;
      simulation.alpha(0.32).restart();
    });

  const linkForce = forceLink<SimNode, SimLink>([])
    .id((node) => node.id)
    .distance((link) => link.distance)
    .strength((link) => link.strength);
  const collideForce = forceCollide<SimNode>()
    .radius((node) => node.radius + (node.kind === "company" ? 3.2 : 4.4))
    .iterations(3)
    .strength(0.9);
  const chargeForce = forceManyBody<SimNode>()
    .strength((node) => nodeCharge(node.kind, compact))
    .distanceMax(320);
  const clusterForce = createClusterForce(0.18);
  const centerForce = forceCenter(width / 2, height / 2).strength(0.055);
  const xForce = forceX(width / 2).strength(0.035);
  const yForce = forceY(height / 2).strength(0.035);

  const simulation: Simulation<SimNode, SimLink> = forceSimulation<
    SimNode,
    SimLink
  >([])
    .force("link", linkForce)
    .force("charge", chargeForce)
    .force("collide", collideForce)
    .force("cluster", clusterForce)
    .force("center", centerForce)
    .force("x", xForce)
    .force("y", yForce)
    .alphaDecay(0.018)
    .velocityDecay(0.36)
    .randomSource(lcg(1))
    .on("tick", () => {
      if (frame) return;
      frame = requestAnimationFrame(() => {
        frame = 0;
        if (!destroyed) drawFrame();
      });
    })
    .on("end", () => {
      if (!destroyed && pendingFit && !userAdjustedView) {
        pendingFit = false;
        fitGraph();
      }
    });

  function bindLayers() {
    lensHullLayer.selectAll("path")
      .data(
        hulls.filter((hull) => hull.kind === "lens"),
        (hull) => (hull as GraphHull).id,
      )
      .join("path")
      .attr("fill-rule", "evenodd")
      .style("pointer-events", "none");

    familyHullLayer.selectAll("path")
      .data(
        hulls.filter((hull) => hull.kind === "family"),
        (hull) => (hull as GraphHull).id,
      )
      .join("path")
      .attr("fill-rule", "evenodd")
      .style("pointer-events", "none");

    hullLabelLayer.selectAll("text")
      .data(
        hulls.filter((hull) =>
          hull.kind === "lens" && hull.groupId === "other"
        ),
        (hull) => (hull as GraphHull).id,
      )
      .join("text")
      .attr("text-anchor", "middle")
      .attr("dominant-baseline", "middle")
      .style("pointer-events", "none")
      .style("font-family", FONT_FAMILY)
      .style("font-weight", "600")
      .style("paint-order", "stroke")
      .style("stroke", CANVAS_FILL)
      .style("stroke-width", 3.5)
      .style("fill", "#3f5663");

    edgeLayer.selectAll("line")
      .data(links, (link) => (link as SimLink).id)
      .join("line")
      .attr("stroke-linecap", "round");

    edgeLabelLayer.selectAll("text")
      .data(links, (link) => (link as SimLink).id)
      .join("text")
      .attr("text-anchor", "middle")
      .style("pointer-events", "none")
      .style("font-family", FONT_FAMILY)
      .style("font-weight", "500")
      .style("paint-order", "stroke")
      .style("stroke", CANVAS_FILL)
      .style("stroke-width", 3)
      .text((link) => link.label);

    const nodeSelection = nodeLayer.selectAll<SVGGElement, SimNode>(
      "g.graph-node",
    )
      .data(nodes, (node) => node.id);
    nodeSelection.exit().remove();
    const entered = nodeSelection.enter().append("g")
      .attr("class", "graph-node")
      .style("cursor", "grab")
      .call(nodeDrag)
      .on("click", (event, node) => {
        event.stopPropagation();
        if (event.defaultPrevented) return;
        handlers.onSelect(node.id);
      })
      .on("pointerenter", (_event, node) => {
        hoveredId = node.id;
        applySemanticZoom();
      })
      .on("pointerleave", () => {
        hoveredId = null;
        applySemanticZoom();
      });
    entered.append("circle").attr("class", "node-glow");
    entered.append("circle").attr("class", "node-core");
    entered.append("text")
      .attr("class", "node-label")
      .style("font-family", FONT_FAMILY)
      .style("paint-order", "stroke")
      .style("pointer-events", "none");
  }

  function drawFrame() {
    const k = transform.k || 1;
    lensHullLayer.selectAll<SVGPathElement, GraphHull>("path")
      .attr("d", (hull) => roundedHullPath(hullMembers(hull), 28) ?? "")
      .attr("fill", (hull) => lensHullFill(lens, hull.groupId))
      .attr("stroke", (hull) => lensHullStroke(lens, hull.groupId))
      .attr("stroke-width", 1 / k)
      .attr("stroke-opacity", 0.62)
      .attr("stroke-dasharray", `${6 / k} ${5 / k}`);

    familyHullLayer.selectAll<SVGPathElement, GraphHull>("path")
      .attr("d", (hull) => roundedHullPath(hullMembers(hull), 14) ?? "")
      .attr("fill", "rgba(255,255,255,0.34)")
      .attr("stroke", "#d5dfe4")
      .attr("stroke-width", 1 / k)
      .attr("stroke-opacity", 0.78);

    edgeLayer.selectAll<SVGLineElement, SimLink>("line")
      .attr("x1", (link) => endpoint(link.source)?.x ?? 0)
      .attr("y1", (link) => endpoint(link.source)?.y ?? 0)
      .attr("x2", (link) => endpoint(link.target)?.x ?? 0)
      .attr("y2", (link) => endpoint(link.target)?.y ?? 0);
    applyAppearance();
    applySemanticZoom();
  }

  function applyAppearance() {
    const k = transform.k || 1;
    const hasFocus = pathNodeIds.size > 0;
    edgeLayer.selectAll<SVGLineElement, SimLink>("line")
      .each(function (link) {
        const visual = edgeVisual(
          link,
          lens,
          hasFocus && pathEdgeIds.has(link.id),
          hasFocus && !pathEdgeIds.has(link.id),
        );
        select(this)
          .attr("stroke", visual.color)
          .attr("stroke-width", visual.width / k)
          .attr("stroke-opacity", visual.opacity)
          .attr(
            "stroke-dasharray",
            visual.dash ? visual.dash.map((item) => item / k).join(" ") : null,
          )
          .attr("marker-end", visual.arrow ? "url(#casys-edge-arrow)" : null);
      });

    edgeLabelLayer.selectAll<SVGTextElement, SimLink>("text")
      .attr("x", (link) => mid(link, "x"))
      .attr("y", (link) => mid(link, "y") - 8 / k)
      .attr("fill", (link) => edgeVisual(link, lens, true, false).color)
      .style("font-size", `${9 / k}px`)
      .style(
        "opacity",
        (link) =>
          hasFocus && pathEdgeIds.has(link.id) &&
            (link.classes.includes("influence-path") || k >= 1.15)
            ? 1
            : 0,
      );

    nodeLayer.selectAll<SVGGElement, SimNode>("g.graph-node")
      .attr(
        "transform",
        (node) => `translate(${node.x ?? 0},${node.y ?? 0})`,
      )
      .attr(
        "opacity",
        (node) => hasFocus && !pathNodeIds.has(node.id) ? 0.58 : 1,
      )
      .each(function (node) {
        const visual = nodeVisual(node);
        const selected = node.id === focusId;
        const onPath = hasFocus && pathNodeIds.has(node.id);
        const group = select(this);
        group.select("circle.node-glow")
          .attr("r", node.radius + (selected ? 8 : onPath ? 5 : 0))
          .attr("fill", selected || onPath ? "#176887" : "transparent")
          .attr("fill-opacity", selected ? 0.11 : onPath ? 0.07 : 0);
        group.select("circle.node-core")
          .attr("r", node.radius)
          .attr("fill", visual.fill)
          .attr("stroke", selected || onPath ? "#176887" : visual.stroke)
          .attr(
            "stroke-width",
            (selected ? 2.4 : onPath ? 1.8 : visual.strokeWidth) / k,
          )
          .attr(
            "stroke-dasharray",
            visual.dash ? visual.dash.map((item) => item / k).join(" ") : null,
          );
      });
  }

  function applySemanticZoom() {
    const k = transform.k || 1;
    nodeLayer.selectAll<SVGGElement, SimNode>("g.graph-node")
      .each(function (node) {
        const visual = nodeVisual(node);
        const highlighted = hoveredId === node.id || node.id === focusId ||
          pathNodeIds.has(node.id);
        const visible = nodeLabelVisible({
          kind: node.kind,
          zoom: k,
          hovered: hoveredId === node.id,
          focused: highlighted,
        }) &&
          (highlighted || k >= 1.08 || node.kind === "market" ||
            node.kind === lensNodeKind(lens));
        const screen = node.kind === "market"
          ? 11
          : node.kind === "company"
          ? 9
          : 10;
        select(this).select("text.node-label")
          .text(node.label)
          .attr("x", node.kind === "company" ? node.radius + 6 : 0)
          .attr("y", node.kind === "company" ? 0 : 0.35)
          .attr("text-anchor", node.kind === "company" ? "start" : "middle")
          .attr("dominant-baseline", "middle")
          .attr("fill", visual.text)
          .style("font-size", `${screen / k}px`)
          .style("font-weight", node.kind === "market" ? "650" : "600")
          .style("stroke", node.kind === "market" ? "#284b5b" : CANVAS_FILL)
          .style("stroke-width", `${3.2 / k}`)
          .style("opacity", visible ? 1 : 0);
      });

    hullLabelLayer.selectAll<SVGTextElement, GraphHull>("text")
      .text((hull) => hullLabel(hull, graph, lens))
      .attr("x", (hull) => centroid(hullMembers(hull)).x)
      .attr("y", (hull) => centroid(hullMembers(hull)).y)
      .style("font-size", `${10 / k}px`)
      .style("stroke-width", `${3.4 / k}`);
  }

  function hullMembers(hull: GraphHull): SimNode[] {
    return hull.memberIds
      .map((id) => nodeById.get(id))
      .filter((node): node is SimNode => Boolean(node));
  }

  function refreshFocusPath() {
    const path = focusedPath(graph, focusId);
    pathNodeIds = path.nodeIds;
    pathEdgeIds = path.edgeIds;
  }

  function rebuild(nextGraph: MarketInfluenceGraph) {
    const presentation = presentGraph(nextGraph);
    const seeds = seedNodePositions(
      nextGraph.nodes.map((node) => node.data.id),
      width,
      height,
    );
    const previous = new Map(nodes.map((node) => [node.id, node]));
    nodes = nextGraph.nodes.map((node) => {
      const prior = previous.get(node.data.id);
      const seed = seeds.get(node.data.id) ?? { x: width / 2, y: height / 2 };
      const next: SimNode = {
        id: node.data.id,
        kind: node.data.kind,
        label: displayNodeLabel(node.data),
        classes: node.classes,
        extraClass: presentation.extraClassById.get(node.data.id) ?? "",
        radius: presentation.radiusById.get(node.data.id) ?? 8,
        clusterId: clusterIdForNode(nextGraph, lens, node.data.id),
        x: seed.x,
        y: seed.y,
        vx: 0,
        vy: 0,
        fx: null,
        fy: null,
      };
      if (!prior) return next;
      prior.kind = next.kind;
      prior.label = next.label;
      prior.classes = next.classes;
      prior.extraClass = next.extraClass;
      prior.radius = next.radius;
      prior.clusterId = next.clusterId;
      return prior;
    });
    links = nextGraph.edges.map((edge) => {
      const force = linkForceParams(edge.data.basis, compact);
      return {
        id: edge.data.id,
        source: edge.data.source,
        target: edge.data.target,
        basis: edge.data.basis,
        kind: edge.data.kind,
        tone: edge.data.tone,
        label: edge.data.label,
        classes: edge.classes,
        strength: force.strength,
        distance: force.distance,
      };
    });
    refreshIndexes(nextGraph);
    simulation.nodes(nodes);
    linkForce.links(links);
  }

  function syncPresentation(nextGraph: MarketInfluenceGraph) {
    const presentation = presentGraph(nextGraph);
    for (const node of nodes) {
      const source = nextGraph.nodes.find((item) => item.data.id === node.id);
      if (!source) continue;
      node.label = displayNodeLabel(source.data);
      node.classes = source.classes;
      node.extraClass = presentation.extraClassById.get(node.id) ?? "";
      node.radius = presentation.radiusById.get(node.id) ?? node.radius;
      node.clusterId = clusterIdForNode(nextGraph, lens, node.id);
    }
    const sourceEdges = new Map(
      nextGraph.edges.map((edge) => [edge.data.id, edge]),
    );
    for (const link of links) {
      const source = sourceEdges.get(link.id);
      if (source) {
        link.basis = source.data.basis;
        link.kind = source.data.kind;
        link.tone = source.data.tone;
        link.label = source.data.label;
        link.classes = source.classes;
      }
      const force = linkForceParams(link.basis, compact);
      link.strength = force.strength;
      link.distance = force.distance;
    }
    linkForce.links(links);
    refreshIndexes(nextGraph);
  }

  function refreshIndexes(nextGraph: MarketInfluenceGraph) {
    nodeById.clear();
    for (const node of nodes) nodeById.set(node.id, node);
    hulls = graphHulls(nextGraph, lens);
    chargeForce.strength((node) => nodeCharge(node.kind, compact));
    collideForce.radius((node) =>
      node.radius + (node.kind === "company" ? 3.2 : 4.4)
    );
    centerForce.x(width / 2).y(height / 2);
    xForce.x(width / 2);
    yForce.y(height / 2);
  }

  function fitGraph() {
    if (!nodes.length) return;
    let minX = Infinity;
    let minY = Infinity;
    let maxX = -Infinity;
    let maxY = -Infinity;
    for (const node of nodes) {
      const x = node.x ?? 0;
      const y = node.y ?? 0;
      minX = Math.min(minX, x - node.radius);
      minY = Math.min(minY, y - node.radius);
      maxX = Math.max(maxX, x + node.radius);
      maxY = Math.max(maxY, y + node.radius);
    }
    if (!Number.isFinite(minX) || !Number.isFinite(minY)) return;
    const padding = 52;
    const nextK = Math.min(
      MAX_ZOOM,
      Math.max(
        MIN_ZOOM,
        0.92 * Math.min(
          width / Math.max(maxX - minX + padding * 2, 1),
          height / Math.max(maxY - minY + padding * 2, 1),
        ),
      ),
    );
    const next = zoomIdentity
      .translate(width / 2, height / 2)
      .scale(nextK)
      .translate(-(minX + maxX) / 2, -(minY + maxY) / 2);
    svg.call(zoomer.transform, next);
    userAdjustedView = false;
  }

  function recenter(nodeId: string) {
    const node = nodeById.get(nodeId);
    if (!node || node.x == null || node.y == null) return;
    const screenX = transform.applyX(node.x);
    const screenY = transform.applyY(node.y);
    const insetX = width * 0.18;
    const insetY = height * 0.18;
    if (
      screenX >= insetX && screenX <= width - insetX &&
      screenY >= insetY && screenY <= height - insetY
    ) return;
    const next = zoomIdentity
      .translate(width / 2, height / 2)
      .scale(transform.k)
      .translate(-node.x, -node.y);
    svg.call(zoomer.transform, next);
  }

  function updateSize() {
    const nextWidth = Math.max(container.clientWidth, 120);
    const nextHeight = Math.max(container.clientHeight, 120);
    if (nextWidth === width && nextHeight === height) return;
    const worldX = (width / 2 - transform.x) / (transform.k || 1);
    const worldY = (height / 2 - transform.y) / (transform.k || 1);
    width = nextWidth;
    height = nextHeight;
    svg.attr("width", width).attr("height", height);
    svg.select("rect.graph-background")
      .attr("width", width)
      .attr("height", height);
    centerForce.x(width / 2).y(height / 2);
    xForce.x(width / 2);
    yForce.y(height / 2);
    const next = zoomIdentity
      .translate(
        width / 2 - worldX * transform.k,
        height / 2 - worldY * transform.k,
      )
      .scale(transform.k);
    if (userAdjustedView) svg.call(zoomer.transform, next);
    else fitGraph();
  }

  return {
    update(input) {
      const nextKey = graphTopologyKey(input.graph);
      const topologyChanged = nextKey !== topologyKey;
      const lensChanged = input.lens !== lens;
      const compactChanged = input.compact !== compact;
      graph = input.graph;
      lens = input.lens;
      compact = input.compact;
      if (input.focusId !== undefined) focusId = input.focusId;
      if (topologyChanged) {
        topologyKey = nextKey;
        rebuild(graph);
        bindLayers();
        pendingFit = true;
        userAdjustedView = false;
        simulation.alpha(1).restart();
      } else {
        syncPresentation(graph);
        bindLayers();
        if (lensChanged) {
          pendingFit = !userAdjustedView;
          simulation.alpha(0.88).restart();
        } else if (compactChanged) {
          simulation.alpha(0.4).restart();
        }
      }
      refreshFocusPath();
      applyAppearance();
      applySemanticZoom();
    },
    setFocus(nodeId, options) {
      focusId = nodeId;
      refreshFocusPath();
      applyAppearance();
      applySemanticZoom();
      if (nodeId && options?.recenter) recenter(nodeId);
    },
    fit() {
      userAdjustedView = false;
      fitGraph();
    },
    reorganize() {
      for (const node of nodes) {
        const jitter = 28 + node.radius;
        node.x = (node.x ?? width / 2) + (Math.random() - 0.5) * jitter;
        node.y = (node.y ?? height / 2) + (Math.random() - 0.5) * jitter;
        node.vx = 0;
        node.vy = 0;
        node.fx = null;
        node.fy = null;
      }
      pendingFit = true;
      userAdjustedView = false;
      simulation.alpha(1).restart();
    },
    zoomBy(factor) {
      userAdjustedView = true;
      pendingFit = false;
      zoomer.scaleBy(svg, factor, [width / 2, height / 2]);
    },
    resize() {
      updateSize();
    },
    destroy() {
      destroyed = true;
      if (frame) cancelAnimationFrame(frame);
      simulation.stop();
      simulation.on("tick", null);
      simulation.on("end", null);
      svg.on(".zoom", null);
      svg.on(".graph", null);
      select(window).on(".drag", null).on(".zoom", null);
      svg.remove();
    },
  };
}

function createClusterForce(strength: number): Force<SimNode, SimLink> {
  let nodes: SimNode[] = [];
  const force = (alpha: number) => {
    const groups = new Map<string, { x: number; y: number; count: number }>();
    for (const node of nodes) {
      if (!node.clusterId || node.x == null || node.y == null) continue;
      const group = groups.get(node.clusterId) ?? { x: 0, y: 0, count: 0 };
      group.x += node.x;
      group.y += node.y;
      group.count += 1;
      groups.set(node.clusterId, group);
    }
    for (const group of groups.values()) {
      group.x /= group.count;
      group.y /= group.count;
    }
    const k = strength * alpha;
    for (const node of nodes) {
      if (!node.clusterId || node.x == null || node.y == null) continue;
      const group = groups.get(node.clusterId);
      if (!group) continue;
      node.vx = (node.vx ?? 0) + (group.x - node.x) * k;
      node.vy = (node.vy ?? 0) + (group.y - node.y) * k;
    }
  };
  force.initialize = (init: SimNode[]) => {
    nodes = init;
  };
  return force;
}

function nodeCharge(kind: InfluenceNodeKind, isCompact: boolean): number {
  const scale = isCompact ? 0.86 : 1;
  if (kind === "company") return -26 * scale;
  if (kind === "family") return -68 * scale;
  if (kind === "domain") return -92 * scale;
  if (kind === "market") return -108 * scale;
  return -84 * scale;
}

function lensNodeKind(lens: GraphLens): InfluenceNodeKind {
  if (lens === "domain") return "domain";
  if (lens === "macro") return "driver";
  return "market";
}

function nodeVisual(node: SimNode): {
  fill: string;
  stroke: string;
  strokeWidth: number;
  text: string;
  dash?: number[];
} {
  const classes = `${node.classes} ${node.extraClass}`;
  if (node.kind === "market") {
    return {
      fill: "#284b5b",
      stroke: "#1d3946",
      strokeWidth: 1.5,
      text: "#ffffff",
    };
  }
  if (node.kind === "domain") {
    return {
      fill: "#dce8ed",
      stroke: "#7793a0",
      strokeWidth: 1.1,
      text: "#172b36",
    };
  }
  if (node.kind === "driver") {
    const heuristic = classes.includes("heuristic") ? [4, 3] : undefined;
    if (classes.includes("signal-supportive")) {
      return {
        fill: "#e5f2ed",
        stroke: "#26735a",
        strokeWidth: 1.35,
        text: "#245443",
        dash: heuristic,
      };
    }
    if (classes.includes("signal-headwind")) {
      return {
        fill: "#f9ece9",
        stroke: "#b45149",
        strokeWidth: 1.35,
        text: "#713a36",
        dash: heuristic,
      };
    }
    return {
      fill: "#f5f0e6",
      stroke: "#aa8c57",
      strokeWidth: 1.2,
      text: "#59482e",
      dash: heuristic,
    };
  }
  if (node.kind === "family") {
    if (classes.includes("priority-favored")) {
      return {
        fill: "#e5f2ed",
        stroke: "#26735a",
        strokeWidth: 1.35,
        text: "#172b36",
      };
    }
    if (classes.includes("priority-deprioritized")) {
      return {
        fill: "#f9ece9",
        stroke: "#b45149",
        strokeWidth: 1.25,
        text: "#172b36",
      };
    }
    return {
      fill: "#f4f7f8",
      stroke: "#aabcc5",
      strokeWidth: 1,
      text: classes.includes("unranked") ? "#61737c" : "#172b36",
      dash: classes.includes("unranked") ? [4, 3] : undefined,
    };
  }
  return {
    fill: "#9eb4be",
    stroke: "#718d99",
    strokeWidth: 1,
    text: "#526771",
  };
}

function edgeVisual(
  link: SimLink,
  lens: GraphLens,
  focused: boolean,
  dimmed: boolean,
): {
  color: string;
  width: number;
  opacity: number;
  dash?: number[];
  arrow: boolean;
} {
  let color = "#b7c6cd";
  let width = 0.9;
  let opacity = 0.28;
  let dash: number[] | undefined;
  if (link.classes.includes("company-path")) {
    color = "#c8d3d8";
    opacity = 0.16;
  }
  if (link.classes.includes("market-path")) {
    dash = [2, 3.5];
    opacity = 0.2;
  }
  if (link.classes.includes("influence-path")) {
    color = "#7695a3";
    opacity = 0.34;
  }
  if (link.classes.includes("possible")) dash = [8, 5];
  if (link.classes.includes("evidence")) {
    color = "#176887";
    width = 1.15;
    opacity = 0.42;
  }
  if (link.tone === "supportive") color = "#26735a";
  if (link.tone === "headwind") color = "#b45149";
  if (link.tone === "mixed") color = "#9a6a25";
  if (lens === "market" && link.basis === "market_membership") {
    opacity = Math.max(opacity, 0.4);
  }
  if (lens === "domain" && link.basis === "governed_domain") {
    opacity = Math.max(opacity, 0.42);
  }
  if (lens === "macro" && link.basis === "heuristic_domain") {
    opacity = Math.max(opacity, 0.45);
  }
  if (dimmed) opacity = 0.08;
  if (focused) {
    opacity = 1;
    width = 2.2;
    color = link.tone === "supportive"
      ? "#26735a"
      : link.tone === "headwind"
      ? "#b45149"
      : link.tone === "mixed"
      ? "#9a6a25"
      : "#176887";
  }
  return {
    color,
    width,
    opacity,
    dash,
    arrow: link.classes.includes("possible"),
  };
}

function lensHullFill(lens: GraphLens, groupId: string): string {
  if (groupId === "other") return "rgba(231,237,241,0.22)";
  if (lens === "domain") return "rgba(232,238,244,0.27)";
  if (lens === "macro") return "rgba(243,238,227,0.3)";
  return "rgba(223,236,239,0.26)";
}

function lensHullStroke(lens: GraphLens, groupId: string): string {
  if (groupId === "other") return "#bdccd3";
  if (lens === "domain") return "#9aafc0";
  if (lens === "macro") return "#b9a276";
  return "#8faab5";
}

function hullLabel(
  hull: GraphHull,
  graph: MarketInfluenceGraph,
  lens: GraphLens,
): string {
  const node = graph.nodes.find((item) => item.data.id === hull.groupId);
  if (node) return displayNodeLabel(node.data);
  if (hull.groupId === "other") {
    return lens === "macro" ? "Other factors" : "Other";
  }
  return "";
}

function roundedHullPath(members: SimNode[], padding: number): string | null {
  if (!members.length) return null;
  const samples: [number, number][] = [];
  const steps = members.length === 1 ? 10 : 8;
  for (const member of members) {
    const radius = member.radius + padding;
    for (let index = 0; index < steps; index += 1) {
      const angle = (Math.PI * 2 * index) / steps;
      samples.push([
        (member.x ?? 0) + Math.cos(angle) * radius,
        (member.y ?? 0) + Math.sin(angle) * radius,
      ]);
    }
  }
  const hull = polygonHull(samples);
  if (!hull || hull.length < 3) {
    const member = members[0];
    const radius = member.radius + padding;
    return `M ${member.x ?? 0} ${
      (member.y ?? 0) - radius
    } a ${radius} ${radius} 0 1 0 0.01 0`;
  }
  return hullLine(hull);
}

function endpoint(value: SimLink["source"]): SimNode | null {
  return typeof value === "object" ? value : null;
}

function mid(link: SimLink, axis: "x" | "y"): number {
  const source = endpoint(link.source);
  const target = endpoint(link.target);
  return (((source?.[axis] ?? 0) + (target?.[axis] ?? 0)) / 2);
}

function centroid(members: SimNode[]): { x: number; y: number } {
  if (!members.length) return { x: 0, y: 0 };
  let x = 0;
  let y = 0;
  for (const member of members) {
    x += member.x ?? 0;
    y += member.y ?? 0;
  }
  return { x: x / members.length, y: y / members.length };
}

function graphTopologyKey(graph: MarketInfluenceGraph): string {
  return [
    graph.nodes.map((node) => node.data.id).join("|"),
    graph.edges.map((edge) =>
      `${edge.data.id}:${edge.data.source}>${edge.data.target}`
    ).join("|"),
  ].join("::");
}

function lcg(seed: number): () => number {
  let value = seed;
  return () => {
    value = (value * 1664525 + 1013904223) % 4294967296;
    return value / 4294967296;
  };
}

function displayNodeLabel(
  data: MarketInfluenceGraph["nodes"][number]["data"],
): string {
  if (data.kind === "market") return venueLabel(data.scopeKey || data.label);
  if (data.kind === "family") {
    return compactFamilyLabel(data.family, data.scopeKey);
  }
  return data.label;
}

function compactFamilyLabel(family?: string, scopeKey?: string): string {
  const full = familyLabel(family);
  const market = venueLabel(scopeKey);
  if (
    String(family ?? "").toLowerCase().startsWith(
      `${String(scopeKey ?? "").toLowerCase()}_`,
    )
  ) {
    const withoutFirstWord = full.split(/\s+/).slice(1).join(" ").trim();
    if (withoutFirstWord) return withoutFirstWord;
  }
  for (const prefix of [market, market.replace(/e$/, "") + "n", scopeKey]) {
    const candidate = String(prefix ?? "").trim();
    if (
      candidate && full.toLowerCase().startsWith(`${candidate.toLowerCase()} `)
    ) {
      return full.slice(candidate.length + 1).trim() || full;
    }
  }
  return full;
}
