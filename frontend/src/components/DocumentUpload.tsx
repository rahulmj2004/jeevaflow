import { useRef, useState } from "react";
import { ApiError, uploadDocument } from "../api";
import type { IngestionResult } from "../types";
import { CameraCapture } from "./CameraCapture";

const ACCEPTED = ["application/pdf", "image/jpeg", "image/png"];
const MAX_BYTES = 10 * 1024 * 1024;

type Phase =
  | { kind: "idle" }
  | { kind: "uploading"; fileName: string; progress: number }
  | { kind: "processing"; fileName: string }
  | { kind: "done"; fileName: string; result: IngestionResult }
  | { kind: "error"; fileName: string; message: string };

type StepState = "todo" | "active" | "done" | "warn" | "fail" | "skip";

interface Step {
  label: string;
  state: StepState;
  detail?: string;
}

function stageOf(result: IngestionResult, name: string) {
  return result.stages.find((stage) => stage.stage === name);
}

export function buildSteps(phase: Phase): Step[] {
  const labels = [
    "Uploading",
    "Validated & scanned",
    "Encrypted",
    "Quality check",
    "Extracted",
    "Evidence linked",
    "Complete",
  ];

  if (phase.kind === "idle") return labels.map((label) => ({ label, state: "todo" }));

  if (phase.kind === "uploading") {
    return labels.map((label, index) => ({
      label,
      state: index === 0 ? "active" : "todo",
      detail: index === 0 ? `${Math.round(phase.progress * 100)}%` : undefined,
    }));
  }

  if (phase.kind === "processing") {
    return labels.map((label, index) => ({
      label,
      state: index === 0 ? "done" : index <= 4 ? "active" : "todo",
    }));
  }

  if (phase.kind === "error") {
    return labels.map((label, index) => ({
      label,
      state: index === 0 ? "done" : index === labels.length - 1 ? "fail" : "skip",
      detail: index === labels.length - 1 ? "Not accepted" : undefined,
    }));
  }

  const { result } = phase;

  if (result.duplicate) {
    return labels.map((label, index) => ({
      label,
      state: "done",
      detail: index === labels.length - 1 ? "Already received — reused" : undefined,
    }));
  }

  const quality = stageOf(result, "QUALITY_CHECK");
  const scan = stageOf(result, "MALWARE_SCAN");
  const retake = result.ingestion_status === "RETAKE";
  const review = result.needs_manual_review;
  const extraction = stageOf(result, "EXTRACTION");

  return [
    { label: "Uploading", state: "done" },
    { label: "Validated & scanned", state: "done", detail: scan?.detail ?? undefined },
    { label: "Encrypted", state: "done", detail: "AES-256-GCM" },
    {
      label: "Quality check",
      state: quality?.status === "RETAKE" ? "fail" : quality?.status === "WARN" ? "warn" : "done",
      detail: quality?.status === "SKIPPED" ? "PDF" : quality?.status,
    },
    {
      label: "Extracted",
      state: retake || extraction?.status === "SKIPPED" ? "skip" : "done",
      detail: retake
        ? "Not attempted"
        : extraction?.status === "SKIPPED"
          ? "Doctor reads original"
          : (result.extraction_method ?? undefined),
    },
    {
      label: "Evidence linked",
      state: retake ? "skip" : "done",
      detail: retake ? undefined : `${result.evidence_created} quotes`,
    },
    {
      label: "Complete",
      state: retake ? "fail" : review ? "warn" : "done",
      detail: retake ? "Retake needed" : review ? "Needs doctor review" : undefined,
    },
  ];
}

const STEP_ICON: Record<StepState, string> = {
  todo: "",
  active: "",
  done: "✓",
  warn: "!",
  fail: "✕",
  skip: "–",
};

interface Props {
  onProcessed: () => void;
}

/**
 * Patient portal upload. The backend requires an active consent and
 * runs the secure ingestion pipeline before responding.
 */
export function DocumentUpload({ onProcessed }: Props) {
  const [phase, setPhase] = useState<Phase>({ kind: "idle" });
  const [cameraOpen, setCameraOpen] = useState(false);
  const [dragging, setDragging] = useState(false);
  const fileInput = useRef<HTMLInputElement>(null);
  const photoInput = useRef<HTMLInputElement>(null);

  const busy = phase.kind === "uploading" || phase.kind === "processing";

  async function handleFile(file: File | undefined, source: "WEB" | "CAMERA") {
    if (!file || busy) return;

    if (!ACCEPTED.includes(file.type)) {
      setPhase({ kind: "error", fileName: file.name, message: "Only PDF, JPG and PNG files are supported." });
      return;
    }
    if (file.size > MAX_BYTES) {
      setPhase({ kind: "error", fileName: file.name, message: "File size must be below 10 MB." });
      return;
    }

    setPhase({ kind: "uploading", fileName: file.name, progress: 0 });

    try {
      const result = await uploadDocument(
        file,
        source === "CAMERA",
        (progress) => setPhase({ kind: "uploading", fileName: file.name, progress }),
        () => setPhase({ kind: "processing", fileName: file.name }),
      );
      setPhase({ kind: "done", fileName: file.name, result });
      onProcessed();
    } catch (err) {
      const message =
        err instanceof ApiError && err.status === 422
          ? `The document was received but could not be processed reliably. ${err.message}`
          : (err as Error).message;
      setPhase({ kind: "error", fileName: file.name, message });
      onProcessed();
    }
  }

  const steps = buildSteps(phase);

  return (
    <div className="upload">
      <div
        className={`dropzone ${dragging ? "dropzone-active" : ""}`}
        onDragOver={(event) => {
          event.preventDefault();
          setDragging(true);
        }}
        onDragLeave={() => setDragging(false)}
        onDrop={(event) => {
          event.preventDefault();
          setDragging(false);
          handleFile(event.dataTransfer.files[0], "WEB");
        }}
      >
        <p className="dropzone-title">Drop a medical report here</p>
        <p className="small muted">PDF, JPG or PNG · up to 10 MB</p>
        <p className="small muted">Photos: place the page on a flat surface, use good light and keep the whole page visible.</p>
        <div className="btn-row center">
          <button type="button" className="btn btn-primary" disabled={busy} onClick={() => fileInput.current?.click()}>
            Choose file
          </button>
          <button type="button" className="btn" disabled={busy} onClick={() => setCameraOpen(true)}>
            Use camera
          </button>
          <button type="button" className="btn only-touch" disabled={busy} onClick={() => photoInput.current?.click()}>
            Take photo
          </button>
        </div>
        <input
          ref={fileInput}
          type="file"
          accept="application/pdf,image/jpeg,image/png"
          hidden
          onChange={(event) => {
            handleFile(event.target.files?.[0], "WEB");
            event.target.value = "";
          }}
        />
        <input
          ref={photoInput}
          type="file"
          accept="image/jpeg,image/png"
          capture="environment"
          hidden
          onChange={(event) => {
            handleFile(event.target.files?.[0], "CAMERA");
            event.target.value = "";
          }}
        />
      </div>

      {phase.kind !== "idle" && (
        <div className="progress-card" aria-live="polite">
          <p className="progress-file">{phase.fileName}</p>
          <ol className="steps">
            {steps.map((step) => (
              <li key={step.label} className={`step step-${step.state}`}>
                <span className="step-icon" aria-hidden="true">
                  {STEP_ICON[step.state]}
                </span>
                <span className="step-label">{step.label}</span>
                {step.detail && <span className="step-detail">{step.detail}</span>}
              </li>
            ))}
          </ol>

          {phase.kind === "done" && <Outcome result={phase.result} />}
          {phase.kind === "error" && (
            <div className="banner banner-danger" role="alert">
              {phase.message}
            </div>
          )}
        </div>
      )}

      {cameraOpen && (
        <CameraCapture
          onClose={() => setCameraOpen(false)}
          onCapture={(file) => {
            setCameraOpen(false);
            handleFile(file, "CAMERA");
          }}
        />
      )}
    </div>
  );
}

function Outcome({ result }: { result: IngestionResult }) {
  if (result.ingestion_status === "RETAKE") {
    return (
      <div className="banner banner-danger retake" role="alert">
        <strong>RETAKE</strong>
        <span>{result.quality_reason} Nothing was read from this image. Please take a clearer photo.</span>
      </div>
    );
  }

  if (result.duplicate) {
    return (
      <div className="banner banner-neutral">
        This exact file was already received. The existing result was reused; no duplicates were created.
      </div>
    );
  }

  return (
    <>
      {result.needs_manual_review && (
        <div className="banner banner-warning" role="status">
          <span className="badge badge-warning">
            {result.content_kind === "HANDWRITTEN" || result.content_kind === "MIXED"
              ? "Handwritten document: needs doctor review"
              : "Needs doctor review"}
          </span>{" "}
          Your document was received and will be reviewed by a doctor. Nothing from it is treated as confirmed until
          then.
        </div>
      )}
      {result.quality_status === "WARN" && (
        <div className="banner banner-warning">
          <strong>WARN</strong> {result.quality_reason} Extracted values need careful review against the source.
        </div>
      )}
      <div className="banner banner-success">
        Shared securely: {result.facts_created} medication/allergy item{result.facts_created === 1 ? "" : "s"},{" "}
        {result.observations_created} lab value{result.observations_created === 1 ? "" : "s"} and{" "}
        {result.open_loops_created} instruction{result.open_loops_created === 1 ? "" : "s"} for your doctor to review
        {result.potential_matches.length > 0 &&
          ` · ${result.potential_matches.length} potential loop completion${
            result.potential_matches.length === 1 ? "" : "s"
          } detected`}
        .
        {result.observations_created + result.open_loops_created + result.facts_created === 0 &&
          !result.needs_manual_review &&
          " No recognised results or instructions were found in this document."}
      </div>
    </>
  );
}
