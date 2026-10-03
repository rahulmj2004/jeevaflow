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

Metrics
    precision     correct suggestions / all suggestions
    recall        reports containing the test where it was suggested
    false_alarm   reports without the test that still got a suggestion
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

            for index in range(REPORTS_PER_INSTRUCTION):
                report = rng.sample(others, DISTRACTORS)
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
    saved = semantic.SUGGEST_THRESHOLD
    semantic.SUGGEST_THRESHOLD = threshold

    try:
        for instruction, concept, report, present in rows:
            loop_vector = vectors[instruction_concept(instruction)]
            scored = semantic.rank(loop_vector, [(i, result_label(l), vectors[result_label(l)]) for i, (_, l) in enumerate(report)])
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
    finally:
        semantic.SUGGEST_THRESHOLD = saved

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

    for threshold in np.arange(0.30, 0.80, 0.01):
        result = evaluate(0, encode, float(threshold))["ai"]

        if result["precision"] is not None and result["precision"] >= min_precision and result["recall"] > best[1]:
            best = (round(float(threshold), 2), result["recall"])

    return best[0]


def main():
    from app.followthrough.encoder import MODEL_ID, load

    encoder, problem = load()

    if encoder is None:
        print(f"model unavailable: {problem}")
        return 1

    calibrated = calibrate(encoder.encode)
    print(f"model: {MODEL_ID}")
    print(f"calibrated threshold (precision >= 0.90 on calibration split): {calibrated}")
    print(f"configured threshold: {semantic.SUGGEST_THRESHOLD}")

    for name, split in (("calibration", 0), ("held-out test", 1)):
        result = evaluate(split, encoder.encode, semantic.SUGGEST_THRESHOLD)
        print(f"\n{name} split ({result['ai']['reports']} reports)")
        for method in ("rule", "ai"):
            print(f"  {method:5s} {result[method]}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
