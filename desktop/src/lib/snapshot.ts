import { invoke } from "@tauri-apps/api/core";
import type { Snapshot } from "@/lib/types";

export async function readSnapshot(): Promise<Snapshot> {
  return invoke<Snapshot>("read_snapshot");
}
