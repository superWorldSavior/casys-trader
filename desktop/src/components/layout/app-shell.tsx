import { Activity, BookOpen, HeartPulse, LayoutDashboard, Logs, Orbit, Wallet } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { cn } from "@/lib/utils";
import type { Snapshot } from "@/lib/types";

export type PageKey = "overview" | "decisions" | "portfolio" | "health" | "universe" | "logs";

const NAV: {
  key: PageKey;
  label: string;
  icon: typeof LayoutDashboard;
  ready: boolean;
}[] = [
  { key: "overview", label: "Overview", icon: LayoutDashboard, ready: true },
  { key: "decisions", label: "Decisions", icon: BookOpen, ready: true },
  { key: "portfolio", label: "Book", icon: Wallet, ready: true },
  { key: "health", label: "Health", icon: HeartPulse, ready: false },
  { key: "universe", label: "Universe", icon: Orbit, ready: false },
  { key: "logs", label: "Logs", icon: Logs, ready: false },
];

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

  return (
    <div className="grain flex h-screen overflow-hidden bg-ink text-fg">
      <aside className="flex w-56 shrink-0 flex-col border-r border-line bg-ink-raised">
        <div className="px-5 pb-6 pt-7">
          <p className="font-mono text-[10px] uppercase tracking-[0.28em] text-accent">Casys</p>
          <h1 className="mt-1 text-xl font-semibold tracking-tight">Trader</h1>
          <p className="mt-2 text-xs text-dim">Desktop skeleton · read-only</p>
        </div>
        <nav className="flex flex-1 flex-col gap-1 px-3">
          {NAV.map((item) => {
            const Icon = item.icon;
            const active = page === item.key;
            return (
              <button
                key={item.key}
                type="button"
                onClick={() => onPage(item.key)}
                className={cn(
                  "flex items-center gap-2.5 rounded-md px-3 py-2 text-left text-sm transition-colors",
                  active ? "bg-panel text-fg" : "text-dim hover:bg-panel/60 hover:text-muted",
                )}
              >
                <Icon className={cn("size-4", active ? "text-accent" : "text-faint")} />
                <span className="flex-1">{item.label}</span>
                {item.ready ? null : (
                  <span className="font-mono text-[9px] uppercase tracking-[0.12em] text-faint">later</span>
                )}
              </button>
            );
          })}
        </nav>
        <div className="space-y-2 border-t border-hairline px-4 py-4">
          <div className="flex items-center gap-2 text-xs">
            <span className={cn("size-1.5 rounded-full", alive ? "bg-gain shadow-[0_0_8px_#a5c98c]" : "bg-loss")} />
            <span className="text-muted">{alive ? "daemon up" : "daemon down"}</span>
          </div>
          <div className="flex flex-wrap gap-1.5">
            <Badge tone={dry ? "warn" : "gain"}>{dry ? "paper dry" : "live paper"}</Badge>
            {daemon?.phase ? <Badge>{daemon.phase}</Badge> : null}
          </div>
          {daemon?.pid ? <p className="font-mono text-[10px] text-faint">pid {daemon.pid}</p> : null}
        </div>
      </aside>
      <div className="flex min-w-0 flex-1 flex-col">
        <header className="flex h-12 items-center justify-between border-b border-line px-5">
          <div className="flex items-center gap-3 text-xs text-dim">
            <Activity className="size-3.5 text-accent" />
            <span>{snapshot?.open_venues_list?.length ? snapshot.open_venues_list.join(" · ") : "no venue open"}</span>
            {daemon?.current_symbol ? (
              <span className="font-mono text-muted">now {daemon.current_symbol}</span>
            ) : null}
          </div>
          <p className="font-mono text-[10px] uppercase tracking-[0.16em] text-faint">
            {updatedAt ? `polled ${updatedAt}` : "waiting for snapshot"}
          </p>
        </header>
        <main className="min-h-0 flex-1 overflow-auto p-5">{children}</main>
      </div>
    </div>
  );
}
