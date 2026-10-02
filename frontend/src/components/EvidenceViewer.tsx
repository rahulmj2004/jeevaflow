import { useEffect, useState } from "react";
import { api } from "../api";
import { formatDate, formatDateTime } from "../format";
import type { Evidence } from "../types";
import { SecureEvidence } from "./SecureEvidence";
import { EmptyState, ErrorBanner, SourceIcon, StatusBadge } from "./StatusBadge";

export interface EvidenceSelection {
  key: string;
  kind: "OBSERVATION" | "COMMITMENT" | "MATCH" | "CONFLICT";
  heading: string;
  subheading?: string;
  observationId?: number;
  reviewStatus?: string | null;
  evidence: Evidence;
}

interface Props {
  selection: EvidenceSelection | null;
  canViewSource: boolean;
  onReviewed: () => void;
}

export function EvidenceViewer({ selection, canViewSource, onReviewed }: Props) {
  const [detail, setDetail] = useState<Evidence | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [reloadToken, setReloadToken] = useState(0);

  const evidenceId = selection?.evidence.id;

  useEffect(() => {
    if (evidenceId === undefined) return;
    let cancelled = false;
    setLoading(true);
    setError(null);
    setActionError(null);
    api
      .evidence(evidenceId)
      .then((value) => !cancelled && setDetail(value))
      .catch((err: Error) => !cancelled && setError(err.message))
      .finally(() => !cancelled && setLoading(false));
    return () => {
      cancelled = true;
    };
  }, [evidenceId, reloadToken]);

  if (!selection) {
    return (
      <EmptyState title="Select an item to inspect its source">
        Every observation and instruction links to the exact quote it was read from.
      </EmptyState>
    );
  }

  const evidence = detail?.id === selection.evidence.id ? detail : selection.evidence;
  const observationLink = detail?.linked_items?.find(
    (item) => item.type === "OBSERVATION" && item.id === selection.observationId,
  );
  const reviewStatus = observationLink?.review_status ?? selection.reviewStatus ?? null;

  async function review(action: "verify" | "reject") {
    if (selection?.observationId === undefined) return;
    setBusy(true);
    setActionError(null);
    try {
      if (action === "verify") await api.verifyObservation(selection.observationId);
      else await api.rejectObservation(selection.observationId);
      setReloadToken((token) => token + 1);
      onReviewed();
    } catch (err) {
      setActionError((err as Error).message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="evidence" aria-live="polite">
      <div className="evidence-head">
        <div>
          <p className="eyebrow">{selection.kind === "COMMITMENT" ? "Doctor instruction" : "Source evidence"}</p>
          <h3 className="evidence-title">{selection.heading}</h3>
          {selection.subheading && <p className="muted">{selection.subheading}</p>}
        </div>
        {reviewStatus && <StatusBadge status={reviewStatus} />}
      </div>

      {error && <ErrorBanner message={`Could not load evidence: ${error}`} onRetry={() => setReloadToken((t) => t + 1)} />}

      <blockquote className="quote">
        <span className="quote-mark" aria-hidden="true">“</span>
        {evidence.quote}
      </blockquote>

      <dl className="evidence-grid">
        <div>
          <dt>Source</dt>
          <dd className="evidence-file">{evidence.document_filename ?? "Shared document"}</dd>
        </div>
        <div>
          <dt>Page</dt>
          <dd>{evidence.page_number ?? "—"}</dd>
        </div>
        <div>
          <dt>Evidence ID</dt>
          <dd>#{evidence.id}</dd>
        </div>
        <div>
          <dt>Confidence</dt>
          <dd>{evidence.confidence != null ? `${Math.round(evidence.confidence * 100)}%` : "—"}</dd>
        </div>
        <div>
          <dt>Report date</dt>
          <dd>{formatDate(evidence.document_date)}</dd>
        </div>
        <div>
          <dt>Received via</dt>
          <dd>
            <SourceIcon source={evidence.document_source} /> {formatDateTime(evidence.document_received_at)}
          </dd>
        </div>
        <div>
          <dt>Document SHA-256</dt>
          <dd className="mono">{evidence.document_sha256 ? `${evidence.document_sha256.slice(0, 12)}…` : "—"}</dd>
        </div>
        <div>
          <dt>Pipeline</dt>
          <dd>{evidence.pipeline_version ?? "—"}</dd>
        </div>
      </dl>

      <SecureEvidence evidenceId={evidence.id} canView={canViewSource} />

      {loading && <p className="muted small">Loading linked items…</p>}
      {detail?.linked_items && detail.linked_items.length > 0 && (
        <div className="linked">
          <p className="eyebrow">Linked to this quote</p>
          <ul>
            {detail.linked_items.map((item) => (
              <li key={`${item.type}-${item.id}`}>
                <span>{item.label}</span>
                <StatusBadge status={item.review_status ?? item.state ?? null} />
              </li>
            ))}
          </ul>
        </div>
      )}

      {selection.observationId !== undefined && (
        <div className="review-actions">
          <p className="small muted">
            Extracted values stay in <strong>Review</strong> until a person checks them against the source.
          </p>
          {actionError && <ErrorBanner message={actionError} />}
          <div className="btn-row">
            <button
              type="button"
              className="btn btn-primary"
              disabled={busy || reviewStatus === "VERIFIED"}
              onClick={() => review("verify")}
            >
              Verify against source
            </button>
            <button
              type="button"
              className="btn btn-ghost-danger"
              disabled={busy || reviewStatus === "REJECTED"}
              onClick={() => review("reject")}
            >
              Reject extraction
            </button>
          </div>
        </div>
      )}
    </div>
  );
}
