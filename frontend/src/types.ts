// Shapes returned by the JeevaFlow FastAPI backend.

export interface Evidence {
  id: number;
  evidence_id: number;
  document_id?: number;
  document_ref?: string | null;
  document_filename: string | null;
  document_source: string | null;
  document_date: string | null;
  document_received_at: string | null;
  quote: string;
  page_number: number | null;
  start_position: number | null;
  end_position: number | null;
  bbox?: number[] | null;
  confidence?: number | null;
  document_sha256?: string | null;
  pipeline_version?: string | null;
  extracted_at?: string | null;
  linked_items?: LinkedItem[];
}

export interface LinkedItem {
  type: string;
  id: number | string;
  label: string;
  review_status: string | null;
  state?: string;
}

export type TimelineType = "DOCUMENT" | "OBSERVATION" | "COMMITMENT";

export interface TimelineItem {
  id: number;
  type: TimelineType;
  title: string;
  value: string;
  unit: string | null;
  date: string;
  review_status: string | null;
  evidence_id: number | null;
  document_id: number | null;
  source: string | null;
  processing_status: string | null;
  quality_status: string | null;
  state: string | null;
  due_date: string | null;
  evidence: Evidence | null;
  created_at: string | null;
}

export interface LoopMatch {
  id: number;
  status: string;
  rule: string;
  observation: {
    id: number;
    observation_type: string;
    value: string;
    unit: string | null;
    event_date: string | null;
    review_status: string;
  } | null;
  document_id: number;
  document_filename: string | null;
  document_source: string | null;
  evidence: Evidence | null;
  reviewed_by: string | null;
  reviewed_at: string | null;
  created_at: string;
}

export interface LoopHistoryEntry {
  id: number;
  action: string;
  from_state: string | null;
  to_state: string | null;
  actor_type: "SYSTEM" | "HUMAN";
  actor_name: string | null;
  note: string | null;
  created_at: string;
}

export interface OpenLoop {
  id: number;
  instruction: string;
  due_date: string | null;
  state: string;
  stored_state?: string;
  evidence_id: number | null;
  document_id: number | null;
  document_filename: string | null;
  document_date: string | null;
  evidence: Evidence | null;
  potential_matches: LoopMatch[];
  confirmed_match: LoopMatch | null;
  history?: LoopHistoryEntry[];
  created_at: string;
  updated_at: string;
}

export interface ConflictObservation {
  id: number;
  value: string;
  unit: string | null;
  document_id: number;
  evidence_id: number | null;
  review_status: string;
  evidence: Evidence | null;
}

export interface Conflict {
  type: "CONFLICT";
  observation_type: string;
  event_date: string;
  status: string;
  message: string;
  observations: ConflictObservation[];
}

export interface JourneyDocument {
  ref: string;
  label: string;
  status: string;
  quality: string;
  source: string;
  document_date: string | null;
  created_at: string;
}

export interface Journey {
  patient: { case_alias: string };
  consent: { ref: string; scopes: string[]; expires_at: string; status: string };
  summary: {
    document_count: number;
    observation_count: number;
    open_loop_count: number;
    potential_match_count: number;
    conflict_count: number;
  };
  documents: JourneyDocument[];
  timeline: TimelineItem[];
  open_loops: OpenLoop[];
  conflicts: Conflict[];
}

export interface IngestionStage {
  stage: string;
  status: string;
  detail: string | null;
}

export interface IngestionResult {
  ref: string;
  label: string;
  processing_status: string;
  quality_status: string;
  quality_reason: string | null;
  processing_error: string | null;
  extraction_method: string | null;
  document_date: string | null;
  source: string;
  scan_status: string | null;
  scan_engine: string | null;
  ingestion_status: "RECEIVED" | "PROCESSED" | "DUPLICATE" | "RETAKE" | "FAILED";
  duplicate: boolean;
  observations_created: number;
  open_loops_created: number;
  facts_created: number;
  evidence_created: number;
  evidence_rejected: number;
  potential_matches: LoopMatch[];
  stages: IngestionStage[];
  error: string | null;
}

// ---------------- staff ----------------

export interface StaffUser {
  user_ref: string;
  display_name: string;
  role: "DOCTOR" | "ADMIN" | "AUDITOR";
  csrf_token: string;
}

export interface ConsentedPatient {
  case_alias: string;
  consent_ref: string;
  scopes: string[];
  expires_at: string;
}

// ---------------- doctor-ready form ----------------

export type FactState = "SOURCE_FACT" | "AI_INFERRED" | "UNCERTAIN" | "MISSING";

export interface FactField {
  value: string | null;
  state: FactState;
  source_text: string | null;
  note: string | null;
}

export interface FormEvidence {
  evidence_id: number;
  document_ref: string | null;
  document_label: string | null;
  document_date: string | null;
  page_number: number | null;
  quote: string;
  has_region: boolean;
  confidence: number | null;
}

export interface FormFact {
  ref: string;
  label: string;
  fields: Record<string, FactField>;
  state: FactState;
  review_status: string;
  confidence: number | null;
  evidence: FormEvidence | null;
}

export interface FormLabValue {
  id: number;
  value: string;
  unit: string | null;
  date: string;
  review_status: string;
  state: FactState;
  note: string | null;
  evidence: FormEvidence | null;
}

export interface FormFlag {
  section: string;
  item: string | null;
  field: string | null;
  note: string | null;
  fact_ref?: string;
  evidence?: FormEvidence | null;
}

export interface DoctorForm {
  generated_at: string;
  patient: { case_alias: string; age_band: string | null; demographics_shared: boolean };
  consent: { ref: string; purpose: string; scopes: string[]; expires_at: string; can_view_source: boolean };
  not_shared: { scope: string; label: string }[];
  medications: FormFact[] | null;
  prescribers: FormFact[] | null;
  allergies: FormFact[] | null;
  labs: { test: string; latest: FormLabValue; series: FormLabValue[]; has_conflict: boolean }[] | null;
  conflicts: Conflict[];
  notes:
    | {
        id: number;
        instruction: string;
        state: string;
        due_date: string | null;
        documented_on: string | null;
        evidence: FormEvidence | null;
      }[]
    | null;
  uncertain: FormFlag[];
  missing: FormFlag[];
  sources: {
    ref: string;
    label: string;
    source: string;
    document_date: string | null;
    received_at: string;
    pages: number | null;
    sha256: string;
    scan_engine: string | null;
  }[];
  notice: string;
}

// ---------------- patient portal ----------------

export interface ConsentRecord {
  ref: string;
  doctor_ref: string;
  doctor_name: string;
  purpose: string;
  purpose_label: string;
  scopes: string[];
  retention_days: number;
  granted_at: string;
  expires_at: string;
  revoked_at: string | null;
  status: "ACTIVE" | "REVOKED" | "EXPIRED";
}

export interface PortalDocument {
  ref: string;
  label: string;
  source: string;
  status: string;
  received_at: string;
  retain_until: string | null;
  scan_status: string | null;
}

export interface PortalSession {
  csrf_token: string;
  patient: { ref: string; case_alias: string; name: string | null; phone_masked: string | null };
  transaction: {
    ref: string;
    purpose: "UPLOAD" | "PORTAL_ACCESS";
    status: string;
    expires_at: string;
    pending_documents: number;
  } | null;
  consents: ConsentRecord[];
  documents: PortalDocument[];
  options: {
    doctors: { ref: string; display_name: string }[];
    scopes: { key: string; label: string }[];
    durations_days: number[];
    purposes: { key: string; label: string }[];
    retention_days: number;
  };
}

export interface ConsentResult {
  consent: ConsentRecord;
  processing: {
    documents: number;
    processed: number;
    facts: number;
    observations: number;
    instructions: number;
    failed: number;
  } | null;
}

// ---------------- security ----------------

export interface SecurityControl {
  name: string;
  status: string;
  detail: string;
  grade: "IMPLEMENTED" | "DEMO IMPLEMENTATION" | "PRODUCTION REQUIRED";
  production: string | null;
}

export interface ChainResult {
  valid: boolean;
  events: number;
  head_hash: string | null;
  broken_at_seq: number | null;
  reason: string | null;
}

export interface SecurityStatus {
  generated_at: string;
  statement: string;
  environment: string;
  demo_mode: boolean;
  controls: SecurityControl[];
  audit_chain: ChainResult;
}

export interface AuditEvent {
  seq: number;
  created_at: string;
  actor_type: string;
  actor_ref: string | null;
  action: string;
  object_type: string | null;
  object_ref: string | null;
  result: string;
  reason: string | null;
  prev_hash: string;
  event_hash: string;
}

// ---------------- demo ----------------

export interface DemoInfo {
  enabled: boolean;
  patient: { ref: string | null; phone_masked: string | null };
  documents: { key: string; name: string; title: string }[];
  staff: { username: string; display_name: string; role: string }[];
  signing: string;
  portal_base_url: string;
}

export interface SimulateResult {
  http_status: number;
  signature: "VERIFIED" | "REJECTED" | null;
  status: string | null;
  messages: string[];
  transaction_ref: string | null;
  portal_link: string | null;
  twiml: string | null;
  /** Present when the request was rate limited (HTTP 429). */
  error?: "rate_limit_exceeded";
  retry_after?: number;
}

export interface PhoneNotification {
  kind: string;
  body: string;
  delivery: string;
  created_at: string;
}
