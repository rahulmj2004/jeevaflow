import { Fragment, useCallback, useEffect, useState } from "react";
import { api } from "../api";
import { formatDate, formatDateTime, formatValue, statusLabel } from "../format";
import type { DoctorForm as Form, FactField, FactState, FormEvidence, FormFact } from "../types";
import { SecureEvidence } from "./SecureEvidence";
import { ErrorBanner } from "./StatusBadge";

const STATE_LABEL: Record<FactState, string> = {
  SOURCE_FACT: "Source fact",
  AI_INFERRED: "AI-inferred",
  UNCERTAIN: "Uncertain — confirm",
  MISSING: "Missing",
};

const FIELD_LABEL: Record<string, string> = {
  name: "Medicine",
  dose: "Dosage",
  frequency: "Frequency",
  duration: "Duration",
  substance: "Substance",
  reaction: "Reaction",
  registration: "Registration",
};

function StateChip({ state }: { state: FactState }) {
  return <span className={`fact-chip fact-${state.toLowerCase()}`}>{STATE_LABEL[state]}</span>;
}

function FieldValue({ field }: { field: FactField }) {
  return (
    <span className="fact-field">
      <span className={field.value ? "" : "muted"}>{field.value ?? "Not stated"}</span>
      {field.state !== "SOURCE_FACT" && <StateChip state={field.state} />}
      {field.note && <span className="fact-note">{field.note}</span>}
    </span>
  );
}

function EvidenceButton({
  evidence,
  open,
  onToggle,
}: {
  evidence: FormEvidence | null;
  open: boolean;
  onToggle: () => void;
}) {
  if (!evidence) return <span className="muted small">No source</span>;
  return (
    <button type="button" className="evidence-link" onClick={onToggle} aria-expanded={open}>
      <span className="evidence-chip">Source</span>
      <span>
        {evidence.document_label ?? "Document"}, p.{evidence.page_number ?? "—"}
      </span>
    </button>
  );
}

function EvidencePanel({ evidence, canView }: { evidence: FormEvidence; canView: boolean }) {
  return (
    <div className="form-evidence">
      <blockquote className="quote">
        <span className="quote-mark" aria-hidden="true">
          “
        </span>
        {evidence.quote}
      </blockquote>
      <p className="small muted">
        {evidence.document_label} · report {formatDate(evidence.document_date)} · page {evidence.page_number ?? "—"}
        {evidence.confidence != null && ` · confidence ${Math.round(evidence.confidence * 100)}%`}
      </p>
      <SecureEvidence evidenceId={evidence.evidence_id} canView={canView && evidence.has_region} />
    </div>
  );
}

function FactRows({
  facts,
  fields,
  canView,
  onReview,
}: {
  facts: FormFact[];
  fields: string[];
  canView: boolean;
  onReview: (fact: FormFact, action: "confirm" | "reject") => void;
}) {
  const [open, setOpen] = useState<string | null>(null);

  return (
    <table className="brief-table">
      <thead>
        <tr>
          {fields.map((name) => (
            <th key={name}>{FIELD_LABEL[name] ?? name}</th>
          ))}
          <th>Status</th>
          <th>Evidence</th>
        </tr>
      </thead>
      <tbody>
        {facts.map((fact) => (
          <Fragment key={fact.ref}>
            <tr>
              {fields.map((name) => (
                <td key={name}>{fact.fields[name] ? <FieldValue field={fact.fields[name]} /> : "—"}</td>
              ))}
              <td>
                <StateChip state={fact.state} />
                {fact.review_status !== "REVIEW" && (
                  <span className="brief-src">Doctor: {statusLabel(fact.review_status)}</span>
                )}
                {fact.review_status === "REVIEW" && (
                  <span className="fact-actions">
                    <button type="button" className="link-button small" onClick={() => onReview(fact, "confirm")}>
                      Confirm
                    </button>
                    <button type="button" className="link-button small danger" onClick={() => onReview(fact, "reject")}>
                      Reject
                    </button>
                  </span>
                )}
              </td>
              <td>
                <EvidenceButton
                  evidence={fact.evidence}
                  open={open === fact.ref}
                  onToggle={() => setOpen(open === fact.ref ? null : fact.ref)}
                />
              </td>
            </tr>
            {open === fact.ref && fact.evidence && (
              <tr className="evidence-row">
                <td colSpan={fields.length + 2}>
                  <EvidencePanel evidence={fact.evidence} canView={canView} />
                </td>
              </tr>
            )}
          </Fragment>
        ))}
      </tbody>
    </table>
  );
}

function NotShared({ label }: { label: string }) {
  return <p className="small muted not-shared">Not shared by the patient: {label}.</p>;
}

export function DoctorForm({ patientRef, doctorName }: { patientRef: string; doctorName: string }) {
  const [form, setForm] = useState<Form | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [openLab, setOpenLab] = useState<number | null>(null);

  const load = useCallback(() => {
    setError(null);
    api
      .doctorForm(patientRef)
      .then(setForm)
      .catch((err: Error) => {
        setForm(null);
        setError(err.message);
      });
  }, [patientRef]);

  useEffect(load, [load]);

  async function review(fact: FormFact, action: "confirm" | "reject") {
    try {
      if (action === "confirm") await api.confirmFact(fact.ref);
      else await api.rejectFact(fact.ref);
      load();
    } catch (err) {
      setError((err as Error).message);
    }
  }

  if (error) return <ErrorBanner message={error} onRetry={load} />;

  if (!form) {
    return (
      <div className="loading" role="status">
        <span className="spinner" aria-hidden="true" /> Preparing doctor-ready form…
      </div>
    );
  }

  const canView = form.consent.can_view_source;
  const label = (scope: string) => form.not_shared.find((item) => item.scope === scope)?.label ?? scope;

  return (
    <article className="brief" aria-label="Doctor-ready form">
      <header className="brief-head">
        <div>
          <p className="brief-eyebrow">JeevaFlow · Doctor-ready form</p>
          <h1 className="brief-name mono">Case {form.patient.case_alias}</h1>
          <p className="brief-meta">
            Identity withheld (pseudonymized)
            {form.patient.age_band && ` · age ${form.patient.age_band}`} · consent until{" "}
            {formatDateTime(form.consent.expires_at)}
          </p>
        </div>
        <p className="brief-generated">
          Viewed by {doctorName}
          <br />
          {formatDateTime(form.generated_at)}
        </p>
      </header>

      <section className="brief-glance" aria-label="At a glance">
        <div className="glance">
          <strong>{form.medications?.length ?? "–"}</strong> medications
        </div>
        <div className="glance">
          <strong>{form.labs?.length ?? "–"}</strong> lab tests
        </div>
        <div className="glance">
          <strong>{form.allergies?.length ?? "–"}</strong> allergies
        </div>
        <div className={form.uncertain.length ? "glance glance-warn" : "glance"}>
          <strong>{form.uncertain.length}</strong> uncertain
        </div>
        <div className={form.missing.length ? "glance glance-danger" : "glance"}>
          <strong>{form.missing.length}</strong> missing
        </div>
      </section>

      <section className="brief-section">
        <h2>Medications</h2>
        {form.medications === null ? (
          <NotShared label={label("MEDICATIONS")} />
        ) : form.medications.length === 0 ? (
          <p className="muted small">No medications found in shared documents.</p>
        ) : (
          <FactRows
            facts={form.medications}
            fields={["name", "dose", "frequency", "duration"]}
            canView={canView}
            onReview={review}
          />
        )}
        {form.prescribers && form.prescribers.length > 0 && (
          <p className="small muted">
            Prescriber (from source): {form.prescribers.map((item) => item.label).join(", ")}
            {form.prescribers[0].fields.registration?.value && ` · Reg. ${form.prescribers[0].fields.registration.value}`}
          </p>
        )}
      </section>

      <section className="brief-section">
        <h2>Laboratory results</h2>
        {form.labs === null ? (
          <NotShared label={label("LABS")} />
        ) : form.labs.length === 0 ? (
          <p className="muted small">No results extracted yet.</p>
        ) : (
          <table className="brief-table">
            <thead>
              <tr>
                <th>Test</th>
                <th>Latest</th>
                <th>Earlier values</th>
                <th>Evidence</th>
              </tr>
            </thead>
            <tbody>
              {form.labs.map((group) => (
                <Fragment key={group.test}>
                  <tr>
                    <td>
                      <strong>{group.test}</strong>
                      {group.has_conflict && <span className="brief-tag brief-tag-danger">Conflict</span>}
                    </td>
                    <td>
                      <strong>{formatValue(group.latest.value, group.latest.unit)}</strong>{" "}
                      <span className="muted">{formatDate(group.latest.date)}</span>
                      {group.latest.state !== "SOURCE_FACT" && <StateChip state={group.latest.state} />}
                      {group.latest.review_status !== "VERIFIED" && (
                        <span className="brief-tag brief-tag-warn">Unverified</span>
                      )}
                    </td>
                    <td>
                      {group.series.length < 2
                        ? "—"
                        : group.series
                            .slice(0, -1)
                            .reverse()
                            .map((item) => (
                              <div key={item.id}>
                                {formatValue(item.value, item.unit)}{" "}
                                <span className="muted">{formatDate(item.date)}</span>
                              </div>
                            ))}
                    </td>
                    <td>
                      <EvidenceButton
                        evidence={group.latest.evidence}
                        open={openLab === group.latest.id}
                        onToggle={() => setOpenLab(openLab === group.latest.id ? null : group.latest.id)}
                      />
                    </td>
                  </tr>
                  {openLab === group.latest.id && group.latest.evidence && (
                    <tr className="evidence-row">
                      <td colSpan={4}>
                        <EvidencePanel evidence={group.latest.evidence} canView={canView} />
                      </td>
                    </tr>
                  )}
                </Fragment>
              ))}
            </tbody>
          </table>
        )}
      </section>

      <section className="brief-section">
        <h2>Allergies</h2>
        {form.allergies === null ? (
          <NotShared label={label("ALLERGIES")} />
        ) : form.allergies.length === 0 ? (
          <p className="muted small">Allergy status is not documented in shared documents.</p>
        ) : (
          <FactRows facts={form.allergies} fields={["substance", "reaction"]} canView={canView} onReview={review} />
        )}
      </section>

      <section className="brief-section">
        <h2>Important source-supported notes</h2>
        {form.notes === null ? (
          <NotShared label={label("INSTRUCTIONS")} />
        ) : form.notes.length === 0 ? (
          <p className="muted small">No instructions found.</p>
        ) : (
          <ul className="brief-notes">
            {form.notes.map((note) => (
              <li key={note.id}>
                <span className={`brief-state brief-state-${note.state}`}>{statusLabel(note.state)}</span>{" "}
                {note.instruction}
                <span className="brief-src">
                  Due {formatDate(note.due_date)} · documented {formatDate(note.documented_on)} · “
                  {note.evidence?.quote}”
                </span>
              </li>
            ))}
          </ul>
        )}
      </section>

      <section className="brief-section">
        <h2>Uncertain information — doctor confirmation required</h2>
        {form.uncertain.length === 0 ? (
          <p className="muted small">Nothing flagged as uncertain.</p>
        ) : (
          <ul className="flag-list flag-uncertain">
            {form.uncertain.map((item, index) => (
              <li key={index}>
                <strong>{item.item}</strong>
                {item.field && ` · ${FIELD_LABEL[item.field] ?? item.field}`} — {item.note}
              </li>
            ))}
          </ul>
        )}
      </section>

      <section className="brief-section">
        <h2>Missing information</h2>
        {form.missing.length === 0 ? (
          <p className="muted small">Nothing missing.</p>
        ) : (
          <ul className="flag-list flag-missing">
            {form.missing.map((item, index) => (
              <li key={index}>
                {item.item && <strong>{item.item} · </strong>}
                {item.field ? `${FIELD_LABEL[item.field] ?? item.field}: ` : ""}
                {item.note}
              </li>
            ))}
          </ul>
        )}
      </section>

      <section className="brief-section">
        <h2>Source evidence</h2>
        <table className="brief-table">
          <thead>
            <tr>
              <th>Document</th>
              <th>Report date</th>
              <th>Received</th>
              <th>SHA-256</th>
            </tr>
          </thead>
          <tbody>
            {form.sources.map((source) => (
              <tr key={source.ref}>
                <td>{source.label}</td>
                <td>{formatDate(source.document_date)}</td>
                <td>{formatDateTime(source.received_at)}</td>
                <td className="mono small">{source.sha256.slice(0, 16)}…</td>
              </tr>
            ))}
          </tbody>
        </table>
        {!canView && <NotShared label={label("SOURCE_DOCUMENTS")} />}
      </section>

      <footer className="brief-notice">{form.notice}</footer>
    </article>
  );
}
