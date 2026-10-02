import { useCallback, useEffect, useState } from "react";
import { api } from "../api";
import { DemoLoginHint, StaffLogin } from "../components/StaffLogin";
import { ErrorBanner } from "../components/StatusBadge";
import { formatDateTime } from "../format";
import type { AuditEvent, ChainResult, SecurityStatus, StaffUser } from "../types";
import { Card, SignedInBar, useStaffSession } from "./DoctorConsole";

const GOOD = new Set([
  "ACTIVE", "ENFORCED", "PASSED", "PRIVATE", "DISABLED", "BLOCKED", "VERIFIED", "AUTHORIZED", "HTTPS", "FIELD-LEVEL",
]);

export function SecurityDashboard() {
  const { user, setUser, checking, signOut } = useStaffSession();

  if (checking) return <p className="muted">Checking session…</p>;
  if (!user) return <StaffLogin onSignedIn={setUser} hint={<DemoLoginHint />} />;

  return (
    <>
      <SignedInBar user={user} onSignOut={signOut} />
      <Dashboard user={user} />
    </>
  );
}

function Dashboard({ user }: { user: StaffUser }) {
  const [status, setStatus] = useState<SecurityStatus | null>(null);
  const [events, setEvents] = useState<AuditEvent[] | null>(null);
  const [chain, setChain] = useState<ChainResult | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);

  const canAudit = user.role === "AUDITOR" || user.role === "ADMIN";

  const load = useCallback(async () => {
    setError(null);
    try {
      const next = await api.securityStatus();
      setStatus(next);
      setChain(next.audit_chain);
      if (canAudit) setEvents(await api.auditEvents(200));
    } catch (err) {
      setError((err as Error).message);
    }
  }, [canAudit]);

  useEffect(() => {
    load();
  }, [load]);

  async function run(key: string, action: () => Promise<unknown>) {
    setBusy(key);
    setError(null);
    try {
      await action();
      await load();
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setBusy(null);
    }
  }

  return (
    <>
      {error && <ErrorBanner message={error} onRetry={load} />}

      {status && (
        <Card title="Security architecture" subtitle={status.statement}>
          <div className="controls">
            {status.controls.map((control) => (
              <div key={control.name} className="control">
                <div className="control-head">
                  <span className="control-name">{control.name}</span>
                  <span className={`control-status ${GOOD.has(control.status) ? "ok" : "attention"}`}>
                    {control.status}
                  </span>
                </div>
                <p className="small">{control.detail}</p>
                <span className={`grade grade-${control.grade.split(" ")[0].toLowerCase()}`}>{control.grade}</span>
                {control.production && <p className="small muted">Production: {control.production}</p>}
              </div>
            ))}
          </div>
          <p className="small muted">
            Environment: {status.environment}
            {status.demo_mode ? " · demo mode (synthetic data only)" : ""} · generated{" "}
            {formatDateTime(status.generated_at)}. These are technical controls; they do not by themselves establish
            regulatory compliance.
          </p>
        </Card>
      )}

      <Card
        title="Tamper-evident audit trail"
        subtitle="Each event stores the SHA-256 hash of the previous event, plus an HMAC. Changing any record breaks the chain."
        actions={
          canAudit ? (
            <div className="btn-row">
              <button
                type="button"
                className="btn btn-primary btn-small"
                disabled={busy !== null}
                onClick={() => run("verify", async () => setChain(await api.verifyAudit()))}
              >
                {busy === "verify" ? "Verifying…" : "Verify audit chain"}
              </button>
              {status?.demo_mode && (
                <button
                  type="button"
                  className="btn btn-small"
                  disabled={busy !== null}
                  onClick={() => run("restore", () => api.restoreAudit())}
                >
                  Restore (demo)
                </button>
              )}
            </div>
          ) : undefined
        }
      >
        {chain && (
          <div className={`banner ${chain.valid ? "banner-success" : "banner-danger"}`} role="status">
            {chain.valid
              ? `Audit chain VERIFIED · ${chain.events} events · head ${chain.head_hash?.slice(0, 16)}…`
              : `Audit chain BROKEN at event #${chain.broken_at_seq} (${chain.reason}). The record was modified after it was written.`}
          </div>
        )}

        {!canAudit && (
          <p className="small muted">Sign in as an auditor to see the full trail and run verification.</p>
        )}

        {canAudit && events && (
          <div className="table-wrap">
            <table className="brief-table audit-table">
              <thead>
                <tr>
                  <th>#</th>
                  <th>Time (UTC)</th>
                  <th>Actor</th>
                  <th>Action</th>
                  <th>Object</th>
                  <th>Result</th>
                  <th>Hash</th>
                  {status?.demo_mode && <th>Demo</th>}
                </tr>
              </thead>
              <tbody>
                {events.map((event) => (
                  <tr key={event.seq} className={chain && chain.broken_at_seq === event.seq ? "audit-broken" : ""}>
                    <td>{event.seq}</td>
                    <td className="nowrap small">{event.created_at.replace("T", " ").slice(0, 19)}</td>
                    <td className="small">
                      {event.actor_type}
                      {event.actor_ref && <span className="mono block">{event.actor_ref}</span>}
                    </td>
                    <td>
                      <strong className="small">{event.action}</strong>
                      {event.reason && <span className="block small muted">{event.reason}</span>}
                    </td>
                    <td className="mono small">{event.object_ref ?? "—"}</td>
                    <td className="small">{event.result}</td>
                    <td className="mono small">{event.event_hash.slice(0, 10)}…</td>
                    {status?.demo_mode && (
                      <td>
                        <button
                          type="button"
                          className="link-button small danger"
                          disabled={busy !== null}
                          onClick={() => run(`tamper-${event.seq}`, () => api.tamperAudit(event.seq))}
                        >
                          Tamper
                        </button>
                      </td>
                    )}
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Card>
    </>
  );
}
