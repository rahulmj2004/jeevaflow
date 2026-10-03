# AI Opportunity Analysis

This analysis was written after a full repository audit, before any new code. It records what exists, which AI opportunities follow from the architecture, and why one was chosen.

## 1. Audit summary (what exists, verified)

| Area | Implementation (verified in code) |
|---|---|
| Intake | Portal upload, camera and WhatsApp (Twilio, signed webhook) → `pipeline.receive_document`. Size, magic-byte and type allowlist; sandboxed worker inspection and sanitisation; malware scan (clamd, or a heuristic in demo); SHA-256; AES-256-GCM vault; quarantine |
| Consent | OTP-verified patient session; consent per doctor, with scopes (LABS, MEDICATIONS, ALLERGIES, INSTRUCTIONS, DEMOGRAPHICS, SOURCE_DOCUMENTS) and expiry. Every doctor route goes through `authorize_doctor` |
| OCR | Tesseract in the isolated worker (`worker.op_extract`). Network blocked, environment scrubbed, RLIMIT_CPU 120 s |
| Extraction | **Rule-based.** Lab values for 5 analytes, plus generic "label: value unit" lines (added in the baseline commit); instructions (repeat, review, refer, recheck, check, monitor, ...); medications, allergies and prescriber (`facts.py`) |
| Provenance | Quote-or-reject: every item has a `SourceEvidence` record (quote, page, character span, bbox, OCR confidence, document hash, pipeline version) |
| Open Loops | Instructions tracked as OPEN, POTENTIAL_MATCH or CLOSED, with due dates and overdue state; closed only by a doctor (`confirm-completion`) |
| Matching | `matching.py`: a 5-row keyword table, plus the baseline follow-through AI pass (PubMedBERT embeddings) |
| Conflicts | `conflicts.py`: same test, same date, different values, flagged for review |
| Masking | `security/masking.py`: regex and identity-term pseudonymisation of every doctor response; masked evidence image rendering |
| Audit | Hash-chained, MAC-protected, append-only, fixed-code reasons (`_SAFE` regex) |
| Existing AI | Tesseract OCR (pretrained); drift detector (`nli-deberta-v3-xsmall`, pretrained and frozen, plus rules); follow-through v1 (`pubmedbert-base-embeddings`, pretrained and frozen, plus our calibration); handwriting detector (heuristic) |
| Tests | Backend 259 passed; frontend 15 passed; `tsc` clean; `vite build` OK (baseline commit `b5bc5d2`) |
| Deployment | `render.yaml`: free plan, Python 3.11, `pip install -r requirements.txt`, production mode |

**Audit findings that shape the design**

1. **The models aren't deployed.** `backend/models/` is git-ignored and the Render build doesn't fetch it. A free 512 MB instance is also unlikely to hold the 438 MB fp32 encoder. In the deployed app, any model-dependent feature must degrade honestly and say which backend produced each result.
2. **Production won't start on Render as configured.** The production config now requires `JEEVAFLOW_CLAMD_ADDRESS` (malware scanning must not fall back to the heuristic in production), and `render.yaml` doesn't set it. This is reported, not changed: it is a deployment decision.
3. **Follow-through v1 abstains silently.** When the model declines, nothing is recorded or shown, so a doctor can't tell "AI checked and abstained" from "AI never ran".
4. **There's no way to ask the record a question.** The doctor's only route to a past result is to scroll the form or timeline.
5. **Lab series are grouped by exact name.** "HbA1c" and "Glycated Hemoglobin" become two separate series.

## 2. Candidate opportunities

Scores run 1–5, where 5 is best; for the risk rows, 5 means lowest risk.

| | A. Follow-through v2: abstention + fallback + evidence retrieval | B. Lab-concept series grouping | C. Medication change timeline | D. NER de-identification | E. Handwriting recognition | F. Drift detector v2 |
|---|---|---|---|---|---|---|
| Healthcare problem | Lost follow-ups; slow evidence lookup | Fragmented trends | Unnoticed medicine changes | Identifier leakage to doctors | Handwritten prescriptions unread | Extraction changes meaning |
| Data available | Loops, quoted results, embeddings (exist) | Observations | Medication facts per document | Page text | Images | Facts and quotes (exist) |
| Method | Frozen biomedical encoder; our calibrated ranking, abstention and retrieval | Embedding clustering | Name alignment and diff | Pretrained NER | Pretrained OCR transformer | NLI (exists) |
| Novelty | 4 | 3 | 2 | 2 | 3 | 3 (already built) |
| Complexity (5 = easy) | 4 | 3 | 3 | 3 | 1 | n/a |
| Privacy risk (5 = low) | 5: labels and concepts only | 5 | 4 | 3: sees all text | 3 | 4 |
| Clinical safety risk (5 = low) | 4: suggest-only, abstains | 2: a wrong merge corrupts a trend | 3 | 4 | 1: misread drug names | 4 |
| Explainability | 5: scores, alternatives, quotes | 3 | 4 | 3 | 2 | 5 |
| Evaluation feasibility | 5: synthetic splits exist | 4 | 3 | 4 | 1: no labelled handwriting | 3 |
| Demo impact | 5 | 3 | 3 | 3 | 5 if it works | 4 |
| Scalability | 5: dot products | 4 | 4 | 3 | 2 | 4 |
| External API dependency | none | none | none | none | none | none |
| Training-data dependency | none (frozen) | none | none | none | high | none |
| Main failure mode | Wrong suggestion (doctor rejects) or abstention (loop stays open) | Merging different tests | Brand/generic confusion | Missed names | Confident misreads | False flags |

## 3. Selection: A, Evidence-Grounded Follow-Through

**Chosen:** one frozen biomedical embedding space with our calibrated decision layer, applied to two doctor workflows, with explicit abstention and an honest fallback:

1. **Follow-through.** Link a doctor's instruction to the later quoted result that completes it, or record and show **AI_ABSTAINED**.
2. **Evidence finder.** A doctor types a test concept ("thyroid test") and gets back the cited, consented results for *this case*, or **AI_ABSTAINED** when nothing scores above the threshold.

**Why it beats the alternatives**

- **It targets the product's core promise** ("every instruction, followed through"), not a side feature.
- **It's measurably better than the deployed baseline.** On held-out data, rule recall was 0.04 and baseline-AI recall 0.71, and it can be re-measured.
- **Lowest privacy exposure of any option.** The model sees test concepts and result labels only, never values, names or images.
- **Failures are safe by construction.** A wrong suggestion still needs a doctor; an abstention leaves the loop open.
- **It reuses the existing vectors**, so the evidence finder costs one extra embedding per query.
- **B was rejected:** a wrong merge silently corrupts a clinical trend, which suggest-only can't make safe.
- **E was rejected:** no labelled data to evaluate it, and confident drug-name misreads are dangerous.
- **D was rejected as primary:** valuable, but it supports the product rather than being its core.
- **F already exists.**

**What v2 adds (because the audit found gaps)**

- A durable AI decision record (`ai_decisions`) for every loop the AI considered: SUGGESTED or AI_ABSTAINED, with backend, model, score, threshold, document and timestamp (finding 3).
- A deterministic `lexical-fallback` backend, calibrated separately and labelled on every result (finding 1).
- The evidence finder, with abstention, case isolation, scope checks and audit (finding 4).
- A stronger benchmark: lexical traps, unanswerable queries, AUROC and AUPRC, latency and memory, both backends, and a determinism check.
