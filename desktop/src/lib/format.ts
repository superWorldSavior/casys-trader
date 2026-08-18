import { format, formatDistanceToNowStrict, parseISO } from "date-fns";

export function parseTs(value: string | null | undefined): Date | null {
  if (!value) return null;
  try {
    const date = parseISO(value.endsWith("Z") ? value : value.replace(/ /, "T"));
    return Number.isNaN(date.getTime()) ? null : date;
  } catch {
    return null;
  }
}

export function formatClock(value: string | null | undefined): string {
  const date = parseTs(value);
  if (!date) return "—";
  return format(date, "HH:mm:ss");
}

export function formatDayTime(value: string | null | undefined): string {
  const date = parseTs(value);
  if (!date) return "—";
  return format(date, "dd MMM HH:mm");
}

export function formatAgo(value: string | null | undefined): string {
  const date = parseTs(value);
  if (!date) return "—";
  return formatDistanceToNowStrict(date, { addSuffix: true });
}

export function formatUsd(value: number | null | undefined, digits = 0): string {
  if (value == null || Number.isNaN(value)) return "—";
  return new Intl.NumberFormat("en-US", {
    style: "currency",
    currency: "USD",
    maximumFractionDigits: digits,
    minimumFractionDigits: digits,
  }).format(value);
}

export function formatPct(value: number | null | undefined, digits = 2): string {
  if (value == null || Number.isNaN(value)) return "—";
  const sign = value > 0 ? "+" : "";
  return `${sign}${value.toFixed(digits)}%`;
}

export function formatQty(value: number | null | undefined): string {
  if (value == null || Number.isNaN(value)) return "—";
  return new Intl.NumberFormat("en-US", { maximumFractionDigits: 2 }).format(value);
}

export function signedClass(value: number | null | undefined): string {
  if (value == null || value === 0) return "text-dim";
  return value > 0 ? "text-gain" : "text-loss";
}

/** Human-readable countdown from a float hours value (e.g. 50.5 → "in 2d 2h"). */
export function formatCountdown(in_h: number): string {
  if (in_h < 0) return "past";
  if (in_h < 1) {
    const mins = Math.round(in_h * 60);
    return mins <= 0 ? "now" : `in ${mins}m`;
  }
  const totalHours = Math.round(in_h);
  if (totalHours < 24) return `in ${totalHours}h`;
  const days = Math.floor(totalHours / 24);
  const hours = totalHours % 24;
  return hours === 0 ? `in ${days}d` : `in ${days}d ${hours}h`;
}
