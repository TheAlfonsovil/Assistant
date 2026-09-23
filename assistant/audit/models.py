from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator


def audit_now() -> datetime:
    return datetime.now(UTC)


class TargetKind(StrEnum):
    PROJECT = "project"
    WORKSPACE = "workspace"
    DOCUMENTATION = "documentation"
    DEVICE = "device"
    DEPLOYMENT = "deployment"
    GENERAL = "general"


class Depth(StrEnum):
    SHALLOW = "shallow"
    STANDARD = "standard"
    DEEP = "deep"


class EvidenceKind(StrEnum):
    EVIDENCE = "evidence"
    INFERENCE = "inference"
    RECOMMENDATION = "recommendation"


class FindingStatus(StrEnum):
    PASS = "pass"
    FAIL = "fail"
    WARN = "warn"
    UNKNOWN = "unknown"
    NOT_APPLICABLE = "not_applicable"


class AuditProfile(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=100)
    description: str = ""
    target_kinds: list[TargetKind] = Field(default_factory=lambda: list(TargetKind))
    default_depth: Depth = Depth.STANDARD
    default_scope: list[str] = Field(default_factory=list)
    checks: list[str] = Field(default_factory=list)


class AuditTarget(BaseModel):
    """A target is descriptive; collecting it must never imply a side effect."""

    model_config = ConfigDict(extra="forbid")

    kind: TargetKind = TargetKind.GENERAL
    identifier: str = Field(min_length=1, max_length=2000)
    name: str | None = Field(default=None, max_length=255)
    scope: str | None = Field(default=None, max_length=500)
    metadata: dict[str, Any] = Field(default_factory=dict)


class AuditSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(default_factory=lambda: str(uuid4()))
    target: AuditTarget
    objective: str = "Assess the target and report actionable findings."
    scope: list[str] = Field(default_factory=list)
    depth: Depth = Depth.STANDARD
    accepted_constraints: list[str] = Field(default_factory=list)
    include: list[str] = Field(default_factory=list)
    exclude: list[str] = Field(default_factory=list)
    profile: str = "general"
    scoring: bool = False
    read_only: bool = True
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("scope", "accepted_constraints", "include", "exclude")
    @classmethod
    def normalize_terms(cls, values: list[str]) -> list[str]:
        return list(dict.fromkeys(value.strip() for value in values if value.strip()))


class Evidence(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(default_factory=lambda: str(uuid4()))
    kind: EvidenceKind = EvidenceKind.EVIDENCE
    source: str = Field(min_length=1, max_length=2000)
    value: Any
    description: str = ""
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)
    metadata: dict[str, Any] = Field(default_factory=dict)


class Facts(BaseModel):
    """Facts are structured observations. They are not conclusions."""

    model_config = ConfigDict(extra="forbid")

    values: dict[str, Any] = Field(default_factory=dict)
    evidence: list[Evidence] = Field(default_factory=list)
    source: str = "collector"

    @property
    def items(self) -> dict[str, object]:
        """Compatibility-friendly name for consumers treating facts as a mapping."""
        return self.values


class AuditFinding(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(default_factory=lambda: str(uuid4()))
    title: str = Field(min_length=1, max_length=500)
    status: FindingStatus = FindingStatus.UNKNOWN
    severity: str = "info"
    category: str = "general"
    message: str = ""
    evidence: list[Evidence] = Field(default_factory=list)
    inferences: list[str] = Field(default_factory=list)
    recommendations: list[str] = Field(default_factory=list)
    score: float | None = Field(default=None, ge=0.0, le=1.0)
    in_scope: bool = True
    effort: str | None = None
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)


class AuditReport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    spec: AuditSpec
    findings: list[AuditFinding] = Field(default_factory=list)
    facts: Facts = Field(default_factory=Facts)
    generated_at: datetime = Field(default_factory=audit_now)
    score: float | None = Field(default=None, ge=0.0, le=1.0)
    summary: str = ""
    errors: list[str] = Field(default_factory=list)
    accepted_constraints: list[str] = Field(default_factory=list)
    deferred: list[str] = Field(default_factory=list)

    @property
    def passed(self) -> bool:
        return not any(
            f.status is FindingStatus.FAIL and f.in_scope
            for f in self.findings
        )
