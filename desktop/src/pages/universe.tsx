import { Badge } from "@/components/ui/badge";
import { Card, CardHeader, CardTitle } from "@/components/ui/card";
import { useUniverse } from "@/hooks/use-desk-api";
import { humanToken, venueLabel } from "@/lib/humanize";
import type { UniverseRow } from "@/lib/types";
import { cn } from "@/lib/utils";

const VENUES = ["TW", "EU", "US"] as const;

type Props = {
  onSymbol: (symbol: string) => void;
};

export function UniversePage({ onSymbol }: Props) {
  const query = useUniverse();
  if (query.error && !query.data) {
    return (
      <div className="grid gap-3">
        <p className="text-sm text-loss">The tracked-company list is temporarily unavailable.</p>
        <details className="text-xs text-dim">
          <summary className="cursor-pointer">Technical details</summary>
          <p className="mt-1 break-words font-mono text-[10px] text-faint">
            {query.error instanceof Error ? query.error.message : String(query.error)}
          </p>
        </details>
      </div>
    );
  }
  if (!query.data) {
    return <p className="text-sm text-faint" role="status">Loading tracked companies…</p>;
  }

  const rows = query.data.rows;
  const known = new Set<string>(VENUES);
  const grouped: { venue: string; rows: UniverseRow[] }[] = VENUES.map((venue) => ({
    venue,
    rows: rows.filter((row) => row.venue === venue),
  }));
  const other = rows.filter((row) => !known.has(row.venue));
  if (other.length) grouped.push({ venue: "OTHER", rows: other });
  return (
    <div className="grid gap-4">
      <div className="flex flex-wrap items-center gap-2">
        <Badge>{rows.length} total</Badge>
        <Badge tone="accent">{query.data.overrides.pinned.length} manually included</Badge>
        <Badge tone="loss">{query.data.overrides.banned.length} excluded</Badge>
        {Object.entries(query.data.hotset).map(([venue, list]) => (
          <Badge key={venue}>
            {venueLabel(venue)} · {list.length} in focus
          </Badge>
        ))}
      </div>
      {query.error ? (
        <p className="text-xs text-warn">Showing the last recorded list; the latest refresh was unavailable.</p>
      ) : null}
      {grouped.map((group) => (
        <Card key={group.venue}>
          <CardHeader>
            <CardTitle>{venueLabel(group.venue)}</CardTitle>
            <span className="font-mono text-[10px] text-faint">{group.rows.length}</span>
          </CardHeader>
          <div className="overflow-x-auto">
          <div className="min-w-[940px]">
          <div className="grid grid-cols-[minmax(220px,1.3fr)_120px_110px_100px_minmax(170px,1fr)_120px_120px] gap-3 border-b border-hairline px-4 py-2 font-mono text-[10px] uppercase tracking-[0.14em] text-faint">
            <span>Company</span>
            <span>Region</span>
            <span>Status</span>
            <span>Position</span>
            <span>Latest outcome</span>
            <span>Next review</span>
            <span>Data</span>
          </div>
          {group.rows.map((row) => (
            <UniverseLine
              key={row.symbol}
              row={row}
              onSymbol={onSymbol}
            />
          ))}
          {group.rows.length === 0 ? <p className="px-4 py-6 text-sm text-faint">No companies are tracked here.</p> : null}
          </div>
          </div>
        </Card>
      ))}
    </div>
  );
}

function UniverseLine({
  row,
  onSymbol,
}: {
  row: UniverseRow;
  onSymbol: (symbol: string) => void;
}) {
  return (
    <button
      type="button"
      className="grid w-full grid-cols-[minmax(220px,1.3fr)_120px_110px_100px_minmax(170px,1fr)_120px_120px] items-center gap-3 border-b border-hairline px-4 py-2 text-left last:border-0 hover:bg-panel-hover focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-accent/50"
      onClick={() => onSymbol(row.symbol)}
    >
      <div className="min-w-0">
        <p className="truncate text-sm font-medium text-fg">{row.name || row.symbol}</p>
        <p className="font-mono text-[10px] text-faint">{row.symbol}</p>
      </div>
      <span className="text-xs text-dim">{venueLabel(row.venue)}</span>
      <span className="text-xs text-dim">{humanToken(row.state_label)}</span>
      <span className="text-xs text-muted">{positionLabel(row.pos)}</span>
      <span className="truncate font-mono text-[11px] text-muted">{row.last_decision}</span>
      <span className={cn("font-mono text-[11px]", row.wake_urgent ? "text-warn" : "text-faint")}>{row.wake_text}</span>
      <span className={cn("font-mono text-[11px]", row.data_stale ? "text-warn" : "text-faint")}>{row.data_text}</span>
    </button>
  );
}

function positionLabel(value: string): string {
  const key = value.toUpperCase();
  if (["L", "LONG"].includes(key)) return "Long position";
  if (["S", "SHORT"].includes(key)) return "Short position";
  if (["FLAT", "NONE", "—", ""].includes(key)) return "No position";
  return humanToken(value);
}
