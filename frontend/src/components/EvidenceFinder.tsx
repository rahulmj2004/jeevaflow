import { useState, type FormEvent } from "react";
import { api } from "../api";
import { formatDate, formatValue } from "../format";
import type { EvidenceFinderResponse } from "../types";
import type { EvidenceSelection } from "./EvidenceViewer";
import { ErrorBanner } from "./StatusBadge";

const ABSTAIN_REASON: Record<string, string> = {
  BELOW_THRESHOLD: "No result on this record scored above the calibrated threshold.",
  QUERY_NAMES_NO_TEST: "The question does not name a test.",
  NO_CANDIDATES: "No lab results are on record for this patient.",
  NO_COMPARABLE_CANDIDATES: "The stored results were indexed by a different AI backend.",
  MODEL_UNAVAILABLE: "The AI model is unavailable right now.",
};

const EXAMPLES = ["When was thyroid last checked?", "vitamin D", "MRI brain"];

/**
 * AI evidence finder: ranks this patient's quoted lab results against a
 * test concept. It cites evidence or abstains; it never writes an answer.
 */
export function EvidenceFinder({
  patientRef,
  onInspect,
}: {
  patientRef: string;
  onInspect: (selection: EvidenceSelection) => void;
}) {
  const [query, setQuery] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [answer, setAnswer] = useState<EvidenceFinderResponse | null>(null);

  async function ask(text: string) {
    const trimmed = text.trim();
    if (!trimmed) return;

    setQuery(trimmed);
    setBusy(true);
    setError(null);

    try {
      setAnswer(await api.evidenceFinder(patientRef, trimmed));
    } catch (err) {
      setAnswer(null);
      setError((err as Error).message);
    } finally {
      setBusy(false);
    }
  }

  function submit(event: FormEvent) {
    event.preventDefault();
    void ask(query);
  }

  return (
    <div className="finder">
      <form className="finder-form" onSubmit={submit}>
        <label htmlFor="finder-query" className="visually-hidden">
          Find evidence in this record
        </label>
        <input
          id="finder-query"
          className="input"
          value={query}
          maxLength={120}
          placeholder="e.g. When was thyroid last checked?"
          onChange={(event) => setQuery(event.target.value)}
        />
        <button type="submit" className="btn btn-primary" disabled={busy || !query.trim()}>
          {busy ? "Searching…" : "Find evidence"}
        </button>
      </form>
      <p className="small muted finder-examples">
        Try:{" "}
        {EXAMPLES.map((example) => (
          <button key={example} type="button" className="link-button small" onClick={() => void ask(example)}>
            {example}
          </button>
        ))}
      </p>

      {busy && (
        <div className="loading" role="status">
          <span className="spinner" aria-hidden="true" /> Ranking this record’s results…
        </div>
      )}

      {error && <ErrorBanner message={error} />}

      {!busy && answer && (
        <div aria-live="polite">
          <p className="small muted">
            <span className="badge badge-accent">AI evidence finder</span> Backend: {answer.backend ?? "—"}
            {answer.threshold != null && <> · threshold {answer.threshold.toFixed(2)}</>} · {answer.candidates} results
            considered
          </p>

          {answer.decision === "AI_ABSTAINED" ? (
            <div className="ai-abstained" role="note" aria-label="AI abstained">
              <p className="small">
                <span className="badge badge-neutral">AI_ABSTAINED</span> AI abstained because available evidence was
                insufficient. {ABSTAIN_REASON[answer.reason ?? ""] ?? ""}
              </p>
              {answer.best_score != null && answer.threshold != null && (
                <p className="small muted">
                  Best candidate scored {answer.best_score.toFixed(2)} (threshold {answer.threshold.toFixed(2)}).
                </p>
              )}
            </div>
          ) : (
            <ul className="finder-results">
              {answer.results.map((item) => (
                <li key={item.observation_id} className="finder-result">
                  <div>
                    <strong>{item.observation_type}</strong> {formatValue(item.value, item.unit)}{" "}
                    <span className="muted small">{formatDate(item.date)}</span>
                    {item.fact_state !== "SOURCE_FACT" && (
                      <span className="badge badge-warning finder-state">{item.fact_state}</span>
                    )}
                  </div>
                  {item.evidence && (
                    <button
                      type="button"
                      className="link-button small finder-quote"
                      onClick={() =>
                        onInspect({
                          key: `OBSERVATION-${item.observation_id}`,
                          kind: "OBSERVATION",
                          heading: `${item.observation_type} ${formatValue(item.value, item.unit)}`,
                          subheading: "Found by the AI evidence finder",
                          observationId: item.observation_id,
                          reviewStatus: item.review_status,
                          evidence: item.evidence!,
                        })
                      }
                    >
                      “{item.evidence.quote}” · page {item.evidence.page_number ?? "?"}
                    </button>
                  )}
                  <span className="mono small muted finder-score">similarity {item.score.toFixed(2)}</span>
                </li>
              ))}
            </ul>
          )}
        </div>
      )}

      <p className="small muted">
        Ranks this patient’s consented, quoted results only. It never writes an answer and never interprets values.
      </p>
    </div>
  );
}
