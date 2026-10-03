# AI Follow-Through Engine

> JeevaFlow's promise is "every instruction, followed through". Until now that only worked for five lab tests. A local biomedical model now connects a doctor's instruction to the later result that completes it, even when the two never share a word. It shows its evidence, holds back when unsure, and leaves every decision to a doctor.

Everything here uses synthetic data. Every number below was measured and can be reproduced with the commands in section 10.

---

## How the idea was chosen

**Audit finding.** JeevaFlow's core object is the Open Loop: a documented doctor instruction tracked until a person confirms it was done. Two parts of the codebase limited it:

- Matching instructions to results (`app/matching.py`) used a five-row keyword table: HbA1c, glucose, creatinine, LDL and blood pressure.
- The result extractor only read those same five tests.

So "Check thyroid function after 6 weeks" could never be linked to a later TSH result. Follow-through, the product's main promise, failed for nearly every real instruction.

**Concepts considered** (rejected unless noted):

| # | Concept | Verdict |
|---|---|---|
| 1 | Open-vocabulary follow-through: semantic instruction-to-result matching | **Chosen.** Fixes the product's main gap; measurable; visual |
| 2 | Drift detector: semantic check of extraction against its source | Already built; this feature complements it |
| 3 | Local handwriting recognition (TrOCR-class) | Low accuracy on prescriptions; can't be demonstrated honestly |
| 4 | NER-based de-identification for the doctor view | Real value, but supports the product rather than being its core |
| 5 | LLM chat over the patient record | Cloud LLM conflicts with the privacy model; hallucination risk |
| 6 | Lab trend anomaly detection | Statistics more than AI; edges into clinical interpretation |
| 7 | Medication reconciliation across prescriptions | Needs a drug knowledge base more than learning |
| 8 | Calibrated extraction-confidence model | No labelled data to calibrate on |
| 9 | WhatsApp agent that chases patients | Outbound clinical messaging risk |
| 10 | Zero-shot document-type routing | Low impact |

**Self-challenge (Phase 5).**

- **"Could rules do it?"** Measured: the existing rules found 4.2% of completed follow-ups on held-out phrasings. A synonym table only covers what someone has already written into it.
- **"Is it an LLM wrapper?"** No. It's a 110M-parameter biomedical encoder running locally with a calibrated decision rule, and no text is generated.
- **"Could it hallucinate?"** It can only rank results that already exist with an exact source quote. It can't create a result, and it can't close a loop.
- **Reuse rejected:** the drift detector's NLI model was reused first and failed. It found about 1 true match in 10 because it has no medical knowledge, so it was replaced by a biomedical model.

---

## 1. The feature

**AI Follow-Through Engine.** When a newer document arrives, every open instruction is compared with every quoted result in that document by meaning, not by keyword:

- "Check thyroid function after 6 weeks" is suggested as completed by "TSH: 3.2 mIU/L".
- "Monitor liver enzymes in 2 months" is suggested as completed by "SGPT (ALT): 32 U/L".

Each suggestion comes with a score, its rank and the alternatives considered. A doctor confirms or rejects it.

It was chosen because it fixes the main gap in the existing product. It's real AI (biomedical semantics no rule table holds), it can be measured against the existing baseline, and the demo makes the result visible on screen.

## 2. The AI

- **Model:** `NeuML/pubmedbert-base-embeddings` (Apache-2.0). This is PubMedBERT-base fine-tuned for sentence similarity on PubMed title/abstract pairs: 768 dimensions, mean pooling, about 110M parameters.
- **Inference:** a from-scratch NumPy implementation of the BERT encoder (`app/followthrough/encoder.py`), read straight from the pinned safetensors file. It uses no PyTorch and no ONNX conversion. It was checked against the PyTorch reference: maximum absolute difference 8.3×10⁻⁷, cosine 1.000.
- **Where it runs:** only inside the isolated processing worker (network blocked, no database, CPU-limited). The API process only does vector maths on stored vectors.
- **Why AI is necessary:** knowing that "thyroid function" means TSH or Free T4, "liver enzymes" means ALT or AST, and "KFT" means creatinine, eGFR or urea is biomedical knowledge, and the phrasing varies without limit. The pretrained biomedical model holds that knowledge. In the selection benchmark, the general-purpose MiniLM model scored AUC 0.74 and the biomedical model 0.90. With instruction cleaning it reached 0.93.

## 3. The differentiator

It isn't "upload, call an LLM, show the answer":

- **Nothing is generated.** The output is a ranked choice among results that already exist as exact quotes from a newer source document.
- **It abstains when unsure.** The threshold (0.34) was chosen on a separate calibration split, and below it the engine says nothing. In the demo it ranks haemoglobin first for "Check for anaemia after iron therapy", but at 0.30 it stays silent and the loop stays open.
- **It explains itself.** The doctor sees the score, rank, threshold and every other candidate the model considered, as a bar chart.
- **It is private by construction.** The model runs locally in a sandbox. It sees only test names, never values, names or identifiers. Its vectors are stored encrypted.
- **It is measured.** It has a held-out benchmark against the system it improves on, plus a regression test with minimum scores.

## 4. Architecture

```
newer document (portal / camera / WhatsApp)
  → existing secure intake: validate · malware scan · encrypt · OTP + consent
  → isolated worker (no network):
       extraction (rules, quote-anchored)
         · any "Label: value unit" result line   ← new, so the AI has candidates
         · instructions incl. check / monitor / screen for   ← new
       PREPROCESSING  instruction → test concept ("thyroid function"); result → label ("TSH")
       AI             PubMedBERT encoder (NumPy) → 768-d unit vectors, model-tagged
  → pipeline stores vectors in encrypted columns
  → matcher (API process, vector maths only):
       1. existing keyword rules (unchanged)
       2. AI pass for loops the rules did not match:
          cosine score → calibrated threshold → top-rank (+0.06 margin, ≤3)
  → VALIDATION   only results from a NEWER document, with source evidence, not rejected
  → OUTPUT       LoopMatch(method=AI_SEMANTIC, score, encrypted explanation)
                 loop state → POTENTIAL_MATCH (never CLOSED)
  → HUMAN        doctor sees instruction + quote + score bars → Confirm / Keep open
  → AUDIT        loop history: SYSTEM "ai-followthrough" → HUMAN doctor
```

Labels as the system shows them: **KNOWN** (quoted result), **AI-INFERRED** (`AI_SEMANTIC` suggestion with a score), **UNCERTAIN** (below threshold, so no suggestion and the loop stays open), **HUMAN-CONFIRMED** (closed only by a doctor).

## 5. Files changed

**New**
- `backend/app/followthrough/encoder.py`: pinned model, NumPy BERT, verification cache
- `backend/app/followthrough/text.py`: instruction-to-concept and result-to-label
- `backend/app/followthrough/semantic.py`: scoring, abstention, explanations, model-tagged vectors
- `backend/evaluation/followthrough_eval.py`: synthetic benchmark with calibration and held-out splits
- `backend/scripts/fetch_followthrough_model.py`: one-time pinned download
- `backend/tests/test_followthrough.py`: 24 tests

**Changed**
- `backend/app/extraction.py`: generic result lines; check/monitor/screen-for instructions
- `backend/app/worker.py`: `_embed_for_followthrough`
- `backend/app/matching.py`: the AI pass after the rules
- `backend/app/pipeline.py`: stores the vectors
- `backend/app/models.py` and `database.py`: new columns (section 7)
- `backend/app/loops.py`: match serialisation
- `backend/app/demo.py` and `routes/demo.py`: two demo documents
- `frontend/src/components/OpenLoops.tsx`: `AiMatchExplanation` score bars
- `frontend/src/types.ts`, `styles.css`, `src/test/app.test.tsx`

## 6. APIs

No new endpoints. Existing ones carry more data:

- `GET /api/v1/doctor/patients/{case}/loops` and `GET /api/v1/doctor/commitments/{id}`: each match now has `method` (`RULE` | `AI_SEMANTIC`), `score` and `explanation` (`model`, `score`, `rank`, `candidates`, `threshold`, `alternatives`).
- `POST /api/v1/doctor/commitments/{id}/confirm-completion` works the same for AI suggestions.
- The demo endpoints offer `followthrough_plan` and `followthrough_labs`.

## 7. Database (additive upgrades in `database.py`)

- `commitments.embedding`: encrypted; vector of the instruction's test concept, tagged with its model. NULL if the instruction names no test.
- `observations.embedding`: encrypted; vector of the result **label** only.
- `loop_matches.method`: `RULE` by default.
- `loop_matches.score`: float.
- `loop_matches.explanation`: encrypted JSON.

New result lines are stored as ordinary `observations` with source evidence.

## 8. Security and privacy

- **Data that enters the AI:** the test concept of an instruction ("thyroid function") and the label of a result ("TSH").
- **Data that never enters the AI:** result values, patient name, phone, IDs, dates, document images, or any external service.
- **Stored:** model-tagged vectors and match explanations, in AES-GCM encrypted columns. The plain-text `rule` field holds only the method, score and date.
- **Logged:** loop history events (actor `ai-followthrough`, then the human reviewer). The fixed-code audit log is unchanged, and no text is written to application logs.
- **Masked:** doctor responses still go through `mask_tree`.
- **Access:** existing consent and scope checks apply. A doctor without consent gets 403, which is tested.
- **Model integrity:** files are SHA-256 pinned at a fixed revision. A missing or modified model disables only the AI pass, and the rules keep working.
- **Known gap:** the verification cache trusts the file's size, modification time and inode once it has been hashed.

## 9. Test results (actual)

- **Backend:** `259 passed` (full suite, including 24 new follow-through tests). The new tests cover concept and label extraction, values never being embedded, model tagging, malformed vectors, abstention, a tampered or missing model, determinism, biomedical knowledge, minimum held-out scores, the end-to-end match, doctor confirmation, encryption at rest, model-absent fallback, corrupt stored vectors and unauthorised access.
- **Frontend:** `15 passed`; `tsc --noEmit` clean.

**Benchmark** (`venv/bin/python -m evaluation.followthrough_eval`)
- Setup: 16 test concepts, 96 held-out reports. Half the reports contain the requested test among 3 distractors; half don't.
- Threshold: 0.34, chosen on the calibration split for precision ≥ 0.90.

| Held-out split | Recall | Precision | False alarm |
|---|---|---|---|
| Existing rules | 0.042 | 0.667 | 0.000 |
| AI engine | **0.708** | **0.850** | 0.104 |

On the calibration split the AI scores 0.909 recall and 0.909 precision. The fall to 0.85 precision on held-out data is the honest generalisation gap.

**Latency** (CPU, measured): model load 0.19 s per worker job once verified; 5 phrases encoded in 0.07 s.

## 10. Demo commands

```bash
cd backend
venv/bin/python scripts/fetch_followthrough_model.py   # once; pinned, ~440 MB
venv/bin/python scripts/fetch_drift_model.py           # once; drift detector
venv/bin/python -m evaluation.followthrough_eval       # prints the benchmark
venv/bin/uvicorn app.main:app --reload --port 8000

cd ../frontend && npm run dev                          # http://localhost:5173
```

In the Demo console:
1. Send **"AI follow-through: follow-up plan"**, verify the OTP and grant consent to Dr. Example.
2. Send **"AI follow-through: later lab report"**, then verify and apply the consent.
3. Sign in as `dr.example` (password in `backend/.env`; code from `venv/bin/python -m app.security.demo_totp`) and open **Open Loops**.

## 11. Three-minute demo script

- **Problem (0:00).** "A doctor writes 'check thyroid function in 6 weeks'. Three months later a lab report arrives. Did anyone notice the instruction was done, or that it wasn't?"
- **Current failure (0:20).** "JeevaFlow tracked instructions, but its matcher knew five tests. Thyroid, liver and blood counts were invisible. On our held-out benchmark the rules caught 4% of completed follow-ups."
- **JeevaFlow (0:40).** Send the follow-up plan over WhatsApp, then OTP and consent. Five instructions become Open Loops, each anchored to its source quote.
- **AI activation (1:05).** Send the later lab report. "It contains TSH, ALT, creatinine, haemoglobin and B12, and no instruction mentions any of those words except kidney."
- **AI result (1:25).** Open Loops shows the thyroid instruction with "TSH: 3.2 mIU/L" as a potential completion, and the same for liver enzymes and ALT. Kidney is matched by the old rule; both methods are shown side by side.
- **Explanation (1:50).** Point at the score bars. "TSH 0.65; the threshold line is 0.34; ALT, B12 and creatinine were considered and lost. This is a local PubMedBERT model running in our network-blocked sandbox. It saw the words 'thyroid function' and 'TSH', never the value or the patient."
- **Abstention (2:15).** "For 'check for anaemia', the model ranked haemoglobin first, but below our calibrated threshold, so it stayed silent and the loop stays open. 'Review in 4 weeks' names no test, so it stays open too."
- **Human verification (2:30).** Click **Confirm completion** on the thyroid loop. History reads: SYSTEM ai-followthrough → HUMAN Dr. Example.
- **Impact (2:45).** "Follow-up recall went from 4% to 71% at 85% precision on held-out phrasings. The model is local and private, explains itself, and never closes anything on its own."

## 12. Thirty-second pitch

Doctors' instructions get lost between documents. JeevaFlow tracks each one as an Open Loop, and our AI Follow-Through Engine now recognises when a later report completes it. "Check thyroid function" is linked to "TSH 3.2", even though no word is shared. A biomedical language model runs entirely inside our sandbox and sees test names, never patient data. It shows the doctor why it matched and holds back when unsure. On held-out data it finds 71% of completed follow-ups, against 4% for rules.

## 13. Technical pitch

- **Matching by biomedical meaning.** PubMedBERT sentence embeddings place instruction concepts and result labels in one space. A tuned text-cleaning step lifts top-1 ranking from 81% to 93.5%: it removes timing and verbs, and turns visit-only instructions into "no test".
- **Calibrated decision.** The rule (threshold 0.34, top rank, 0.06 margin, at most 3) was chosen on a calibration split and reported on a held-out split, against the production baseline.
- **Self-contained inference.** The encoder is implemented in NumPy, matches PyTorch to 1e-6, needs no ML framework at runtime, and is SHA-256 pinned.
- **Privacy split.** Vectors are computed in the network-blocked worker, and the API process only takes dot products.
- **Model-tagged vectors.** A model upgrade can never silently compare old and new vectors.
- **Graceful degradation.** Without the model, the original rules keep working.

## 14. Judge questions

1. **Isn't this just a synonym list?** No list exists. The 16 test concepts in the benchmark were never coded into the system, and held-out phrasings ("Prostate marker test yearly", "Repeat PT/INR after dose change") are matched from the model's knowledge.
2. **Why not GPT-4 or a cloud LLM?** Patient documents would leave the sandbox, and generation can hallucinate. Ranking existing quotes can't invent a result.
3. **Why not the NLI model you already had?** We measured it: about 1 true match in 10, because it has no medical knowledge.
4. **How do you know 0.34 isn't overfit?** It was chosen on the calibration split only. Held-out precision fell from 0.91 to 0.85, and we report that gap.
5. **Your benchmark is synthetic. Isn't that weak?** Yes. It shows the method works and fixes a regression baseline; real-world accuracy needs annotated de-identified clinical data (section 15).
6. **What happens when it's wrong?** A false suggestion appears for a doctor to reject. A miss leaves the loop open and still tracked for overdue. Nothing is closed without a human.
7. **Can it say a result is normal?** No. It never reads values and never judges results.
8. **What does the model see?** "thyroid function" and "TSH". No values, names, dates or images.
9. **Could embeddings leak PHI?** They encode test names only, and they're stored encrypted.
10. **Why NumPy instead of PyTorch or ONNX?** It removes a 300 MB+ runtime dependency, can be checked line by line, matches PyTorch to 1e-6, and it's fast enough for a few dozen phrases per document.
11. **Latency?** About 0.19 s model load per worker job plus about 15 ms per phrase on CPU.
12. **Does it scale?** Matching is a dot product: per patient, open loops times new results, which is tiny. Batching or an ONNX runtime would be enough at clinic scale.
13. **Can a tampered model slip in?** Files are SHA-256 pinned. A mismatch disables the AI pass, which is tested.
14. **What if instructions and results come in the reverse order?** Only newer documents can complete a loop. That's existing JeevaFlow logic, kept on purpose.
15. **A kidney panel has three results. Which one is chosen?** Up to 3 within a 0.06 margin of the best, so creatinine and eGFR can both support one loop.
16. **What about results without units, like INR?** Not captured yet. The result-line parser needs a recognised unit. This is a known limitation.
17. **Imaging or ECG?** Not yet: the extractor reads lab-style "label: value unit" lines only.
18. **Is it regulated as a medical device?** It is decision support that suggests and never decides. Regulatory classification would need formal assessment; we make no compliance claim.
19. **What does 85% precision mean for a doctor?** About 1 in 7 suggestions on held-out phrasings is wrong and gets rejected with one click, with the evidence shown beside it.
20. **What would you do next?** Calibrate on real annotated data, add unit-less and imaging results, and learn per-clinic thresholds from Confirm and Keep-open decisions.

## 15. Limitations

- The benchmark is synthetic and small (184 reports). Real clinical accuracy is unknown.
- It recognises results only in "label: value unit" form, with a known unit. Unit-less values (INR), narrative results and imaging aren't captured.
- Instruction extraction is still rule-based. Instructions phrased outside the recognised verbs aren't tracked.
- Some correct pairs fall below the threshold (anaemia and haemoglobin at 0.30). The engine chooses precision over recall.
- Related-but-wrong tests can clear the threshold. "Gout follow-up blood test for urate" scores serum creatinine at 0.40 when no uric acid result is present: a false suggestion that a doctor would have to reject.
- Loops created before this change have no vectors, so only the rules apply to them (no backfill).
- The model is 438 MB (fp32) and loaded per worker job.
- The verification cache trusts the file's size, modification time and inode after the first hash.

## 16. Future scale

- Calibrate thresholds on de-identified annotated data, and learn from doctors' Confirm and Keep-open decisions as labelled feedback.
- Map labels to LOINC codes for interoperability, using the embeddings as candidate generation.
- Quantise the encoder to int8, or serve it from a long-lived sandboxed worker.
- Extend to imaging and procedure reports and to unit-less results.
- Add a clinic dashboard of completed, overdue and unmatched follow-ups as a care-gap metric.
