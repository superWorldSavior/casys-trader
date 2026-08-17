import { Card, CardBody, CardHeader, CardTitle } from "@/components/ui/card";

type Props = {
  title: string;
  note: string;
};

export function ComingSoonPage({ title, note }: Props) {
  return (
    <Card className="mx-auto mt-10 max-w-xl">
      <CardHeader>
        <CardTitle>{title}</CardTitle>
        <span className="font-mono text-[10px] uppercase tracking-[0.16em] text-accent">skeleton</span>
      </CardHeader>
      <CardBody className="space-y-3 text-sm leading-relaxed text-muted">
        <p>{note}</p>
        <p className="text-dim">
          The TUI still owns this surface. The desktop app only reads <span className="font-mono text-xs">state/</span>{" "}
          through the Python snapshot bridge — no writes, no ACPX, no broker.
        </p>
      </CardBody>
    </Card>
  );
}
