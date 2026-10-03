"""
What gets embedded. An instruction is reduced to its test concept
("Repeat kidney function test in 3 months" -> "kidney function
test"): timing and imperative verbs carry no information about WHICH
test, and measurably blur the embedding (see evaluation/). A result is
represented by its label as written ("Serum Creatinine"), never by
its value, so the model sees no patient values.

No config or database imports: used inside the isolated worker.
"""

import re

_TIMING = re.compile(
    r"\b(?:in|after|within|before|every|next|by|at)\s+(?:the\s+)?"
    r"(?:\d+|one|two|three|four|five|six|eight|ten|twelve|a|an|next)?\s*"
    r"(?:days?|weeks?|months?|years?|quarter|visit|follow[\s-]?up)\b"
    r"|\b(?:yearly|weekly|monthly|annually|daily|fortnightly|quarterly)\b",
    re.IGNORECASE,
)
_LEADING = re.compile(
    r"^\s*(?:please\s+)?(?:repeat|recheck|re-check|check|do|get|monitor|review|order|"
    r"screen\s+for|test\s+for|refer(?:red)?\s+to|follow[\s-]?up\s+(?:with|on)?)\b\s*(?:(?:an|a|the|your)\b)?\s*",
    re.IGNORECASE,
)
# Visit / referral targets: people and places, not tests.
_NOT_A_TEST = re.compile(
    r"^(?:for\s+)?(?:the\s+)?(?:physician|doctor|dr\b.*|clinic|opd|consultant|specialist|"
    r"\w*(?:logist|ician|surgeon)|visit|consultation)$",
    re.IGNORECASE,
)
_TRAILING = re.compile(r"\s*\b(?:done|again|levels?|report)\b\s*$", re.IGNORECASE)


def instruction_concept(instruction: str) -> str:
    """
    The test the instruction asks for, or "" if it names none.
    """

    text = _TIMING.sub(" ", instruction or "")
    text = re.sub(r"[.;,:]+", " ", text)
    text = _LEADING.sub("", text)
    text = re.sub(r"\s+", " ", text).strip()
    text = _TRAILING.sub("", text).strip()

    text = re.sub(r"^for\s+", "", text, flags=re.IGNORECASE)

    if _NOT_A_TEST.match(text):
        return ""

    # Empty: the instruction names no test (e.g. "Review after 2
    # weeks"), so no result can complete it and it is not embedded.
    return text


def result_label(observation_type: str) -> str:
    return re.sub(r"\s+", " ", (observation_type or "").split(":")[0]).strip()
