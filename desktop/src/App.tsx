import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { useEffect, useMemo, useState } from "react";
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
import { RegionsPage } from "@/pages/regions";
import { ReportsPage } from "@/pages/reports";
import { SettingsPage } from "@/pages/settings";
import { SymbolDetailPage } from "@/pages/symbol-detail";
import { TodayPage } from "@/pages/today";
import { UniversePage } from "@/pages/universe";
import { WorldPage } from "@/pages/world";

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
  const [page, setPage] = useState<PageKey>("today");
  const [symbol, setSymbol] = useState<string | null>(null);
  const [companyVenue, setCompanyVenue] = useState<string | null>(null);
  const updated = useMemo(
    () => (dataUpdatedAt ? formatClock(new Date(dataUpdatedAt).toISOString()) : undefined),
    [dataUpdatedAt],
  );

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
      setSymbol(null);
      if (next === "companies") setCompanyVenue(null);
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
        if (next === "companies") setCompanyVenue(null);
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
      {isPending && !data && page === "overview" ? <LoadingSkeleton /> : null}
      {symbol ? <SymbolDetailPage symbol={symbol} onBack={() => setSymbol(null)} /> : null}
      {!symbol && page === "today" ? (
        <TodayPage
          snapshot={data}
          onSymbol={setSymbol}
          onPage={(next) => {
            setSymbol(null);
            setPage(next);
          }}
        />
      ) : null}
      {!symbol && page === "overview" && data ? (
        <OverviewPage
          snapshot={data}
          onSymbol={setSymbol}
          onPage={(next) => {
            setSymbol(null);
            setPage(next);
          }}
        />
      ) : null}
      {!symbol && page === "world" ? <WorldPage onReports={() => setPage("reports")} /> : null}
      {!symbol && page === "regions" ? (
        <RegionsPage
          onCompanies={(venue) => {
            setCompanyVenue(venue);
            setPage("companies");
          }}
          onUniverse={() => setPage("universe")}
          onSymbol={setSymbol}
        />
      ) : null}
      {!symbol && page === "companies" ? (
        <CompaniesPage initialVenue={companyVenue} onSymbol={setSymbol} />
      ) : null}
      {!symbol && page === "portfolio" ? <PortfolioPage snapshot={data} onSymbol={setSymbol} /> : null}
      {!symbol && page === "decisions" ? <DecisionsPage snapshot={data} onSymbol={setSymbol} /> : null}
      {!symbol && page === "health" ? <HealthPage /> : null}
      {!symbol && page === "logs" ? <LogsPage /> : null}
      {!symbol && page === "universe" ? <UniversePage onSymbol={setSymbol} /> : null}
      {!symbol && page === "settings" ? <SettingsPage /> : null}
      {!symbol && page === "reports" ? <ReportsPage /> : null}
    </AppShell>
  );
}
