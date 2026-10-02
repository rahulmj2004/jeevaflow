const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

/** "2026-06-15" -> "15 Jun 2026" without timezone shifts. */
export function formatDate(value: string | null | undefined): string {
  if (!value) return "—";
  const match = /^(\d{4})-(\d{2})-(\d{2})/.exec(value);
  if (!match) return value;
  const [, year, month, day] = match;
  return `${Number(day)} ${MONTHS[Number(month) - 1]} ${year}`;
}

/** Backend timestamps are naive UTC. */
export function formatDateTime(value: string | null | undefined): string {
  if (!value) return "—";
  const date = new Date(/[zZ]|[+-]\d\d:\d\d$/.test(value) ? value : `${value}Z`);
  if (Number.isNaN(date.getTime())) return value;
  return date.toLocaleString(undefined, {
    day: "numeric",
    month: "short",
    hour: "2-digit",
    minute: "2-digit",
  });
}

export function humanize(value: string | null | undefined): string {
  if (!value) return "—";
  return value
    .toLowerCase()
    .split("_")
    .map((word) => word.charAt(0).toUpperCase() + word.slice(1))
    .join(" ");
}

export function formatValue(value: string, unit: string | null): string {
  if (!unit) return value;
  return unit === "%" ? `${value}%` : `${value} ${unit}`;
}

export type Tone = "neutral" | "info" | "success" | "warning" | "danger" | "accent";

const TONES: Record<string, Tone> = {
  OPEN: "info",
  OVERDUE: "danger",
  POTENTIAL_MATCH: "accent",
  CLOSED: "success",
  NEEDS_REVIEW: "warning",
  REVIEW: "warning",
  VERIFIED: "success",
  REJECTED: "danger",
  PROCESSED: "success",
  PROCESSING: "info",
  FAILED: "danger",
  DUPLICATE: "neutral",
  GOOD: "success",
  WARN: "warning",
  RETAKE: "danger",
  PENDING: "accent",
  CONFIRMED: "success",
  DISMISSED: "neutral",
  TEXT_ONLY: "neutral",
  UNKNOWN_PATIENT: "danger",
  UNSUPPORTED_MEDIA: "warning",
  FILE_TOO_LARGE: "warning",
  PARTIAL: "warning",
  MALFORMED: "danger",
  MISSING_SENDER: "danger",
  CONFLICT: "danger",
  WHATSAPP: "success",
  WEB: "neutral",
  CAMERA: "neutral",
  DEMO: "neutral",
};

export function toneFor(status: string | null | undefined): Tone {
  return (status && TONES[status]) || "neutral";
}

const LABELS: Record<string, string> = {
  POTENTIAL_MATCH: "Potential match",
  NEEDS_REVIEW: "Needs review",
  REVIEW: "Review",
  RETAKE: "Retake",
  WARN: "Warn",
};

export function statusLabel(status: string | null | undefined): string {
  if (!status) return "—";
  return LABELS[status] ?? humanize(status);
}
