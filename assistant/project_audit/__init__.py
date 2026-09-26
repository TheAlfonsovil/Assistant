"""Project-audit contracts and deterministic report evaluation."""

from .evaluator import evaluate
from .models import (
    AuditFinding,
    AuditProfile,
    AuditReport,
    AuditSpec,
    AuditTarget,
    Depth,
    Evidence,
    EvidenceKind,
    Facts,
    FindingStatus,
    TargetKind,
)
from .profiles import PROFILES, get_profile, register_profile

__all__ = [
    "PROFILES",
    "AuditFinding",
    "AuditProfile",
    "AuditReport",
    "AuditSpec",
    "AuditTarget",
    "Depth",
    "Evidence",
    "EvidenceKind",
    "Facts",
    "FindingStatus",
    "TargetKind",
    "evaluate",
    "get_profile",
    "register_profile",
]