import {
  Activity,
  BookOpen,
  Building2,
  FileText,
  Globe2,
  HeartPulse,
  LayoutDashboard,
  Logs,
  Map,
  Newspaper,
  Orbit,
  Settings,
  Wallet,
} from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { formatAgo, formatUsd } from "@/lib/format";
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

const INTELLIGENCE_NAV: NavItem[] = [
  { key: "today", label: "Today", shortcut: "1", icon: Newspaper },
  { key: "world", label: "World", shortcut: "2", icon: Globe2 },
  { key: "regions", label: "Regions", shortcut: "3", icon: Map },
  { key: "companies", label: "Companies", shortcut: "4", icon: Building2 },
];

const OPS_NAV: NavItem[] = [
  { key: "overview", label: "Overview", shortcut: "5", icon: LayoutDashboard },
  { key: "portfolio", label: "Portfolio", shortcut: "6", icon: Wallet },
  { key: "decisions", label: "Decisions", shortcut: "7", icon: BookOpen },
  { key: "universe", label: "Universe", shortcut: "8", icon: Orbit },
  { key: "health", label: "Health", shortcut: "9", icon: HeartPulse },
  { key: "logs", label: "Logs", shortcut: "0", icon: Logs },
  { key: "reports", label: "Reports", shortcut: "r", icon: FileText },
  { key: "settings", label: "Settings", shortcut: "s", icon: Settings },
];

const NAV = [...INTELLIGENCE_NAV, ...OPS_NAV];

export const PAGE_BY_KEY: Record<string, PageKey> = Object.fromEntries(
  NAV.map((item) => [item.shortcut, item.key]),
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
  const alive = Boolean(daemon?.alive);
  const dry = daemon?.dry_run !== false;
  const kill = Boolean(snapshot?.kill_active);
  const equity = snapshot?.portfolio.equity;
  const cash = snapshot?.portfolio.cash_available ?? snapshot?.portfolio.cash;
  const section = INTELLIGENCE_NAV.some((item) => item.key === page) ? "Intelligence" : "Agent / Ops";
  const pageLabel = NAV.find((item) => item.key === page)?.label ?? page;

  return (
    <div className="grain flex h-screen overflow-hidden bg-ink text-fg">
      <aside className="flex w-52 shrink-0 flex-col border-r border-line bg-ink-raised xl:w-56">
        <div className="px-5 pb-4 pt-5">
          <h1 className="text-lg font-semibold tracking-tight text-fg">Casys</h1>
        </div>
        <nav className="min-h-0 flex-1 overflow-y-auto px-3 pb-3">
          <NavGroup label="Intelligence" items={INTELLIGENCE_NAV} page={page} onPage={onPage} />
          <NavGroup label="Agent / Ops" items={OPS_NAV} page={page} onPage={onPage} />
        </nav>
        <div className="space-y-2 border-t border-hairline px-4 py-3">
          <div className="flex items-center gap-2 text-xs">
            <span className={cn("size-1.5 rounded-full", alive ? "bg-gain" : "bg-loss")} />
            <span className="text-muted">{alive ? "agent online" : "agent offline"}</span>
          </div>
          <div className="flex flex-wrap gap-1.5">
            <Badge tone={dry ? "warn" : "gain"}>{dry ? "paper dry" : "live paper"}</Badge>
            {daemon?.phase ? <Badge>{daemon.phase}</Badge> : null}
            {kill ? <Badge tone="loss">kill</Badge> : null}
          </div>
          {daemon?.pid ? <p className="font-mono text-[10px] text-faint">pid {daemon.pid}</p> : null}
        </div>
      </aside>
      <div className="flex min-w-0 flex-1 flex-col">
        <header className="flex h-12 items-center justify-between gap-4 border-b border-line bg-panel/75 px-4 backdrop-blur xl:px-5">
          <div className="flex min-w-0 items-center gap-3 text-xs text-dim">
            <Activity className="size-3.5 shrink-0 text-accent" />
            <p className="truncate text-xs text-muted">
              <span className="text-faint">{section}</span>
              <span className="mx-1.5 text-faint">/</span>
              {pageLabel}
            </p>
          </div>
          <div className="flex min-w-0 items-center gap-3 text-xs text-dim">
            <Kpi
              label="venues"
              value={snapshot?.open_venues_list?.length ? snapshot.open_venues_list.join(" · ") : "closed"}
            />
            <Kpi label="eq" value={formatUsd(equity, 0)} />
            <Kpi label="cash" value={formatUsd(cash, 0)} />
            <Kpi label="phase" value={daemon?.phase || "—"} />
            <Kpi label="kill" value={kill ? "ON" : "off"} tone={kill ? "text-loss" : undefined} />
            <Kpi label="wake" value={snapshot?.default_next_wake ? formatAgo(snapshot.default_next_wake) : "—"} />
            <p className="hidden shrink-0 font-mono text-[10px] uppercase tracking-[0.16em] text-faint xl:block">
              {updatedAt ? `polled ${updatedAt}` : "waiting"}
            </p>
          </div>
        </header>
        <main className="min-h-0 flex-1 overflow-auto p-4 xl:p-5">{children}</main>
      </div>
    </div>
  );
}

function NavGroup({
  label,
  items,
  page,
  onPage,
}: {
  label: string;
  items: NavItem[];
  page: PageKey;
  onPage: (page: PageKey) => void;
}) {
  return (
    <section className="mb-4">
      <p className="mb-1.5 px-3 font-mono text-[9px] uppercase tracking-[0.22em] text-faint">{label}</p>
      <div className="space-y-0.5">
        {items.map((item) => {
          const Icon = item.icon;
          const active = page === item.key;
          return (
            <button
              key={item.key}
              type="button"
              title={`${item.label} · ${item.shortcut}`}
              aria-current={active ? "page" : undefined}
              onClick={() => onPage(item.key)}
              className={cn(
                "group flex w-full items-center gap-2.5 rounded-md px-3 py-1.5 text-left text-sm transition-colors",
                active ? "bg-accent/10 text-fg" : "text-dim hover:bg-ink hover:text-muted",
              )}
            >
              <Icon className={cn("size-3.5", active ? "text-accent" : "text-faint group-hover:text-dim")} />
              <span className="flex-1">{item.label}</span>
              <span className="font-mono text-[9px] uppercase text-faint">{item.shortcut}</span>
            </button>
          );
        })}
      </div>
    </section>
  );
}

function Kpi({ label, value, tone }: { label: string; value: string; tone?: string }) {
  return (
    <span className="hidden items-baseline gap-1.5 lg:flex">
      <span className="font-mono text-[10px] uppercase tracking-[0.14em] text-faint">{label}</span>
      <span className={cn("font-mono text-[11px] text-muted", tone)}>{value}</span>
    </span>
  );
}
