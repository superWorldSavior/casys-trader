import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { useCallback, useEffect, useMemo, useState } from "react";
import { Toaster } from "sonner";
import { LoadingSkeleton } from "@/components/loading-skeleton";
import { AppShell, PAGE_BY_KEY, type PageKey } from "@/components/layout/app-shell";
import { useSnapshot } from "@/hooks/use-snapshot";
import { formatClock } from "@/lib/format";
import { CompaniesPage } from "@/pages/companies";
import { DecisionsPage } from "@/pages/decisions";
import { HealthPage } from "@/pages/health";
import { LogsPage } from "@/pages/logs";
import { OverviewPage } from "@/pages/overview";
import { PortfolioPage } from "@/pages/portfolio";
import { ReportsPage } from "@/pages/reports";
import { SettingsPage } from "@/pages/settings";
import { SymbolDetailPage } from "@/pages/symbol-detail";
import { TodayPage } from "@/pages/today";
import { UniversePage } from "@/pages/universe";

const queryClient = new QueryClient();

export default function App() {
  return (
    <QueryClientProvider client={queryClient}>
      <Desk />
      <Toaster theme="light" position="bottom-right" />
    </QueryClientProvider>
  );
}

function Desk() {
  const { data, error, dataUpdatedAt, isPending } = useSnapshot();
  const [page, setPage] = useState<PageKey>("overview");
  const [symbol, setSymbol] = useState<string | null>(null);
  const [companyVenue, setCompanyVenue] = useState<string | null>(null);
  const updated = useMemo(
    () => (dataUpdatedAt ? formatClock(new Date(dataUpdatedAt).toISOString()) : undefined),
    [dataUpdatedAt],
  );

  const openPage = useCallback((next: PageKey) => {
    setSymbol(null);
    if (next === "companies") setCompanyVenue(null);
    setPage(next);
  }, []);

  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if (event.defaultPrevented || event.isComposing || event.metaKey || event.ctrlKey || event.altKey) return;
      const target = event.target as HTMLElement | null;
      if (
        target?.closest(
          'input, textarea, select, button, [contenteditable="true"], [role="textbox"], [role="combobox"]',
        )
      )
        return;
      const next = PAGE_BY_KEY[event.key];
      if (!next) return;
      openPage(next);
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [openPage]);

  return (
    <AppShell
      page={page}
      onPage={openPage}
      snapshot={data}
      updatedAt={updated}
    >
      {error ? (
        <div className="mb-4 rounded-lg border border-loss/40 bg-loss/10 px-4 py-3 text-sm text-loss">
          <p>Casys cannot refresh the current desktop data. Existing information is not treated as current.</p>
          <details className="mt-2 text-xs text-dim">
            <summary className="cursor-pointer">Technical details</summary>
            <p className="mt-1 break-words font-mono text-[10px] text-faint">{error instanceof Error ? error.message : String(error)}</p>
          </details>
        </div>
      ) : null}
      {isPending && !data && page === "overview" ? <LoadingSkeleton /> : null}
      {symbol ? <SymbolDetailPage symbol={symbol} onBack={() => setSymbol(null)} /> : null}
      {!symbol && (page === "today" || page === "world" || page === "regions") ? (
        <TodayPage
          snapshot={data}
          onSymbol={setSymbol}
        />
      ) : null}
      {!symbol && page === "overview" && data ? (
        <OverviewPage
          snapshot={data}
          onSymbol={setSymbol}
          onPage={openPage}
        />
      ) : null}
      {!symbol && page === "companies" ? (
        <CompaniesPage initialVenue={companyVenue} onSymbol={setSymbol} />
      ) : null}
      {!symbol && page === "portfolio" ? <PortfolioPage snapshot={data} onSymbol={setSymbol} /> : null}
      {!symbol && page === "decisions" ? <DecisionsPage snapshot={data} onSymbol={setSymbol} /> : null}
      {!symbol && page === "health" ? (
        <HealthPage
          snapshot={data}
          onPage={openPage}
        />
      ) : null}
      {!symbol && page === "logs" ? <LogsPage /> : null}
      {!symbol && page === "universe" ? <UniversePage onSymbol={setSymbol} /> : null}
      {!symbol && page === "settings" ? <SettingsPage /> : null}
      {!symbol && page === "reports" ? <ReportsPage /> : null}
    </AppShell>
  );
}
