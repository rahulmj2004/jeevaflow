import { useRef, useState } from "react";
import { api } from "../api";
import type { StaffUser } from "../types";

/**
 * Staff sign-in: password, then a TOTP code (MFA). Errors are generic.
 */
export function StaffLogin({ onSignedIn, hint }: { onSignedIn: (user: StaffUser) => void; hint?: React.ReactNode }) {
  const [step, setStep] = useState<"password" | "mfa">("password");
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [code, setCode] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  // Synchronous guard: repeated Enter presses must not send several
  // attempts, which would use up the account lockout allowance.
  const inFlight = useRef(false);

  async function submitPassword(event: React.FormEvent) {
    event.preventDefault();
    if (inFlight.current) return;
    inFlight.current = true;
    setBusy(true);
    setError(null);
    try {
      await api.login(username.trim(), password);
      setPassword("");
      setStep("mfa");
    } catch (err) {
      setError((err as Error).message);
    } finally {
      inFlight.current = false;
      setBusy(false);
    }
  }

  async function submitCode(event: React.FormEvent) {
    event.preventDefault();
    if (inFlight.current) return;
    inFlight.current = true;
    setBusy(true);
    setError(null);
    try {
      onSignedIn(await api.mfa(code.trim()));
    } catch (err) {
      setError((err as Error).message);
      setCode("");
    } finally {
      inFlight.current = false;
      setBusy(false);
    }
  }

  return (
    <section className="card auth-card" aria-label="Staff sign-in">
      <p className="eyebrow">Staff sign-in · step {step === "password" ? "1" : "2"} of 2</p>
      <h2 className="card-title">{step === "password" ? "Sign in" : "Enter your authenticator code"}</h2>

      {step === "password" ? (
        <form className="auth-form" onSubmit={submitPassword}>
          <label className="field">
            <span>Username</span>
            <input value={username} onChange={(e) => setUsername(e.target.value)} autoComplete="username" required />
          </label>
          <label className="field">
            <span>Password</span>
            <input
              type="password"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              autoComplete="current-password"
              required
            />
          </label>
          <button type="submit" className="btn btn-primary" disabled={busy}>
            {busy ? "Checking…" : "Continue"}
          </button>
        </form>
      ) : (
        <form className="auth-form" onSubmit={submitCode}>
          <label className="field">
            <span>6-digit code (TOTP)</span>
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
          <div className="btn-row">
            <button type="submit" className="btn btn-primary" disabled={busy || code.length !== 6}>
              {busy ? "Verifying…" : "Verify"}
            </button>
            <button type="button" className="btn" onClick={() => setStep("password")}>
              Back
            </button>
          </div>
        </form>
      )}

      {error && (
        <div className="banner banner-danger" role="alert">
          {error}
        </div>
      )}
      {hint && <div className="small muted auth-hint">{hint}</div>}
    </section>
  );
}

export function DemoLoginHint() {
  return (
    <>
      <strong>Synthetic demo accounts:</strong> <code>dr.example</code>, <code>dr.other</code>, <code>auditor</code>,{" "}
      <code>admin</code>. The password is <code>JEEVAFLOW_DEMO_STAFF_PASSWORD</code> in <code>backend/.env</code>. Get
      the current code with <code>venv/bin/python -m app.security.demo_totp</code>.
    </>
  );
}
