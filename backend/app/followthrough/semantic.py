"""
Semantic follow-through scoring (vector maths only, API process).

For each open loop, every result from a NEWER document is a
candidate. The score is the cosine similarity between the loop's
instruction-concept embedding and the result-label embedding (both
computed in the isolated worker). A candidate is suggested only if:

    score >= threshold           calibrated per backend on the
                                 synthetic calibration split
                                 (backends.py, evaluation/) for
                                 precision first; below it, abstain
    it ranks first for the loop  among the document's results, or
                                 within RANK_MARGIN of the first (a
                                 kidney panel can satisfy one loop
                                 with creatinine AND eGFR)

Every suggestion keeps its score, its rank and the runner-up
candidates, so a doctor can see why it was made, and it is a
POTENTIAL_MATCH only: nothing is closed without human confirmation.
"""

import base64
import json
from typing import Optional

import numpy as np

# Default = pubmedbert follow-through threshold (backends.py).
SUGGEST_THRESHOLD = 0.68
RANK_MARGIN = 0.06
MAX_SUGGESTIONS = 3
ALTERNATIVES_SHOWN = 3


def pack(model_id: str, vector) -> str:
    """
    "<model id>|<base64 float16>": compact, stored inside an encrypted
    column, and tagged so vectors from different models never meet.
    """
    return f"{model_id}|" + base64.b64encode(np.asarray(vector, dtype=np.float16).tobytes()).decode()


def unpack(blob: Optional[str], model_id: str) -> Optional[np.ndarray]:
    """
    The unit vector, or None if absent, malformed or from another model.
    """

    if not blob or not blob.startswith(f"{model_id}|"):
        return None

    try:
        vector = np.frombuffer(base64.b64decode(blob.split("|", 1)[1]), dtype=np.float16).astype(np.float32)
    except Exception:
        return None

    norm = float(np.linalg.norm(vector))

    return vector / norm if vector.size and norm > 0 else None


def rank(loop_vector: np.ndarray, candidates: list[tuple[object, str, np.ndarray]],
         threshold: Optional[float] = None) -> list[dict]:
    """
    candidates: (key, label, vector). Returns every candidate scored,
    best first, each marked suggested or not. threshold defaults to
    the pubmedbert follow-through threshold.
    """

    threshold = SUGGEST_THRESHOLD if threshold is None else threshold

    scored = sorted(
        (
            {"key": key, "label": label, "score": round(float(loop_vector @ vector), 4)}
            for key, label, vector in candidates
        ),
        key=lambda item: item["score"],
        reverse=True,
    )

    best = scored[0]["score"] if scored else 0.0

    for position, item in enumerate(scored, start=1):
        item["rank"] = position
        item["suggested"] = (
            item["score"] >= threshold
            and best - item["score"] <= RANK_MARGIN
            and position <= MAX_SUGGESTIONS
        )

    return scored


def explanation(model_id: str, item: dict, scored: list[dict], threshold: Optional[float] = None) -> dict:
    """
    Structured "why" for one suggestion: backend, score, rank,
    threshold and the other candidates considered.
    """

    from .backends import BACKEND_OF

    return {
        "method": "AI_SEMANTIC",
        "backend": BACKEND_OF.get(model_id, "unknown"),
        "model": model_id,
        "score": item["score"],
        "rank": item["rank"],
        "candidates": len(scored),
        "threshold": SUGGEST_THRESHOLD if threshold is None else threshold,
        "alternatives": [
            {"label": other["label"], "score": other["score"]}
            for other in scored
            if other["key"] != item["key"]
        ][:ALTERNATIVES_SHOWN],
    }


def explanation_json(model_id: str, item: dict, scored: list[dict], threshold: Optional[float] = None) -> str:
    return json.dumps(explanation(model_id, item, scored, threshold))
