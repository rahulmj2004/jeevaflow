"""
Potential matching between Open Loops and newer source-backed
observations: the keyword rules below, then the AI Follow-Through
Engine (app/followthrough) for loops the rules cannot match.

A match is a suggestion for human review. It never closes a loop
and never interprets whether a result is good or bad.

Rules (all must hold):
1. The loop is still active (OPEN / NEEDS_REVIEW / POTENTIAL_MATCH).
2. The instruction text names the observation type
   (e.g. "Repeat HbA1c ..." <-> HbA1c).
3. The observation comes from a different, newer document than
   the one the instruction was read from.
4. The observation has source evidence and has not been rejected.

AI pass (same conditions 1, 3 and 4; replaces condition 2): the
loop's instruction-concept embedding and the result-label embeddings
are compared, and only calibrated, top-ranked candidates are
suggested, each with its score, rank and the alternatives considered.
"""

import re
from datetime import date

from sqlalchemy.orm import Session

from .followthrough import backends, semantic
from .loops import ACTIVE_STATES, record_event
from .security.crypto import new_ref
from .security.phi_log import log_event
from .models import (
    AIDecision,
    Commitment,
    Document,
    LoopMatch,
    LoopState,
    Observation,
    ReviewStatus,
)


INSTRUCTION_KEYWORDS = {
    "HbA1c": r"\b(?:hba1c|a1c|glycated|glycosylated)\b",
    "Glucose": r"\b(?:glucose|blood sugar|fbs|ppbs)\b",
    "Creatinine": r"\b(?:creatinine|kidney function|renal function|kft|rft)\b",
    "LDL Cholesterol": r"\b(?:ldl|lipid profile|lipids|cholesterol)\b",
    "Blood Pressure": r"\b(?:blood pressure|bp)\b",
}


def instruction_mentions(
    instruction: str,
    observation_type: str,
) -> bool:
    pattern = INSTRUCTION_KEYWORDS.get(observation_type)

    if pattern is None:
        return False

    return re.search(pattern, instruction, re.IGNORECASE) is not None


def effective_date(document: Document) -> date:
    return document.document_date or document.created_at.date()


def is_newer(candidate: Document, original: Document) -> bool:
    if candidate.id == original.id:
        return False

    candidate_date = effective_date(candidate)
    original_date = effective_date(original)

    if candidate_date != original_date:
        return candidate_date > original_date

    # Same calendar day: only trust receive order when at least one
    # side has no stated report date. Two reports both dated the
    # same day are treated as the same visit, not a follow-up.
    if (
        candidate.document_date is None
        or original.document_date is None
    ):
        return candidate.created_at > original.created_at

    return False


def detect_potential_matches(
    db: Session,
    document: Document,
) -> list[LoopMatch]:
    """
    Check observations from a newly processed document against
    the patient's active loops. Returns newly created matches.

    The caller commits the session.
    """

    observations = (
        db.query(Observation)
        .filter(
            Observation.document_id == document.id,
            Observation.evidence_id.isnot(None),
            Observation.review_status
            != ReviewStatus.REJECTED.value,
        )
        .all()
    )

    if not observations:
        return []

    commitments = (
        db.query(Commitment)
        .filter(
            Commitment.patient_id == document.patient_id,
            Commitment.state.in_(ACTIVE_STATES),
            Commitment.document_id != document.id,
        )
        .all()
    )

    created = []

    for commitment in commitments:
        original = db.get(Document, commitment.document_id)

        if original is None or not is_newer(document, original):
            continue

        for observation in observations:
            if not instruction_mentions(
                commitment.instruction,
                observation.observation_type,
            ):
                continue

            exists = (
                db.query(LoopMatch)
                .filter(
                    LoopMatch.commitment_id == commitment.id,
                    LoopMatch.observation_id == observation.id,
                )
                .first()
            )

            if exists:
                continue

            match = LoopMatch(
                commitment_id=commitment.id,
                observation_id=observation.id,
                document_id=document.id,
                evidence_id=observation.evidence_id,
                rule=(
                    f"Instruction mentions {observation.observation_type}; "
                    f"a newer document "
                    f"({effective_date(document).isoformat()}) "
                    f"contains a source-backed "
                    f"{observation.observation_type} result."
                ),
            )

            db.add(match)
            db.flush()

            record_event(
                db,
                commitment,
                action="POTENTIAL_MATCH_DETECTED",
                to_state=LoopState.POTENTIAL_MATCH.value,
                actor_type="SYSTEM",
                actor_name="matching-rules",
                match_id=match.id,
            )

            created.append(match)

    # Failure isolation: all AI work (vectors, scoring, decisions) is
    # planned read-only first; if it fails, nothing is written and the
    # document and the rule matches are kept.
    try:
        plans = _plan_semantic(db, document, observations, commitments, created)
    except Exception:
        log_event("AI_FOLLOWTHROUGH_FAILED", result="FAILED", object=document.ref)
        plans = []

    created += _apply_semantic(db, document, observations, plans)

    return created


def _plan_semantic(db, document, observations, commitments, rule_matches) -> list[dict]:
    """
    Read-only AI pass for loops the rules did not match in this
    document: which backend, which threshold, every candidate scored.
    """

    rule_matched = {match.commitment_id for match in rule_matches}
    plans = []

    for commitment in commitments:
        model_id = backends.model_of(commitment.embedding)
        loop_vector = semantic.unpack(commitment.embedding, model_id) if model_id else None
        original = db.get(Document, commitment.document_id)

        if (
            loop_vector is None
            or commitment.id in rule_matched
            or original is None
            or not is_newer(document, original)
        ):
            continue

        # Only vectors from the same backend are comparable.
        candidates = [
            (observation.id, observation.observation_type, semantic.unpack(observation.embedding, model_id))
            for observation in observations
        ]
        candidates = [item for item in candidates if item[2] is not None]
        threshold = backends.FOLLOW_THROUGH_THRESHOLD[model_id]
        scored = semantic.rank(loop_vector, candidates, threshold) if candidates else []

        plans.append({
            "commitment": commitment,
            "model_id": model_id,
            "threshold": threshold,
            "scored": scored,
            "explanations": {
                item["key"]: semantic.explanation_json(model_id, item, scored, threshold)
                for item in scored if item["suggested"]
            },
        })

    return plans


def _apply_semantic(db, document, observations, plans) -> list[LoopMatch]:
    """
    Write the planned AI decisions: SUGGESTED (with POTENTIAL_MATCH
    suggestions) or AI_ABSTAINED (the loop is left exactly as it was).
    """

    by_id = {observation.id: observation for observation in observations}
    created = []

    for plan in plans:
        commitment, model_id, threshold, scored = plan["commitment"], plan["model_id"], plan["threshold"], plan["scored"]
        suggested = [item for item in scored if item["suggested"]]

        decision = AIDecision(
            ref=new_ref("aid"),
            feature="FOLLOW_THROUGH",
            patient_id=commitment.patient_id,
            commitment_id=commitment.id,
            document_id=document.id,
            backend=backends.BACKEND_OF[model_id],
            model_id=model_id,
            best_score=scored[0]["score"] if scored else None,
            threshold=threshold,
            candidates=len(scored),
        )

        if not suggested:
            decision.decision = "AI_ABSTAINED"
            decision.reason = (
                "BELOW_THRESHOLD" if scored
                else "NO_COMPARABLE_CANDIDATES" if observations
                else "NO_CANDIDATES"
            )
            db.add(decision)
            record_event(
                db, commitment, action="AI_ABSTAINED", to_state=None,
                actor_type="SYSTEM", actor_name="ai-followthrough",
                note="AI abstained: available evidence was insufficient. The loop stays open.",
            )
            continue

        for item in suggested:
            observation = by_id[item["key"]]

            if db.query(LoopMatch).filter(
                LoopMatch.commitment_id == commitment.id, LoopMatch.observation_id == observation.id
            ).first():
                continue

            match = LoopMatch(
                commitment_id=commitment.id,
                observation_id=observation.id,
                document_id=document.id,
                evidence_id=observation.evidence_id,
                # Plain text column: no document content, only the method.
                rule=(
                    f"AI semantic match ({backends.BACKEND_OF[model_id]}): similarity {item['score']:.2f}, "
                    f"ranked {item['rank']} of {len(scored)} results in a newer document "
                    f"({effective_date(document).isoformat()}). Suggestion only."
                ),
                method="AI_SEMANTIC",
                score=item["score"],
                explanation=plan["explanations"][item["key"]],
            )

            db.add(match)
            db.flush()

            if decision.match_id is None:
                decision.match_id = match.id

            record_event(
                db,
                commitment,
                action="POTENTIAL_MATCH_DETECTED",
                to_state=LoopState.POTENTIAL_MATCH.value,
                actor_type="SYSTEM",
                actor_name="ai-followthrough",
                match_id=match.id,
            )

            created.append(match)

        decision.decision = "SUGGESTED"
        db.add(decision)

    return created
