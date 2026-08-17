import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { useEffect, useMemo, useState } from "react";
import { Toaster } from "sonner";
import { LoadingSkeleton } from "@/components/loading-skeleton";
import { AppShell, type PageKey } from "@/components/layout/app-shell";
import { useSnapshot } from "@/hooks/use-snapshot";
import { formatClock } from "@/lib/format";
import { ComingSoonPage } from "@/pages/coming-soon";
import { DecisionsPage } from "@/pages/decisions";
import { OverviewPage } from "@/pages/overview";
import { PortfolioPage } from "@/pages/portfolio";
import { SymbolDetailPage } from "@/pages/symbol-detail";

const queryClient = new QueryClient();
const LIVE_PAGES: PageKey[] = ["overview", "decisions", "portfolio"];

export default function App() {
  return (
    <QueryClientProvider client={queryClient}>
      <Desk />
      <Toaster theme="dark" position="bottom-right" />
    </QueryClientProvider>
  );
}

function Desk() {
  const { data, error, dataUpdatedAt, isPending } = useSnapshot();
  const [page, setPage] = useState<PageKey>("overview");
  const [symbol, setSymbol] = useState<string | null>(null);
  const updated = useMemo(
    () => (dataUpdatedAt ? formatClock(new Date(dataUpdatedAt).toISOString()) : undefined),
    [dataUpdatedAt],
  );

  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if (event.metaKey || event.ctrlKey || event.altKey) return;
      const target = event.target as HTMLElement | null;
      if (target && ["INPUT", "TEXTAREA"].includes(target.tagName)) return;
      const next = ({ "1": "overview", "2": "decisions", "3": "portfolio" } as const)[event.key];
      if (!next) return;
      setSymbol(null);
      setPage(next);
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);

  return (
    <AppShell
      page={page}
      onPage={(next) => {
        setSymbol(null);
        setPage(next);
      }}
      snapshot={data}
      updatedAt={updated}
    >
      {error ? (
        <div className="mb-4 rounded-lg border border-loss/40 bg-loss/10 px-4 py-3 text-sm text-loss">
          {error instanceof Error ? error.message : String(error)}
        </div>
      ) : null}
      {isPending && !data ? <LoadingSkeleton /> : null}
      {data && symbol && LIVE_PAGES.includes(page) ? (
        <SymbolDetailPage symbol={symbol} snapshot={data} onBack={() => setSymbol(null)} />
      ) : null}
      {data && !symbol && page === "overview" ? (
        <OverviewPage snapshot={data} onSymbol={setSymbol} />
      ) : null}
      {data && !symbol && page === "decisions" ? (
        <DecisionsPage snapshot={data} onSymbol={setSymbol} />
      ) : null}
      {data && !symbol && page === "portfolio" ? (
        <PortfolioPage snapshot={data} onSymbol={setSymbol} />
      ) : null}
      {page === "health" ? (
        <ComingSoonPage
          title="Health"
          note="Gates, kill-switch, stale streaks, queue workers. Next slice after the desk shell settles."
        />
      ) : null}
      {page === "universe" ? (
        <ComingSoonPage
          title="Universe"
          note="Radar, pins, bans, venue rotation. Still a TUI page — this slot is reserved."
        />
      ) : null}
      {page === "logs" ? (
        <ComingSoonPage
          title="Logs"
          note="daemon_console and agent_trace, follow mode. Intentionally not in the first skeleton."
        />
      ) : null}
    </AppShell>
  );
}
