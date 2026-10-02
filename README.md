# JeevaFlow

**One Health Journey. Every instruction, followed through.**

> AI reads. Rules validate. Humans decide.

JeevaFlow turns fragmented medical documents and WhatsApp messages into a source-linked, longitudinal patient journey, and tracks every documented doctor instruction as an **Open Loop** until a person confirms it was followed through.

---

## Problem

Patients carry their health history as loose PDFs, phone photos and WhatsApp forwards. Instructions such as *"Repeat HbA1c after 3 months"* are written once and then forgotten. Nobody tracks whether they happened, and nobody notices when two reports disagree.

## Solution

JeevaFlow:

1. **Ingests** PDFs, photos and WhatsApp media through one pipeline.
2. **Reads** lab values and doctor instructions with deterministic rules.
3. **Anchors** every extracted item to an exact quote, page and character span in the source (quote-or-reject).
4. **Builds** a chronological timeline per patient.
5. **Tracks** instructions as Open Loops, with due dates derived from the stated interval.
6. **Suggests** when a newer document may fulfil an open loop (`POTENTIAL_MATCH`), but **never closes it on its own**.
7. **Flags** conflicting values for human review without choosing a winner.

---

## Core architecture

```
 WhatsApp (Twilio, signed) ─┐                       receive_document()
 Portal upload / camera ────┴─► secure gateway ──►  validate · worker inspect/sanitise
   (OTP session + consent)                          · malware scan · SHA-256
                                                    · AES-256-GCM vault (QUARANTINED)
                     OTP verified + consent ──────► process_document()
                                                    · decrypt → isolated worker (OCR)
                                                    · rules: labs, instructions,
                                                      medications, allergies, prescriber
                                                    · quote-or-reject + bbox provenance
                                                    · potential Open Loop matches
```

| Module | Responsibility |
| --- | --- |
| `app/pipeline.py` | Secure ingestion gateway and the single processing pipeline |
| `app/worker.py`, `app/worker_client.py` | Isolated processing worker (parse, sanitise, OCR, render) |
| `app/extraction.py`, `app/facts.py` | Rule-based extraction; medication/allergy facts with states |
| `app/provenance.py` | Quote-or-reject anchoring, evidence serialization |
| `app/vault.py` | Encrypted document storage and crypto-shredding |
| `app/identity.py` | Separate identity database (patients' identity, staff, sessions) |
| `app/security/` | Crypto, auth/MFA, OTP, consent, scanner, audit chain, PHI-safe logs, rate limits |
| `app/routes/` | Webhook, portal, auth, doctor, security, admin and demo APIs |
| `app/doctor_brief.py` | Doctor-ready form (consent-scoped) |
| `app/loops.py`, `app/matching.py`, `app/conflicts.py`, `app/timeline.py` | Open Loops, matching, conflicts, timeline |
| `app/retention.py` | Retention sweep and expiry |
| `frontend/` | Doctor console, patient portal, security dashboard, demo room |

## Security architecture

> WhatsApp is only the communication channel. JeevaFlow is the controlled healthcare-data environment.

This is a defense-in-depth design built and tested with **synthetic data only**. It is not "100% secure", and these technical controls do not by themselves make the system HIPAA or DPDP compliant.

```
Patient → WhatsApp → Twilio → signed webhook → OTP → consent → secure ingestion
→ validation + malware scan → AES-256-GCM vault → isolated OCR/extraction
→ source-linked facts (SOURCE_FACT / AI_INFERRED / UNCERTAIN / MISSING)
→ doctor-ready form → doctor MFA → consent check → secure evidence viewer
→ tamper-evident audit log
```

| Layer | What is implemented | Where |
| --- | --- | --- |
| Untrusted channel | `X-Twilio-Signature` always enforced (fails closed), MessageSid replay table, AccountSid check, freshness check against Twilio's message record, invalid-signature attempts limited per IP, valid messages limited per sender and patient, body-size cap. Replies are generic templates only. Unknown numbers get a generic reply and their media is never downloaded. Media is fetched server-side and then deleted from Twilio. A restricted API key is preferred for REST calls. | `routes/webhook.py`, `whatsapp.py` |
| Transactions + OTP | `txn_<random>` binds patient, verification, consent and documents, and expires after 30 min. 6-digit CSPRNG OTP valid 5 min and single use, stored only as HMAC-SHA256(secret, otp + txn). Max 5 attempts with exponential back-off, then the transaction locks. Constant-time compare and one generic error. | `security/otp.py` |
| Consent | Purpose, doctor, scopes, expiry and retention. Grant, revoke or end now. Checked **server-side on every doctor request**; revocation takes effect on the next request. | `security/consent.py`, `routes/portal.py` |
| Ingestion gateway | Size, declared type and magic bytes. In the isolated worker: structure, page/dimension limits, encrypted/active-content PDFs rejected, decompression bombs rejected, PDFs scrubbed and rebuilt, images re-encoded without EXIF. Then malware scan (ClamAV if installed, otherwise heuristics), SHA-256, encryption, quarantine. | `pipeline.py`, `worker.py`, `security/scanner.py` |
| Encryption | Per-document random DEK with AES-256-GCM, a unique nonce and the document ref as AAD. The DEK is wrapped by a KEK behind a `KeyProvider` interface (swap in KMS / Key Vault / Vault / HSM). Medical text columns and identity columns are field-encrypted with separate keys. | `security/crypto.py`, `vault.py` |
| Identity separation | `identity.db` holds phone (HMAC blind index + ciphertext), name, DOB and staff accounts. `jeevaflow.db` knows patients only by `pt_<random>`. | `identity.py`, `models.py` |
| Isolated processing | A separate process per job with no secrets in its environment, no DB access, egress blocked, no core dumps, a CPU limit, a refusal to run as root, and a private temp dir deleted after the job. Local Tesseract only, and nothing is sent to external AI. | `worker.py`, `worker_client.py` |
| Hallucination protection | Extractive rules only. Every fact has quote, page, bbox, confidence, document SHA-256 and pipeline version (quote-or-reject). Formulary, dose range, unit, frequency, duration and date checks plus cross-field consistency. Absent data is `MISSING`; normalisations are `AI_INFERRED`; anything doubtful is `UNCERTAIN`. The doctor confirms. | `facts.py`, `pipeline.py` |
| Doctor access | Password (scrypt) + TOTP, with TOTP replay blocked. Lockout after 5 failures. Server-side sessions (HMAC-hashed tokens) in HttpOnly SameSite=Strict cookies, a CSRF token on every state change, idle timeout, deny-by-default roles (DOCTOR / ADMIN / AUDITOR / PATIENT / SERVICE). No "list all patients" API. Unknown and unconsented objects return the same 403. Bulk-access anomaly detection. | `security/auth.py`, `routes/doctor.py` |
| Evidence viewer | No file downloads. A single-use 60 s token bound to the session and document, with consent re-checked at view time. The worker renders only the page with the region highlighted and a watermark (doctor ID + time). Served with `Cache-Control: no-store` and held only as an in-memory blob URL. | `routes/doctor.py`, `SecureEvidence.tsx` |
| PHI-safe logging | Allow-listed events and fields only. Other app logs are dropped, tracebacks are stripped and phone-like numbers are scrubbed from third-party logs. Validation errors never echo input. | `security/phi_log.py`, `main.py` |
| Audit chain | Append-only (SQLite triggers). SHA-256 hash chain plus HMAC, with the head anchored in the identity DB. `Verify audit chain` reports the first broken event. | `security/audit.py`, `routes/security.py` |
| Retention + deletion | Unverified quarantined documents are crypto-shredded when their transaction expires, and documents expire after 90 days. Delete = destroy DEK → delete object → delete rows → audit. | `retention.py`, `pipeline.py` |
| API hardening | Strict CORS, Origin check, security headers (CSP `default-src 'none'`, nosniff, DENY, no-referrer, HSTS in production), generic 500s, request-size cap, uploads kept in memory. | `main.py` |

**Demo-grade controls**, labelled as such on the security dashboard:

- KEK in an environment variable. **Production:** KMS/HSM.
- Heuristic malware scanner when ClamAV is absent. **Production:** ClamAV or a managed scanner.
- In-process egress block. **Production:** a worker container with no network.
- In-memory rate limits. **Production:** Redis. Counters are intentionally reset when the backend restarts (including `--reload` restarts). Limits are set with `JEEVAFLOW_RATE_LIMIT_PER_{IP,SENDER,USER,PATIENT}` and `JEEVAFLOW_RATE_LIMIT_WINDOW_SECONDS` (see `backend/.env.example`). A blocked request gets HTTP 429 with `{"error": "rate_limit_exceeded", "message": ..., "retry_after": N}` and `Retry-After`; outside production, responses also carry `X-RateLimit-Limit/Remaining/Reset`. In demo mode, `POST /api/v1/demo/rate-limit/reset` (local only) clears the counters and nothing else.
- SQLite with field-level encryption. **Production:** SQLCipher/managed DB with least-privilege accounts.
- Plain HTTP locally. **Production:** `JEEVAFLOW_ENV=production` (HTTPS, Secure cookies, no demo mode, no API docs).
- The frontend has no CSP of its own. **Production:** serve it with a strict CSP header.

## Patient journey

The timeline merges **documents received**, **observations** (for example HbA1c 9.4%) and **doctor instructions**. Each item is dated by the report date found in the document (`Report Date: 15/06/2026`), falling back to the date received.

## Evidence and provenance

Every observation and instruction stores a `document_id`, an `evidence_id`, the exact quote, the page number and the start/end character position. Before anything is stored, the quote is checked against the page text. **Items that cannot be anchored are dropped** (`evidence_rejected` in the upload response). All extracted observations start in `REVIEW`. Only a person can mark them `VERIFIED` or `REJECTED`. The original file can always be opened from the evidence viewer.

## Open Loops

| State | Meaning |
| --- | --- |
| `OPEN` | Instruction documented, nothing newer addresses it |
| `OVERDUE` | `OPEN` and past its due date (computed) |
| `POTENTIAL_MATCH` | A newer, source-backed result may fulfil it. **Awaiting a person.** |
| `NEEDS_REVIEW` | A person flagged it for review |
| `CLOSED` | A person confirmed completion |

**Matching rules.** All of these must hold:

1. The loop is active.
2. The instruction names the test.
3. The result comes from a different document with a later report date.
4. The result has source evidence and is not rejected.

**Only an explicit human action closes a loop.** `POST /api/v1/doctor/commitments/{id}/confirm-completion` is recorded under the signed-in, MFA-verified doctor. Every state change, system or human, is written to an append-only audit trail (`loop_events`). Rejecting the observation behind a match withdraws that match.

## Conflict handling

Different values for the **same test on the same report date** (for example 9.4% vs 8.2%) are returned as `CONFLICT` / `HUMAN_REVIEW_REQUIRED`, with full evidence for each side. Results on different dates form a series, not a conflict. JeevaFlow never decides which value is correct.

## Technology stack

- **Backend:** Python 3, FastAPI, SQLAlchemy, SQLite, PyMuPDF, Tesseract (pytesseract), Pillow, OpenCV, httpx
- **Frontend:** React 19, TypeScript, Vite, plain CSS
- **Tests:** pytest (backend), Vitest + Testing Library (frontend)
- **Messaging:** Twilio WhatsApp (sandbox or sender)

## Project structure

```
jeevaflow/
├── backend/
│   ├── app/                 FastAPI application (modules listed above)
│   ├── tests/               pytest suite (no network, no Twilio credentials needed)
│   ├── test_api.py          live end-to-end smoke test against a running server
│   ├── seed_demo.py         initialise DB, demo patient and demo PDFs
│   ├── run_demo.sh          start server + run smoke test
│   ├── requirements.txt
│   └── .env.example
├── frontend/
│   ├── src/                 App, components, API client, tests
│   └── vite.config.ts       dev server proxies /api → :8000
└── data/demo/               synthetic demo PDFs (generated, deterministic)
```

---

## Setup

Prerequisites: Python 3.11+, Node 20+, Tesseract (`brew install tesseract`). Optional: ClamAV (`brew install clamav`).

```bash
cd backend
python3 -m venv venv && venv/bin/pip install -r requirements.txt
cp .env.example .env
venv/bin/python -m app.security.init_secrets   # random keys + demo secrets, chmod 600
venv/bin/python -m app                          # http://127.0.0.1:8000

cd ../frontend && npm install && npm run dev    # http://localhost:5173
```

The backend refuses to start without its secrets. `backend/.env`, the databases and `backend/vault/` are git-ignored.

### Testing

```bash
cd backend && venv/bin/python -m pytest      # 119 tests incl. the security suite
cd frontend && npm test                      # Vitest
cd backend && venv/bin/python test_api.py    # live end-to-end check (server running)
```

`tests/test_security.py` covers invalid signatures, replayed MessageSid, expired, incorrect and brute-forced OTPs, OTP reuse, revoked and expired consent, wrong doctor, scope limits, malicious, oversized, mismatched and bomb files, ID enumeration, direct downloads, single-use/expired/forwarded evidence tokens, PHI in logs, audit tampering and truncation, missing authentication and roles, SQL injection, XSS, CSRF and foreign origins, and encryption at rest.

## Demo instructions (synthetic data only)

Open http://localhost:5173/demo.

1. **Phone → Synthetic prescription.** The form is signed and sent through the real webhook. The reply is generic and contains a portal link and an OTP. **Forged signature** shows a 403.
2. Open the link, enter the code, and **grant consent** to Dr. Example. The document is decrypted in the isolated worker and read. The phone receives "Your document has been processed…".
3. **Doctor** tab: sign in as `dr.example`. The password is `JEEVAFLOW_DEMO_STAFF_PASSWORD` in `backend/.env`; the TOTP code comes from `venv/bin/python -m app.security.demo_totp`. The doctor-ready form lists medications with source-fact, AI-inferred, uncertain and missing labels, plus allergies, notes, uncertain and missing items. **Source → View source region** shows the watermarked page region.
4. Portal: **Revoke**. The doctor's next request is denied, and the patient disappears from "My patients".
5. **Security** tab: sign in as `auditor`. Verify the chain, click **Tamper** on an event, verify again to see it BROKEN at that event, then **Restore**.

Also try **Text "Hi"** for portal access, the lab and follow-up reports for Open Loops and conflicts, `dr.other` to see access denied, and **Reset demo** to crypto-shred all demo documents.

The demo controls work only in demo mode, from localhost, and never through a proxy or tunnel. They act only on the synthetic patient.

## Safety boundaries

- JeevaFlow **never diagnoses, prescribes or changes medication**, and its replies make no medical claims.
- Extracted values are **never treated as verified** until a person verifies them.
- Unanchored extractions are **rejected**, not stored.
- Conflicts are **never silently resolved**.
- Consequential loops are **never closed automatically**. `POTENTIAL_MATCH → CLOSED` requires a named human.
- Unreadable images are answered with **RETAKE**, and nothing is read from them.
- Unknown WhatsApp numbers are **never** turned into new patients.
- The demo uses **synthetic data only**.

## Known limitations

- Extraction is rule-based and covers a small set of tests (HbA1c, glucose, creatinine, LDL, blood pressure) and instruction verbs (repeat, follow-up with/in, review, refer to, recheck). Free-text clinical notes, tables with unusual layouts and handwriting are not reliably understood.
- Numeric report dates are read as day-first (DD/MM/YYYY). If no report date is found, the date received is used for the timeline and as the basis for due dates.
- OCR quality depends on Tesseract. The HbA1c label tolerates common OCR misreads ("HbAtc"). The value must still match exactly, and the stored quote shows the literal OCR text.
- Hackathon MVP: see the demo-grade controls in *Security architecture*. Do not use with real patient data without the production replacements and an independent security review.
- WhatsApp processing runs synchronously inside the webhook. Very large or multi-page scanned PDFs may approach Twilio's 15-second webhook timeout.
- Staff accounts are seeded synthetic demo accounts; there is no staff enrolment UI yet.
- SQLite with additive in-place schema upgrades. There is no migration framework.
