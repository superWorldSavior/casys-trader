import { useQuery } from "@tanstack/react-query";
import {
  readDecisions,
  readHealth,
  readLogTrace,
  readOverview,
  readPlans,
  readPortfolio,
  readReports,
  readSettings,
  readSymbol,
  readSymbolBars,
  readUniverse,
} from "@/lib/api";

export function useSymbolBars(symbol: string) {
  return useQuery({
    queryKey: ["symbol-bars", symbol],
    queryFn: () => readSymbolBars(symbol),
    enabled: Boolean(symbol),
    staleTime: 60_000,
    refetchInterval: 120_000,
  });
}

export function useDecisions(filter: string) {
  return useQuery({
    queryKey: ["decisions", filter],
    queryFn: () => readDecisions({ limit: 120, filter }),
    staleTime: 4000,
    refetchInterval: 8000,
  });
}

export function usePlans() {
  return useQuery({
    queryKey: ["plans"],
    queryFn: readPlans,
    staleTime: 4000,
    refetchInterval: 8000,
  });
}

export function useOverview() {
  return useQuery({
    queryKey: ["overview"],
    queryFn: readOverview,
    staleTime: 8000,
    refetchInterval: 15_000,
  });
}

export function useUniverse() {
  return useQuery({
    queryKey: ["universe"],
    queryFn: readUniverse,
    staleTime: 8000,
    refetchInterval: 15_000,
  });
}

export function useHealth() {
  return useQuery({
    queryKey: ["health"],
    queryFn: readHealth,
    staleTime: 8000,
    refetchInterval: 12_000,
  });
}

export function useReports() {
  return useQuery({
    queryKey: ["reports"],
    queryFn: readReports,
    staleTime: 30_000,
  });
}

export function useSettings() {
  return useQuery({
    queryKey: ["settings"],
    queryFn: readSettings,
    staleTime: 30_000,
  });
}

export function usePortfolio(sort: number) {
  return useQuery({
    queryKey: ["portfolio", sort],
    queryFn: () => readPortfolio(sort),
    staleTime: 4000,
    refetchInterval: 8000,
  });
}

export function useSymbol(symbol: string | null) {
  return useQuery({
    queryKey: ["symbol", symbol],
    queryFn: () => readSymbol(symbol ?? ""),
    enabled: Boolean(symbol),
    staleTime: 4000,
    refetchInterval: 10_000,
  });
}

export function useLogTrace(enabled: boolean) {
  return useQuery({
    queryKey: ["logs-trace"],
    queryFn: () => readLogTrace(240),
    enabled,
    staleTime: 4000,
    refetchInterval: enabled ? 6000 : false,
  });
}
