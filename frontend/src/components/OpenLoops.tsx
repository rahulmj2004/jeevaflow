import { useState } from "react";
import { api } from "../api";
import { formatDate, formatDateTime, formatValue, humanize } from "../format";
import type { Evidence, LoopHistoryEntry, LoopMatch, OpenLoop } from "../types";
import type { EvidenceSelection } from "./EvidenceViewer";
import { EmptyState, ErrorBanner, SourceIcon, StatusBadge } from "./StatusBadge";

interface Props {
  loops: OpenLoop[];
  onChanged: () => void;
  onInspect: (selection: EvidenceSelection) => void;
}

const ORDER: Record<string, number> = {
  POTENTIAL_MATCH: 0,
  OVERDUE: 1,
  NEEDS_REVIEW: 2,
  OPEN: 3,
  CLOSED: 4,
};

function EvidenceLink({
  evidence,
  label,
  onClick,
}: {
  evidence: Evidence | null;
  label: string;
  onClick: () => void;
}) {
  if (!evidence) return <span className="muted small">No source evidence</span>;
  return (
    <button type="button" className="evidence-link" onClick={onClick}>
      <span className="evidence-chip">Evidence #{evidence.id}</span>
      <span>{label}</span>
    </button>
  );
}

function History({ loopId }: { loopId: number }) {
  const [entries, setEntries] = useState<LoopHistoryEntry[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [open, setOpen] = useState(false);

  async function toggle() {
    const next = !open;
    setOpen(next);
    if (next) {
      setError(null);
      try {
        setEntries((await api.loopDetail(loopId)).history ?? []);
      } catch (err) {
        setError((err as Error).message);
      }
    }
  }

  return (
    <div className="history">
      <button type="button" className="link-button small" onClick={toggle} aria-expanded={open}>
        {open ? "Hide audit trail" : "Show audit trail"}
      </button>
      {open && error && <ErrorBanner message={error} />}
      {open && entries && entries.length === 0 && <p className="small muted">No changes recorded yet.</p>}
      {open && entries && entries.length > 0 && (
        <ol className="history-list">
          {entries.map((entry) => (
            <li key={entry.id}>
              <span className={`actor actor-${entry.actor_type.toLowerCase()}`}>{entry.actor_type}</span>
              <span>
                {humanize(entry.action)}
                {entry.actor_name ? ` · ${entry.actor_name}` : ""}
              </span>
              <span className="muted small">{formatDateTime(entry.created_at)}</span>
            </li>
          ))}
        </ol>
      )}
    </div>
  );
}

function LoopCard({ loop, onChanged, onInspect }: { loop: OpenLoop } & Omit<Props, "loops">) {
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  const match = loop.potential_matches[0];

  // The decision is recorded under the signed-in doctor's identity.
  async function act(action: "confirm" | "keep" | "review") {
    setBusy(action);
    setError(null);
    try {
      if (action === "confirm") await api.confirmCompletion(loop.id, match?.id);
      else if (action === "keep") await api.keepOpen(loop.id);
      else await api.flagForReview(loop.id);
      onChanged();
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setBusy(null);
    }
  }

  const inspectInstruction = () =>
    loop.evidence &&
    onInspect({
      key: `COMMITMENT-${loop.id}`,
      kind: "COMMITMENT",
      heading: loop.instruction,
      subheading: `Open loop #${loop.id}`,
      evidence: loop.evidence,
    });

  const inspectMatch = (m: NonNullable<typeof match>) =>
    m.evidence &&
    onInspect({
      key: `MATCH-${m.id}`,
      kind: "MATCH",
      heading: m.observation
        ? `${m.observation.observation_type} ${formatValue(m.observation.value, m.observation.unit)}`
        : "Supporting evidence",
      subheading: `New evidence for: ${loop.instruction}`,
      observationId: m.observation?.id,
      reviewStatus: m.observation?.review_status,
      evidence: m.evidence,
    });

  return (
    <article className={`loop loop-${loop.state.toLowerCase()}`}>
      <header className="loop-head">
        <div>
          <p className="eyebrow">Doctor instruction · Open loop #{loop.id}</p>
          <h3 className="loop-instruction">{loop.instruction}</h3>
        </div>
        <StatusBadge status={loop.state} />
      </header>

      <dl className="loop-meta">
        <div>
          <dt>Due</dt>
          <dd>{loop.due_date ? formatDate(loop.due_date) : "No interval stated"}</dd>
        </div>
        <div>
          <dt>From</dt>
          <dd>
            {loop.document_filename ?? "—"}
            {loop.document_date ? ` · ${formatDate(loop.document_date)}` : ""}
          </dd>
        </div>
      </dl>

      <EvidenceLink
        evidence={loop.evidence}
        label={`“${loop.evidence?.quote ?? ""}” · p.${loop.evidence?.page_number ?? "—"}`}
        onClick={inspectInstruction}
      />

      {match && loop.state !== "CLOSED" && (
        <div className="potential" role="region" aria-label="Potential completion">
          <p className="potential-title">
            <span className="pulse" aria-hidden="true" /> Potential completion detected
          </p>
          <div className="potential-grid">
            <div className="potential-side">
              <p className="eyebrow">Existing instruction</p>
              <p className="potential-text">{loop.instruction}</p>
              <p className="muted small">
                {loop.document_filename} · {formatDate(loop.document_date)}
              </p>
            </div>
            <div className="potential-plus" aria-hidden="true">
              +
            </div>
            <div className="potential-side">
              <p className="eyebrow">New supporting evidence</p>
              {match.observation && (
                <p className="potential-value">
                  {match.observation.observation_type} {formatValue(match.observation.value, match.observation.unit)}
                </p>
              )}
              <p className="muted small">
                <SourceIcon source={match.document_source} /> {match.document_filename}
                {match.observation?.event_date ? ` · ${formatDate(match.observation.event_date)}` : ""}
              </p>
              <EvidenceLink
                evidence={match.evidence}
                label={`“${match.evidence?.quote ?? ""}”`}
                onClick={() => inspectMatch(match)}
              />
            </div>
          </div>
          {match.method === "AI_SEMANTIC" && match.explanation ? (
            <AiMatchExplanation match={match} />
          ) : (
            <p className="small muted rule">Why suggested: {match.rule}</p>
          )}
          <p className="small">
            JeevaFlow does not judge the result. A person decides whether this fulfils the instruction.
          </p>
          {error && <ErrorBanner message={error} />}
          <div className="btn-row">
            <button type="button" className="btn btn-primary" disabled={busy !== null} onClick={() => act("confirm")}>
              {busy === "confirm" ? "Confirming…" : "Confirm completion"}
            </button>
            <button type="button" className="btn" disabled={busy !== null} onClick={() => act("keep")}>
              {busy === "keep" ? "Saving…" : "Keep open"}
            </button>
            <button
              type="button"
              className="btn"
              disabled={busy !== null || loop.state === "NEEDS_REVIEW"}
              onClick={() => act("review")}
            >
              {busy === "review" ? "Saving…" : "Review"}
            </button>
          </div>
        </div>
      )}

      {!match && loop.state !== "CLOSED" && (
        <p className="small muted">Waiting for a newer document that addresses this instruction.</p>
      )}

      {loop.state === "CLOSED" && (
        <div className="closed-note">
          {loop.confirmed_match ? (
            <>
              <span>
                Closed by <strong>{loop.confirmed_match.reviewed_by}</strong> on{" "}
                {formatDateTime(loop.confirmed_match.reviewed_at)} — confirmed with
              </span>
              <EvidenceLink
                evidence={loop.confirmed_match.evidence}
                label={`“${loop.confirmed_match.evidence?.quote ?? ""}”`}
                onClick={() => inspectMatch(loop.confirmed_match!)}
              />
            </>
          ) : (
            <span>Closed manually by a person.</span>
          )}
        </div>
      )}

      <History key={loop.updated_at} loopId={loop.id} />
    </article>
  );
}

export function OpenLoops({ loops, onChanged, onInspect }: Props) {
  if (loops.length === 0) {
    return (
      <EmptyState title="No open loops">Doctor instructions found in documents will be tracked here.</EmptyState>
    );
  }

  const sorted = [...loops].sort((a, b) => (ORDER[a.state] ?? 9) - (ORDER[b.state] ?? 9) || b.id - a.id);

  return (
    <div className="loops">
      {sorted.map((loop) => (
        <LoopCard key={loop.id} loop={loop} onChanged={onChanged} onInspect={onInspect} />
      ))}
    </div>
  );
}

/**
 * Why the follow-through engine suggested this result: every candidate
 * it scored, against the calibrated threshold. It ranks quoted
 * evidence only; it never decides that the instruction was fulfilled.
 */
export function AiMatchExplanation({ match }: { match: LoopMatch }) {
  const explanation = match.explanation;
  if (!explanation) return null;

  const rows = [
    { label: match.observation?.observation_type ?? "Suggested result", score: explanation.score, chosen: true },
    ...explanation.alternatives.map((item) => ({ ...item, chosen: false })),
  ];
  const width = (score: number) => `${Math.max(0, Math.min(1, score)) * 100}%`;

  return (
    <div className="ai-match" aria-label="AI follow-through explanation">
      <p className="small">
        <span className="badge badge-accent">AI follow-through</span> Local biomedical model matched the instruction’s
        test to this result: similarity <strong>{explanation.score.toFixed(2)}</strong>, ranked {explanation.rank} of{" "}
        {explanation.candidates}. Suggestions need a similarity of at least {explanation.threshold.toFixed(2)}.
      </p>
      <ul className="ai-bars">
        {rows.map((row) => (
          <li key={row.label} className={row.chosen ? "ai-bar ai-bar-chosen" : "ai-bar"}>
            <span className="ai-bar-label">{row.label}</span>
            <span className="ai-bar-track">
              <span className="ai-bar-fill" style={{ width: width(row.score) }} />
              <span className="ai-bar-threshold" style={{ left: width(explanation.threshold) }} aria-hidden="true" />
            </span>
            <span className="ai-bar-score mono">{row.score.toFixed(2)}</span>
          </li>
        ))}
      </ul>
      <p className="small muted">Model: {explanation.model}. A suggestion, not a decision.</p>
    </div>
  );
}
