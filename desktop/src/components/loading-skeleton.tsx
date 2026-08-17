import { Card, CardBody } from "@/components/ui/card";

export function LoadingSkeleton() {
  return (
    <div className="grid gap-4">
      <div className="grid grid-cols-4 gap-3">
        {Array.from({ length: 4 }, (_, index) => (
          <Card key={index}>
            <CardBody className="space-y-3">
              <div className="h-2 w-16 animate-pulse rounded bg-hairline" />
              <div className="h-7 w-28 animate-pulse rounded bg-line" />
            </CardBody>
          </Card>
        ))}
      </div>
      <Card className="h-[320px]">
        <CardBody>
          <div className="h-full animate-pulse rounded-md bg-hairline" />
        </CardBody>
      </Card>
    </div>
  );
}
