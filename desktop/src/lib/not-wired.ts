import { toast } from "sonner";

export function notWired(action: string) {
  toast.message(`${action} — not wired`, {
    description: "Read-only desk. Operator writes stay unhooked on purpose.",
  });
}
