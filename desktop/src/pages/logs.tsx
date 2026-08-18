import { useEffect, useMemo, useRef, useState } from "react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardBody, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { useLogTrace } from "@/hooks/use-desk-api";
import { readLogEvents } from "@/lib/api";
import type { LogEvent } from "@/lib/types";
import { cn } from "@/lib/utils";

const CLASSES = [
  "cycle",
  "decision_executed",
  "risk_reject",
  "hold",
  "stale",
  "watch",
  "learning",
  "error",
  "other",
] as const;

export function LogsPage() {
  const [follow, setFollow] = useState(true);
  const [klass, setKlass] = useState<string>("all");
  const [regex, setRegex] = useState("");
  const [events, setEvents] = useState<LogEvent[]>([]);
  const [error, setError] = useState<string | null>(null);
  const cursor = useRef(0);
  const trace = useLogTrace(true);

  useEffect(() => {
    let cancelled = false;
    const pull = async () => {
      try {
        const payload = await readLogEvents({
          cursor: cursor.current,
          limit: 200,
        });
        if (cancelled) return;
        cursor.current = payload.cursor;
        setEvents((current) => {
          const next = cursor.current && current.length ? [...current, ...payload.events] : payload.events;
          return next.slice(-400);
        });
        setError(null);
      } catch (err) {
        if (!cancelled) setError(err instanceof Error ? err.message : String(err));
      }
    };
    void pull();
    if (!follow) return () => {
      cancelled = true;
    };
    const timer = window.setInterval(() => void pull(), 4000);
    return () => {
      cancelled = true;
      window.clearInterval(timer);
    };
  }, [follow]);

  const compiled = useMemo(() => {
    if (!regex.trim()) return null;
    try {
      return new RegExp(regex, "i");
    } catch {
      return null;
    }
  }, [regex]);

  const visible = events.filter((event) => {
    if (klass !== "all" && event.klass !== klass) return false;
    if (compiled && !compiled.test(event.text)) return false;
    return true;
  });

  return (
    <div className="grid grid-cols-2 gap-4">
      <Card className="min-h-[70vh]">
        <CardHeader>
          <CardTitle>Events</CardTitle>
          <div className="flex items-center gap-2">
            <Button variant={follow ? "default" : "subtle"} size="sm" onClick={() => setFollow((value) => !value)}>
              {follow ? "Following" : "Follow"}
            </Button>
            <span className="font-mono text-[10px] text-faint">{visible.length}</span>
          </div>
        </CardHeader>
        <CardBody className="space-y-3">
          <Input value={regex} onChange={(event) => setRegex(event.target.value)} placeholder="Regex filter…" />
          <div className="flex flex-wrap gap-1.5">
            <Chip active={klass === "all"} onClick={() => setKlass("all")} label="all" />
            {CLASSES.map((item) => (
              <Chip key={item} active={klass === item} onClick={() => setKlass(item)} label={item.replaceAll("_", " ")} />
            ))}
          </div>
          {error ? <p className="text-sm text-loss">{error}</p> : null}
          {regex.trim() && !compiled ? <p className="text-xs text-warn">Invalid regex.</p> : null}
          <div className="max-h-[58vh] space-y-1 overflow-auto font-mono text-[11px] leading-relaxed">
            {visible.map((event, index) => (
              <p key={`${event.ts}-${index}`} className={tone(event.klass)}>
                {event.text}
              </p>
            ))}
          </div>
        </CardBody>
      </Card>
      <Card className="min-h-[70vh]">
        <CardHeader>
          <CardTitle>Agent trace</CardTitle>
          <Badge>{trace.data?.lines.length ?? 0}</Badge>
        </CardHeader>
        <CardBody>
          <pre className="max-h-[64vh] overflow-auto font-mono text-[11px] leading-relaxed text-dim">
            {(trace.data?.lines ?? []).join("\n") || "No agent_trace.log yet."}
          </pre>
        </CardBody>
      </Card>
    </div>
  );
}

function Chip({ active, onClick, label }: { active: boolean; onClick: () => void; label: string }) {
  return (
    <button
      type="button"
      onClick={onClick}
      className={cn(
        "rounded-sm px-2 py-1 font-mono text-[10px] uppercase tracking-[0.12em]",
        active ? "bg-accent/15 text-accent" : "text-faint hover:text-muted",
      )}
    >
      {label}
    </button>
  );
}

function tone(klass: string): string {
  if (klass === "decision_executed") return "text-gain";
  if (klass === "risk_reject" || klass === "error") return "text-loss";
  if (klass === "stale") return "text-warn";
  if (klass === "watch" || klass === "learning") return "text-accent";
  return "text-muted";
}
