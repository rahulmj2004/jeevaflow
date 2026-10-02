import { useCallback, useEffect, useState } from "react";
import { api, ApiError } from "../api";
import { DocumentUpload } from "../components/DocumentUpload";
import { EmptyState, ErrorBanner, SourceIcon, StatusBadge } from "../components/StatusBadge";
import { formatDateTime } from "../format";
import type { ConsentResult, PortalSession } from "../types";
import { Card } from "./DoctorConsole";

function transactionFromUrl(): string | null {
  const value = new URLSearchParams(window.location.search).get("txn");
  return value && /^txn_[A-Za-z0-9_-]{8,40}$/.test(value) ? value : null;
}

export function PatientPortal() {
  const [session, setSession] = useState<PortalSession | null>(null);
  const [checking, setChecking] = useState(true);
  const [transactionRef] = useState(transactionFromUrl);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    setError(null);
    try {
      setSession(await api.portalSession());
    } catch (err) {
      setSession(null);
      if (!(err instanceof ApiError && err.status === 401)) setError((err as Error).message);
    } finally {
      setChecking(false);
    }
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  if (checking) return <p className="muted">Checking session…</p>;

  if (!session) {
    return (
      <>
        {error && <ErrorBanner message={error} />}
        {transactionRef ? (
          <OtpForm
            transactionRef={transactionRef}
            onVerified={() => {
              // Remove the transaction reference from the address bar.
              window.history.replaceState(null, "", "/portal");
              load();
            }}
          />
        ) : (
          <EmptyState title="Open the link from WhatsApp">
            Send a photo or PDF of your document to the JeevaFlow WhatsApp number. You will receive a secure link and
            a one-time code.
          </EmptyState>
        )}
      </>
    );
  }

  return <PortalHome session={session} reload={load} onSignedOut={() => setSession(null)} />;
}

function OtpForm({ transactionRef, onVerified }: { transactionRef: string; onVerified: () => void }) {
  const [code, setCode] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function submit(event: React.FormEvent) {
    event.preventDefault();
    setBusy(true);
    setError(null);
    try {
      await api.portalVerify(transactionRef, code);
      onVerified();
    } catch (err) {
      setError((err as Error).message);
      setCode("");
    } finally {
      setBusy(false);
    }
  }

  return (
    <section className="card auth-card" aria-label="Verification">
      <p className="eyebrow">Verification required</p>
      <h2 className="card-title">Enter the code sent to your WhatsApp</h2>
      <p className="small muted">
        The 6-digit code expires 5 minutes after it was sent and works once. After 5 wrong attempts this link is
        locked.
      </p>
      <form className="auth-form" onSubmit={submit}>
        <label className="field">
          <span>Verification code</span>
          <input
            value={code}
            onChange={(e) => setCode(e.target.value.replace(/\D/g, "").slice(0, 6))}
            inputMode="numeric"
            autoComplete="one-time-code"
            pattern="\d{6}"
            required
            autoFocus
          />
        </label>
        <button type="submit" className="btn btn-primary" disabled={busy || code.length !== 6}>
          {busy ? "Verifying…" : "Verify"}
        </button>
      </form>
      {error && (
        <div className="banner banner-danger" role="alert">
          {error}
        </div>
      )}
    </section>
  );
}

function PortalHome({
  session,
  reload,
  onSignedOut,
}: {
  session: PortalSession;
  reload: () => void;
  onSignedOut: () => void;
}) {
  const [result, setResult] = useState<ConsentResult | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);

  const active = session.consents.filter((consent) => consent.status === "ACTIVE");
  const pending =
    session.transaction?.status === "VERIFIED" ? session.transaction.pending_documents : 0;

  async function run(key: string, action: () => Promise<unknown>) {
    setBusy(key);
    setError(null);
    try {
      const value = await action();
      if (value && typeof value === "object" && "consent" in value) setResult(value as ConsentResult);
      reload();
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setBusy(null);
    }
  }

  return (
    <>
      <div className="signed-in">
        <span>
          Verified for <strong>{session.patient.name ?? "your record"}</strong> ({session.patient.phone_masked}) · this
          session ends after 15 minutes
        </span>
        <button
          type="button"
          className="btn btn-small"
          onClick={async () => {
            await api.portalLogout().catch(() => undefined);
            onSignedOut();
          }}
        >
          Sign out
        </button>
      </div>

      <div className="banner banner-neutral case-code" role="note">
        <span>
          Your case code: <strong className="mono">{session.patient.case_alias}</strong>
        </span>
        <span className="case-code-hint">
          Show this to your doctor at the visit. Doctors see this code instead of your name.
        </span>
      </div>

      {error && <ErrorBanner message={error} />}

      {result?.processing && (
        <div className="banner banner-success" role="status">
          Consent recorded. {result.processing.processed} document{result.processing.processed === 1 ? "" : "s"}{" "}
          processed privately: {result.processing.facts} medication/allergy items, {result.processing.observations} lab
          values and {result.processing.instructions} instructions are now visible to{" "}
          <strong>{result.consent.doctor_name}</strong> until {formatDateTime(result.consent.expires_at)}.
        </div>
      )}

      {pending > 0 && (
        <Card
          title={`${pending} document${pending === 1 ? "" : "s"} waiting for your consent`}
          subtitle="Your document is stored encrypted. Nothing is read from it until you choose who may see it."
        >
          {active.length > 0 && (
            <div className="existing-consents">
              {active.map((consent) => (
                <div key={consent.ref} className="existing-consent">
                  <span>
                    Share with <strong>{consent.doctor_name}</strong> under your existing consent (until{" "}
                    {formatDateTime(consent.expires_at)})
                  </span>
                  <button
                    type="button"
                    className="btn btn-primary btn-small"
                    disabled={busy !== null}
                    onClick={() => run(`apply-${consent.ref}`, () => api.applyConsent(consent.ref))}
                  >
                    {busy === `apply-${consent.ref}` ? "Sharing…" : "Share"}
                  </button>
                </div>
              ))}
              <p className="small muted">Or grant a new consent below.</p>
            </div>
          )}
          <ConsentForm session={session} busy={busy !== null} onGrant={(args) => run("grant", () => api.grantConsent(...args))} />
        </Card>
      )}

      {pending === 0 && active.length === 0 && (
        <Card title="Grant consent" subtitle="Choose a doctor who may view the information you share.">
          <ConsentForm session={session} busy={busy !== null} onGrant={(args) => run("grant", () => api.grantConsent(...args))} />
        </Card>
      )}

      <Card title="Your consents" subtitle="Revoking stops the doctor's access immediately.">
        {session.consents.length === 0 ? (
          <EmptyState title="No consents yet" />
        ) : (
          <ul className="consent-list">
            {session.consents.map((consent) => (
              <li key={consent.ref} className="consent-item">
                <div>
                  <strong>{consent.doctor_name}</strong> · {consent.purpose_label}
                  <div className="small muted">
                    {consent.scopes.map((scope) => scope.replace("_", " ").toLowerCase()).join(", ")}
                  </div>
                  <div className="small muted">
                    Granted {formatDateTime(consent.granted_at)} · until {formatDateTime(consent.expires_at)}
                    {consent.revoked_at && ` · revoked ${formatDateTime(consent.revoked_at)}`}
                  </div>
                </div>
                <div className="btn-row">
                  <StatusBadge status={consent.status} />
                  {consent.status === "ACTIVE" && (
                    <>
                      <button
                        type="button"
                        className="btn btn-small btn-ghost-danger"
                        disabled={busy !== null}
                        onClick={() => run(`revoke-${consent.ref}`, () => api.revokeConsent(consent.ref))}
                      >
                        Revoke
                      </button>
                      <button
                        type="button"
                        className="btn btn-small"
                        disabled={busy !== null}
                        onClick={() => run(`expire-${consent.ref}`, () => api.expireConsent(consent.ref))}
                      >
                        End now
                      </button>
                    </>
                  )}
                </div>
              </li>
            ))}
          </ul>
        )}
      </Card>

      <Card
        title="Your documents"
        subtitle={`Stored encrypted for up to ${session.options.retention_days} days. Deleting destroys the document's encryption key.`}
      >
        {session.documents.length === 0 ? (
          <EmptyState title="No documents yet" />
        ) : (
          <ul className="docs">
            {session.documents.map((doc) => (
              <li key={doc.ref} className="doc">
                <div className="doc-main">
                  <span className="doc-name">{doc.label}</span>
                  <span className="small muted">
                    <SourceIcon source={doc.source} /> received {formatDateTime(doc.received_at)} · scan{" "}
                    {doc.scan_status ?? "—"} · kept until {formatDateTime(doc.retain_until)}
                  </span>
                </div>
                <div className="doc-badges">
                  <StatusBadge status={doc.status} />
                  <button
                    type="button"
                    className="btn btn-small btn-ghost-danger"
                    disabled={busy !== null}
                    onClick={() => {
                      if (window.confirm("Delete this document permanently? Its encryption key will be destroyed.")) {
                        run(`delete-${doc.ref}`, () => api.deleteDocument(doc.ref));
                      }
                    }}
                  >
                    Delete
                  </button>
                </div>
              </li>
            ))}
          </ul>
        )}
      </Card>

      {active.length > 0 && (
        <Card title="Share another document" subtitle="Shared under your active consent and processed privately.">
          <DocumentUpload onProcessed={reload} />
        </Card>
      )}
    </>
  );
}

function ConsentForm({
  session,
  busy,
  onGrant,
}: {
  session: PortalSession;
  busy: boolean;
  onGrant: (args: [string, string[], number, string]) => void;
}) {
  const { options } = session;
  const [doctor, setDoctor] = useState(options.doctors[0]?.ref ?? "");
  const [scopes, setScopes] = useState<string[]>(options.scopes.map((scope) => scope.key));
  const [days, setDays] = useState(options.durations_days.includes(7) ? 7 : options.durations_days[0]);
  const [purpose, setPurpose] = useState(options.purposes[0]?.key ?? "CLINICAL_REVIEW");
  const [agreed, setAgreed] = useState(false);

  const doctorName = options.doctors.find((item) => item.ref === doctor)?.display_name ?? "the doctor";

  function toggle(key: string) {
    setScopes((current) => (current.includes(key) ? current.filter((item) => item !== key) : [...current, key]));
  }

  return (
    <form
      className="consent-form"
      onSubmit={(event) => {
        event.preventDefault();
        onGrant([doctor, scopes, days, purpose]);
      }}
    >
      <div className="consent-grid">
        <label className="field">
          <span>Doctor / provider</span>
          <select value={doctor} onChange={(e) => setDoctor(e.target.value)} required>
            {options.doctors.map((item) => (
              <option key={item.ref} value={item.ref}>
                {item.display_name}
              </option>
            ))}
          </select>
        </label>
        <label className="field">
          <span>Purpose</span>
          <select value={purpose} onChange={(e) => setPurpose(e.target.value)}>
            {options.purposes.map((item) => (
              <option key={item.key} value={item.key}>
                {item.label}
              </option>
            ))}
          </select>
        </label>
        <label className="field">
          <span>Expires after</span>
          <select value={days} onChange={(e) => setDays(Number(e.target.value))}>
            {options.durations_days.map((value) => (
              <option key={value} value={value}>
                {value} day{value === 1 ? "" : "s"}
              </option>
            ))}
          </select>
        </label>
      </div>

      <fieldset className="scope-list">
        <legend>Information {doctorName} may see</legend>
        {options.scopes.map((scope) => (
          <label key={scope.key} className="scope-option">
            <input type="checkbox" checked={scopes.includes(scope.key)} onChange={() => toggle(scope.key)} />
            {scope.label}
          </label>
        ))}
      </fieldset>

      <ul className="consent-terms small">
        <li>
          <strong>Purpose:</strong> {options.purposes.find((item) => item.key === purpose)?.label} by {doctorName}{" "}
          only.
        </li>
        <li>
          <strong>Expiry:</strong> access ends automatically after {days} day{days === 1 ? "" : "s"}.
        </li>
        <li>
          <strong>Retention:</strong> documents are kept encrypted for up to {options.retention_days} days, then
          deleted. You can delete any document at any time.
        </li>
        <li>
          <strong>Revocation:</strong> you can revoke this consent here at any time; access stops immediately.
        </li>
      </ul>

      <label className="scope-option">
        <input type="checkbox" checked={agreed} onChange={(e) => setAgreed(e.target.checked)} />I agree to share the
        selected information with {doctorName} for this purpose.
      </label>

      <button type="submit" className="btn btn-primary" disabled={busy || !agreed || scopes.length === 0 || !doctor}>
        {busy ? "Saving…" : "Grant consent"}
      </button>
    </form>
  );
}
