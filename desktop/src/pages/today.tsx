import { format } from "date-fns";
import { ArrowRight, ExternalLink } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardBody, CardHeader, CardTitle } from "@/components/ui/card";
import type { PageKey } from "@/components/layout/app-shell";
import { useDailyBriefing, useNewsFeed } from "@/hooks/use-intelligence";
import { formatAgo, formatCountdown, formatPct, formatUsd, signedClass } from "@/lib/format";
import type {
  BriefingStory,
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
  onPage: (page: PageKey) => void;
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

// ── Inline sparkline ─────────────────────────────────────────────────────────

function Sparkline({ history }: { history: Array<{ period: string; value: number }> }) {
  if (history.length < 2) return null;
  const values = history.map((p) => p.value);
  const min = Math.min(...values);
  const max = Math.max(...values);
  const range = max - min || 1;
  const W = 72;
  const H = 24;
  const step = W / (values.length - 1);
  const pts = values.map((v, i) => `${i * step},${H - ((v - min) / range) * H}`).join(" ");
  const last = values[values.length - 1];
  const prev = values[values.length - 2];
  const rising = last >= prev;
  return (
    <svg
      width={W}
      height={H}
      viewBox={`0 0 ${W} ${H}`}
      className={cn("shrink-0 overflow-visible", rising ? "text-gain" : "text-loss")}
      aria-hidden
    >
      <polyline
        points={pts}
        fill="none"
        stroke="currentColor"
        strokeWidth="1.5"
        strokeLinejoin="round"
        strokeLinecap="round"
      />
    </svg>
  );
}

// ── Story severity dot ────────────────────────────────────────────────────────

function SeverityDot({ severity }: { severity: string | null }) {
  // Daemon vocabulary: risk > watch > info (high/medium kept as synonyms).
  const cls =
    severity === "risk" || severity === "high"
      ? "bg-loss"
      : severity === "watch" || severity === "medium"
        ? "bg-warn"
        : "bg-faint";
  return <span className={cn("mt-1.5 size-1.5 shrink-0 rounded-full", cls)} />;
}

// ── Main page ─────────────────────────────────────────────────────────────────

export function TodayPage({ snapshot, onSymbol, onPage }: Props) {
  const briefingQuery = useDailyBriefing();
  const newsFeedQuery = useNewsFeed({ limit: 40 });

  const briefing = briefingQuery.data;
  const headline = briefing?.headline;
  const stories = briefing?.top_stories ?? [];
  const indicators = briefing?.indicators ?? [];
  const calendar = briefing?.calendar ?? [];
  const earnings = briefing?.earnings ?? [];
  const postureHistory = briefing?.posture_history ?? [];
  const newsFeedItems = newsFeedQuery.data?.items ?? briefing?.news ?? [];

  const todayLabel = format(new Date(), "EEEE, MMMM d");

  return (
    <div className="grid gap-6">
      {/* ── Manchette ───────────────────────────────────────────────────── */}
      <section className="relative overflow-hidden rounded-lg border border-line bg-panel/90 p-5 shadow-[0_1px_2px_rgba(23,43,54,0.04)]">
        <div className="pointer-events-none absolute -right-8 -top-16 size-52 rounded-full border border-accent/10" />
        <div className="pointer-events-none absolute -right-0 -top-8 size-32 rounded-full border border-accent/15" />
        <div className="relative">
          <p className="font-mono text-[9px] uppercase tracking-[0.22em] text-accent">{todayLabel}</p>
          {headline?.lead ? (
            <>
              <p className="mt-3 font-mono text-[9px] uppercase tracking-[0.22em] text-faint">
                Agent desk note
              </p>
              <p className="mt-1.5 max-w-4xl text-[17px] font-semibold leading-snug tracking-tight text-fg">
                {headline.lead}
              </p>
            </>
          ) : (
            <p className="mt-3 text-sm text-faint">
              No lead yet — the intelligence agent hasn't filed today's briefing.
            </p>
          )}
          <div className="mt-4 flex flex-wrap gap-2">
            {knownSignal(headline?.regime) ? (
              <Badge tone="warn">
                {describeSignal(headline?.regime)}
              </Badge>
            ) : null}
            {knownSignal(headline?.rates_bias) ? (
              <Badge tone={headline?.rates_bias === "hawkish" ? "loss" : "gain"}>
                {describeSignal(headline?.rates_bias)}
              </Badge>
            ) : null}
            {knownSignal(headline?.usd_bias) ? (
              <Badge>{describeSignal(headline?.usd_bias)}</Badge>
            ) : null}
          </div>
          {headline?.digest_as_of ? (
            <p className="mt-3 font-mono text-[9px] uppercase tracking-[0.14em] text-faint">
              Signals as of {formatAgo(headline.digest_as_of)}
            </p>
          ) : null}
        </div>
      </section>

      {/* ── Body: main + sidebar ────────────────────────────────────────── */}
      <div className="grid gap-4 xl:grid-cols-[minmax(0,1.4fr)_minmax(280px,0.6fr)]">

        {/* Main column */}
        <div className="space-y-4">
          {/* Top stories */}
          <Card>
            <CardHeader>
              <CardTitle>Top stories</CardTitle>
              {briefingQuery.error ? (
                <span className="font-mono text-[9px] text-loss">unavailable</span>
              ) : null}
            </CardHeader>
            <CardBody className="p-0">
              {stories.length > 0 ? (
                <div className="px-4 pt-3 pb-1">
                  <RiskPulse stories={stories} />
                </div>
              ) : null}
              {stories.length === 0 ? (
                <p className="px-4 py-6 text-sm text-faint">
                  Top stories come from the agent's daily global digest. Run the
                  intelligence cycle to populate them.
                </p>
              ) : (
                stories.map((story, idx) => (
                  <StoryRow key={`${story.point}-${idx}`} story={story} onSymbol={onSymbol} />
                ))
              )}
            </CardBody>
          </Card>

          {/* News wire */}
          <Card>
            <CardHeader>
              <CardTitle>News wire</CardTitle>
              {newsFeedQuery.isPending ? (
                <span className="font-mono text-[9px] text-faint">loading…</span>
              ) : null}
            </CardHeader>
            <CardBody className="p-0">
              {newsFeedItems.length === 0 ? (
                <p className="px-4 py-6 text-sm text-faint">
                  The news wire aggregates articles gathered by the agent per symbol and region.
                  It fills up as the agent analyses companies in your universe.
                </p>
              ) : (
                newsFeedItems.map((item) => (
                  <NewsRow key={item.id} item={item} onSymbol={onSymbol} />
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
              {indicators.length === 0 ? (
                <p className="text-sm text-faint">
                  Macro indicators appear here once the agent has collected economic series data.
                </p>
              ) : (
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
              <ComingUp calendar={calendar} earnings={earnings} onSymbol={onSymbol} />
            </CardBody>
          </Card>

          {/* Stance history */}
          {postureHistory.length >= 2 ? (
            <Card>
              <CardHeader>
                <CardTitle>Stance history</CardTitle>
              </CardHeader>
              <CardBody>
                <StanceHistory points={postureHistory} />
              </CardBody>
            </Card>
          ) : null}

          {/* Your money */}
          <Card>
            <CardHeader>
              <CardTitle>Your money</CardTitle>
            </CardHeader>
            <CardBody>
              <PortfolioSnapshot snapshot={snapshot} onPage={onPage} />
            </CardBody>
          </Card>
        </aside>
      </div>
    </div>
  );
}

// ── Story row ────────────────────────────────────────────────────────────────

function StoryRow({ story, onSymbol }: { story: BriefingStory; onSymbol: (s: string) => void }) {
  const bullish = story.direction === "bullish" || story.direction === "up";
  const bearish = story.direction === "bearish" || story.direction === "down";
  return (
    <div className="flex gap-3 border-b border-hairline px-4 py-3 last:border-0">
      <SeverityDot severity={story.severity} />
      <div className="min-w-0 flex-1">
        <div className="flex flex-wrap items-center gap-1.5">
          {bullish ? <span className="text-xs font-semibold text-gain">▲</span> : null}
          {bearish ? <span className="text-xs font-semibold text-loss">▼</span> : null}
          <p className="text-sm leading-relaxed text-muted">{story.point}</p>
        </div>
        <div className="mt-1.5 flex flex-wrap items-center gap-1.5">
          {story.venue ? (
            <Badge>{story.venue}</Badge>
          ) : null}
          {story.symbols.map((sym) => (
            <button
              key={sym}
              type="button"
              onClick={() => onSymbol(sym)}
              className="rounded-sm bg-accent/10 px-1.5 py-0.5 font-mono text-[10px] uppercase tracking-[0.12em] text-accent transition-colors hover:bg-accent/20"
            >
              {sym}
            </button>
          ))}
        </div>
        {story.sources.length > 0 ? (
          <p className="mt-1 font-mono text-[9px] text-faint">
            {story.sources.slice(0, 3).join(" · ")}
            {story.sources.length > 3 ? ` +${story.sources.length - 3}` : ""}
          </p>
        ) : null}
      </div>
    </div>
  );
}

// ── News row ─────────────────────────────────────────────────────────────────

function NewsRow({ item, onSymbol }: { item: NewsFeedItem; onSymbol: (s: string) => void }) {
  const timeLabel = item.published_at ? formatAgo(item.published_at) : null;
  return (
    <div className="flex gap-3 border-b border-hairline px-4 py-3 last:border-0">
      <div className="min-w-0 flex-1">
        <div className="flex flex-wrap items-start justify-between gap-2">
          {item.url ? (
            <a
              href={item.url}
              target="_blank"
              rel="noreferrer"
              className="group flex min-w-0 items-start gap-1 text-sm leading-relaxed text-muted hover:text-fg"
            >
              <span className="flex-1">{item.title}</span>
              <ExternalLink className="mt-0.5 size-3 shrink-0 text-faint group-hover:text-dim" />
            </a>
          ) : (
            <p className="flex-1 text-sm leading-relaxed text-muted">{item.title}</p>
          )}
        </div>
        <div className="mt-1.5 flex flex-wrap items-center gap-1.5">
          {timeLabel ? (
            <span className="font-mono text-[9px] text-faint">{timeLabel}</span>
          ) : null}
          <span className="font-mono text-[9px] text-faint">{item.source}</span>
          {item.kind === "company" && (item.name || item.symbol) ? (
            <button
              type="button"
              onClick={() => item.symbol && onSymbol(item.symbol)}
              disabled={!item.symbol}
              className="rounded-sm bg-hairline px-1.5 py-0.5 font-mono text-[9px] text-muted transition-colors hover:bg-line disabled:pointer-events-none"
            >
              {item.name || item.symbol}
            </button>
          ) : null}
          {item.kind === "geo" && item.country ? (
            <span className="rounded-sm bg-hairline px-1.5 py-0.5 font-mono text-[9px] text-muted">
              {item.country}
            </span>
          ) : null}
        </div>
      </div>
    </div>
  );
}

// ── Indicator row ─────────────────────────────────────────────────────────────

function IndicatorRow({ indicator }: { indicator: MacroIndicator }) {
  const { label, value, unit, delta, history } = indicator;
  const formatted =
    unit === "%"
      ? `${value.toFixed(2)}%`
      : value.toLocaleString("en-US", { maximumFractionDigits: 2 });
  const deltaFormatted =
    delta == null
      ? null
      : unit === "%"
        ? `${delta > 0 ? "+" : ""}${delta.toFixed(2)}pp`
        : `${delta > 0 ? "+" : ""}${delta.toLocaleString("en-US", { maximumFractionDigits: 2 })}`;
  return (
    <div className="flex items-end justify-between gap-4">
      <div className="min-w-0 flex-1">
        <p className="truncate font-mono text-[9px] uppercase tracking-[0.14em] text-faint">{label}</p>
        <div className="mt-1 flex items-baseline gap-1.5">
          <span className="text-sm font-semibold text-muted tabular">{formatted}</span>
          {unit && unit !== "%" ? (
            <span className="font-mono text-[9px] text-faint">{unit}</span>
          ) : null}
          {deltaFormatted ? (
            <span className={cn("font-mono text-[10px] tabular", signedClass(delta))}>
              {deltaFormatted}
            </span>
          ) : null}
        </div>
        {indicator.period ? (
          <p className="mt-0.5 font-mono text-[9px] text-faint">as of {indicator.period}</p>
        ) : null}
      </div>
      {history.length >= 2 ? <Sparkline history={history} /> : null}
    </div>
  );
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
  const risk = stories.filter((s) => s.severity === "risk" || s.severity === "high").length;
  const watch = stories.filter((s) => s.severity === "watch" || s.severity === "medium").length;
  const info = stories.length - risk - watch;
  const total = stories.length;
  if (total === 0) return null;

  const legend: string[] = [];
  if (risk > 0) legend.push(`${risk} risk`);
  if (watch > 0) legend.push(`${watch} watch`);
  if (info > 0) legend.push(`${info} info`);

  return (
    <div className="space-y-1.5">
      <div className="flex h-1.5 overflow-hidden rounded-full bg-hairline">
        {risk > 0 ? (
          <div className="bg-loss transition-all" style={{ width: `${(risk / total) * 100}%` }} />
        ) : null}
        {watch > 0 ? (
          <div className="bg-warn transition-all" style={{ width: `${(watch / total) * 100}%` }} />
        ) : null}
        {info > 0 ? (
          <div className="bg-faint transition-all" style={{ width: `${(info / total) * 100}%` }} />
        ) : null}
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
        item.kind === "macro" ? (
          <CalendarRow key={`macro-${item.event.event}-${idx}`} event={item.event} />
        ) : (
          <EarningsRow
            key={`earnings-${item.earning.symbol}-${idx}`}
            earning={item.earning}
            onSymbol={onSymbol}
          />
        ),
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
      const isToday =
        parts[0] === now.getUTCFullYear() &&
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
          {earning.stale ? (
            <span className="ml-1.5 text-faint">· date to confirm</span>
          ) : null}
        </p>
      </div>
      <span className="shrink-0 font-mono text-[10px] text-dim">{dateLabel}</span>
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

  const W = 72;
  const H = 28;

  const tss = points
    .map((p) => new Date(p.as_of).getTime())
    .filter((t) => !Number.isNaN(t));
  if (tss.length < 2) return null;

  const tMin = Math.min(...tss);
  const tMax = Math.max(...tss);
  const tRange = tMax - tMin || 1;

  function toX(asOf: string): number {
    const t = new Date(asOf).getTime();
    return Number.isNaN(t) ? -1 : ((t - tMin) / tRange) * W;
  }

  function stepLine(scale: string[], key: "gross_mode" | "net_bias"): string {
    const pts: Array<{ x: number; y: number }> = [];
    for (const p of points) {
      const val = p[key];
      const idx = val ? scale.indexOf(val) : -1;
      if (idx < 0) continue;
      const x = toX(p.as_of);
      if (x < 0) continue;
      const y = H - (idx / (scale.length - 1)) * H;
      pts.push({ x, y });
    }
    if (pts.length < 2) return "";
    const result: string[] = [];
    for (let i = 0; i < pts.length; i++) {
      if (i === 0) {
        result.push(`${pts[i].x.toFixed(1)},${pts[i].y.toFixed(1)}`);
      } else {
        result.push(`${pts[i].x.toFixed(1)},${pts[i - 1].y.toFixed(1)}`);
        result.push(`${pts[i].x.toFixed(1)},${pts[i].y.toFixed(1)}`);
      }
    }
    result.push(`${W},${pts[pts.length - 1].y.toFixed(1)}`);
    return result.join(" ");
  }

  const grossPts = stepLine(STANCE_GROSS, "gross_mode");
  const netPts = stepLine(STANCE_NET, "net_bias");

  if (!grossPts && !netPts) return null;

  return (
    <div className="space-y-3">
      {grossPts ? (
        <div>
          <p className="font-mono text-[9px] uppercase tracking-[0.14em] text-faint">Gross mode</p>
          <svg
            width={W}
            height={H}
            viewBox={`0 0 ${W} ${H}`}
            aria-hidden
            className="mt-1 overflow-visible text-accent"
          >
            <polyline
              points={grossPts}
              fill="none"
              stroke="currentColor"
              strokeWidth="1.5"
              strokeLinejoin="round"
              strokeLinecap="round"
            />
          </svg>
        </div>
      ) : null}
      {netPts ? (
        <div>
          <p className="font-mono text-[9px] uppercase tracking-[0.14em] text-faint">Net bias</p>
          <svg
            width={W}
            height={H}
            viewBox={`0 0 ${W} ${H}`}
            aria-hidden
            className="mt-1 overflow-visible text-dim"
          >
            <polyline
              points={netPts}
              fill="none"
              stroke="currentColor"
              strokeWidth="1.5"
              strokeLinejoin="round"
              strokeLinecap="round"
            />
          </svg>
        </div>
      ) : null}
    </div>
  );
}

// ── Portfolio snapshot ────────────────────────────────────────────────────────

function PortfolioSnapshot({
  snapshot,
  onPage,
}: {
  snapshot?: Snapshot | null;
  onPage: (page: PageKey) => void;
}) {
  if (!snapshot) {
    return (
      <div className="space-y-2">
        <p className="text-sm text-faint">Portfolio data is loading…</p>
      </div>
    );
  }

  const { portfolio } = snapshot;
  const equity = portfolio.equity;
  const returnPct = portfolio.total_return_pct;
  const holdings = (portfolio.holdings ?? []).filter((h) => Math.abs(h.quantity) > 1e-9);

  return (
    <div className="space-y-4">
      <div>
        <p className="font-mono text-[9px] uppercase tracking-[0.14em] text-faint">Portfolio value</p>
        <div className="mt-1 flex items-baseline gap-2">
          <span className="text-xl font-semibold tracking-tight">{formatUsd(equity, 0)}</span>
          {returnPct != null ? (
            <span className={cn("font-mono text-[11px] tabular", signedClass(returnPct))}>
              {formatPct(returnPct)}
            </span>
          ) : null}
        </div>
      </div>
      {holdings.length > 0 ? (
        <div>
          <p className="mb-1.5 font-mono text-[9px] uppercase tracking-[0.14em] text-faint">
            {holdings.length} open position{holdings.length !== 1 ? "s" : ""}
          </p>
          <div className="space-y-1">
            {holdings.slice(0, 4).map((h) => {
              const pnl = h.unrealized_pnl_net ?? h.unrealized_pnl;
              return (
                <div key={h.symbol} className="flex items-center justify-between gap-3">
                  <span className="min-w-0 flex-1 truncate text-sm text-muted">
                    {snapshot.company_map?.[h.symbol] || h.symbol}
                  </span>
                  <span className={cn("shrink-0 font-mono text-[11px] tabular", signedClass(pnl))}>
                    {formatUsd(pnl, 0)}
                  </span>
                </div>
              );
            })}
            {holdings.length > 4 ? (
              <p className="font-mono text-[9px] text-faint">+{holdings.length - 4} more</p>
            ) : null}
          </div>
        </div>
      ) : (
        <p className="text-sm text-faint">No open positions right now.</p>
      )}
      <Button
        variant="ghost"
        size="sm"
        onClick={() => onPage("portfolio")}
        className="w-full justify-between"
      >
        See portfolio
        <ArrowRight className="size-3.5" />
      </Button>
    </div>
  );
}
