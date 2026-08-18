import { useMemo, useState } from "react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardBody, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { useSettings } from "@/hooks/use-desk-api";
import { notWired } from "@/lib/not-wired";

export function SettingsPage() {
  const query = useSettings();
  const data = query.data;
  const [draft, setDraft] = useState<Record<string, string>>({});
  const values = useMemo(() => {
    if (!data) return {};
    const next: Record<string, string> = {};
    for (const row of data.editable) {
      const source = row.yaml_file === "portfolio" ? data.portfolio : data.radar;
      next[row.key] = String(source[row.key] ?? "");
    }
    return next;
  }, [data]);

  if (query.error) {
    return <p className="text-sm text-loss">{query.error instanceof Error ? query.error.message : String(query.error)}</p>;
  }
  if (!data) return <p className="text-sm text-faint">Loading settings…</p>;

  const budget = data.editable.filter((row) => row.yaml_file === "portfolio");
  const rotation = data.editable.filter((row) => row.yaml_file === "radar");

  return (
    <div className="grid grid-cols-2 gap-4">
      <div className="grid gap-4">
        <Card>
          <CardHeader>
            <CardTitle>Runtime</CardTitle>
            {data.kill_active ? <Badge tone="loss">kill file present</Badge> : <Badge>kill off</Badge>}
          </CardHeader>
          <CardBody className="space-y-1.5">
            {Object.entries(data.env).map(([key, value]) => (
              <div key={key} className="flex justify-between gap-4 font-mono text-[11px]">
                <span className="text-faint">{key}</span>
                <span className="truncate text-muted">{value}</span>
              </div>
            ))}
            {Object.keys(data.env).length === 0 ? <p className="text-sm text-faint">No .env keys to show.</p> : null}
          </CardBody>
        </Card>
        <Card>
          <CardHeader>
            <CardTitle>Cycle / budget</CardTitle>
            <Button size="sm" onClick={() => notWired("Write portfolio.yaml")}>
              Write
            </Button>
          </CardHeader>
          <CardBody className="space-y-3">
            {budget.map((row) => (
              <Field
                key={row.key}
                name={row.key}
                label={row.label}
                effect={row.effect}
                value={draft[row.key] ?? values[row.key] ?? ""}
                onChange={(value) => setDraft((current) => ({ ...current, [row.key]: value }))}
              />
            ))}
          </CardBody>
        </Card>
      </div>
      <div className="grid gap-4">
        <Card>
          <CardHeader>
            <CardTitle>Data</CardTitle>
            <Badge>restart required</Badge>
          </CardHeader>
          <CardBody className="space-y-1.5">
            <Row label="profile" value={String(data.data_sources.profile ?? "—")} />
            <pre className="max-h-40 overflow-auto text-xs text-dim">
              {JSON.stringify(data.data_sources, null, 2)}
            </pre>
          </CardBody>
        </Card>
        <Card>
          <CardHeader>
            <CardTitle>Rotation</CardTitle>
            <Button size="sm" onClick={() => notWired("Write radar.yaml")}>
              Write
            </Button>
          </CardHeader>
          <CardBody className="space-y-3">
            {rotation.map((row) => (
              <Field
                key={row.key}
                name={row.key}
                label={row.label}
                effect={row.effect}
                value={draft[row.key] ?? values[row.key] ?? ""}
                onChange={(value) => setDraft((current) => ({ ...current, [row.key]: value }))}
              />
            ))}
          </CardBody>
        </Card>
        <Card>
          <CardHeader>
            <CardTitle>Risk</CardTitle>
            <Badge>read-only</Badge>
          </CardHeader>
          <CardBody className="space-y-1.5">
            {Object.entries(data.risk).map(([key, value]) => (
              <Row key={key} label={key.replaceAll("_", " ")} value={String(value)} />
            ))}
          </CardBody>
        </Card>
      </div>
    </div>
  );
}

function Field({
  name,
  label,
  effect,
  value,
  onChange,
}: {
  name: string;
  label: string;
  effect: string;
  value: string;
  onChange: (value: string) => void;
}) {
  return (
    <label className="block space-y-1">
      <div className="flex items-center justify-between">
        <span className="text-sm text-muted">{label}</span>
        <span className="font-mono text-[10px] text-faint">{effect}</span>
      </div>
      <Input name={name} value={value} onChange={(event) => onChange(event.target.value)} />
    </label>
  );
}

function Row({ label, value }: { label: string; value: string }) {
  return (
    <div className="flex justify-between gap-4 font-mono text-[11px]">
      <span className="text-faint">{label}</span>
      <span className="truncate text-muted">{value}</span>
    </div>
  );
}
