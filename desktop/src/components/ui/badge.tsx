import { cva, type VariantProps } from "class-variance-authority";
import type { HTMLAttributes } from "react";
import { cn } from "@/lib/utils";

const badgeVariants = cva(
  "inline-flex items-center rounded-sm px-1.5 py-0.5 font-mono text-[10px] uppercase tracking-[0.14em]",
  {
    variants: {
      tone: {
        muted: "bg-hairline text-dim",
        accent: "bg-accent/15 text-accent",
        gain: "bg-gain/15 text-gain",
        loss: "bg-loss/15 text-loss",
        warn: "bg-warn/15 text-warn",
      },
    },
    defaultVariants: { tone: "muted" },
  },
);

type BadgeProps = HTMLAttributes<HTMLSpanElement> & VariantProps<typeof badgeVariants>;

export function Badge({ className, tone, ...props }: BadgeProps) {
  return <span className={cn(badgeVariants({ tone }), className)} {...props} />;
}
