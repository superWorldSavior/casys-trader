import {
  BookOpen,
  HeartPulse,
  LayoutDashboard,
  Map,
  Wallet,
} from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { formatAgo, formatUsd } from "@/lib/format";
import { marketsLabel, paperBookLabel, processStatusLabel } from "@/lib/humanize";
import type { Snapshot } from "@/lib/types";
import { cn } from "@/lib/utils";

export type PageKey =
  | "today"
  | "overview"
  | "world"
  | "regions"
  | "companies"
  | "portfolio"
  | "decisions"
  | "health"
  | "logs"
  | "universe"
  | "settings"
  | "reports";

type NavItem = { key: PageKey; label: string; shortcut: string; icon: typeof LayoutDashboard };

const SIDEBAR_NAV: NavItem[] = [
  { key: "overview", label: "Now", shortcut: "1", icon: LayoutDashboard },
  { key: "portfolio", label: "Your money", shortcut: "2", icon: Wallet },
  { key: "today", label: "Markets", shortcut: "3", icon: Map },
  { key: "decisions", label: "Activity", shortcut: "4", icon: BookOpen },
  { key: "health", label: "System", shortcut: "5", icon: HeartPulse },
];

const PAGE_LABELS: Record<PageKey, string> = {
  overview: "Now",
  portfolio: "Your money",
  today: "Markets",
  world: "Markets",
  regions: "Markets",
  companies: "Company views",
  decisions: "Activity",
  health: "System",
  logs: "System events",
  universe: "Tracked companies",
  settings: "Settings",
  reports: "Technical reports",
};

export const PAGE_BY_KEY: Record<string, PageKey> = Object.fromEntries(
  SIDEBAR_NAV.map((item) => [item.shortcut, item.key]),
) as Record<string, PageKey>;

type Props = {
  page: PageKey;
  onPage: (page: PageKey) => void;
  snapshot?: Snapshot;
  updatedAt?: string;
  children: React.ReactNode;
};

export function AppShell({ page, onPage, snapshot, updatedAt, children }: Props) {
  const daemon = snapshot?.daemon;
  const kill = Boolean(snapshot?.kill_active);
  const equity = snapshot?.portfolio.equity;
  const pageLabel = PAGE_LABELS[page];
  const activePage = parentPage(page);
  const contextualPage = activePage !== page;
  const processLabel = snapshot ? processStatusLabel(daemon) : "Loading current status";
  const marketHoursLabel = snapshot ? marketsLabel(snapshot.open_venues_list) : "Checking market hours";

  return (
    <div className="grain flex h-screen overflow-hidden bg-ink text-fg">
      <aside className="flex w-16 shrink-0 flex-col border-r border-line bg-ink-raised min-[900px]:w-44">
        <div className="px-3 pb-4 pt-5 min-[900px]:px-5">
          <h1 className="text-center text-lg font-semibold tracking-tight text-fg min-[900px]:text-left">
            <span className="min-[900px]:hidden">C</span>
            <span className="hidden min-[900px]:inline">Casys</span>
          </h1>
        </div>
        <nav className="min-h-0 flex-1 space-y-0.5 overflow-y-auto px-3 pb-3" aria-label="Desk">
          {SIDEBAR_NAV.map((item) => (
            <NavButton
              key={item.key}
              item={item}
              active={activePage === item.key}
              contextual={contextualPage}
              onPage={onPage}
            />
          ))}
        </nav>
        <div className="hidden space-y-2 border-t border-hairline px-4 py-3 min-[900px]:block">
          {kill ? (
            <Badge tone="loss" className="w-full justify-center py-1">
              Kill switch on
            </Badge>
          ) : null}
          <div>
            <p className="text-xs font-medium text-muted">{processLabel}</p>
            <p className="mt-0.5 text-[11px] text-faint">{marketHoursLabel}</p>
          </div>
          <details className="group">
            <summary className="cursor-pointer font-mono text-[10px] uppercase tracking-[0.14em] text-faint outline-none focus-visible:ring-2 focus-visible:ring-accent/50">
              Technical
            </summary>
            <div className="mt-2 space-y-1 font-mono text-[10px] text-faint">
              <p>phase {daemon?.phase || "—"}</p>
              <p>pid {daemon?.pid ?? "—"}</p>
              <p>next review {snapshot?.default_next_wake ? formatAgo(snapshot.default_next_wake) : "—"}</p>
            </div>
          </details>
        </div>
      </aside>
      <div className="flex min-w-0 flex-1 flex-col">
        <header className="flex h-12 items-center justify-between gap-4 border-b border-line bg-panel/75 px-4 backdrop-blur xl:px-5">
          <div className="flex min-w-0 items-center gap-3 text-xs text-dim">
            <p className="truncate text-xs font-medium text-muted">{pageLabel}</p>
          </div>
          <div className="flex min-w-0 items-center gap-3 text-xs text-dim">
            {kill ? (
              <span className="font-mono text-[11px] font-semibold uppercase tracking-[0.12em] text-loss">
                Kill switch on
              </span>
            ) : null}
            <Badge tone="warn" className="shrink-0">{paperBookLabel()}</Badge>
            <Kpi label="Session" value={marketHoursLabel} />
            <Kpi label="Total value" value={formatUsd(equity, 0)} />
            <Kpi label="Synced" value={updatedAt ? updatedAt : "waiting"} />
          </div>
        </header>
        <main className="min-h-0 flex-1 overflow-x-hidden overflow-y-auto p-3 min-[700px]:p-4 xl:p-5">{children}</main>
      </div>
    </div>
  );
}

function parentPage(page: PageKey): PageKey {
  if (page === "world" || page === "regions" || page === "companies") return "today";
  if (page === "universe" || page === "settings" || page === "reports" || page === "logs") return "health";
  return page;
}

function NavButton({
  item,
  active,
  contextual,
  onPage,
}: {
  item: NavItem;
  active: boolean;
  contextual: boolean;
  onPage: (page: PageKey) => void;
}) {
  const Icon = item.icon;
  return (
    <button
      key={item.key}
      type="button"
      title={`${item.label} · ${item.shortcut}`}
      aria-current={active ? (contextual ? "location" : "page") : undefined}
      onClick={() => onPage(item.key)}
      className={cn(
        "group flex w-full items-center gap-2.5 rounded-md px-3 py-1.5 text-left text-sm transition-colors",
        "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent/50",
        active ? "bg-accent/10 text-fg" : "text-dim hover:bg-ink hover:text-muted",
      )}
    >
      <Icon className={cn("size-3.5", active ? "text-accent" : "text-faint group-hover:text-dim")} />
      <span className="hidden flex-1 min-[900px]:inline">{item.label}</span>
      <span className="hidden font-mono text-[9px] uppercase text-faint min-[900px]:inline">{item.shortcut}</span>
    </button>
  );
}

function Kpi({ label, value }: { label: string; value: string }) {
  return (
    <span className="hidden items-baseline gap-1.5 min-[1100px]:flex">
      <span className="font-mono text-[10px] uppercase tracking-[0.14em] text-faint">{label}</span>
      <span className="font-mono text-[11px] text-muted">{value}</span>
    </span>
  );
}
