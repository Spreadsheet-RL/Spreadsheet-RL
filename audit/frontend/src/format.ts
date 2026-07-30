import type { AuditSplit } from "../../shared/types";

export const EM_DASH = "—";

export const SPLIT_LABELS: Record<AuditSplit, string> = {
  training: "Training",
  domain: "Domain",
};

export const SPLIT_ORDER: readonly AuditSplit[] = ["training", "domain"];

/** Fractions from the API are `null` when no completed response exists yet. */
export function formatFraction(value: number | null): string {
  if (value === null || !Number.isFinite(value)) return EM_DASH;
  return `${Math.round(value * 100)}%`;
}

export function formatCount(value: number | null): string {
  return value === null ? EM_DASH : String(value);
}

export function formatTimestamp(value: string): string {
  if (!value) return EM_DASH;
  // The worker stores `CURRENT_TIMESTAMP`, i.e. "YYYY-MM-DD HH:MM:SS" in UTC.
  const normalized = value.includes("T") ? value : `${value.replace(" ", "T")}Z`;
  const parsed = new Date(normalized);
  if (Number.isNaN(parsed.getTime())) return value;
  return parsed.toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" });
}

export function errorMessage(caught: unknown): string {
  if (caught instanceof Error && caught.message) return caught.message;
  return "Something went wrong. Please try again.";
}
