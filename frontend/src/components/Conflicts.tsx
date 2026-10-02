import { formatDate, formatValue } from "../format";
import type { Conflict } from "../types";
import type { EvidenceSelection } from "./EvidenceViewer";
import { EmptyState, SourceIcon, StatusBadge } from "./StatusBadge";

interface Props {
  conflicts: Conflict[];
  onInspect: (selection: EvidenceSelection) => void;
}

const SIDE_LABELS = ["Document A", "Document B", "Document C", "Document D"];

export function Conflicts({ conflicts, onInspect }: Props) {
  if (conflicts.length === 0) {
    return (
      <EmptyState title="No conflicting values">
        When two sources report different values for the same test on the same date, both are shown here for human
        review.
      </EmptyState>
    );
  }

  return (
    <div className="conflicts">
      {conflicts.map((conflict) => (
        <article key={`${conflict.observation_type}-${conflict.event_date}`} className="conflict">
          <header className="conflict-head">
            <div>
              <p className="eyebrow">Conflict · {formatDate(conflict.event_date)}</p>
              <h3>{conflict.observation_type}</h3>
            </div>
            <StatusBadge status="CONFLICT" label="Human review required" />
          </header>

          <div className="conflict-sides">
            {conflict.observations.map((observation, index) => (
              <div key={observation.id} className="conflict-side">
                <p className="eyebrow">{SIDE_LABELS[index] ?? `Document ${index + 1}`}</p>
                <p className="conflict-value">{formatValue(observation.value, observation.unit)}</p>
                <p className="small muted">
                  <SourceIcon source={observation.evidence?.document_source} />{" "}
                  {observation.evidence?.document_filename ?? "Shared document"}
                </p>
                <StatusBadge status={observation.review_status} />
                {observation.evidence && (
                  <button
                    type="button"
                    className="evidence-link"
                    onClick={() =>
                      onInspect({
                        key: `CONFLICT-${observation.id}`,
                        kind: "CONFLICT",
                        heading: `${conflict.observation_type} ${formatValue(observation.value, observation.unit)}`,
                        subheading: `${SIDE_LABELS[index] ?? "Document"} in a conflict`,
                        observationId: observation.id,
                        reviewStatus: observation.review_status,
                        evidence: observation.evidence!,
                      })
                    }
                  >
                    <span className="evidence-chip">Evidence #{observation.evidence.id}</span>
                    <span>Inspect source</span>
                  </button>
                )}
              </div>
            ))}
          </div>

          <p className="small">
            {conflict.message} JeevaFlow does not decide which value is correct — verify or reject each against its
            source.
          </p>
        </article>
      ))}
    </div>
  );
}
