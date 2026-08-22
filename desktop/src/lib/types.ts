export type DecisionRow = {
  action?: string;
  confidence?: number | null;
  cycle_ts?: string;
  decision_id?: string;
  decision_reason_code?: string;
  decision_source?: string;
  executed?: boolean;
  intent?: string;
  llm_model?: string;
  llm_provider?: string;
  model_called?: boolean;
  next_wake_in_minutes?: number;
  opportunity_side?: string;
  price?: number;
  qty?: number;
  rationale?: string;
  reason?: string;
  sequence?: number;
  symbol?: string;
  ts?: string;
  runtime?: {
    armed_plan_id?: string;
    blocked?: boolean;
    data_source?: string;
    indicator_watch_created?: boolean;
    reject_reason?: string;
    trade_plan_created?: boolean;
  };
  indicator_watch?: {
    expires_at?: string;
    purpose?: string;
    symbol?: string;
  };
};

export type Holding = {
  symbol: string;
  quantity: number;
  avg_price?: number;
  last_price?: number;
  unrealized_pnl?: number;
  unrealized_pnl_net?: number;
  fx_rate?: number;
};

export type Portfolio = {
  cash?: number;
  cash_ledger?: number;
  cash_available?: number;
  equity?: number;
  total_return_pct?: number;
  long_exposure_usd?: number;
  short_exposure_usd?: number;
  gross_exposure_usd?: number;
  net_exposure_usd?: number;
  holdings?: Holding[];
};

export type DaemonStatus = {
  pid?: number | null;
  alive?: boolean;
  phase?: string | null;
  dry_run?: boolean;
  current_symbol?: string | null;
  decisions_done?: number;
  symbols_due?: number;
  symbols_total?: number;
  model_calls_used?: number;
  max_model_calls_per_cycle?: number;
  ts?: string;
};

export type EquityPoint = {
  ts: string;
  equity: number;
  cash?: number | null;
};

export type Watch = {
  symbol?: string;
  purpose?: string;
  expires_at?: string;
  logic?: string;
  conditions?: unknown;
  watch_id?: string;
};

export type Snapshot = {
  generated_at: string;
  repo_root: string;
  source?: string;
  dry_run?: boolean;
  starting_cash?: number | null;
  ts?: string;
  daemon: DaemonStatus;
  portfolio: Portfolio;
  kpis: Record<string, unknown>;
  equity_series: EquityPoint[];
  decisions: DecisionRow[];
  recent_decisions: DecisionRow[];
  trade_plans: Record<string, unknown>[];
  indicator_watches: Watch[];
  armed_plans: Watch[];
  default_next_wake?: string | null;
  symbol_wakes: Record<string, string>;
  stale_market_data: Record<string, unknown>;
  queue_worker_activity: {
    active_workers?: number;
    running_tasks?: number;
    pending_tasks?: number;
  };
  open_venues_list: string[];
  company_map: Record<string, string>;
  universe_symbols: string[];
  attribution: { recent_trips: Record<string, unknown>[] };
  kill_active?: boolean;
};

export type DecisionKind =
  | "exec"
  | "risk"
  | "stale"
  | "armed"
  | "plan"
  | "watch"
  | "quiet"
  | "hold"
  | "signal";

export type LedgerRow = {
  row_key: string;
  is_batch: boolean;
  utc_text: string;
  symbol: string;
  action: string;
  action_text: string;
  confidence_text: string;
  source_text: string;
  effect_text: string;
  effect_kind: string;
  has_detail: boolean;
  cycle_ts?: string;
  rationale?: string;
  reason?: string;
  executed?: boolean;
  decision_source?: string;
  model_called?: boolean;
  summary_kind?: "automatic_cycle";
  llm_model?: string;
  confidence?: number | null;
  qty?: number;
  price?: number;
  decision_reason_code?: string;
  runtime?: Record<string, unknown>;
  execution_status?: "confirmed" | "recorded";
};

export type DecisionsPayload = {
  filter: string;
  counts: Record<string, number>;
  rows: LedgerRow[];
};

export type FireItem = {
  label: string;
  detail: string;
  at?: string | null;
  countdown: string;
  kind: string;
};

export type ArmedRow = {
  symbol: string;
  action: string;
  quantity?: number | null;
  conditions?: string[];
  logic?: string;
  extra_condition_count?: number;
  stop_price?: number | null;
  stop_distance_pct?: number | null;
  countdown: string;
};

export type ExitPlanRow = {
  symbol: string;
  side: string;
  quantity?: number | null;
  entry_price?: number | null;
  stop_price?: number | null;
  stop_left_pct?: number | null;
  take_profit_label?: string;
  protect_label?: string;
  review_label?: string;
  review_kind?: string;
  stop_is_near?: boolean;
};

export type WatchRow = {
  symbol: string;
  condition_label: string;
  countdown: string;
  ttl_fraction?: number;
};

export type PlansPayload = {
  armed: { rows: ArmedRow[] };
  exit_plans: { rows: ExitPlanRow[]; rejected_symbols?: string[] };
  watches: { rows: WatchRow[] };
  exit_watches: { rows: WatchRow[] };
  next_to_fire: FireItem[];
  default_next_wake?: string | null;
};

export type UniverseRow = {
  symbol: string;
  venue: string;
  name: string;
  state_label: string;
  pos: string;
  last_decision: string;
  last_decision_action: string;
  wake_text: string;
  wake_urgent: boolean;
  data_text: string;
  data_stale: boolean;
};

export type UniversePayload = {
  rows: UniverseRow[];
  hotset: Record<string, string[]>;
  overrides: { pinned: string[]; banned: string[] };
  universe_symbols: string[];
};

export type PositionRow = {
  symbol: string;
  side: string;
  qty: number;
  avg?: number | null;
  last?: number | null;
  notional: number;
  pnl: number;
  pnl_pct: number;
  stop_dist?: number | null;
  stop_left_pct?: number | null;
  is_stale: boolean;
  data_age_m?: number | null;
};

export type PortfolioStory = {
  symbol: string;
  name: string | null;
  side: "L" | "S";
  qty: number;
  pnl: number | null;
  pnl_pct: number | null;
  venue: string | null;
  why: string | null;
  why_at: string | null;
  thesis: string | null;
  thesis_status: string | null;
  business: string | null;
};

export type PortfolioPayload = {
  sort_mode: number;
  positions: {
    rows: PositionRow[];
    gross_long: number;
    gross_short: number;
    gross: number;
    net_long: number;
    unrealized_total: number;
  };
  equity: {
    equity: number;
    cash: number;
    cash_available?: number;
    return_pct: number;
    pnl_usd: number;
    unrealized: number;
  };
  exposure: {
    long_usd: number;
    short_usd: number;
    gross: number;
    net: number;
    by_venue: Record<string, number>;
  };
  fx: { source_available: boolean; rows: { currency: string; rate: number }[] };
  closed_trips: Record<string, unknown>[];
  stories?: PortfolioStory[];
};

export type HealthPayload = {
  freshness: {
    venues: Array<{
      venue: string;
      display_name: string;
      symbol_count: number;
      is_stale: boolean;
      max_age_minutes?: number | null;
      stale_symbols: Array<{ symbol: string; age_minutes?: number | null }>;
    }>;
    stale_symbols: Array<{ symbol: string; age_minutes?: number | null }>;
  };
  fx: { source_available: boolean; rows: { currency: string; rate: number }[] };
  sources: { rows: { name: string; detail: string }[] };
  llm: {
    calls_label: string;
    fallbacks_label: string;
    calls_this_cycle?: number | null;
    total_fills: number;
    total_fallbacks: number;
  };
  learnings: {
    pending_label: string;
    consolidation_label: string;
    last_run_label: string;
    notes: { symbol: string; note: string }[];
  };
  memory: {
    available: boolean;
    missing_label?: string;
    notes_label?: string;
    lift_label?: string;
    useful_label?: string;
    rules_label?: string;
    n_helps?: number;
    n_hurts?: number;
    sync_label?: string;
    sync_is_error?: boolean;
    situation_label?: string;
  };
  universe: {
    total_symbols: number;
    symbols_label: string;
    hot_total: number;
    hotset_label: string;
    venue_counts: [string, number][];
  };
  risk: {
    reject_count: number;
    recent_rejects: Array<{ symbol?: string; action?: string; reason?: string; cycle_ts?: string }>;
    caps: Record<string, unknown>;
  };
  model: {
    model_performance: Record<string, unknown>[];
    providers?: Record<string, number>;
  };
  queue: Record<string, unknown>;
  kill_active: boolean;
};

export type ReportItem = {
  kind: string;
  key: string;
  label: string;
  as_of?: string | null;
  depth?: string | null;
};

export type ReportsPayload = { items: ReportItem[] };

export type ReportDetail = ReportItem & {
  payload?: Record<string, unknown>;
  error?: string;
};

export type LogEvent = {
  text: string;
  klass: string;
  event?: string;
  symbol?: string;
  ts?: string;
};

export type LogsEventsPayload = { events: LogEvent[]; cursor: number };
export type LogsTracePayload = { lines: string[] };

export type SettingsPayload = {
  env: Record<string, string>;
  portfolio: Record<string, unknown>;
  radar: Record<string, unknown>;
  risk: Record<string, unknown>;
  data_sources: Record<string, unknown>;
  editable: Array<{
    key: string;
    label: string;
    yaml_file: string;
    effect: string;
    vtype: string;
  }>;
  kill_active: boolean;
};

export type SymbolDetail = {
  symbol: string;
  name?: string | null;
  holding?: Record<string, unknown> | null;
  decisions: DecisionRow[];
  why?: DecisionRow | null;
  exit_plans: ExitPlanRow[];
  armed: ArmedRow[];
  watches: WatchRow[];
  exit_watches: WatchRow[];
  wake?: string | null;
  stale?: unknown;
  last_price?: number | null;
};

export type OverviewName = {
  symbol: string;
  name?: string;
  side?: string;
  state_label?: string;
  pnl?: number | null;
  stale?: boolean;
  wake_text?: string;
  last_decision?: string;
  venue?: string;
  wake_urgent?: boolean;
};

export type OverviewFire = {
  label: string;
  detail: string;
  at?: string | null;
  countdown: string;
  kind: string;
  venue?: string | null;
  on_book?: boolean;
  state_label?: string | null;
};

export type OverviewVenue = {
  venue: string;
  session: "open" | "stale" | "closed" | string;
  posture?: string | null;
  roster: number;
  hot: number;
  pool: number;
  stale: number;
  overnight_hot: number;
  on_book: OverviewName[];
  watchlist: OverviewName[];
  overnight: { symbol: string; name?: string }[];
  fire: OverviewFire[];
  brief: string;
  brief_as_of?: string | null;
};

export type OverviewPayload = {
  thesis: {
    gross_mode?: string | null;
    net_bias?: string | null;
    rationale: string;
    favored: string[];
    deprioritized: string[];
    venue_posture: Record<string, string>;
    as_of?: string | null;
  };
  venues: OverviewVenue[];
  placement: OverviewName[];
  next_to_fire: OverviewFire[];
  open_venues: string[];
};

export type IntelligenceChange = {
  field: string;
  from?: unknown;
  to?: unknown;
};

export type IntelligenceEvent = {
  event_id: string;
  kind: string;
  layer: "world" | "region" | "family" | "company" | "decision" | string;
  as_of?: string | null;
  status: string;
  linkage?: "linked" | "unlinked" | null;
  venue?: string | null;
  symbol?: string | null;
  scope_id?: string | null;
  mandate_id?: string | null;
  agent_run_id?: string | null;
  title: string;
  summary: string;
  coverage?: string | null;
  source_count: number;
  refs: Record<string, unknown>;
  changes: IntelligenceChange[];
  payload: Record<string, unknown>;
};

export type GlobalIntelligencePosture = {
  posture_id?: string;
  as_of?: string;
  valid_until?: string;
  status?: string;
  gross_mode?: string;
  net_bias?: string;
  rationale?: string;
  persistence_status?: string;
  venue_posture?: Record<string, string>;
  family_priority?: {
    favored?: string[];
    deprioritized?: string[];
  };
};

export type GlobalSituationDigest = {
  digest_id?: string;
  as_of?: string;
  valid_until?: string;
  status?: string;
  regime?: string;
  rates_bias?: string;
  usd_bias?: string;
  persistence_status?: string;
  points?: Array<{
    point?: string;
    direction?: string;
    signal?: string;
    severity?: string;
    source_refs?: string[];
  }>;
  coverage?: Record<string, unknown>;
};

export type MacroBrief = {
  brief_id?: string;
  as_of?: string;
  valid_until?: string;
  venue?: string;
  status?: string;
  coverage?: string | Record<string, unknown>;
  alerts?: Array<{
    point?: string;
    direction?: string;
    signal?: string;
    severity?: string;
    horizon?: string;
    source_refs?: string[];
  }>;
  zones?: Record<string, Array<Record<string, unknown>>>;
  families?: Record<string, Array<Record<string, unknown>>>;
  symbols?: Record<string, Array<Record<string, unknown>>>;
};

export type WorldIntelligencePayload = {
  generated_at: string;
  window_days: number;
  as_of_max?: string | null;
  current: {
    posture?: GlobalIntelligencePosture | null;
    digest?: GlobalSituationDigest | null;
    family_board?: {
      board_id?: string | null;
      as_of?: string | null;
      status?: string | null;
      coverage?: Record<string, unknown>;
    } | null;
    macro: Record<string, MacroBrief>;
    latest_failure?: Record<string, unknown> | null;
  };
  events: IntelligenceEvent[];
  counts: Record<string, number>;
};

export type RegionCurrentIntelligence = {
  posture?: string | null;
  macro_brief?: MacroBrief | null;
  regional_run?: Record<string, unknown> | null;
  latest_failure?: Record<string, unknown> | null;
  mandate?: Record<string, unknown> | null;
  freshness_hours?: number | null;
};

export type FamilyIntelligence = {
  venue: string;
  family: string;
  priority: "favored" | "deprioritized" | "neutral" | string;
  rank?: number | null;
  rank_delta?: number | null;
  persistence: "new" | "unchanged" | "moved" | string;
  history: Array<{
    as_of?: string | null;
    rank?: number | null;
    attractiveness?: number | null;
    candidate_count?: number | null;
  }>;
  attractiveness?: number | null;
  attractiveness_delta?: number | null;
  candidate_count?: number | null;
  bias_counts: Record<string, number>;
  situation_status?: string | null;
  direction_counts: Record<string, number>;
  severity_counts: Record<string, number>;
  observations: Array<Record<string, unknown>>;
  regime: Record<string, unknown>;
  regime_status?: string | null;
  summary: string;
  symbols: string[];
  sticky_symbols: string[];
};

export type FamilyComparisonCell = {
  status: "active" | "observed" | "not_observed" | "not_in_taxonomy";
  best_rank?: number | null;
  leader?: string | null;
  average_attractiveness?: number | null;
  candidate_count: number;
  reason: string;
  families: Array<{
    family: string;
    rank?: number | null;
    attractiveness?: number | null;
    candidate_count?: number | null;
    situation_status?: string | null;
    summary?: string | null;
  }>;
};

export type FamilyComparison = {
  group: string;
  label: string;
  venues: Record<"TW" | "EU" | "US", FamilyComparisonCell>;
};

export type RegionIntelligencePayload = {
  generated_at: string;
  window_days: number;
  venue?: string | null;
  current: Record<string, RegionCurrentIntelligence>;
  families: FamilyIntelligence[];
  comparison: FamilyComparison[];
  events: IntelligenceEvent[];
  counts: Record<string, number>;
};

export type CompanyEvidencePoint = {
  point?: string;
  label?: string;
  detail?: string;
  direction?: string;
  signal?: string;
  severity?: string;
  source_refs?: string[];
};

export type CompanyBrief = {
  brief_id?: string;
  as_of?: string;
  valid_until?: string;
  symbol?: string;
  depth?: string;
  coverage?: string | Record<string, unknown>;
  issuer_identity?: Record<string, unknown>;
  business?: { summary?: string; [key: string]: unknown };
  financial_snapshot?: Record<string, unknown>;
  earnings_and_guidance?: Record<string, unknown>;
  company_thesis?: {
    status?: string;
    summary?: string;
    [key: string]: unknown;
  };
  catalysts?: CompanyEvidencePoint[];
  risks?: CompanyEvidencePoint[];
  open_questions?: CompanyEvidencePoint[];
  selection_view?: {
    posture?: string;
    confidence?: number | string;
    summary?: string;
    reasons?: string[];
    [key: string]: unknown;
  };
};

export type CompanyIntelligence = {
  symbol: string;
  name: string;
  venue: string;
  as_of?: string | null;
  age_hours?: number | null;
  depth?: string | null;
  coverage?: string | null;
  source_count: number;
  thesis_status: string;
  previous_thesis_status?: string | null;
  thesis_changed: boolean;
  summary: string;
  business_summary: string;
  selection_posture?: string | null;
  selection_confidence?: number | null;
  selection_confidence_label?: string | null;
  catalyst_count: number;
  risk_count: number;
  open_question_count: number;
  history_count: number;
  on_book: boolean;
  side?: string | null;
  stale_market: boolean;
  decision_count: number;
  linked_decision_count: number;
  latest_action?: string | null;
  latest_decision_at?: string | null;
  brief: CompanyBrief;
};

export type CompanyIntelligencePayload = {
  generated_at: string;
  window_days: number;
  filters: {
    symbol?: string | null;
    venue?: string | null;
  };
  companies: CompanyIntelligence[];
  events: IntelligenceEvent[];
  counts: {
    companies: number;
    on_book: number;
    changed: number;
    stale: number;
    decisions: number;
    unlinked_decisions: number;
  };
};

// ── Daily Briefing ──────────────────────────────────────────────────────────

export type NewsFeedItem = {
  id: string;
  kind: "company" | "geo";
  title: string;
  source: string;
  published_at: string | null;
  url: string | null;
  symbol: string | null;
  name: string | null;
  venue: string | null;
  country: string | null;
};

export type BriefingStory = {
  point: string;
  direction: string | null;
  severity: string | null;
  signal: string | null;
  horizon: string | null;
  venue: string | null;
  symbols: string[];
  sources: string[];
  source_refs: string[];
  as_of: string | null;
  origin: "global_digest" | "news_brief";
};

export type MacroIndicator = {
  series_id: string;
  label: string;
  unit: string | null;
  value: number;
  period: string;
  previous_value: number | null;
  delta: number | null;
  history: Array<{ period: string; value: number }>;
};

export type MacroCalendarEvent = {
  event: string;
  at: string;
  in_h: number;
};

export type BriefingHeadline = {
  posture: GlobalIntelligencePosture | null;
  regime: string | null;
  rates_bias: string | null;
  usd_bias: string | null;
  digest_as_of: string | null;
  lead: string | null;
};

export type PostureHistoryPoint = {
  as_of: string;
  gross_mode: string | null;
  net_bias: string | null;
  regime: string | null;
};

export type UpcomingEarning = {
  symbol: string;
  name: string | null;
  earnings_date: string;
  eps_consensus: number | null;
  revenue_consensus: number | null;
  as_of: string | null;
  stale: boolean;
};

export type DailyBriefingPayload = {
  generated_at: string;
  as_of: string | null;
  headline: BriefingHeadline;
  top_stories: BriefingStory[];
  indicators: MacroIndicator[];
  calendar: MacroCalendarEvent[];
  news: NewsFeedItem[];
  posture_history?: PostureHistoryPoint[];
  earnings?: UpcomingEarning[];
  counts: { stories: number; indicators: number; news: number };
};

export type PriceBar = {
  ts: string;
  open: number;
  high: number;
  low: number;
  close: number;
  volume: number | null;
};

export type SymbolBarsPayload = {
  symbol: string;
  as_of: string | null;
  bars: PriceBar[];
};

export type NewsFeedPayload = {
  generated_at: string;
  window_days: number;
  items: NewsFeedItem[];
  counts: { company: number; geo: number; total: number };
};
