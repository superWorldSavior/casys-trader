import { format } from "date-fns";
import { ChevronDown, ExternalLink } from "lucide-react";
import { useState } from "react";
import { Line, LineChart, ResponsiveContainer, YAxis } from "recharts";
import { MarketIntelligenceAtlas } from "@/components/intelligence/market-intelligence-atlas";
import { Badge } from "@/components/ui/badge";
import { Card, CardBody, CardHeader, CardTitle } from "@/components/ui/card";
import {
  useDailyBriefing,
  useNewsFeed,
  useRegionIntelligence,
} from "@/hooks/use-intelligence";
import { formatAgo, formatCountdown, signedClass } from "@/lib/format";
import {
  companyDisplayName,
  grossModeLabel,
  netBiasLabel,
  plainMarketLanguage,
  projectionFreshness,
  venueLabel,
} from "@/lib/humanize";
import { collectScopeKeys } from "@/lib/market-intelligence-atlas";
import type {
  BriefingStory,
  GlobalIntelligencePosture,
  MacroCalendarEvent,
  MacroIndicator,
  NewsFeedItem,
  PostureHistoryPoint,
  Snapshot,
  UpcomingEarning,
} from "@/lib/types";
import { cn } from "@/lib/utils";

type Props = {
  snapshot?: Snapshot | null;
  onSymbol: (symbol: string) => void;
};

// ── Signal label helper ──────────────────────────────────────────────────────

const SIGNAL_MAP: Record<string, string> = {
  risk_on: "risk appetite",
  risk_off: "risk aversion",
  hawkish: "rates pressure up",
  dovish: "rates easing",
  strong: "strong USD",
  weak: "weak USD",
  bullish: "bullish",
  bearish: "bearish",
  neutral: "neutral",
  sideways: "sideways",
  trending_up: "trending up",
  trending_down: "trending down",
  expanding: "expanding",
  contracting: "contracting",
};

function describeSignal(value: string | null | undefined): string {
  if (!value) return "—";
  return SIGNAL_MAP[value] ?? value.replaceAll("_", " ");
}

// The daemon writes the literal string "unknown" when a signal is unset.
function knownSignal(value: string | null | undefined): string | null {
  if (!value || value === "unknown") return null;
  return value;
}

function dedupeBriefingStories(stories: BriefingStory[]): BriefingStory[] {
  const seenRefSets: string[][] = [];
  const seenPoints = new Set<string>();

  return stories.filter((story) => {
    const refs = Array.from(new Set(story.source_refs.filter(Boolean))).sort();
    const pointKey = plainMarketLanguage(story.point).toLowerCase().replace(
      /[^a-z0-9]+/g,
      " ",
    ).trim();
    const sameEvidenceCluster = refs.length > 0 && seenRefSets.some((seen) => {
      const shared = refs.filter((ref) => seen.includes(ref)).length;
      const sameSet = shared === refs.length && shared === seen.length;
      const containedSet = shared >= 2 &&
        shared === Math.min(refs.length, seen.length);
      return sameSet || containedSet;
    });
    if (sameEvidenceCluster || seenPoints.has(pointKey)) return false;
    if (refs.length) seenRefSets.push(refs);
    seenPoints.add(pointKey);
    return true;
  });
}

function BriefingPosture({ posture }: { posture: GlobalIntelligencePosture }) {
  const freshness = projectionFreshness(posture);
  return (
    <div className="border-t border-line pt-4 xl:border-l xl:border-t-0 xl:pl-5 xl:pt-0">
      <dl className="space-y-3">
        <div className="flex items-baseline justify-between gap-4">
          <dt className="text-xs text-dim">Risk appetite</dt>
          <dd className="text-sm font-semibold text-fg">
            {grossModeLabel(posture.gross_mode)}
          </dd>
        </div>
        <div className="flex items-baseline justify-between gap-4 border-t border-hairline pt-3">
          <dt className="text-xs text-dim">Portfolio direction</dt>
          <dd className="text-sm font-semibold text-fg">
            {netBiasLabel(posture.net_bias)}
          </dd>
        </div>
      </dl>
      <p
        className={cn(
          "mt-4 font-mono text-[9px] uppercase tracking-[0.16em]",
          freshness.current ? "text-gain" : "text-warn",
        )}
      >
        {freshness.label}
      </p>
    </div>
  );
}

// ── Compact trend ────────────────────────────────────────────────────────────

function Sparkline(
  { history }: { history: Array<{ period: string; value: number }> },
) {
  if (history.length < 2) return null;
  const values = history.map((p) => p.value);
  const last = values[values.length - 1];
  const prev = values[values.length - 2];
  const rising = last >= prev;
  return (
    <div className="h-6 w-[72px] shrink-0" aria-hidden="true">
      <ResponsiveContainer width="100%" height="100%">
        <LineChart data={history}>
          <Line
            type="monotone"
            dataKey="value"
            stroke={rising ? "var(--color-gain)" : "var(--color-loss)"}
            strokeWidth={1.5}
            dot={false}
            isAnimationActive={false}
          />
        </LineChart>
      </ResponsiveContainer>
    </div>
  );
}

// ── Story severity dot ────────────────────────────────────────────────────────

function SeverityDot({ severity }: { severity: string | null }) {
  // Daemon vocabulary: risk > watch > info (high/medium kept as synonyms).
  const cls = severity === "risk" || severity === "high"
    ? "bg-loss"
    : severity === "watch" || severity === "medium"
    ? "bg-warn"
    : "bg-faint";
  return <span className={cn("mt-1.5 size-1.5 shrink-0 rounded-full", cls)} />;
}

// ── Main page ─────────────────────────────────────────────────────────────────

export function TodayPage({ snapshot, onSymbol }: Props) {
  const briefingQuery = useDailyBriefing();
  const newsFeedQuery = useNewsFeed({ limit: 40 });
  const regionQuery = useRegionIntelligence();
  const [selectedMarket, setSelectedMarket] = useState("");

  const briefing = briefingQuery.data;
  const headline = briefing?.headline;
  const stories = dedupeBriefingStories(briefing?.top_stories ?? []);
  const indicators = briefing?.indicators ?? [];
  const calendar = briefing?.calendar ?? [];
  const earnings = briefing?.earnings ?? [];
  const postureHistory = briefing?.posture_history ?? [];
  const newsFeedItems = newsFeedQuery.data?.items ?? briefing?.news ?? [];
  const marketScopes = collectScopeKeys(
    regionQuery.data?.current,
    regionQuery.data?.families,
  );
  const activeMarket = marketScopes.includes(selectedMarket)
    ? selectedMarket
    : (marketScopes[0] ?? "");

  const todayLabel = format(new Date(), "EEEE, MMMM d");

  return (
    <div className="grid gap-6">
      {/* ── Manchette ───────────────────────────────────────────────────── */}
      <section className="rounded-lg border border-line bg-panel/90 p-5 shadow-[0_1px_2px_rgba(23,43,54,0.04)]">
        <div className="grid gap-5 xl:grid-cols-[minmax(0,1fr)_250px]">
          <div>
            <div className="flex flex-wrap items-center justify-between gap-2 font-mono text-[9px] uppercase tracking-[0.18em] text-faint">
              <p>{todayLabel}</p>
              {headline?.digest_as_of
                ? <p>Updated {formatAgo(headline.digest_as_of)}</p>
                : null}
            </div>
            {briefingQuery.isPending && !briefing
              ? (
                <p className="mt-3 text-sm text-faint">
                  Loading today’s briefing…
                </p>
              )
              : briefingQuery.error && !briefing
              ? (
                <p className="mt-3 text-sm text-warn">
                  Today’s briefing is temporarily unavailable.
                </p>
              )
              : headline?.lead
              ? (
                <h1 className="mt-3 max-w-4xl text-[17px] font-semibold leading-snug tracking-tight text-fg">
                  {plainMarketLanguage(headline.lead)}
                </h1>
              )
              : (
                <p className="mt-3 text-sm text-faint">
                  Casys has not recorded today's briefing yet.
                </p>
              )}
            <div className="mt-4 flex flex-wrap gap-2">
              {knownSignal(headline?.regime)
                ? (
                  <Badge tone="warn">
                    {describeSignal(headline?.regime)}
                  </Badge>
                )
                : null}
              {knownSignal(headline?.rates_bias)
                ? (
                  <Badge
                    tone={headline?.rates_bias === "hawkish" ? "loss" : "gain"}
                  >
                    {describeSignal(headline?.rates_bias)}
                  </Badge>
                )
                : null}
              {knownSignal(headline?.usd_bias)
                ? <Badge>{describeSignal(headline?.usd_bias)}</Badge>
                : null}
            </div>
          </div>
          {headline?.posture
            ? <BriefingPosture posture={headline.posture} />
            : null}
        </div>
      </section>

      {regionQuery.data
        ? (
          <MarketIntelligenceAtlas
            current={regionQuery.data.current}
            families={regionQuery.data.families}
            comparison={regionQuery.data.comparison}
            companyMap={snapshot?.company_map ?? {}}
            activeScope={activeMarket}
            onSelectScope={setSelectedMarket}
          />
        )
        : regionQuery.isPending
        ? (
          <section className="rounded-lg border border-line bg-panel/70 px-5 py-10 text-center text-sm text-faint">
            Building the current market view…
          </section>
        )
        : (
          <section className="rounded-lg border border-warn/25 bg-warn/5 px-5 py-4 text-sm text-warn">
            The market map is temporarily unavailable; today’s briefing remains
            visible below.
          </section>
        )}

      {/* ── Body: main + sidebar ────────────────────────────────────────── */}
      <div className="grid gap-4 xl:grid-cols-[minmax(0,1.4fr)_minmax(280px,0.6fr)]">
        {/* Main column */}
        <div className="space-y-4">
          {/* Top stories */}
          <Card>
            <CardHeader>
              <CardTitle>What matters today</CardTitle>
              {briefingQuery.error
                ? (
                  <span className="font-mono text-[9px] text-loss">
                    unavailable
                  </span>
                )
                : null}
            </CardHeader>
            <CardBody className="p-0">
              {stories.length > 0
                ? (
                  <div className="px-4 pt-3 pb-1">
                    <RiskPulse stories={stories} />
                  </div>
                )
                : null}
              {briefingQuery.isPending && !briefing
                ? (
                  <p className="px-4 py-6 text-sm text-faint">
                    Loading today’s briefing…
                  </p>
                )
                : briefingQuery.error && !briefing
                ? (
                  <QueryUnavailable
                    label="Today’s market briefing is unavailable."
                    error={briefingQuery.error}
                    className="px-4 py-6"
                  />
                )
                : stories.length === 0
                ? (
                  <p className="px-4 py-6 text-sm text-faint">
                    No material story is recorded in today’s briefing.
                  </p>
                )
                : (
                  stories.map((story, idx) => (
                    <StoryRow
                      key={`${story.point}-${idx}`}
                      story={story}
                      companyMap={snapshot?.company_map ?? {}}
                      onSymbol={onSymbol}
                    />
                  ))
                )}
            </CardBody>
          </Card>
        </div>

        {/* Sidebar */}
        <aside className="space-y-4 xl:sticky xl:top-0 xl:self-start">
          {/* Macro indicators */}
          <Card>
            <CardHeader>
              <CardTitle>The world in numbers</CardTitle>
            </CardHeader>
            <CardBody className="space-y-4">
              {briefingQuery.isPending && !briefing
                ? (
                  <p className="text-sm text-faint">
                    Loading economic indicators…
                  </p>
                )
                : briefingQuery.error && !briefing
                ? (
                  <p className="text-sm text-warn">
                    Economic indicators are temporarily unavailable.
                  </p>
                )
                : indicators.length === 0
                ? (
                  <p className="text-sm text-faint">
                    Economic indicators will appear here once Casys has
                    collected them.
                  </p>
                )
                : (
                  indicators.map((ind) => (
                    <IndicatorRow key={ind.series_id} indicator={ind} />
                  ))
                )}
            </CardBody>
          </Card>

          {/* Calendar + earnings */}
          <Card>
            <CardHeader>
              <CardTitle>Coming up</CardTitle>
            </CardHeader>
            <CardBody className="space-y-3">
              {briefingQuery.isPending && !briefing
                ? <p className="text-sm text-faint">Loading upcoming events…</p>
                : briefingQuery.error && !briefing
                ? (
                  <p className="text-sm text-warn">
                    Upcoming events are temporarily unavailable.
                  </p>
                )
                : (
                  <ComingUp
                    calendar={calendar}
                    earnings={earnings}
                    onSymbol={onSymbol}
                  />
                )}
            </CardBody>
          </Card>

          {/* Outlook history */}
          {postureHistory.length >= 2
            ? (
              <Card>
                <CardHeader>
                  <CardTitle>How the outlook changed</CardTitle>
                </CardHeader>
                <CardBody>
                  <StanceHistory points={postureHistory} />
                </CardBody>
              </Card>
            )
            : null}
        </aside>
      </div>

      {/* Adjacent research material, not an evidence join to each story. */}
      <details className="group overflow-hidden rounded-lg border border-line bg-panel/80">
        <summary className="flex cursor-pointer list-none items-center justify-between gap-3 px-4 py-3 [&::-webkit-details-marker]:hidden">
          <span className="font-mono text-[10px] uppercase tracking-[0.18em] text-faint">
            Recent source material
          </span>
          <span className="flex items-center gap-2 font-mono text-[9px] text-faint">
            {newsFeedQuery.isPending
              ? "refreshing"
              : `${newsFeedItems.length} reports`}
            <ChevronDown className="size-3.5 transition-transform group-open:rotate-180" />
          </span>
        </summary>
        <div
          className="border-t border-line"
          aria-label="Recent source reports"
        >
          {newsFeedItems.length
            ? (
              <>
                {(newsFeedQuery.isPending || newsFeedQuery.error) &&
                    !newsFeedQuery.data
                  ? (
                    <p className="border-b border-hairline px-4 py-2 text-xs text-warn">
                      Showing recorded source material while the live feed
                      refreshes.
                    </p>
                  )
                  : null}
                {newsFeedItems.map((item) => (
                  <NewsRow key={item.id} item={item} onSymbol={onSymbol} />
                ))}
              </>
            )
            : newsFeedQuery.isPending && !newsFeedQuery.data
            ? (
              <p className="px-4 py-6 text-sm text-faint">
                Loading source material…
              </p>
            )
            : newsFeedQuery.error
            ? (
              <QueryUnavailable
                label="Source material is temporarily unavailable."
                error={newsFeedQuery.error}
                className="px-4 py-6"
              />
            )
            : (
              <p className="px-4 py-6 text-sm text-faint">
                No source material is available for the current briefing.
              </p>
            )}
        </div>
      </details>
    </div>
  );
}

// ── Story row ────────────────────────────────────────────────────────────────

function StoryRow({
  story,
  companyMap,
  onSymbol,
}: {
  story: BriefingStory;
  companyMap: Record<string, string>;
  onSymbol: (s: string) => void;
}) {
  const bullish = story.direction === "bullish" || story.direction === "up";
  const bearish = story.direction === "bearish" || story.direction === "down";
  return (
    <div className="flex gap-3 border-b border-hairline px-4 py-3 last:border-0">
      <SeverityDot severity={story.severity} />
      <div className="min-w-0 flex-1">
        <div className="flex flex-wrap items-center gap-1.5">
          {bullish
            ? <span className="text-xs font-semibold text-gain">▲</span>
            : null}
          {bearish
            ? <span className="text-xs font-semibold text-loss">▼</span>
            : null}
          <p className="text-sm leading-relaxed text-muted">
            {plainMarketLanguage(story.point)}
          </p>
        </div>
        <div className="mt-1.5 flex flex-wrap items-center gap-1.5">
          {story.venue ? <Badge>{venueLabel(story.venue)}</Badge> : null}
          {story.symbols.map((sym) => (
            <button
              key={sym}
              type="button"
              onClick={() => onSymbol(sym)}
              className="rounded-sm bg-accent/10 px-1.5 py-0.5 text-[10px] text-accent transition-colors hover:bg-accent/20 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent/50"
            >
              {companyDisplayName(companyMap, sym)}{" "}
              <span className="font-mono text-[9px] text-accent/70">{sym}</span>
            </button>
          ))}
        </div>
        {story.sources.length > 0
          ? (
            <p className="mt-1 font-mono text-[9px] text-faint">
              {story.sources.slice(0, 3).map(sourceLabel).join(" · ")}
              {story.sources.length > 3 ? ` +${story.sources.length - 3}` : ""}
            </p>
          )
          : null}
      </div>
    </div>
  );
}

// ── News row ─────────────────────────────────────────────────────────────────

function NewsRow(
  { item, onSymbol }: { item: NewsFeedItem; onSymbol: (s: string) => void },
) {
  const timeLabel = item.published_at ? formatAgo(item.published_at) : null;
  return (
    <div className="flex gap-3 border-b border-hairline px-4 py-3 last:border-0">
      <div className="min-w-0 flex-1">
        <div className="flex flex-wrap items-start justify-between gap-2">
          {item.url
            ? (
              <a
                href={item.url}
                target="_blank"
                rel="noreferrer"
                className="group flex min-w-0 items-start gap-1 text-sm leading-relaxed text-muted hover:text-fg"
              >
                <span className="flex-1" title={item.title}>
                  {plainMarketLanguage(item.title)}
                </span>
                <ExternalLink className="mt-0.5 size-3 shrink-0 text-faint group-hover:text-dim" />
              </a>
            )
            : (
              <p
                className="flex-1 text-sm leading-relaxed text-muted"
                title={item.title}
              >
                {plainMarketLanguage(item.title)}
              </p>
            )}
        </div>
        <div className="mt-1.5 flex flex-wrap items-center gap-1.5">
          {timeLabel
            ? (
              <span className="font-mono text-[9px] text-faint">
                {timeLabel}
              </span>
            )
            : null}
          <span className="font-mono text-[9px] text-faint">{item.source}</span>
          {item.kind === "company" && (item.name || item.symbol)
            ? (
              <button
                type="button"
                onClick={() => item.symbol && onSymbol(item.symbol)}
                disabled={!item.symbol}
                className="rounded-sm bg-hairline px-1.5 py-0.5 font-mono text-[9px] text-muted transition-colors hover:bg-line disabled:pointer-events-none"
              >
                {item.name || item.symbol}
              </button>
            )
            : null}
          {item.kind === "geo" && item.country
            ? (
              <span className="rounded-sm bg-hairline px-1.5 py-0.5 font-mono text-[9px] text-muted">
                {item.country}
              </span>
            )
            : null}
        </div>
      </div>
    </div>
  );
}

function QueryUnavailable(
  { label, error, className }: {
    label: string;
    error: unknown;
    className?: string;
  },
) {
  return (
    <div className={cn("text-sm text-warn", className)}>
      <p>{label}</p>
      <details className="mt-2 text-xs text-dim">
        <summary className="cursor-pointer">Technical details</summary>
        <p className="mt-1 break-words font-mono text-[10px] text-faint">
          {error instanceof Error ? error.message : String(error)}
        </p>
      </details>
    </div>
  );
}

// ── Indicator row ─────────────────────────────────────────────────────────────

function IndicatorRow({ indicator }: { indicator: MacroIndicator }) {
  const { label, value, unit, delta, history } = indicator;
  const displayUnit = indicatorUnitLabel(unit);
  const formatted = unit === "%"
    ? `${value.toFixed(2)}%`
    : value.toLocaleString("en-US", { maximumFractionDigits: 2 });
  const deltaFormatted = delta == null
    ? null
    : unit === "%"
    ? `${delta > 0 ? "+" : ""}${delta.toFixed(2)} pts`
    : `${delta > 0 ? "+" : ""}${
      delta.toLocaleString("en-US", { maximumFractionDigits: 2 })
    }`;
  return (
    <div className="flex items-end justify-between gap-4">
      <div className="min-w-0 flex-1">
        <p className="truncate font-mono text-[9px] uppercase tracking-[0.14em] text-faint">
          {indicatorLabel(label)}
        </p>
        <div className="mt-1 flex items-baseline gap-1.5">
          <span className="text-sm font-semibold text-muted tabular">
            {formatted}
          </span>
          {displayUnit && unit !== "%"
            ? (
              <span className="font-mono text-[9px] text-faint">
                {displayUnit}
              </span>
            )
            : null}
          {deltaFormatted
            ? (
              <span
                className={cn(
                  "font-mono text-[10px] tabular",
                  signedClass(delta),
                )}
              >
                {deltaFormatted}
              </span>
            )
            : null}
        </div>
        {indicator.period
          ? (
            <p className="mt-0.5 font-mono text-[9px] text-faint">
              as of {indicator.period}
            </p>
          )
          : null}
      </div>
      {history.length >= 2 ? <Sparkline history={history} /> : null}
    </div>
  );
}

function sourceLabel(value: string): string {
  return value
    .replace(/\bbrent crude usd\b/gi, "Brent oil price")
    .replace(/\bgold usd\b/gi, "Gold price")
    .replace(/\bfed funds effective\b/gi, "US central bank rate")
    .replace(/\bFOMC\b/g, "Federal Reserve meeting");
}

function indicatorLabel(value: string): string {
  const key = value.trim().toLowerCase();
  if (key === "brent crude") return "Oil price · Brent";
  if (key === "us cpi") return "US consumer prices";
  if (key === "ecb deposit rate") return "European Central Bank rate";
  if (key === "fed funds rate") return "US central bank rate";
  if (key === "gold") return "Gold price";
  if (key === "euro area hicp") return "Euro area consumer prices";
  if (key === "us unemployment rate") return "US unemployment";
  return value;
}

function indicatorUnitLabel(value?: string | null): string {
  const key = String(value ?? "").toLowerCase();
  if (key === "usd/bbl") return "USD per barrel";
  if (key === "usd/oz") return "USD per ounce";
  return value ?? "";
}

// ── Calendar row ──────────────────────────────────────────────────────────────

function CalendarRow({ event }: { event: MacroCalendarEvent }) {
  const countdown = formatCountdown(event.in_h);
  const past = event.in_h < 0;
  return (
    <div className="flex items-start justify-between gap-4">
      <p className="flex-1 text-sm leading-relaxed text-muted">{event.event}</p>
      <span
        className={cn(
          "shrink-0 font-mono text-[10px] tabular",
          past ? "text-faint" : event.in_h < 4 ? "text-warn" : "text-dim",
        )}
      >
        {countdown}
      </span>
    </div>
  );
}

// ── Risk pulse ────────────────────────────────────────────────────────────────

function RiskPulse({ stories }: { stories: BriefingStory[] }) {
  const risk =
    stories.filter((s) => s.severity === "risk" || s.severity === "high")
      .length;
  const watch =
    stories.filter((s) => s.severity === "watch" || s.severity === "medium")
      .length;
  const info = stories.length - risk - watch;
  const total = stories.length;
  if (total === 0) return null;

  const legend: string[] = [];
  if (risk > 0) legend.push(`${risk} urgent`);
  if (watch > 0) legend.push(`${watch} to watch`);
  if (info > 0) legend.push(`${info} context`);

  return (
    <div className="space-y-1.5">
      <div className="flex h-1.5 overflow-hidden rounded-full bg-hairline">
        {risk > 0
          ? (
            <div
              className="bg-loss transition-all"
              style={{ width: `${(risk / total) * 100}%` }}
            />
          )
          : null}
        {watch > 0
          ? (
            <div
              className="bg-warn transition-all"
              style={{ width: `${(watch / total) * 100}%` }}
            />
          )
          : null}
        {info > 0
          ? (
            <div
              className="bg-faint transition-all"
              style={{ width: `${(info / total) * 100}%` }}
            />
          )
          : null}
      </div>
      <p className="font-mono text-[9px] text-faint">{legend.join(" · ")}</p>
    </div>
  );
}

// ── Combined calendar + earnings ──────────────────────────────────────────────

type CombinedEvent =
  | { kind: "macro"; event: MacroCalendarEvent; in_h: number }
  | { kind: "earnings"; earning: UpcomingEarning; in_h: number };

function ComingUp({
  calendar,
  earnings,
  onSymbol,
}: {
  calendar: MacroCalendarEvent[];
  earnings: UpcomingEarning[];
  onSymbol: (s: string) => void;
}) {
  const now = new Date();

  const macroItems: CombinedEvent[] = calendar.map((ev) => ({
    kind: "macro",
    event: ev,
    in_h: ev.in_h,
  }));

  const earningItems: CombinedEvent[] = earnings.map((e) => {
    // Pivot at noon UTC so sort order is consistent with UTC-based macro events.
    // Parsing as T00:00:00 local time introduces a ~12h skew vs UTC midnight events.
    const parts = e.earnings_date.split("-").map(Number);
    const pivotMs = Date.UTC(parts[0], parts[1] - 1, parts[2], 12, 0, 0);
    const in_h = (pivotMs - now.getTime()) / 3_600_000;
    return { kind: "earnings", earning: e, in_h };
  });

  const combined = [...macroItems, ...earningItems]
    .sort((a, b) => a.in_h - b.in_h)
    .slice(0, 8);

  if (combined.length === 0) {
    return (
      <p className="text-sm text-faint">
        Upcoming economic events and company earnings will appear here.
      </p>
    );
  }

  return (
    <div className="space-y-3">
      {combined.map((item, idx) =>
        item.kind === "macro"
          ? (
            <CalendarRow
              key={`macro-${item.event.event}-${idx}`}
              event={item.event}
            />
          )
          : (
            <EarningsRow
              key={`earnings-${item.earning.symbol}-${idx}`}
              earning={item.earning}
              onSymbol={onSymbol}
            />
          )
      )}
    </div>
  );
}

function EarningsRow({
  earning,
  onSymbol,
}: {
  earning: UpcomingEarning;
  onSymbol: (s: string) => void;
}) {
  const displayName = earning.name || earning.symbol;
  const dateLabel = (() => {
    try {
      const parts = earning.earnings_date.split("-").map(Number);
      const now = new Date();
      const isToday = parts[0] === now.getUTCFullYear() &&
        parts[1] - 1 === now.getUTCMonth() &&
        parts[2] === now.getUTCDate();
      if (isToday) return "today";
      return format(new Date(earning.earnings_date + "T00:00:00"), "MMM d");
    } catch {
      return earning.earnings_date;
    }
  })();

  return (
    <div className="flex items-start justify-between gap-4">
      <div className="min-w-0 flex-1">
        <button
          type="button"
          onClick={() => onSymbol(earning.symbol)}
          className="block max-w-full truncate text-left text-sm leading-relaxed text-muted transition-colors hover:text-fg"
        >
          {displayName}
        </button>
        <p className="font-mono text-[9px] text-faint">
          results
          {earning.stale
            ? <span className="ml-1.5 text-faint">· date to confirm</span>
            : null}
        </p>
      </div>
      <span className="shrink-0 font-mono text-[10px] text-dim">
        {dateLabel}
      </span>
    </div>
  );
}

// ── Stance history ────────────────────────────────────────────────────────────

// Ordinal scales must stay in sync with posture-panel.tsx.
// gross_mode domain: risk_off | cautious | normal  (verified in live data)
// net_bias  domain: short     | neutral  | long
const STANCE_GROSS = ["risk_off", "cautious", "normal"];
const STANCE_NET = ["short", "neutral", "long"];

function StanceHistory({ points }: { points: PostureHistoryPoint[] }) {
  if (points.length < 2) return null;
  const data = points
    .map((point) => ({
      timestamp: Date.parse(point.as_of),
      gross: point.gross_mode ? STANCE_GROSS.indexOf(point.gross_mode) : -1,
      net: point.net_bias ? STANCE_NET.indexOf(point.net_bias) : -1,
    }))
    .filter((point) => Number.isFinite(point.timestamp))
    .map((point) => ({
      ...point,
      gross: point.gross >= 0 ? point.gross : undefined,
      net: point.net >= 0 ? point.net : undefined,
    }));
  const hasGross = data.filter((point) => point.gross != null).length > 1;
  const hasNet = data.filter((point) => point.net != null).length > 1;
  if (!hasGross && !hasNet) return null;

  return (
    <div className="space-y-3">
      {hasGross
        ? (
          <div>
            <p className="font-mono text-[9px] uppercase tracking-[0.14em] text-faint">
              Risk appetite
            </p>
            <StanceLine
              data={data}
              dataKey="gross"
              color="var(--color-accent)"
              label="Recorded risk-appetite history"
            />
          </div>
        )
        : null}
      {hasNet
        ? (
          <div>
            <p className="font-mono text-[9px] uppercase tracking-[0.14em] text-faint">
              Portfolio direction
            </p>
            <StanceLine
              data={data}
              dataKey="net"
              color="var(--color-dim)"
              label="Recorded portfolio-direction history"
            />
          </div>
        )
        : null}
    </div>
  );
}

function StanceLine({
  data,
  dataKey,
  color,
  label,
}: {
  data: Array<{ timestamp: number; gross?: number; net?: number }>;
  dataKey: "gross" | "net";
  color: string;
  label: string;
}) {
  return (
    <div className="mt-1 h-7 w-[72px]" role="img" aria-label={label}>
      <ResponsiveContainer width="100%" height="100%">
        <LineChart data={data}>
          <YAxis hide domain={[0, 2]} reversed />
          <Line
            type="stepAfter"
            dataKey={dataKey}
            stroke={color}
            strokeWidth={1.5}
            dot={false}
            connectNulls={false}
            isAnimationActive={false}
          />
        </LineChart>
      </ResponsiveContainer>
    </div>
  );
}
