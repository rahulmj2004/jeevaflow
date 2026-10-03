"""
Which embedding backend produced a vector, and the decision threshold
calibrated for it (evaluation/followthrough_eval.py, calibration split
only; the held-out split never chooses a threshold).

    pubmedbert        PRETRAINED / FROZEN MODEL - NOT FINE-TUNED
                      (NeuML/pubmedbert-base-embeddings, encoder.py)
    lexical-fallback  OUR ALGORITHM, deterministic (lexical.py)

Vectors are tagged with their model id and are only ever compared
with vectors from the same backend.
"""

from typing import Optional

from . import encoder, lexical

PUBMEDBERT = "pubmedbert"
LEXICAL = "lexical-fallback"

BACKEND_OF = {encoder.MODEL_ID: PUBMEDBERT, lexical.MODEL_ID: LEXICAL}

# Values printed as "calibrated_threshold" by the benchmark; a test
# re-runs the calibration and fails if these drift from it.

# Follow-through: instruction concept -> result label. Rule: highest
# recall with precision >= 0.90 on the calibration split.
FOLLOW_THROUGH_THRESHOLD = {encoder.MODEL_ID: 0.68, lexical.MODEL_ID: 0.74}

# Evidence finder: doctor query -> result label. Rule: best balanced
# accuracy (hit rate, abstention) with result precision >= 0.85.
EVIDENCE_THRESHOLD = {encoder.MODEL_ID: 0.40, lexical.MODEL_ID: 0.20}


def load() -> tuple[object, str, Optional[str]]:
    """
    (encoder, model id, fallback reason). Prefers the pinned
    biomedical model; otherwise the lexical fallback, with the reason.
    """

    model, problem = encoder.load()

    if model is not None:
        return model, encoder.MODEL_ID, None

    return lexical.LexicalEncoder(), lexical.MODEL_ID, problem


def model_of(blob: Optional[str]) -> Optional[str]:
    if not blob or "|" not in blob:
        return None

    model_id = blob.split("|", 1)[0]

    return model_id if model_id in BACKEND_OF else None
