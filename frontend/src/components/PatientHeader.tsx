import { formatDateTime } from "../format";
import type { Journey } from "../types";

function Stat({ label, value, tone }: { label: string; value: number; tone?: string }) {
  return (
    <div className={`stat ${tone && value > 0 ? `stat-${tone}` : ""}`}>
      <span className="stat-value">{value}</span>
      <span className="stat-label">{label}</span>
    </div>
  );
}

export function PatientHeader({ journey }: { journey: Journey }) {
  const name = `Case ${journey.patient.case_alias}`;
  const initials = "··";

  return (
    <section className="card patient-header" aria-label="Patient">
      <div className="patient-identity">
        <div className="avatar" aria-hidden="true">
          {initials}
        </div>
        <div>
          <h2 className="patient-name">{name}</h2>
          <dl className="patient-meta">
            <div>
              <dt>Identity</dt>
              <dd>Withheld (pseudonymized)</dd>
            </div>
            <div>
              <dt>Consent</dt>
              <dd>
                {journey.consent.status} · until {formatDateTime(journey.consent.expires_at)}
              </dd>
            </div>
            <div>
              <dt>Shared scopes</dt>
              <dd>{journey.consent.scopes.length}</dd>
            </div>
          </dl>
        </div>
      </div>
      <div className="stats">
        <Stat label="Documents" value={journey.summary.document_count} />
        <Stat label="Open loops" value={journey.summary.open_loop_count} tone="info" />
        <Stat label="Potential matches" value={journey.summary.potential_match_count} tone="accent" />
        <Stat label="Conflicts" value={journey.summary.conflict_count} tone="danger" />
      </div>
    </section>
  );
}
