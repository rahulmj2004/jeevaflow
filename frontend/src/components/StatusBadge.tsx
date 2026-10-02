import { statusLabel, toneFor } from "../format";

export function StatusBadge({ status, label }: { status: string | null | undefined; label?: string }) {
  if (!status) return null;
  return <span className={`badge badge-${toneFor(status)}`}>{label ?? statusLabel(status)}</span>;
}

export function SourceIcon({ source }: { source: string | null | undefined }) {
  const label =
    source === "WHATSAPP" ? "WhatsApp" : source === "CAMERA" ? "Camera" : source === "DEMO" ? "Demo" : "Upload";
  return <span className={`source source-${(source ?? "WEB").toLowerCase()}`}>{label}</span>;
}

export function ErrorBanner({ message, onRetry }: { message: string; onRetry?: () => void }) {
  return (
    <div className="banner banner-danger" role="alert">
      <span>{message}</span>
      {onRetry && (
        <button type="button" className="btn btn-small" onClick={onRetry}>
          Retry
        </button>
      )}
    </div>
  );
}

export function EmptyState({ title, children }: { title: string; children?: React.ReactNode }) {
  return (
    <div className="empty">
      <p className="empty-title">{title}</p>
      {children && <p className="empty-body">{children}</p>}
    </div>
  );
}
