import { useQuery } from "@tanstack/react-query";
import { readSnapshot } from "@/lib/snapshot";

export function useSnapshot() {
  return useQuery({
    queryKey: ["snapshot"],
    queryFn: readSnapshot,
    refetchInterval: 4000,
    staleTime: 2000,
    retry: 1,
  });
}
