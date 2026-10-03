import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import App from "../App";
import { setCsrf } from "../api";
import { DoctorForm, DriftAlert } from "../components/DoctorForm";
import { buildSteps } from "../components/DocumentUpload";
import { AiMatchExplanation, OpenLoops } from "../components/OpenLoops";
import { StaffLogin } from "../components/StaffLogin";
import { DemoConsole } from "../pages/DemoConsole";
import { formatDate, formatValue, statusLabel, toneFor } from "../format";
import type { DoctorForm as Form, IngestionResult, OpenLoop } from "../types";

type Route = (url: string, init?: RequestInit) => { status?: number; body: unknown } | undefined;

function mockFetch(route: Route) {
  return vi.spyOn(globalThis, "fetch").mockImplementation(async (input, init) => {
    const url = typeof input === "string" ? input : input instanceof URL ? input.href : input.url;
    const match = route(url, init);
    if (!match) return new Response(JSON.stringify({ detail: "Authentication required." }), { status: 401 });
    return new Response(JSON.stringify(match.body), { status: match.status ?? 200 });
  });
}

const evidence = (id: number, quote: string, source = "WHATSAPP") => ({
  id,
  evidence_id: id,
  document_filename: "Document received via WhatsApp",
  document_source: source,
  document_date: "2026-06-15",
  document_received_at: "2026-10-01T10:00:00",
  quote,
  page_number: 1,
  start_position: 0,
  end_position: quote.length,
});

function potentialLoop(): OpenLoop {
  return {
    id: 7,
    instruction: "Repeat HbA1c after 3 months",
    due_date: "2026-09-15",
    state: "POTENTIAL_MATCH",
    evidence_id: 3,
    document_id: 1,
    document_filename: "Document uploaded in portal",
    document_date: "2026-06-15",
    evidence: evidence(3, "Repeat HbA1c after 3 months", "WEB"),
    potential_matches: [
      {
        id: 11,
        status: "PENDING",
        rule: "Instruction mentions HbA1c",
        observation: {
          id: 5,
          observation_type: "HbA1c",
          value: "8.1",
          unit: "%",
          event_date: "2026-09-20",
          review_status: "REVIEW",
        },
        document_id: 2,
        document_filename: "Document received via WhatsApp",
        document_source: "WHATSAPP",
        evidence: evidence(9, "HbA1c: 8.1 %"),
        reviewed_by: null,
        reviewed_at: null,
        created_at: "2026-10-01T10:00:00",
      },
    ],
    confirmed_match: null,
    created_at: "2026-10-01T10:00:00",
    updated_at: "2026-10-01T10:00:00",
  };
}

function formFixture(): Form {
  const ev = (id: number, quote: string) => ({
    evidence_id: id,
    document_ref: "doc_x",
    document_label: "Document received via WhatsApp",
    document_date: "2026-09-20",
    page_number: 1,
    quote,
    has_region: true,
    confidence: 1,
  });
  return {
    generated_at: "2026-10-02T10:00:00",
    patient: { case_alias: "CASE-TEST-0001", age_band: "20-29", demographics_shared: true },
    consent: { ref: "cns_1", purpose: "CLINICAL_REVIEW", scopes: ["MEDICATIONS", "LABS"], expires_at: "2026-10-09T10:00:00", can_view_source: false },
    not_shared: [{ scope: "ALLERGIES", label: "Allergies" }, { scope: "INSTRUCTIONS", label: "Doctor instructions and follow-ups" }],
    medications: [
      {
        ref: "fct_1",
        label: "Metformin 500 mg",
        state: "AI_INFERRED",
        review_status: "REVIEW",
        confidence: 1,
        evidence: ev(1, "Tab Metformin 500 mg 1-0-1 x 30 days"),
        fields: {
          name: { value: "Metformin", state: "SOURCE_FACT", source_text: "Metformin", note: null },
          dose: { value: "500 mg", state: "SOURCE_FACT", source_text: "500 mg", note: null },
          frequency: { value: "twice daily", state: "AI_INFERRED", source_text: "1-0-1", note: "Normalised from “1-0-1”." },
          duration: { value: "30 days", state: "SOURCE_FACT", source_text: "x 30 days", note: null },
        },
      },
      {
        ref: "fct_2",
        label: "Glimepiride 1 mg",
        state: "SOURCE_FACT",
        review_status: "REVIEW",
        confidence: 1,
        evidence: ev(2, "Tab Glimepiride 1 mg"),
        fields: {
          name: { value: "Glimepiride", state: "SOURCE_FACT", source_text: "Glimepiride", note: null },
          dose: { value: "1 mg", state: "SOURCE_FACT", source_text: "1 mg", note: null },
          frequency: { value: null, state: "MISSING", source_text: null, note: "Frequency not stated in the source." },
          duration: { value: null, state: "MISSING", source_text: null, note: "Duration not stated in the source." },
        },
      },
    ],
    prescribers: [],
    allergies: null,
    labs: [],
    conflicts: [],
    notes: null,
    uncertain: [{ section: "MEDICATION", item: "Zyxorin 50 mg", field: "name", note: "Not in the reference formulary; confirm the medicine name." }],
    missing: [{ section: "MEDICATION", item: "Glimepiride 1 mg", field: "frequency", note: "Frequency not stated in the source." }],
    sources: [],
    notice: "JeevaFlow does not diagnose, prescribe or change medication.",
  };
}

afterEach(() => {
  vi.restoreAllMocks();
  setCsrf("staff", null);
  window.history.replaceState(null, "", "/");
});

describe("format helpers", () => {
  it("formats dates and values without timezone drift", () => {
    expect(formatDate("2026-06-15")).toBe("15 Jun 2026");
    expect(formatDate(null)).toBe("—");
    expect(formatValue("9.4", "%")).toBe("9.4%");
    expect(statusLabel("POTENTIAL_MATCH")).toBe("Potential match");
    expect(toneFor("OVERDUE")).toBe("danger");
  });
});

describe("upload steps", () => {
  const base: IngestionResult = {
    ref: "doc_x",
    label: "Document uploaded in portal",
    processing_status: "REJECTED",
    quality_status: "RETAKE",
    quality_reason: "Image is blurred.",
    processing_error: null,
    content_kind: null,
    needs_manual_review: false,
    extraction_method: null,
    document_date: null,
    source: "WEB",
    scan_status: "CLEAN",
    scan_engine: "heuristic",
    ingestion_status: "RETAKE",
    duplicate: false,
    observations_created: 0,
    open_loops_created: 0,
    facts_created: 0,
    evidence_created: 0,
    evidence_rejected: 0,
    potential_matches: [],
    stages: [
      { stage: "MALWARE_SCAN", status: "CLEAN", detail: "heuristic" },
      { stage: "QUALITY_CHECK", status: "RETAKE", detail: "Image is blurred." },
    ],
    error: null,
  };

  it("shows encryption and scanning, and never shows a retake as extracted", () => {
    const steps = buildSteps({ kind: "done", fileName: "x.png", result: base });
    expect(steps.find((s) => s.label === "Encrypted")?.state).toBe("done");
    expect(steps.find((s) => s.label === "Validated & scanned")?.detail).toBe("heuristic");
    expect(steps.find((s) => s.label === "Extracted")?.state).toBe("skip");
    expect(steps.at(-1)?.state).toBe("fail");
  });

  it("shows a handwritten document as doctor review, not as a failure", () => {
    const handwritten: IngestionResult = {
      ...base,
      processing_status: "PROCESSED",
      quality_status: "GOOD",
      ingestion_status: "PROCESSED",
      content_kind: "HANDWRITTEN",
      needs_manual_review: true,
      stages: [
        { stage: "MALWARE_SCAN", status: "CLEAN", detail: "heuristic" },
        { stage: "QUALITY_CHECK", status: "GOOD", detail: null },
        { stage: "EXTRACTION", status: "SKIPPED", detail: "No machine-readable text" },
        { stage: "DOCTOR_REVIEW", status: "REQUIRED", detail: "HANDWRITTEN" },
        { stage: "COMPLETE", status: "REVIEW", detail: null },
      ],
    };

    const steps = buildSteps({ kind: "done", fileName: "rx.jpg", result: handwritten });
    expect(steps.at(-1)).toMatchObject({ state: "warn", detail: "Needs doctor review" });
    expect(steps.find((s) => s.label === "Extracted")?.detail).toBe("Doctor reads original");
    expect(steps.some((s) => s.state === "fail")).toBe(false);
  });
});

describe("OpenLoops", () => {
  it("confirms as the signed-in doctor with the CSRF token", async () => {
    setCsrf("staff", "csrf-123");
    const fetchMock = mockFetch((url) => (url.includes("confirm-completion") ? { body: { ...potentialLoop(), state: "CLOSED" } } : undefined));
    const onChanged = vi.fn();

    render(<OpenLoops loops={[potentialLoop()]} onChanged={onChanged} onInspect={() => undefined} />);

    expect(screen.getByText(/Potential completion detected/)).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Confirm completion" }));

    await waitFor(() => expect(onChanged).toHaveBeenCalled());
    const [url, init] = fetchMock.mock.calls[0];
    expect(String(url)).toContain("/api/v1/doctor/commitments/7/confirm-completion");
    expect(JSON.parse(String(init?.body))).toEqual({ match_id: 11 });
    expect(new Headers(init?.headers).get("X-CSRF-Token")).toBe("csrf-123");
  });
});

describe("StaffLogin", () => {
  it("requires a TOTP code after the password", async () => {
    mockFetch((url) => {
      if (url.endsWith("/auth/login")) return { body: { mfa_required: true } };
      if (url.endsWith("/auth/mfa")) return { body: { user_ref: "usr_1", display_name: "Dr. Example (synthetic)", role: "DOCTOR", csrf_token: "t" } };
      return undefined;
    });
    const onSignedIn = vi.fn();

    render(<StaffLogin onSignedIn={onSignedIn} />);

    await userEvent.type(screen.getByLabelText("Username"), "dr.example");
    await userEvent.type(screen.getByLabelText("Password"), "synthetic");
    await userEvent.click(screen.getByRole("button", { name: "Continue" }));

    const code = await screen.findByLabelText("6-digit code (TOTP)");
    await userEvent.type(code, "123456");
    await userEvent.click(screen.getByRole("button", { name: "Verify" }));

    await waitFor(() => expect(onSignedIn).toHaveBeenCalledWith(expect.objectContaining({ role: "DOCTOR" })));
  });

  it("shows the generic error", async () => {
    mockFetch(() => ({ status: 401, body: { detail: "Invalid credentials." } }));

    render(<StaffLogin onSignedIn={() => undefined} />);
    await userEvent.type(screen.getByLabelText("Username"), "x");
    await userEvent.type(screen.getByLabelText("Password"), "y");
    await userEvent.click(screen.getByRole("button", { name: "Continue" }));

    expect(await screen.findByRole("alert")).toHaveTextContent("Invalid credentials.");
  });
});

describe("DoctorForm", () => {
  it("labels source facts, AI-inferred, uncertain and missing information and respects scope", async () => {
    mockFetch((url) => (url.includes("/form") ? { body: formFixture() } : undefined));

    render(<DoctorForm patientRef="pt_synthetic" doctorName="Dr. Example (synthetic)" />);

    expect(await screen.findByText("Case CASE-TEST-0001")).toBeInTheDocument();
    expect(screen.getAllByText("AI-inferred").length).toBeGreaterThan(0);
    expect(screen.getByText("Normalised from “1-0-1”.")).toBeInTheDocument();
    expect(screen.getAllByText("Not stated").length).toBe(2);
    expect(screen.getByText(/Zyxorin 50 mg/)).toBeInTheDocument();
    expect(screen.getByText(/Not shared by the patient: Allergies/)).toBeInTheDocument();

    await userEvent.click(screen.getAllByRole("button", { name: /Source/ })[0]);
    expect(screen.getByText(/has not shared source documents/)).toBeInTheDocument();
  });
});

describe("App", () => {
  it("asks staff to sign in when there is no session", async () => {
    mockFetch(() => undefined);

    render(<App />);

    expect(await screen.findByRole("button", { name: "Continue" })).toBeInTheDocument();
  });

  it("asks for the WhatsApp code on the portal and removes the reference from the URL after verification", async () => {
    window.history.replaceState(null, "", "/portal?txn=txn_SyntheticReference123");
    let verified = false;

    mockFetch((url) => {
      if (url.endsWith("/portal/verify")) {
        verified = true;
        return { body: { verified: true, csrf_token: "p" } };
      }
      if (url.endsWith("/portal/session") && verified) {
        return {
          body: {
            csrf_token: "p",
            patient: { ref: "pt_1", case_alias: "CASE-TEST-0001", name: "Synthetic Patient", phone_masked: "+91********10" },
            transaction: { ref: "txn_SyntheticReference123", purpose: "UPLOAD", status: "VERIFIED", expires_at: "2026-10-02T10:30:00", pending_documents: 1 },
            consents: [],
            documents: [],
            options: {
              doctors: [{ ref: "usr_1", display_name: "Dr. Example (synthetic)" }],
              scopes: [{ key: "MEDICATIONS", label: "Medications" }],
              durations_days: [1, 7, 30],
              purposes: [{ key: "CLINICAL_REVIEW", label: "Clinical review" }],
              retention_days: 90,
            },
          },
        };
      }
      return undefined;
    });

    render(<App />);

    await userEvent.type(await screen.findByLabelText("Verification code"), "123456");
    await userEvent.click(screen.getByRole("button", { name: "Verify" }));

    expect(await screen.findByText(/1 document waiting for your consent/)).toBeInTheDocument();
    expect(screen.getByText("CASE-TEST-0001")).toBeInTheDocument();
    expect(window.location.search).toBe("");
    expect(screen.getByRole("button", { name: "Grant consent" })).toBeDisabled();

    await userEvent.click(screen.getByLabelText(/I agree/));
    expect(screen.getByRole("button", { name: "Grant consent" })).toBeEnabled();
  });
});

describe("DemoConsole", () => {
  const demoInfo = {
    enabled: true,
    patient: { ref: "pt_demo", phone_masked: "+91******7436" },
    documents: [{ key: "prescription", name: "rx.pdf", title: "Synthetic prescription" }],
    staff: [],
    signing: "DEMO_SIGNING_TOKEN",
    portal_base_url: "http://localhost:5173",
  };

  const accepted = {
    http_status: 200,
    signature: "VERIFIED",
    status: "PORTAL_ACCESS",
    messages: ["Verification required."],
    transaction_ref: "txn_x",
    portal_link: null,
    twiml: null,
  };

  function routes(simulate: { status?: number; body: unknown }) {
    return mockFetch((url) => {
      if (url.endsWith("/api/v1/demo")) return { body: demoInfo };
      if (url.endsWith("/api/v1/demo/phone")) return { body: [] };
      if (url.endsWith("/api/v1/demo/whatsapp")) return simulate;
      if (url.endsWith("/api/v1/demo/rate-limit/reset")) return { body: { cleared: 3 } };
      return undefined;
    });
  }

  const posts = (fetchMock: ReturnType<typeof mockFetch>, path: string) =>
    fetchMock.mock.calls.filter(([url, init]) => String(url).endsWith(path) && init?.method === "POST");

  it("does not submit while a request is still pending", async () => {
    let release: () => void = () => undefined;
    const fetchMock = vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
      const url = String(input);
      if (url.endsWith("/api/v1/demo")) return new Response(JSON.stringify(demoInfo));
      if (url.endsWith("/api/v1/demo/phone")) return new Response("[]");
      await new Promise<void>((resolve) => {
        release = resolve;
      });
      return new Response(JSON.stringify(accepted));
    });
    render(<DemoConsole />);

    const button = await screen.findByRole("button", { name: "Text “Hi” (portal access)" });
    for (let i = 0; i < 5; i++) fireEvent.click(button);

    await waitFor(() => expect(posts(fetchMock, "/api/v1/demo/whatsapp")).toHaveLength(1));
    release();
    expect(await screen.findByText("Request accepted")).toBeInTheDocument();
    expect(posts(fetchMock, "/api/v1/demo/whatsapp")).toHaveLength(1);
  });

  it("shows a rate-limited request as blocked, with a verified signature, and does not retry", async () => {
    const fetchMock = routes({
      status: 429,
      body: {
        error: "rate_limit_exceeded",
        message: "Too many requests. Please try again later.",
        retry_after: 42,
        http_status: 429,
        signature: "VERIFIED",
        status: "RATE_LIMITED",
        messages: [],
        transaction_ref: null,
        portal_link: null,
        twiml: null,
      },
    });
    render(<DemoConsole />);

    await userEvent.click(await screen.findByRole("button", { name: "Text “Hi” (portal access)" }));

    expect(await screen.findByText("Request blocked by abuse-prevention rate limit.")).toBeInTheDocument();
    expect(screen.getByText(/Twilio signature: Verified · Rate limit: Exceeded · Retry after 42s/)).toBeInTheDocument();
    expect(screen.queryByText("Request accepted")).not.toBeInTheDocument();
    expect(posts(fetchMock, "/api/v1/demo/whatsapp")).toHaveLength(1);
  });

  it("resets only the rate-limit counters", async () => {
    const fetchMock = routes({ body: accepted });
    render(<DemoConsole />);

    await userEvent.click(await screen.findByRole("button", { name: "Reset rate limits" }));

    expect(await screen.findByText(/Rate-limit counters cleared/)).toBeInTheDocument();
    expect(posts(fetchMock, "/api/v1/demo/rate-limit/reset")).toHaveLength(1);
    expect(posts(fetchMock, "/api/v1/demo/reset")).toHaveLength(0);
  });
});

describe("DriftAlert", () => {
  it("shows the exact source quote and REVIEW REQUIRED, never a corrected value", () => {
    render(
      <DriftAlert
        drift={{
          status: "REVIEW REQUIRED",
          model: "nli-deberta-v3-xsmall@2a4f614",
          model_checked_fields: ["dose", "frequency"],
          findings: [
            {
              code: "TIMING_MISMATCH",
              field: "frequency",
              reason: "The extracted timing or frequency conflicts with the source evidence.",
              evidence: "Inj Insulin 20 units at night",
              check: "MODEL",
              model_score: 0.989,
              source_word: null,
              action: "REVIEW REQUIRED",
            },
          ],
        }}
      />,
    );

    expect(screen.getByRole("alert")).toHaveTextContent("Possible extraction drift: REVIEW REQUIRED");
    expect(screen.getByText("“Inj Insulin 20 units at night”")).toBeInTheDocument();
    expect(screen.getByText(/Local model · contradiction score 0.99/)).toBeInTheDocument();
  });

  it("renders nothing when the extraction is consistent", () => {
    const { container } = render(
      <DriftAlert drift={{ status: "CONSISTENT", findings: [], model: null, model_checked_fields: [] }} />,
    );
    expect(container).toBeEmptyDOMElement();
  });
});

describe("AiMatchExplanation", () => {
  it("shows the chosen result, the alternatives and the calibrated threshold", () => {
    render(
      <AiMatchExplanation
        match={{
          id: 1,
          status: "PENDING",
          rule: "AI semantic match (local biomedical model)",
          method: "AI_SEMANTIC",
          score: 0.645,
          explanation: {
            method: "AI_SEMANTIC",
            model: "pubmedbert-base-embeddings@b79526d",
            score: 0.645,
            rank: 1,
            candidates: 5,
            threshold: 0.34,
            alternatives: [
              { label: "SGPT (ALT)", score: 0.33 },
              { label: "Vitamin B12", score: 0.21 },
            ],
          },
          observation: { id: 9, observation_type: "TSH", value: "3.2", unit: "mIU/L", event_date: null, review_status: "REVIEW" },
          document_id: 2,
          document_filename: "Synthetic lab report",
          document_source: "WEB",
          evidence: null,
          reviewed_by: null,
          reviewed_at: null,
          created_at: "2026-12-28T10:00:00",
        }}
      />,
    );

    expect(screen.getByText("AI follow-through")).toBeInTheDocument();
    expect(screen.getByText("TSH")).toBeInTheDocument();
    expect(screen.getByText("SGPT (ALT)")).toBeInTheDocument();
    expect(screen.getByText(/at least 0.34/)).toBeInTheDocument();
    expect(screen.getByText(/A suggestion, not a decision/)).toBeInTheDocument();
  });
});
