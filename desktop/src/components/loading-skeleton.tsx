import { Card, CardBody } from "@/components/ui/card";

export function LoadingSkeleton() {
  return (
    <div className="grid gap-4" role="status" aria-label="Loading Casys intelligence">
      <span className="sr-only">Loading Casys intelligence</span>
      <Card className="min-h-[280px]">
        <CardBody className="grid h-full gap-5 p-6 min-[1180px]:grid-cols-[minmax(0,1fr)_300px]">
          <div className="space-y-5">
            <div className="h-2 w-44 animate-pulse rounded bg-hairline motion-reduce:animate-none" />
            <div className="h-9 w-64 animate-pulse rounded bg-line motion-reduce:animate-none" />
            <div className="space-y-2">
              <div className="h-3 w-full animate-pulse rounded bg-hairline motion-reduce:animate-none" />
              <div className="h-3 w-11/12 animate-pulse rounded bg-hairline motion-reduce:animate-none" />
              <div className="h-3 w-8/12 animate-pulse rounded bg-hairline motion-reduce:animate-none" />
            </div>
          </div>
          <div className="animate-pulse rounded-lg bg-ink motion-reduce:animate-none" />
        </CardBody>
      </Card>
      <div className="grid gap-4 min-[1180px]:grid-cols-[minmax(0,1.35fr)_minmax(320px,0.65fr)]">
        <Card className="h-[300px] animate-pulse bg-panel/70 motion-reduce:animate-none" />
        <div className="grid gap-4">
          <Card className="h-[142px] animate-pulse bg-panel/70 motion-reduce:animate-none" />
          <Card className="h-[142px] animate-pulse bg-panel/70 motion-reduce:animate-none" />
        </div>
      </div>
      <Card className="h-16 animate-pulse bg-panel/70 motion-reduce:animate-none" />
    </div>
  );
}
