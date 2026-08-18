import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardHeader, CardTitle } from "@/components/ui/card";
import { useUniverse } from "@/hooks/use-desk-api";
import { notWired } from "@/lib/not-wired";
import type { UniverseRow } from "@/lib/types";
import { cn } from "@/lib/utils";

const VENUES = ["TW", "EU", "US"] as const;

type Props = {
  onSymbol: (symbol: string) => void;
};

export function UniversePage({ onSymbol }: Props) {
  const query = useUniverse();
  const rows = query.data?.rows ?? [];
  const known = new Set<string>(VENUES);
  const grouped: { venue: string; rows: UniverseRow[] }[] = VENUES.map((venue) => ({
    venue,
    rows: rows.filter((row) => row.venue === venue),
  }));
  const other = rows.filter((row) => !known.has(row.venue));
  if (other.length) grouped.push({ venue: "OTHER", rows: other });
  const pinned = new Set(query.data?.overrides.pinned ?? []);
  const banned = new Set(query.data?.overrides.banned ?? []);

  return (
    <div className="grid gap-4">
      <div className="flex flex-wrap items-center gap-2">
        <Badge>{rows.length} symbols</Badge>
        <Badge tone="accent">{(query.data?.overrides.pinned ?? []).length} pinned</Badge>
        <Badge tone="loss">{(query.data?.overrides.banned ?? []).length} banned</Badge>
        {Object.entries(query.data?.hotset ?? {}).map(([venue, list]) => (
          <Badge key={venue}>
            {venue} hot {list.length}
          </Badge>
        ))}
      </div>
      {query.error ? (
        <p className="text-sm text-loss">{query.error instanceof Error ? query.error.message : String(query.error)}</p>
      ) : null}
      {grouped.map((group) => (
        <Card key={group.venue}>
          <CardHeader>
            <CardTitle>{group.venue}</CardTitle>
            <span className="font-mono text-[10px] text-faint">{group.rows.length}</span>
          </CardHeader>
          <div className="grid grid-cols-[110px_1fr_90px_70px_140px_90px_90px_160px] gap-3 border-b border-hairline px-4 py-2 font-mono text-[10px] uppercase tracking-[0.14em] text-faint">
            <span>Sym</span>
            <span>Name</span>
            <span>State</span>
            <span>Pos</span>
            <span>Last</span>
            <span>Wake</span>
            <span>Data</span>
            <span>Actions</span>
          </div>
          {group.rows.map((row) => (
            <UniverseLine
              key={row.symbol}
              row={row}
              pinned={pinned.has(row.symbol)}
              banned={banned.has(row.symbol)}
              onSymbol={onSymbol}
            />
          ))}
          {group.rows.length === 0 ? <p className="px-4 py-6 text-sm text-faint">Empty venue.</p> : null}
        </Card>
      ))}
    </div>
  );
}

function UniverseLine({
  row,
  pinned,
  banned,
  onSymbol,
}: {
  row: UniverseRow;
  pinned: boolean;
  banned: boolean;
  onSymbol: (symbol: string) => void;
}) {
  return (
    <div className="grid grid-cols-[110px_1fr_90px_70px_140px_90px_90px_160px] items-center gap-3 border-b border-hairline px-4 py-2 last:border-0">
      <button type="button" className="text-left text-sm font-medium hover:text-accent" onClick={() => onSymbol(row.symbol)}>
        {row.symbol}
      </button>
      <p className="truncate text-sm text-muted">{row.name}</p>
      <span className="font-mono text-[11px] text-dim">{row.state_label}</span>
      <span className="font-mono text-[11px] text-muted">{row.pos}</span>
      <span className="truncate font-mono text-[11px] text-muted">{row.last_decision}</span>
      <span className={cn("font-mono text-[11px]", row.wake_urgent ? "text-warn" : "text-faint")}>{row.wake_text}</span>
      <span className={cn("font-mono text-[11px]", row.data_stale ? "text-warn" : "text-faint")}>{row.data_text}</span>
      <div className="flex gap-1">
        <Button variant="subtle" size="sm" onClick={() => notWired(`Pin ${row.symbol}`)}>
          {pinned ? "Pinned" : "Pin"}
        </Button>
        <Button variant="subtle" size="sm" onClick={() => notWired(`Ban ${row.symbol}`)}>
          {banned ? "Banned" : "Ban"}
        </Button>
        <Button variant="ghost" size="sm" onClick={() => notWired(`Undo override ${row.symbol}`)}>
          Undo
        </Button>
      </div>
    </div>
  );
}
