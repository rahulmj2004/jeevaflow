# AI Architecture

JeevaFlow uses AI in specific, bounded places. Each one ranks or checks evidence that already exists, and a doctor makes every clinical decision. This page describes the primary AI capability, **Evidence-Grounded Follow-Through**, and lists every other AI or ML component honestly.

> AI suggests → evidence → doctor decides

## 1. Component inventory

| Component | Where | Type |
|---|---|---|
| `NeuML/pubmedbert-base-embeddings` @ `b79526d` (Apache-2.0) | `app/followthrough/encoder.py` | **PRETRAINED / FROZEN MODEL, NOT FINE-TUNED** |
| NumPy BERT inference (reads the safetensors file; matches PyTorch to 8.3×10⁻⁷) | `encoder.py` | **OUR ALGORITHM** (an implementation of a published architecture) |
| Instruction → test concept; query → concept; result → label | `followthrough/text.py` | **RULE-BASED** |
| Cosine ranking, top-rank margin (0.06), at most 3 suggestions | `followthrough/semantic.py` | **OUR ALGORITHM** |
| Per-backend threshold calibration (calibration split only) | `evaluation/followthrough_eval.py`, `backends.py` | **OUR ALGORITHM** (calibration) |
| Abstention (`AI_ABSTAINED`) and decision provenance (`ai_decisions`) | `matching.py`, `routes/doctor.py` | **OUR ALGORITHM** (safety layer) |
| `lexical-fallback@v1`: hashed character trigrams | `followthrough/lexical.py` | **OUR ALGORITHM**, deterministic, no medical knowledge |
| Keyword follow-through table (5 tests) | `matching.py` | **RULE-BASED** (pre-existing; runs first) |
| Lab and instruction extraction, including generic "label: value unit" lines | `extraction.py` | **RULE-BASED** |
| Drift detector: `nli-deberta-v3-xsmall` (int8 ONNX, Apache-2.0) plus rule checks | `app/drift/` | **PRETRAINED / FROZEN MODEL** + **RULE-BASED** |
| Tesseract OCR | `worker.py` | **PRETRAINED MODEL** (OCR engine) |
| Handwriting detector | `app/handwriting/detect.py` | **HEURISTIC** (OpenCV) |

Nothing in JeevaFlow is trained or fine-tuned. Each threshold is a single number chosen on the synthetic calibration split.

## 2. Pipeline

```
INPUT            newer document (portal / camera / WhatsApp) through the existing secure intake
                 (validate · malware scan · AES-GCM vault · OTP + consent)
PREPROCESSING    isolated worker (network blocked, env scrubbed, CPU-limited):
                 OCR → rule extraction → each result reduced to its LABEL ("TSH"),
                 each instruction to its TEST CONCEPT ("thyroid function"; "" if it names no test)
AI INFERENCE     pubmedbert encoder (NumPy) → 768-d unit vector, tagged "<model id>|…"
                 └─ model missing or tampered → lexical-fallback, tagged and labelled
POSTPROCESSING   vectors stored in AES-GCM encrypted columns (commitments / observations)
CALIBRATION      threshold for that backend (backends.py), reproduced by a test
RANKING          API process: cosine(loop, each newer result of the SAME backend)
ABSTENTION       nothing ≥ threshold → AI_ABSTAINED (reason recorded); loop unchanged
EVIDENCE         every suggestion is a LoopMatch → SourceEvidence (quote, page, bbox, document hash)
EXPLANATION      backend, model, score, rank, threshold, alternatives (encrypted JSON)
HUMAN REVIEW     doctor: Confirm completion / Keep open (existing, authenticated, CSRF)
AUDIT            ai_decisions row + loop history (SYSTEM ai-followthrough → HUMAN)
```

**Evidence finder.** A doctor's query is reduced to its concept, embedded inside the worker (`op_embed`) and ranked against this case's consented, processed, non-rejected, quoted results that were embedded by the same backend. It returns cited results at or above the threshold, or `AI_ABSTAINED` with a reason. The query is never stored or logged; only a fixed-code audit event is written.

## 3. Hybrid design

```
              newer document's quoted results
                         │
          ┌──────────────┴──────────────┐
   RULE ENGINE (keyword table)    AI (biomedical embeddings)
   runs first, unchanged          only for loops the rules did not match
          └──────────────┬──────────────┘
          evidence ranking → calibrated threshold → abstention
                         │
              POTENTIAL_MATCH  or  AI_ABSTAINED
                         │
              doctor: CONFIRM  or  KEEP OPEN
```

## 4. Provenance stored per AI decision

`ai_decisions`: `ref`, `feature`, `patient_id` (case), `commitment_id`, `document_id`, `match_id`, `actor_ref` (finder), `decision`, `reason`, `backend`, `model_id`, `best_score`, `threshold`, `candidates`, `results`, `created_at`.

Each suggestion also stores its `LoopMatch` (`method`, `score`, encrypted `explanation`, `evidence_id`). The decision table holds no free text: no query, label or quote.

## 5. Privacy

| | |
|---|---|
| **Enters the AI** | Instruction test concepts ("thyroid function"), result labels ("TSH"), doctor query concepts ("thyroid") |
| **Never enters the AI** | Result values, patient name, phone, address, IDs, dates, images, the full document text |
| **Stored** | Model-tagged vectors and explanations (encrypted); `ai_decisions` (no free text) |
| **Logged** | Fixed-code audit events only (`AI_EVIDENCE_QUERY` with `ANSWERED:n` or `AI_ABSTAINED:<reason>`); loop history notes are fixed sentences |
| **Masked** | Every doctor response goes through `mask_tree` |
| **Network** | None at runtime. The models are fetched once by `scripts/fetch_*.py` (pinned revision and SHA-256). The worker blocks sockets, and a test greps the AI runtime for network code |

## 6. Safety

- The AI never closes a loop. `confirm-completion` needs a doctor and an existing suggestion; abstained loops return 409.
- The AI never edits facts, observations or values; it only writes `LoopMatch` and `ai_decisions` rows.
- Abstention is explicit, recorded and shown, and the loop stays OPEN or OVERDUE.
- Failures are contained:
  - An exception in the AI pass is caught. No AI rows are written, and the document and rule matches are kept (tested).
  - A missing or modified model triggers the labelled fallback.
  - Vectors from different backends are never compared.
- Access is controlled:
  - Consent with LABS scope for the finder, INSTRUCTIONS for loops.
  - Case isolation is tested.
  - Requests are rate-limited through `authorize_doctor`.

## 7. Path to production

- Calibrate on de-identified, annotated clinical data, with per-site thresholds, and use doctors' Confirm and Keep-open decisions as labelled feedback.
- Serve the encoder from a long-lived sandboxed worker pool (int8 or ONNX) instead of starting a process per job.
- Map labels to LOINC, using the embeddings for candidate generation and a coded check before acceptance.
- Keep `ai_decisions` as the monitoring source: abstention rate, confirm and keep-open ratio per backend and model, and drift alerts.
- Deployment: ship the models in the image or on a mounted volume (they're git-ignored), on an instance with at least 1 GB of RAM.
