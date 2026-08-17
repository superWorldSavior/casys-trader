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
