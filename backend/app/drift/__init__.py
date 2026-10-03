"""
Evidence-to-Clinical-Meaning Drift Detector.

An independent semantic safety layer: after extraction, it checks
whether each extracted clinical fact still means what its source
evidence says. It never creates or corrects medical information; a
finding only says REVIEW REQUIRED and points at the exact quote.
Runs inside the isolated worker (no network, no database).
"""

from .checks import check_document  # noqa: F401
