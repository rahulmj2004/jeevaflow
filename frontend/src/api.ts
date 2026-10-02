import type {
  ConsentResult,
  ConsentedPatient,
  DemoInfo,
  DoctorForm,
  Evidence,
  IngestionResult,
  Journey,
  OpenLoop,
  AuditEvent,
  ChainResult,
  PhoneNotification,
  PortalSession,
  SecurityStatus,
  SimulateResult,
  StaffUser,
} from "./types";

export const API_BASE: string =
  (import.meta.env.VITE_API_BASE_URL as string | undefined)?.replace(/\/$/, "") ?? "";

export class ApiError extends Error {
  status: number;

  constructor(status: number, message: string) {
    super(message);
    this.status = status;
  }
}

/**
 * CSRF tokens are kept in memory only (never localStorage), one per
 * session kind. Sessions themselves are HttpOnly cookies.
 */
type SessionKind = "staff" | "patient";

const csrf: Record<SessionKind, string | null> = { staff: null, patient: null };

export function setCsrf(kind: SessionKind, token: string | null) {
  csrf[kind] = token;
}

function describeDetail(detail: unknown): string | null {
  if (typeof detail === "string") return detail;
  if (Array.isArray(detail)) {
    return detail
      .map((item) => (typeof item === "object" && item && "msg" in item ? String(item.msg) : String(item)))
      .join("; ");
  }
  return null;
}

async function send(path: string, init: RequestInit = {}, kind?: SessionKind): Promise<Response> {
  const headers = new Headers(init.headers);

  if (kind && csrf[kind] && init.method && init.method !== "GET") {
    headers.set("X-CSRF-Token", csrf[kind]!);
  }

  try {
    return await fetch(API_BASE + path, { ...init, headers, credentials: "same-origin", cache: "no-store" });
  } catch {
    throw new ApiError(0, "Cannot reach the JeevaFlow API. Is the backend running on port 8000?");
  }
}

async function request<T>(path: string, init: RequestInit = {}, kind?: SessionKind): Promise<T> {
  const response = await send(path, init, kind);
  const text = await response.text();
  let body: unknown = null;

  if (text) {
    try {
      body = JSON.parse(text);
    } catch {
      body = text;
    }
  }

  if (!response.ok) {
    throw new ApiError(response.status, errorMessage(body) ?? `Request failed (HTTP ${response.status})`);
  }

  return body as T;
}

/** Error text from a FastAPI `detail` or a rate-limit `message` body. */
function errorMessage(body: unknown): string | null {
  if (!body || typeof body !== "object") return null;
  if ("detail" in body) return describeDetail((body as { detail: unknown }).detail);
  if ("message" in body && typeof (body as { message: unknown }).message === "string") {
    return (body as { message: string }).message;
  }
  return null;
}

/**
 * The simulator mirrors the webhook's HTTP status. A 429 is a result
 * to show (abuse-prevention demo), not a failure to retry.
 */
async function simulateWhatsApp(document: string | null, tamperSignature: boolean): Promise<SimulateResult> {
  const response = await send("/api/v1/demo/whatsapp", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ document, tamper_signature: tamperSignature }),
  });
  let body: unknown = null;
  try {
    body = await response.json();
  } catch {
    body = null;
  }
  if (response.ok || response.status === 429) return body as SimulateResult;
  throw new ApiError(response.status, errorMessage(body) ?? `Request failed (HTTP ${response.status})`);
}

function post<T>(path: string, body: unknown, kind?: SessionKind, method = "POST"): Promise<T> {
  return request<T>(
    path,
    {
      method,
      headers: { "Content-Type": "application/json" },
      body: body === undefined ? undefined : JSON.stringify(body),
    },
    kind,
  );
}

const enc = encodeURIComponent;

export const api = {
  health: () => request<{ status: string }>("/api/v1/health"),

  // ---------------- staff authentication ----------------
  login: (username: string, password: string) =>
    post<{ mfa_required: boolean }>("/api/v1/auth/login", { username, password }),
  mfa: async (code: string) => {
    const user = await post<StaffUser>("/api/v1/auth/mfa", { code });
    setCsrf("staff", user.csrf_token);
    return user;
  },
  me: async () => {
    const user = await request<StaffUser>("/api/v1/auth/me");
    setCsrf("staff", user.csrf_token);
    return user;
  },
  logout: async () => {
    await post("/api/v1/auth/logout", {}, "staff");
    setCsrf("staff", null);
  },

  // ---------------- doctor (consent-checked server-side) ----------------
  myPatients: () => request<ConsentedPatient[]>("/api/v1/doctor/patients"),
  journey: (ref: string) => request<Journey>(`/api/v1/doctor/patients/${enc(ref)}/journey`),
  doctorForm: (ref: string) => request<DoctorForm>(`/api/v1/doctor/patients/${enc(ref)}/form`),
  loopDetail: (id: number) => request<OpenLoop>(`/api/v1/doctor/commitments/${id}`),
  confirmCompletion: (id: number, matchId?: number) =>
    post<OpenLoop>(`/api/v1/doctor/commitments/${id}/confirm-completion`, { match_id: matchId }, "staff"),
  keepOpen: (id: number) => post<OpenLoop>(`/api/v1/doctor/commitments/${id}/keep-open`, {}, "staff"),
  flagForReview: (id: number) => post<OpenLoop>(`/api/v1/doctor/commitments/${id}/review`, {}, "staff"),
  evidence: (id: number) => request<Evidence>(`/api/v1/doctor/evidence/${id}`),
  verifyObservation: (id: number) => post(`/api/v1/doctor/observations/${id}/verify`, undefined, "staff", "PATCH"),
  rejectObservation: (id: number) => post(`/api/v1/doctor/observations/${id}/reject`, undefined, "staff", "PATCH"),
  confirmFact: (ref: string) => post(`/api/v1/doctor/facts/${enc(ref)}/confirm`, undefined, "staff", "PATCH"),
  rejectFact: (ref: string) => post(`/api/v1/doctor/facts/${enc(ref)}/reject`, undefined, "staff", "PATCH"),

  /**
   * Secure evidence viewer: a single-use, 60-second token bound to
   * this session, then the watermarked region image. The image is
   * kept only as an in-memory blob URL.
   */
  evidenceImage: async (evidenceId: number): Promise<Blob> => {
    const { token } = await post<{ token: string }>(`/api/v1/doctor/evidence/${evidenceId}/view-token`, {}, "staff");
    const response = await send(`/api/v1/doctor/evidence/view/${enc(token)}`);
    if (!response.ok) throw new ApiError(response.status, "The source region could not be displayed.");
    return response.blob();
  },

  // ---------------- patient portal ----------------
  portalVerify: async (transactionRef: string, code: string) => {
    const result = await post<{ verified: boolean; csrf_token: string }>("/api/v1/portal/verify", {
      transaction_ref: transactionRef,
      code,
    });
    setCsrf("patient", result.csrf_token);
    return result;
  },
  portalSession: async () => {
    const session = await request<PortalSession>("/api/v1/portal/session");
    setCsrf("patient", session.csrf_token);
    return session;
  },
  grantConsent: (doctorRef: string, scopes: string[], durationDays: number, purpose: string) =>
    post<ConsentResult>(
      "/api/v1/portal/consents",
      { doctor_ref: doctorRef, scopes, duration_days: durationDays, purpose },
      "patient",
    ),
  applyConsent: (ref: string) => post<ConsentResult>(`/api/v1/portal/consents/${enc(ref)}/apply`, {}, "patient"),
  revokeConsent: (ref: string) => post(`/api/v1/portal/consents/${enc(ref)}/revoke`, {}, "patient"),
  expireConsent: (ref: string) => post(`/api/v1/portal/consents/${enc(ref)}/expire`, {}, "patient"),
  deleteDocument: (ref: string) => post(`/api/v1/portal/documents/${enc(ref)}`, undefined, "patient", "DELETE"),
  portalLogout: async () => {
    await post("/api/v1/portal/logout", {}, "patient");
    setCsrf("patient", null);
  },

  // ---------------- security ----------------
  securityStatus: () => request<SecurityStatus>("/api/v1/security/status"),
  auditEvents: (limit = 200) => request<AuditEvent[]>(`/api/v1/audit/events?limit=${limit}`),
  verifyAudit: () => post<ChainResult>("/api/v1/audit/verify", {}, "staff"),
  tamperAudit: (seq: number) => post("/api/v1/demo/audit/tamper", { seq }, "staff"),
  restoreAudit: () => post<{ restored: number }>("/api/v1/demo/audit/restore", {}, "staff"),

  // ---------------- synthetic demo ----------------
  demo: () => request<DemoInfo>("/api/v1/demo"),
  demoWhatsApp: (document: string | null, tamperSignature = false) => simulateWhatsApp(document, tamperSignature),
  demoPhone: () => request<PhoneNotification[]>("/api/v1/demo/phone"),
  demoReset: () => post<{ deleted: Record<string, number> }>("/api/v1/demo/reset", {}),
  demoRateLimitReset: () => post<{ cleared: number }>("/api/v1/demo/rate-limit/reset", {}),
  demoFileUrl: (name: string) => `${API_BASE}/api/v1/demo/files/${enc(name)}`,
};

/**
 * Portal upload through XHR so the UI can show real upload progress.
 * Requires the patient session (cookie) and its CSRF token.
 */
export function uploadDocument(
  file: File,
  camera: boolean,
  onUploadProgress: (fraction: number) => void,
  onUploaded: () => void,
): Promise<IngestionResult> {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    const form = new FormData();
    form.append("file", file);

    xhr.open("POST", `${API_BASE}/api/v1/portal/documents${camera ? "?camera=true" : ""}`);
    xhr.withCredentials = true;

    if (csrf.patient) xhr.setRequestHeader("X-CSRF-Token", csrf.patient);

    xhr.upload.onprogress = (event) => {
      if (event.lengthComputable) onUploadProgress(event.loaded / event.total);
    };
    xhr.upload.onload = () => onUploaded();

    xhr.onerror = () => reject(new ApiError(0, "Network error while uploading. Is the backend running?"));

    xhr.onload = () => {
      let body: unknown = null;
      try {
        body = JSON.parse(xhr.responseText);
      } catch {
        body = null;
      }

      if (xhr.status >= 200 && xhr.status < 300) {
        resolve(body as IngestionResult);
        return;
      }

      const detail =
        body && typeof body === "object" && "detail" in body
          ? describeDetail((body as { detail: unknown }).detail)
          : null;
      reject(new ApiError(xhr.status, detail ?? `Upload failed (HTTP ${xhr.status})`));
    };

    xhr.send(form);
  });
}
