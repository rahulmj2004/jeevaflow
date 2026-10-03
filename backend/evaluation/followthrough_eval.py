"""
Synthetic evaluation of the AI Follow-Through Engine against the
existing rule matcher (app/matching.py keyword table).

    cd backend && venv/bin/python -m evaluation.followthrough_eval

Dataset: 16 test concepts, each with instruction phrasings split into
a CALIBRATION half (used only to choose the threshold) and a held-out
TEST half (reported), and the result labels a lab report would print
for that test. Every phrasing and label is synthetic and written for
this evaluation; none comes from real patients.

Scenario per instruction (fixed seed): later lab reports that DO
contain the requested test among unrelated results, and reports that
do NOT (the loop must stay open: any suggestion is a false alarm).

Lexical traps: each instruction's reports always include a result
whose NAME overlaps the instruction but is a different test (HbA1c vs
Hemoglobin, lipid vs Lipase, B12 vs Vitamin D, ...).

Follow-through metrics
    precision     correct suggestions / all suggestions
    recall        reports containing the test where it was suggested
    false_alarm   reports without the test that still got a suggestion
    auroc/auprc   pairwise (instruction, result label) ranking quality

Evidence finder (doctor query -> this record's results), with
unanswerable queries (tests no record contains):
    hit_rate              answerable queries with a correct result
    result_precision      correct results / all results returned
    abstention_correct    unanswerable/absent queries that abstained
    unsupported_answers   unanswerable/absent queries that answered

Thresholds are chosen on the CALIBRATION split only, per backend
(pubmedbert, lexical-fallback). Results are written to
evaluation/results/followthrough.json.
"""

import random
import re
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.followthrough import semantic  # noqa: E402
from app.followthrough.text import instruction_concept, result_label  # noqa: E402
from app.matching import instruction_mentions  # noqa: E402

# concept -> (calibration phrasings, held-out test phrasings, result labels)
DATASET = {
    "kidney": (
        ["Repeat kidney function test in 3 months", "Check renal function after 6 weeks"],
        ["Do KFT before next visit", "Monitor kidney health in 1 month", "Repeat renal profile after 8 weeks"],
        ["Serum Creatinine", "eGFR", "Blood Urea Nitrogen"],
    ),
    "thyroid": (
        ["Check thyroid function after 6 weeks", "Repeat thyroid profile in 2 months"],
        ["Thyroid test after dose change", "Recheck TFT in 6 weeks"],
        ["TSH", "Free T4", "Free T3"],
    ),
    "liver": (
        ["Check liver function after 1 month", "Repeat LFT in 6 weeks"],
        ["Monitor liver enzymes on statin", "Liver profile after 3 months"],
        ["SGPT (ALT)", "SGOT (AST)", "Total Bilirubin"],
    ),
    "lipid": (
        ["Repeat lipid profile after 3 months", "Check cholesterol levels in 12 weeks"],
        ["Fasting lipids after starting statin", "Recheck lipid panel in 3 months"],
        ["LDL Cholesterol", "Triglycerides", "HDL Cholesterol"],
    ),
    "diabetes": (
        ["Repeat HbA1c after 3 months", "Check sugar control in 3 months"],
        ["Review long-term glucose control next quarter", "Glycated haemoglobin after 3 months"],
        ["HbA1c", "Glycated Hemoglobin"],
    ),
    "cbc": (
        ["Do a complete blood count", "Repeat CBC in 2 weeks"],
        ["Check for anaemia after iron therapy", "Haemogram after 1 month"],
        ["Hemoglobin", "Platelet Count", "Total WBC Count"],
    ),
    "vitamin_d": (
        ["Check vitamin D levels after 3 months"],
        ["Recheck vitamin D after supplementation"],
        ["25-OH Vitamin D"],
    ),
    "urine_protein": (
        ["Urine test for protein yearly"],
        ["Screen for diabetic kidney damage with urine test", "Urine albumin after 6 months"],
        ["Urine Microalbumin", "Urine Albumin Creatinine Ratio"],
    ),
    "iron": (
        ["Check iron stores in 2 months"],
        ["Repeat iron studies after supplementation"],
        ["Serum Ferritin", "Serum Iron"],
    ),
    "b12": (
        ["Check vitamin B12 levels"],
        ["Recheck B12 after injections"],
        ["Vitamin B12"],
    ),
    "inflammation": (
        ["Repeat inflammatory markers in 2 weeks"],
        ["Check CRP after antibiotics"],
        ["C-Reactive Protein", "ESR"],
    ),
    "electrolytes": (
        ["Check electrolytes after 1 week"],
        ["Repeat serum sodium and potassium"],
        ["Serum Sodium", "Serum Potassium"],
    ),
    "uric_acid": (
        ["Recheck uric acid in 1 month"],
        ["Gout follow-up blood test for urate"],
        ["Serum Uric Acid"],
    ),
    "coagulation": (
        ["Check INR weekly while on warfarin"],
        ["Repeat PT/INR after dose change"],
        ["Prothrombin Time", "INR"],
    ),
    "psa": (
        ["Repeat PSA after 6 months"],
        ["Prostate marker test yearly"],
        ["Prostate Specific Antigen"],
    ),
    "cardiac": (
        ["Repeat troponin after 6 hours"],
        ["Cardiac enzymes follow-up test"],
        ["Troponin I", "CK-MB"],
    ),
}

# concept -> result labels that share words with the instruction but
# are a DIFFERENT test (never a correct completion).
TRAPS = {
    "diabetes": ["Hemoglobin"],
    "cbc": ["HbA1c"],
    "lipid": ["Serum Lipase"],
    "vitamin_d": ["Vitamin B12"],
    "b12": ["25-OH Vitamin D"],
    "thyroid": ["Thyroglobulin Antibody"],
    "kidney": ["Kidney Stone Analysis"],
    "iron": ["Iron Deficiency Questionnaire Score"],
    "urine_protein": ["Total Protein"],
    "liver": ["Liver Kidney Microsomal Antibody"],
}

# Evidence finder: (calibration queries, held-out queries) per concept.
QUERIES = {
    "kidney": (["kidney function", "When was renal function checked?"], ["latest KFT", "creatinine"]),
    "thyroid": (["thyroid", "thyroid profile results"], ["When was thyroid last checked?", "TSH"]),
    "liver": (["liver function"], ["liver enzymes", "LFT results"]),
    "lipid": (["cholesterol"], ["lipid profile", "LDL"]),
    "diabetes": (["HbA1c"], ["sugar control", "glycated haemoglobin"]),
    "cbc": (["complete blood count"], ["haemoglobin", "anaemia workup"]),
    "vitamin_d": (["vitamin D"], ["25 hydroxy vitamin D"]),
    "inflammation": (["CRP"], ["inflammatory markers"]),
    "electrolytes": (["electrolytes"], ["sodium"]),
    "coagulation": (["INR"], ["clotting test"]),
}
UNANSWERABLE = (
    ["MRI brain", "ECG", "chest x-ray"],
    ["CT abdomen", "echocardiogram", "colonoscopy", "pregnancy test"],
)
RECORD_SIZE = 6

# What the existing extractor could even produce for a label, and
# therefore what the rule matcher can see (app/extraction.py names).
_RULE_TYPES = [
    ("HbA1c", r"\b(?:hba1c|a1c)\b"),
    ("Glucose", r"\b(?:glucose|blood sugar)\b"),
    ("Creatinine", r"\bcreatinine\b"),
    ("LDL Cholesterol", r"\bldl\b"),
    ("Blood Pressure", r"\b(?:blood pressure|bp)\b"),
]

SEED = 7
DISTRACTORS = 3
REPORTS_PER_INSTRUCTION = 4


def rule_type(label: str):
    return next((name for name, pattern in _RULE_TYPES if re.search(pattern, label, re.IGNORECASE)), None)


def scenarios(split: int):
    """
    (instruction, concept, report labels, target present?) tuples.
    """

    rng = random.Random(SEED + split)
    all_labels = [(concept, label) for concept, (_, _, labels) in DATASET.items() for label in labels]

    for concept, phrasings in DATASET.items():
        for instruction in phrasings[split]:
            others = [item for item in all_labels if item[0] != concept]

            trap_items = [("trap", label) for label in TRAPS.get(concept, [])]

            for index in range(REPORTS_PER_INSTRUCTION):
                pool = [item for item in others if item[1] not in TRAPS.get(concept, [])]
                report = trap_items[:1] + rng.sample(pool, DISTRACTORS - len(trap_items[:1]))
                present = index % 2 == 0

                if present:
                    report.append((concept, rng.choice(phrasings[2])))
                    rng.shuffle(report)

                yield instruction, concept, report, present


def evaluate(split: int, encode, threshold: float) -> dict:
    """
    encode(texts) -> unit vectors. Runs AI and rule matcher on the
    same scenarios.
    """

    rows = list(scenarios(split))
    texts = sorted({instruction_concept(row[0]) for row in rows} | {result_label(l) for row in rows for _, l in row[2]})
    vectors = dict(zip(texts, encode(texts)))

    stats = {name: {"tp": 0, "fp": 0, "hit": 0, "positives": 0, "alarms": 0, "negatives": 0} for name in ("ai", "rule")}

    if True:
        for instruction, concept, report, present in rows:
            loop_vector = vectors[instruction_concept(instruction)]
            scored = semantic.rank(
                loop_vector, [(i, result_label(l), vectors[result_label(l)]) for i, (_, l) in enumerate(report)], threshold
            )
            ai = [report[item["key"]][0] for item in scored if item["suggested"]]
            rule = [c for c, label in report if rule_type(label) and instruction_mentions(instruction, rule_type(label))]

            for name, suggested in (("ai", ai), ("rule", rule)):
                s = stats[name]
                s["tp"] += sum(c == concept for c in suggested)
                s["fp"] += sum(c != concept for c in suggested)

                if present:
                    s["positives"] += 1
                    s["hit"] += concept in suggested
                else:
                    s["negatives"] += 1
                    s["alarms"] += bool(suggested)

    return {
        name: {
            "precision": round(s["tp"] / (s["tp"] + s["fp"]), 3) if s["tp"] + s["fp"] else None,
            "recall": round(s["hit"] / s["positives"], 3),
            "false_alarm": round(s["alarms"] / s["negatives"], 3),
            "reports": s["positives"] + s["negatives"],
        }
        for name, s in stats.items()
    }


def calibrate(encode, min_precision: float = 0.90) -> float:
    """
    Highest-recall threshold on the CALIBRATION split whose precision
    is at least min_precision.
    """

    best = (None, -1.0)

    for threshold in np.arange(0.20, 0.90, 0.01):
        result = evaluate(0, encode, float(threshold))["ai"]

        if result["precision"] is not None and result["precision"] >= min_precision and result["recall"] > best[1]:
            best = (round(float(threshold), 2), result["recall"])

    return best[0]


# ------------------------------------------------------------
# Pairwise ranking quality
# ------------------------------------------------------------

def _auroc(scores: np.ndarray, labels: np.ndarray) -> float:
    positive, negative = scores[labels], scores[~labels]
    wins = (positive[:, None] > negative[None, :]).sum() + 0.5 * (positive[:, None] == negative[None, :]).sum()
    return float(wins / (len(positive) * len(negative)))


def _auprc(scores: np.ndarray, labels: np.ndarray) -> float:
    order = np.argsort(-scores, kind="stable")
    hits = labels[order]
    precision_at = np.cumsum(hits) / np.arange(1, len(hits) + 1)
    return float((precision_at * hits).sum() / hits.sum())


def pairwise(split: int, encode) -> dict:
    """
    Every (instruction, result label) pair of the split, including the
    lexical traps: is the label a completion of the instruction?
    """

    instructions = [(i, c) for c, phrasings in DATASET.items() for i in phrasings[split]]
    labels = [(l, c) for c, (_, _, ls) in DATASET.items() for l in ls] + [(l, "trap") for ls in TRAPS.values() for l in ls]
    vectors = encode([instruction_concept(i) for i, _ in instructions] + [result_label(l) for l, _ in labels])
    left, right = vectors[: len(instructions)], vectors[len(instructions):]

    scores = (left @ right.T).ravel()
    truth = np.array([ic == lc for _, ic in instructions for _, lc in labels])

    return {"pairs": int(truth.size), "positives": int(truth.sum()),
            "auroc": round(_auroc(scores, truth), 3), "auprc": round(_auprc(scores, truth), 3)}


# ------------------------------------------------------------
# Evidence finder
# ------------------------------------------------------------

def finder_scenarios(split: int):
    """
    (query, target concept or None, record labels, answerable?).
    """

    from app.followthrough.text import query_concept  # noqa: F401  (used by callers)

    rng = random.Random(SEED + 100 + split)
    all_labels = [(c, l) for c, (_, _, ls) in DATASET.items() for l in ls]

    for concept, phrasings in QUERIES.items():
        for query in phrasings[split]:
            for index in range(REPORTS_PER_INSTRUCTION):
                present = index % 2 == 0
                others = [item for item in all_labels if item[0] != concept]
                record = rng.sample(others, RECORD_SIZE - 1)
                if present:
                    record.append((concept, rng.choice(DATASET[concept][2])))
                else:
                    record.append(rng.choice(others))
                rng.shuffle(record)
                yield query, concept, record, present

    for query in UNANSWERABLE[split]:
        for _ in range(REPORTS_PER_INSTRUCTION):
            yield query, None, rng.sample(all_labels, RECORD_SIZE), False


def evaluate_finder(split: int, encode, threshold: float) -> dict:
    from app.followthrough.text import query_concept

    rows = list(finder_scenarios(split))
    texts = sorted({query_concept(q) for q, *_ in rows} | {result_label(l) for row in rows for _, l in row[2]})
    vectors = dict(zip(texts, encode(texts)))

    answered_ok = answerable = correct_results = returned = abstain_ok = should_abstain = unsupported = 0

    for query, concept, record, present in rows:
        query_vector = vectors[query_concept(query)]
        scored = [(float(query_vector @ vectors[result_label(l)]), c) for c, l in record]
        results = [c for score, c in scored if score >= threshold]
        returned += len(results)
        correct_results += sum(c == concept for c in results)

        if present:
            answerable += 1
            answered_ok += concept in results
        else:
            should_abstain += 1
            abstain_ok += not results
            unsupported += bool(results)

    return {
        "queries": len(rows),
        "hit_rate": round(answered_ok / answerable, 3),
        "result_precision": round(correct_results / returned, 3) if returned else None,
        "abstention_correct": round(abstain_ok / should_abstain, 3),
        "unsupported_answers": round(unsupported / should_abstain, 3),
    }


def calibrate_finder(encode, min_precision: float = 0.85) -> float:
    """
    On the CALIBRATION split: the threshold with the best balanced
    accuracy (hit rate + abstention correctness) / 2 among those whose
    result precision is at least min_precision.
    """

    best = (None, -1.0)

    for threshold in np.arange(0.20, 0.90, 0.01):
        result = evaluate_finder(0, encode, float(threshold))
        balanced = (result["hit_rate"] + result["abstention_correct"]) / 2

        if result["result_precision"] is not None and result["result_precision"] >= min_precision and balanced > best[1]:
            best = (round(float(threshold), 2), balanced)

    return best[0]


# ------------------------------------------------------------
# Performance
# ------------------------------------------------------------

def _peak_rss_mb() -> float:
    import resource

    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return round(peak / (1024 * 1024) if sys.platform == "darwin" else peak / 1024, 1)


def performance(load_encoder, runs: int = 50) -> dict:
    import time

    started = time.perf_counter()
    encoder = load_encoder()
    load_seconds = time.perf_counter() - started

    single = []
    for index in range(runs):
        started = time.perf_counter()
        encoder.encode([["thyroid function", "Serum Creatinine", "HbA1c", "liver enzymes"][index % 4]])
        single.append(time.perf_counter() - started)

    started = time.perf_counter()
    encoder.encode([result_label(l) for _, (_, _, ls) in DATASET.items() for l in ls][:16])
    batch16 = time.perf_counter() - started

    single_ms = np.array(single) * 1000

    return {
        "load_seconds": round(load_seconds, 3),
        "single_text_ms_median": round(float(np.median(single_ms)), 2),
        "single_text_ms_p95": round(float(np.percentile(single_ms, 95)), 2),
        "batch16_ms": round(batch16 * 1000, 2),
        "peak_rss_mb_after": _peak_rss_mb(),
    }


# ------------------------------------------------------------
# Report
# ------------------------------------------------------------

def run_backend(name: str, encode) -> dict:
    from app.followthrough import backends

    model_id = {"pubmedbert": backends.encoder.MODEL_ID, "lexical-fallback": backends.lexical.MODEL_ID}[name]

    return {
        "backend": name,
        "model": model_id,
        "follow_through": {
            "calibrated_threshold": calibrate(encode),
            "configured_threshold": backends.FOLLOW_THROUGH_THRESHOLD[model_id],
            "calibration": evaluate(0, encode, backends.FOLLOW_THROUGH_THRESHOLD[model_id]),
            "test": evaluate(1, encode, backends.FOLLOW_THROUGH_THRESHOLD[model_id]),
            "pairwise_test": pairwise(1, encode),
        },
        "evidence_finder": {
            "calibrated_threshold": calibrate_finder(encode),
            "configured_threshold": backends.EVIDENCE_THRESHOLD[model_id],
            "calibration": evaluate_finder(0, encode, backends.EVIDENCE_THRESHOLD[model_id]),
            "test": evaluate_finder(1, encode, backends.EVIDENCE_THRESHOLD[model_id]),
        },
    }


def main():
    import json

    from app.followthrough import encoder as enc
    from app.followthrough.lexical import LexicalEncoder

    report = {"seed": SEED, "backends": []}

    lexical = LexicalEncoder()
    report["backends"].append(run_backend("lexical-fallback", lexical.encode))

    model, problem = enc.load()

    if model is None:
        report["pubmedbert_unavailable"] = problem
    else:
        first = run_backend("pubmedbert", model.encode)
        report["deterministic"] = first == run_backend("pubmedbert", model.encode)
        report["backends"].append(first)
        report["performance"] = {
            "pubmedbert": performance(lambda: enc.load()[0]),
            "lexical-fallback": performance(LexicalEncoder),
        }

    out = Path(__file__).resolve().parent / "results" / "followthrough.json"
    out.parent.mkdir(exist_ok=True)
    out.write_text(json.dumps(report, indent=2, default=str))
    print(json.dumps(report, indent=2, default=str))

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
