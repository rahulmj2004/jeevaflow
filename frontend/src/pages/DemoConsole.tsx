import { useCallback, useEffect, useRef, useState } from "react";
import { api } from "../api";
import { ErrorBanner } from "../components/StatusBadge";
import type { DemoInfo, PhoneNotification, SimulateResult } from "../types";
import { Card } from "./DoctorConsole";

type Bubble =
  | { id: number; side: "out"; text: string }
  | { id: number; side: "in"; text: string; meta?: string };

const STEPS = [
  "Send the synthetic prescription from the phone below (signed Twilio webhook).",
  "Open the portal link from the reply and enter the one-time code.",
  "Grant consent to Dr. Example — the document is decrypted and read in the isolated worker.",
  "Sign in as dr.example (password + TOTP) in the Doctor tab: only consented data is shown.",
  "Click a fact's Source to view the watermarked region from a single-use link.",
  "Revoke consent in the portal: the doctor's next request is denied.",
  "Sign in as auditor on the Security tab: verify the chain, tamper with one event, verify again.",
];

const SIGNATURE_LABEL: Record<string, string> = {
  VERIFIED: "Verified",
  REJECTED: "Rejected",
};

/** Plain-language result of one simulated webhook request. */
export function WebhookOutcome({ result }: { result: SimulateResult }) {
  const rateLimited = result.http_status === 429;
  const accepted = result.http_status === 200;
  const tone = rateLimited ? "warning" : accepted ? "success" : "danger";
  const headline = rateLimited
    ? "Request blocked by abuse-prevention rate limit."
    : accepted
      ? "Request accepted"
      : `Request rejected (HTTP ${result.http_status})`;

  return (
    <div className={`banner banner-${tone}`} role="status">
      <strong>{headline}</strong>
      <div className="small">
        Twilio signature: {result.signature ? SIGNATURE_LABEL[result.signature] : "—"} · Rate limit:{" "}
        {rateLimited ? "Exceeded" : accepted ? "Within limit" : "—"}
        {rateLimited && result.retry_after ? ` · Retry after ${result.retry_after}s` : ""}
      </div>
    </div>
  );
}

/** Turn URLs in a WhatsApp reply into links (new tab, no referrer). */
function Linkified({ text }: { text: string }) {
  const parts = text.split(/(https?:\/\/\S+)/g);
  return (
    <>
      {parts.map((part, index) =>
        /^https?:\/\//.test(part) ? (
          <a key={index} href={part} target="_blank" rel="noreferrer noopener">
            {part}
          </a>
        ) : (
          <span key={index}>{part}</span>
        ),
      )}
    </>
  );
}

export function DemoConsole() {
  const [info, setInfo] = useState<DemoInfo | null>(null);
  const [bubbles, setBubbles] = useState<Bubble[]>([]);
  const [notifications, setNotifications] = useState<PhoneNotification[]>([]);
  const [last, setLast] = useState<SimulateResult | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  // Synchronous guard: a fast double click can fire before the
  // disabled state re-renders, so `busy` alone cannot prevent a
  // duplicate webhook request.
  const inFlight = useRef(false);
  const polling = useRef(false);

  useEffect(() => {
    api
      .demo()
      .then(setInfo)
      .catch((err: Error) => setError(`Demo mode unavailable: ${err.message}`));
  }, []);

  // Read-only, not rate limited. Skipped while the tab is hidden or
  // a previous poll is still pending, so polls never pile up.
  const poll = useCallback(() => {
    if (polling.current || document.hidden) return;
    polling.current = true;
    api
      .demoPhone()
      .then(setNotifications)
      .catch(() => undefined)
      .finally(() => {
        polling.current = false;
      });
  }, []);

  useEffect(() => {
    poll();
    const timer = window.setInterval(poll, 4000);
    return () => window.clearInterval(timer);
  }, [poll]);

  async function send(document: string | null, label: string, tamper = false) {
    // One request per click and never retried automatically: a retry
    // would be a new message to the webhook.
    if (inFlight.current) return;
    inFlight.current = true;
    setBusy(label);
    setError(null);
    setNotice(null);
    const id = Date.now();
    setBubbles((current) => [...current, { id, side: "out", text: label }]);
    try {
      const result = await api.demoWhatsApp(document, tamper);
      setLast(result);
      const rateLimited = result.http_status === 429;
      const meta = rateLimited
        ? "Blocked by rate limit"
        : result.signature === "VERIFIED"
          ? "Twilio signature verified"
          : `Webhook rejected (HTTP ${result.http_status})`;
      const empty = rateLimited ? "(no reply — rate limit exceeded)" : "(no reply — request rejected)";
      setBubbles((current) => [
        ...current,
        ...(result.messages.length
          ? result.messages.map((text, index) => ({ id: id + index + 1, side: "in" as const, text, meta }))
          : [{ id: id + 1, side: "in" as const, text: empty, meta }]),
      ]);
    } catch (err) {
      setError((err as Error).message);
    } finally {
      inFlight.current = false;
      setBusy(null);
    }
  }

  async function resetRateLimits() {
    if (inFlight.current) return;
    inFlight.current = true;
    setBusy("rate-limit");
    setError(null);
    try {
      await api.demoRateLimitReset();
      setNotice("Rate-limit counters cleared. No patient data, consent or audit records were changed.");
    } catch (err) {
      setError((err as Error).message);
    } finally {
      inFlight.current = false;
      setBusy(null);
    }
  }

  async function reset() {
    if (inFlight.current) return;
    if (!window.confirm("Crypto-shred all synthetic demo documents and remove demo consents?")) return;
    inFlight.current = true;
    setBusy("reset");
    try {
      await api.demoReset();
      setBubbles([]);
      setLast(null);
      poll();
    } catch (err) {
      setError((err as Error).message);
    } finally {
      inFlight.current = false;
      setBusy(null);
    }
  }

  return (
    <>
      {error && <ErrorBanner message={error} />}

      <div className="demo-layout">
        <section className="phone" aria-label="Synthetic patient phone">
          <header className="phone-head">
            <strong>WhatsApp · JeevaFlow</strong>
            <span className="small">{info?.patient.phone_masked ?? "synthetic patient"}</span>
          </header>
          <div className="phone-chat" aria-live="polite">
            {bubbles.length === 0 && <p className="phone-empty small">Send a synthetic document to start.</p>}
            {bubbles.map((bubble) => (
              <div key={bubble.id} className={`bubble bubble-${bubble.side}`}>
                <Linkified text={bubble.text} />
                {bubble.side === "in" && bubble.meta && <span className="bubble-meta">{bubble.meta}</span>}
              </div>
            ))}
            {notifications.map((item, index) => (
              <div key={`n-${index}`} className="bubble bubble-in">
                {item.body}
                <span className="bubble-meta">Notification · {item.delivery}</span>
              </div>
            ))}
          </div>
          <div className="phone-actions">
            {(info?.documents ?? []).map((doc) => (
              <button
                key={doc.key}
                type="button"
                className="btn btn-small"
                disabled={busy !== null}
                onClick={() => send(doc.key, `📄 ${doc.title}`)}
              >
                {doc.title}
              </button>
            ))}
            <button type="button" className="btn btn-small" disabled={busy !== null} onClick={() => send(null, "Hi")}>
              Text “Hi” (portal access)
            </button>
            <button
              type="button"
              className="btn btn-small btn-ghost-danger"
              disabled={busy !== null}
              onClick={() => send("prescription", "📄 Prescription with a forged signature", true)}
            >
              Forged signature
            </button>
          </div>
        </section>

        <div className="demo-side">
          <Card title="Demo walkthrough" subtitle="Synthetic patient and documents only.">
            <ol className="demo-steps">
              {STEPS.map((step) => (
                <li key={step}>{step}</li>
              ))}
            </ol>
            {last?.portal_link && (
              <p>
                <a className="btn btn-primary" href={last.portal_link} target="_blank" rel="noreferrer noopener">
                  Open patient portal
                </a>
              </p>
            )}
          </Card>

          <Card title="What the webhook saw">
            {last && <WebhookOutcome result={last} />}
            {last ? (
              <dl className="evidence-grid">
                <div>
                  <dt>HTTP</dt>
                  <dd>{last.http_status}</dd>
                </div>
                <div>
                  <dt>Signature</dt>
                  <dd>{last.signature ?? "—"}</dd>
                </div>
                <div>
                  <dt>Rate limit</dt>
                  <dd>{last.http_status === 429 ? "Exceeded" : last.http_status === 200 ? "Within limit" : "—"}</dd>
                </div>
                <div>
                  <dt>Outcome</dt>
                  <dd>{last.status ?? "—"}</dd>
                </div>
                <div>
                  <dt>Transaction</dt>
                  <dd className="mono small">{last.transaction_ref ?? "—"}</dd>
                </div>
              </dl>
            ) : (
              <p className="small muted">Nothing sent yet.</p>
            )}
            <p className="small muted">
              Signed with {info?.signing === "TWILIO_AUTH_TOKEN" ? "your Twilio auth token" : "the local demo signing token"}{" "}
              and sent through the real webhook code path. Only the media download is replaced by the local synthetic
              file; media deletion and freshness are reported as simulated.
            </p>
          </Card>

          <Card title="Demo accounts">
            <ul className="small">
              {(info?.staff ?? []).map((account) => (
                <li key={account.username}>
                  <code>{account.username}</code> · {account.role} · {account.display_name}
                </li>
              ))}
            </ul>
            <p className="small muted">
              Password: <code>JEEVAFLOW_DEMO_STAFF_PASSWORD</code> in <code>backend/.env</code>. Current TOTP codes:{" "}
              <code>venv/bin/python -m app.security.demo_totp</code>
            </p>
            {notice && <p className="small">{notice}</p>}
            <button type="button" className="btn btn-small" disabled={busy !== null} onClick={resetRateLimits}>
              Reset rate limits
            </button>{" "}
            <button type="button" className="btn btn-small btn-ghost-danger" disabled={busy !== null} onClick={reset}>
              Reset demo
            </button>
          </Card>
        </div>
      </div>
    </>
  );
}
