import { useCallback, useEffect, useRef, useState } from "react";
import { api, ApiError } from "../api";
import { Conflicts } from "../components/Conflicts";
import { DoctorForm } from "../components/DoctorForm";
import { DocumentsList } from "../components/DocumentsList";
import { EvidenceViewer, type EvidenceSelection } from "../components/EvidenceViewer";
import { OpenLoops } from "../components/OpenLoops";
import { PatientHeader } from "../components/PatientHeader";
import { DemoLoginHint, StaffLogin } from "../components/StaffLogin";
import { EmptyState, ErrorBanner } from "../components/StatusBadge";
import { StoryStrip } from "../components/StoryStrip";
import { Timeline, timelineKey } from "../components/Timeline";
import { formatDateTime, formatValue } from "../format";
import type { ConsentedPatient, Journey, StaffUser, TimelineItem } from "../types";

export function Card({
  title,
  subtitle,
  children,
  className = "",
  actions,
}: {
  title: string;
  subtitle?: string;
  children: React.ReactNode;
  className?: string;
  actions?: React.ReactNode;
}) {
  return (
    <section className={`card ${className}`}>
      <header className="card-head">
        <div>
          <h2 className="card-title">{title}</h2>
          {subtitle && <p className="card-subtitle">{subtitle}</p>}
        </div>
        {actions}
      </header>
      {children}
    </section>
  );
}

/**
 * Restores the staff session from its cookie, or shows sign-in.
 */
export function useStaffSession() {
  const [user, setUser] = useState<StaffUser | null>(null);
  const [checking, setChecking] = useState(true);

  useEffect(() => {
    api
      .me()
      .then(setUser)
      .catch(() => setUser(null))
      .finally(() => setChecking(false));
  }, []);

  async function signOut() {
    try {
      await api.logout();
    } finally {
      setUser(null);
    }
  }

  return { user, setUser, checking, signOut };
}

export function SignedInBar({ user, onSignOut }: { user: StaffUser; onSignOut: () => void }) {
  return (
    <div className="signed-in">
      <span>
        Signed in as <strong>{user.display_name}</strong> · {user.role} · MFA verified
      </span>
      <button type="button" className="btn btn-small" onClick={onSignOut}>
        Sign out
      </button>
    </div>
  );
}

export function DoctorConsole() {
  const { user, setUser, checking, signOut } = useStaffSession();

  if (checking) return <p className="muted">Checking session…</p>;
  if (!user) return <StaffLogin onSignedIn={setUser} hint={<DemoLoginHint />} />;

  if (user.role !== "DOCTOR") {
    return (
      <>
        <SignedInBar user={user} onSignOut={signOut} />
        <EmptyState title="Doctor access only">
          {user.role === "AUDITOR"
            ? "Auditors can view the audit trail on the Security page, but not patient data."
            : "Administrators manage enrolment and operations, but cannot view patient data."}
        </EmptyState>
      </>
    );
  }

  return <DoctorWorkspace user={user} onSignOut={signOut} />;
}

function DoctorWorkspace({ user, onSignOut }: { user: StaffUser; onSignOut: () => void }) {
  const [patients, setPatients] = useState<ConsentedPatient[] | null>(null);
  const [selected, setSelected] = useState<string | null>(null);
  const [view, setView] = useState<"form" | "journey">("form");
  const [error, setError] = useState<string | null>(null);

  const loadPatients = useCallback(() => {
    setError(null);
    api
      .myPatients()
      .then((list) => {
        setPatients(list);
        setSelected((current) => (current && list.some((p) => p.patient_ref === current) ? current : list[0]?.patient_ref ?? null));
      })
      .catch((err: Error) => setError(err.message));
  }, []);

  useEffect(loadPatients, [loadPatients]);

  return (
    <>
      <SignedInBar user={user} onSignOut={onSignOut} />

      {error && <ErrorBanner message={error} onRetry={loadPatients} />}

      <Card
        title="My patients"
        subtitle="Only patients who have granted you an active consent are listed. Access is re-checked on every request."
        actions={
          <button type="button" className="btn btn-small" onClick={loadPatients}>
            Refresh
          </button>
        }
      >
        {patients === null ? (
          <p className="muted small">Loading…</p>
        ) : patients.length === 0 ? (
          <EmptyState title="No active consents">
            When a patient grants you consent in the JeevaFlow portal, they appear here.
          </EmptyState>
        ) : (
          <ul className="patient-list">
            {patients.map((patient) => (
              <li key={patient.patient_ref}>
                <button
                  type="button"
                  className={`patient-pick ${selected === patient.patient_ref ? "patient-pick-active" : ""}`}
                  onClick={() => setSelected(patient.patient_ref)}
                >
                  <strong>{patient.name ?? "Name not shared"}</strong>
                  <span className="mono small">{patient.patient_ref}</span>
                  <span className="small muted">
                    {patient.scopes.length} scopes · until {formatDateTime(patient.expires_at)}
                  </span>
                </button>
              </li>
            ))}
          </ul>
        )}
      </Card>

      {selected && (
        <>
          <div className="tabs" role="tablist">
            <button
              type="button"
              role="tab"
              aria-selected={view === "form"}
              className={view === "form" ? "tab tab-active" : "tab"}
              onClick={() => setView("form")}
            >
              Doctor-ready form
            </button>
            <button
              type="button"
              role="tab"
              aria-selected={view === "journey"}
              className={view === "journey" ? "tab tab-active" : "tab"}
              onClick={() => setView("journey")}
            >
              Journey & review
            </button>
          </div>

          {view === "form" ? (
            <DoctorForm key={selected} patientRef={selected} doctorName={user.display_name} />
          ) : (
            <JourneyView key={selected} patientRef={selected} onDenied={loadPatients} />
          )}
        </>
      )}
    </>
  );
}

function JourneyView({ patientRef, onDenied }: { patientRef: string; onDenied: () => void }) {
  const [journey, setJourney] = useState<Journey | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [selection, setSelection] = useState<EvidenceSelection | null>(null);
  const evidenceRef = useRef<HTMLElement>(null);

  const refresh = useCallback(async () => {
    setError(null);
    try {
      setJourney(await api.journey(patientRef));
    } catch (err) {
      setJourney(null);
      setError((err as Error).message);
      if (err instanceof ApiError && err.status === 403) onDenied();
    }
  }, [patientRef, onDenied]);

  useEffect(() => {
    refresh();
  }, [refresh]);

  useEffect(() => {
    if (selection && window.matchMedia?.("(max-width: 1040px)").matches) {
      evidenceRef.current?.scrollIntoView?.({ behavior: "smooth", block: "start" });
    }
  }, [selection]);

  function selectTimelineItem(item: TimelineItem) {
    if (!item.evidence) return;
    setSelection({
      key: timelineKey(item),
      kind: item.type === "OBSERVATION" ? "OBSERVATION" : "COMMITMENT",
      heading: item.type === "OBSERVATION" ? `${item.title} ${formatValue(item.value, item.unit)}` : item.value,
      subheading: item.type === "OBSERVATION" ? "Extracted observation" : "Doctor instruction",
      observationId: item.type === "OBSERVATION" ? item.id : undefined,
      reviewStatus: item.review_status,
      evidence: item.evidence,
    });
  }

  if (error) return <ErrorBanner message={error} onRetry={refresh} />;
  if (!journey)
    return (
      <div className="loading" role="status">
        <span className="spinner" aria-hidden="true" /> Loading health journey…
      </div>
    );

  const canViewSource = journey.consent.scopes.includes("SOURCE_DOCUMENTS");

  return (
    <>
      <PatientHeader journey={journey} />
      <StoryStrip journey={journey} />

      <div className="layout">
        <div className="col-main">
          <Card title="Open loops" subtitle="Documented instructions, tracked until a doctor confirms follow-through.">
            <OpenLoops loops={journey.open_loops} onChanged={refresh} onInspect={setSelection} />
          </Card>

          <Card title="Patient timeline" subtitle="Every item links to the exact source quote it was read from.">
            <Timeline items={journey.timeline} selectedKey={selection?.key ?? null} onSelect={selectTimelineItem} />
          </Card>

          <Card title="Conflicts" subtitle="Different values for the same test and date — never auto-resolved.">
            <Conflicts conflicts={journey.conflicts} onInspect={setSelection} />
          </Card>

          <Card title="Shared documents" subtitle="Metadata only. Originals are never downloadable.">
            <DocumentsList documents={journey.documents} />
          </Card>
        </div>

        <aside className="col-side" ref={evidenceRef}>
          <Card title="Evidence viewer" className="sticky">
            <EvidenceViewer selection={selection} canViewSource={canViewSource} onReviewed={refresh} />
          </Card>
        </aside>
      </div>
    </>
  );
}
