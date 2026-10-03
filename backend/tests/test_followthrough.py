"""
AI Follow-Through Engine: a local biomedical encoder matches doctor
instructions (Open Loops) to later quoted results that the keyword
rules cannot see. It only suggests; a doctor confirms.
"""

import json

import numpy as np
import pytest
from sqlalchemy import text

from app.database import SessionLocal
from app.demo import FOLLOWTHROUGH_LABS, FOLLOWTHROUGH_PLAN, demo_document_bytes
from app.followthrough import encoder as enc
from app.followthrough import semantic
from app.followthrough.text import instruction_concept, result_label
from app.models import Commitment, LoopMatch, Observation

from tests.conftest import case_of, ingest, login


needs_model = pytest.mark.skipif(
    enc.verify() is not None, reason="encoder not fetched (scripts/fetch_followthrough_model.py)"
)


@pytest.fixture(scope="module")
def encoder():
    loaded, problem = enc.load()
    assert problem is None, problem
    return loaded


# ============================================================
# WHAT IS EMBEDDED
# ============================================================

@pytest.mark.parametrize(
    "instruction, concept",
    [
        ("Repeat kidney function test in 3 months", "kidney function test"),
        ("Check thyroid function after 6 weeks.", "thyroid function"),
        ("Get an ECG done", "ECG"),
        ("Check for anaemia after iron therapy", "anaemia after iron therapy"),
        ("Do KFT before next visit", "KFT"),
        ("Review after 2 weeks", ""),
        ("Follow-up with physician.", ""),
        ("Refer to cardiologist", ""),
    ],
)
def test_instruction_concept(instruction, concept):
    assert instruction_concept(instruction) == concept


def test_result_label_never_includes_the_value():
    assert result_label("TSH: 3.2 mIU/L") == "TSH"


def test_worker_embeds_labels_and_concepts_only(monkeypatch):
    from app import worker

    seen = []

    class Recorder:
        def encode(self, texts):
            seen.extend(texts)
            return np.eye(len(texts), enc.DIMENSIONS, dtype=np.float32)

    monkeypatch.setattr(enc, "load", lambda: (Recorder(), None))

    commitments = [{"instruction": "Check thyroid function after 6 weeks"}, {"instruction": "Review in 4 weeks"}]
    observations = [{"observation_type": "TSH", "value": "3.2", "unit": "mIU/L", "quote": "TSH: 3.2 mIU/L"}]
    summary = worker._embed_for_followthrough(commitments, observations)

    assert seen == ["thyroid function", "TSH"]
    assert not any(char.isdigit() for text_ in seen for char in text_)
    assert summary == {"model": enc.MODEL_ID, "status": "ACTIVE", "embedded": 2}
    assert "embedding" not in commitments[1]  # names no test: never AI-matched


def test_worker_without_model_embeds_nothing(monkeypatch):
    from app import worker

    monkeypatch.setattr(enc, "load", lambda: (None, "MODEL_MISSING"))
    commitments = [{"instruction": "Check thyroid function"}]

    assert worker._embed_for_followthrough(commitments, [])["status"] == "MODEL_MISSING"
    assert "embedding" not in commitments[0]


# ============================================================
# SCORING, ABSTENTION, VECTOR HYGIENE
# ============================================================

def _unit(*values):
    vector = np.zeros(enc.DIMENSIONS, dtype=np.float32)
    vector[: len(values)] = values
    return vector / np.linalg.norm(vector)


def test_vectors_are_tagged_with_their_model():
    blob = semantic.pack(enc.MODEL_ID, _unit(1, 0))

    assert np.allclose(semantic.unpack(blob, enc.MODEL_ID), _unit(1, 0), atol=1e-3)
    assert semantic.unpack(blob, "other-model@0000000") is None
    assert semantic.unpack(f"{enc.MODEL_ID}|not-base64!!", enc.MODEL_ID) is None
    assert semantic.unpack(None, enc.MODEL_ID) is None


def test_rank_abstains_below_threshold_and_keeps_alternatives():
    loop = _unit(1, 0, 0)
    candidates = [
        ("close", "Close", _unit(0.9, 0.1, 0)),
        ("near", "Near", _unit(0.88, 0.2, 0)),
        ("far", "Far", _unit(0, 1, 0)),
    ]
    scored = semantic.rank(loop, candidates)

    assert [item["key"] for item in scored if item["suggested"]] == ["close", "near"]
    assert scored[-1]["suggested"] is False

    nothing = semantic.rank(loop, [("far", "Far", _unit(0, 1, 0))])
    assert nothing[0]["suggested"] is False  # below threshold: abstain

    explanation = semantic.explanation(enc.MODEL_ID, scored[0], scored)
    assert explanation["threshold"] == semantic.SUGGEST_THRESHOLD
    assert [item["label"] for item in explanation["alternatives"]] == ["Near", "Far"]


# ============================================================
# THE MODEL
# ============================================================

def test_tampered_or_missing_model_is_refused(tmp_path):
    for name in enc.PINNED_FILES:
        (tmp_path / name).write_bytes(b"tampered")

    assert enc.load(tmp_path) == (None, "MODEL_CHECKSUM_MISMATCH")
    assert enc.load(tmp_path / "absent") == (None, "MODEL_MISSING")


@needs_model
def test_encoder_is_deterministic_and_normalised(encoder):
    first = encoder.encode(["thyroid function", "TSH"])
    second = encoder.encode(["thyroid function", "TSH"])

    assert first.shape == (2, enc.DIMENSIONS)
    assert np.allclose(np.linalg.norm(first, axis=1), 1.0, atol=1e-5)
    assert np.array_equal(first, second)


@needs_model
def test_encoder_carries_biomedical_knowledge(encoder):
    concept, tsh, creatinine = encoder.encode(["thyroid function", "TSH", "Serum Creatinine"])

    assert concept @ tsh > concept @ creatinine + 0.2


@needs_model
def test_held_out_evaluation_beats_rules(encoder):
    """
    Regression guard on the synthetic held-out split (see
    evaluation/followthrough_eval.py). Floors sit below the measured
    values so the test fails on real degradation only.
    """

    from evaluation.followthrough_eval import evaluate

    result = evaluate(1, encoder.encode, semantic.SUGGEST_THRESHOLD)

    assert result["ai"]["recall"] >= 0.65
    assert result["ai"]["precision"] >= 0.80
    assert result["ai"]["false_alarm"] <= 0.15
    assert result["rule"]["recall"] <= 0.10
    assert result["ai"]["recall"] - result["rule"]["recall"] >= 0.5


# ============================================================
# END TO END: worker -> storage -> matcher -> doctor
# ============================================================

def _demo(patient_ref):
    ingest(patient_ref, demo_document_bytes(FOLLOWTHROUGH_PLAN))
    return ingest(patient_ref, demo_document_bytes(FOLLOWTHROUGH_LABS))


def _loops(doctor, patient_ref):
    response = doctor.get(f"/api/v1/doctor/patients/{case_of(patient_ref)}/loops")
    assert response.status_code == 200, response.text
    return {loop["instruction"]: loop for loop in response.json()}


@needs_model
def test_ai_matches_what_the_rules_cannot(doctor, patient_ref):
    labs = _demo(patient_ref)

    matched = {(m["method"], m["observation"]["observation_type"]) for m in labs["potential_matches"]}
    assert matched == {
        ("RULE", "Creatinine"),
        ("AI_SEMANTIC", "TSH"),
        ("AI_SEMANTIC", "SGPT (ALT)"),
    }

    loops = _loops(doctor, patient_ref)
    thyroid = loops["Check thyroid function after 6 weeks"]
    assert thyroid["state"] == "POTENTIAL_MATCH"  # suggested, never closed
    match = thyroid["potential_matches"][0]
    assert match["method"] == "AI_SEMANTIC"
    assert match["score"] >= semantic.SUGGEST_THRESHOLD
    assert match["evidence"]["quote"] == "TSH: 3.2 mIU/L"
    assert match["explanation"]["model"] == enc.MODEL_ID
    assert match["explanation"]["rank"] == 1
    assert {item["label"] for item in match["explanation"]["alternatives"]} <= {
        "SGPT (ALT)", "Creatinine", "Hemoglobin", "Vitamin B12",
    }

    # Haemoglobin ranks first for this instruction but scores below the
    # calibrated threshold: the engine abstains and the loop stays open.
    assert loops["Check for anaemia after iron therapy"]["potential_matches"] == []

    # Names no test: stays open for a person to follow up.
    assert loops["Review in 4 weeks"]["potential_matches"] == []
    assert loops["Review in 4 weeks"]["state"] != "POTENTIAL_MATCH"


@needs_model
def test_doctor_confirms_an_ai_suggestion(doctor, patient_ref):
    _demo(patient_ref)
    loop = _loops(doctor, patient_ref)["Check thyroid function after 6 weeks"]

    confirmed = doctor.post(f"/api/v1/doctor/commitments/{loop['id']}/confirm-completion", json={})
    assert confirmed.status_code == 200, confirmed.text
    body = confirmed.json()

    assert body["state"] == "CLOSED"
    assert body["confirmed_match"]["method"] == "AI_SEMANTIC"
    assert [(e["actor_type"], e["actor_name"]) for e in body["history"]][-2:] == [
        ("SYSTEM", "ai-followthrough"),
        ("HUMAN", "Dr. Example (synthetic)"),
    ]


@needs_model
def test_ai_artifacts_are_encrypted_and_plain_text_has_no_content(patient_ref):
    _demo(patient_ref)

    db = SessionLocal()
    try:
        ai = db.query(LoopMatch).filter(LoopMatch.method == "AI_SEMANTIC").all()
        assert ai

        for match in ai:
            assert "thyroid" not in match.rule.lower() and "TSH" not in match.rule

        raw_explanations = [row[0] for row in db.execute(text("SELECT explanation FROM loop_matches WHERE method = 'AI_SEMANTIC'"))]
        raw_embeddings = [row[0] for row in db.execute(text("SELECT embedding FROM commitments WHERE embedding IS NOT NULL"))]

        assert raw_embeddings and raw_explanations
        assert all(enc.MODEL_NAME not in raw and "TSH" not in raw for raw in raw_explanations + raw_embeddings)
    finally:
        db.close()


def test_without_embeddings_rules_still_work(patient_ref, monkeypatch):
    """
    Model absent (no embeddings from the worker): no AI suggestion, no
    error, and the keyword rules still match the kidney instruction.
    """

    from app import pipeline

    real = pipeline.run_extraction

    def without_model(*args, **kwargs):
        extracted = real(*args, **kwargs)
        for item in extracted["observations"] + extracted["commitments"]:
            item.pop("embedding", None)
        return extracted

    monkeypatch.setattr(pipeline, "run_extraction", without_model)

    labs = _demo(patient_ref)

    assert {(m["method"], m["observation"]["observation_type"]) for m in labs["potential_matches"]} == {
        ("RULE", "Creatinine")
    }


@needs_model
def test_corrupt_stored_vectors_are_skipped(patient_ref):
    from app.matching import detect_potential_matches
    from app.models import Document

    _demo(patient_ref)

    db = SessionLocal()
    try:
        from app.models import LoopEvent

        db.query(LoopEvent).update({LoopEvent.match_id: None})
        db.query(LoopMatch).delete()
        for commitment in db.query(Commitment).all():
            commitment.embedding = "garbage"
            commitment.state = "OPEN"
        db.commit()

        latest = db.query(Document).order_by(Document.id.desc()).first()
        created = detect_potential_matches(db, latest)

        assert {match.method for match in created} == {"RULE"}
    finally:
        db.rollback()
        db.close()


@needs_model
def test_unconsented_doctor_cannot_see_ai_suggestions(client, other_client, patient_ref):
    _demo(patient_ref)
    login(other_client, "dr.other")

    assert other_client.get(f"/api/v1/doctor/patients/{case_of(patient_ref)}/loops").status_code == 403


def test_demo_documents_exist_for_the_walkthrough(client):
    keys = {item["key"] for item in client.get("/api/v1/demo").json()["documents"]}

    assert {"followthrough_plan", "followthrough_labs"} <= keys
