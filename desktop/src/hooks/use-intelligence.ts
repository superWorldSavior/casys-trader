import { useQuery } from "@tanstack/react-query";
import {
  readCompanyIntelligence,
  readDailyBriefing,
  readNewsFeed,
  readRegionIntelligence,
  readWorldIntelligence,
  readWorldGraph,
} from "@/lib/api";

const INTELLIGENCE_STALE_TIME = 60_000;

export function useWorldIntelligence(limit = 240) {
  return useQuery({
    queryKey: ["intelligence", "world", limit],
    queryFn: () => readWorldIntelligence(limit),
    staleTime: INTELLIGENCE_STALE_TIME,
    refetchInterval: 120_000,
  });
}

export function useWorldGraph() {
  return useQuery({
    queryKey: ["world-graph", "current-published-overview"],
    queryFn: readWorldGraph,
    staleTime: INTELLIGENCE_STALE_TIME,
    refetchInterval: 120_000,
    retry: 1,
  });
}

export function useRegionIntelligence(venue?: string, limit = 240) {
  return useQuery({
    queryKey: ["intelligence", "regions", venue || "all", limit],
    queryFn: () => readRegionIntelligence({ venue, limit }),
    staleTime: INTELLIGENCE_STALE_TIME,
    refetchInterval: 120_000,
  });
}

export function useCompanyIntelligence(params?: {
  symbol?: string;
  venue?: string;
  limit?: number;
}, enabled = true) {
  return useQuery({
    queryKey: [
      "intelligence",
      "companies",
      params?.symbol || "all",
      params?.venue || "all",
      params?.limit || 320,
    ],
    queryFn: () => readCompanyIntelligence(params),
    enabled,
    staleTime: INTELLIGENCE_STALE_TIME,
    refetchInterval: 120_000,
  });
}

export function useDailyBriefing() {
  return useQuery({
    queryKey: ["intelligence", "briefing"],
    queryFn: readDailyBriefing,
    staleTime: INTELLIGENCE_STALE_TIME,
    refetchInterval: 120_000,
  });
}

export function useNewsFeed(params?: { venue?: string; symbol?: string; limit?: number }) {
  return useQuery({
    queryKey: [
      "intelligence",
      "news",
      params?.venue || "all",
      params?.symbol || "all",
      params?.limit ?? 40,
    ],
    queryFn: () => readNewsFeed(params),
    staleTime: INTELLIGENCE_STALE_TIME,
    refetchInterval: 120_000,
  });
}
