from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, Field


def contract_now() -> datetime:
    return datetime.now(UTC)


class ContractScope(StrEnum):
    GLOBAL = "global"
    PROJECT = "project"
    SESSION = "session"
    TASK = "task"
    NODE = "node"
    EXECUTION = "execution"


class ArtifactKind(StrEnum):
    FILE = "file"
    DIRECTORY = "directory"
    REPORT = "report"
    CALCULATION = "calculation"
    TEST_RESULT = "test_result"
    COMMAND_OUTPUT = "command_output"
    CODEGRAPH = "codegraph"
    IMAGE = "image"
    DATASET = "dataset"
    ANSWER = "answer"


class CriterionStatus(StrEnum):
    PASS = "PASS"
    FAIL = "FAIL"
    UNKNOWN = "UNKNOWN"
    NOT_APPLICABLE = "NOT_APPLICABLE"


class TaskDeliverable(BaseModel):
    """A result the task promises to produce."""

    id: str = Field(default_factory=lambda: str(uuid4()))
    kind: ArtifactKind
    description: str = Field(min_length=1, max_length=2000)
    path: str | None = None
    required: bool = True


class AcceptanceCriterion(BaseModel):
    """A checkable condition used by verification and final reporting."""

    id: str = Field(min_length=1, max_length=100)
    description: str = Field(min_length=1, max_length=2000)
    required: bool = True


class TaskConstraint(BaseModel):
    description: str = Field(min_length=1, max_length=2000)
    source: str = "user"
    id: str | None = Field(default=None, max_length=100)
    strength: str = Field(default="soft", pattern="^(hard|soft)$")
    weight: float = Field(default=1.0, ge=0.0, le=100.0)
    scope: ContractScope = ContractScope.TASK
    on_violation: str = Field(default="warn", pattern="^(warn|record|ask|replan|block)$")
    applies_to: list[str] = Field(default_factory=list)


class SoftConstraint(TaskConstraint):
    """Non-blocking preference unless its policy explicitly escalates it."""

    strength: str = "soft"
    on_violation: str = Field(default="warn", pattern="^(warn|record|ask|replan|block)$")


class TaskAssumption(BaseModel):
    description: str = Field(min_length=1, max_length=2000)
    source: str = "system"
    confirmed: bool = False


class ValidationStrategy(BaseModel):
    checks: list[str] = Field(default_factory=list)
    require_evidence: bool = True


class InputRef(BaseModel):
    """A declared input to a planned node."""

    kind: str = Field(min_length=1, max_length=100)
    ref: str = Field(min_length=1, max_length=500)
    required: bool = True


class OutputSpec(BaseModel):
    """A declared output a planned node is expected to publish."""

    kind: ArtifactKind
    name: str = Field(min_length=1, max_length=255)
    description: str = ""
    required: bool = True


class RetryPolicy(BaseModel):
    """Execution retry configuration; max_attempts includes the first attempt."""

    max_attempts: int = Field(default=4, ge=1)
    retry_on: list[str] = Field(default_factory=list)
    backoff_seconds: float = Field(default=2.0, ge=0.0)
    max_backoff_seconds: float = Field(default=300.0, ge=0.0)

    @property
    def max_retries(self) -> int:
        return self.max_attempts - 1


class IdempotencyPolicy(BaseModel):
    required: bool = False
    key_template: str | None = None


class FailurePolicy(BaseModel):
    action: str = "replan"
    preserve_outputs: bool = True


class NodeContract(BaseModel):
    """Typed execution contract for one planned node."""

    logical_id: str | None = None
    acceptance: dict[str, Any] = Field(default_factory=dict)
    inputs: list[InputRef] = Field(default_factory=list)
    outputs: list[OutputSpec] = Field(default_factory=list)
    acceptance_criteria: list[AcceptanceCriterion] = Field(default_factory=list)
    allowed_tools: list[str] = Field(default_factory=list)
    retry_policy: RetryPolicy = Field(default_factory=RetryPolicy)
    idempotency_policy: IdempotencyPolicy = Field(default_factory=IdempotencyPolicy)
    failure_policy: FailurePolicy = Field(default_factory=FailurePolicy)
    deadline: datetime | None = None


class NodeRuntimeState(BaseModel):
    """Structured mutable state kept separate from the node contract."""

    next_retry_at: datetime | None = None
    review_required: bool = False
    review_status: str | None = None
    recovery_pending: bool = False
    recovery_expansion_id: str | None = None
    recovery_target_id: str | None = None
    recovery_finalize: bool = False
    recovery_attempt: int = 0
    branch_skipped: bool = False
    operation_hint: dict[str, Any] = Field(default_factory=dict)
    branch_config: dict[str, Any] = Field(default_factory=dict)


class OperationHint(BaseModel):
    """Optional deterministic routing hint produced by the planner."""

    tool: str
    method: str
    args: dict[str, Any] = Field(default_factory=dict)
    timeout: float | None = None


class BranchConfig(BaseModel):
    """Explicit conditional routing without encoding targets in free metadata."""

    operator: str = "truthy"
    value: Any = None
    source_node_id: str | None = None
    skip_on_true: list[str] = Field(default_factory=list)
    skip_on_false: list[str] = Field(default_factory=list)


class TaskContract(BaseModel):
    """Stable, domain-neutral contract for what completing a task means."""

    objective: str = Field(min_length=1, max_length=10000)
    deliverables: list[TaskDeliverable] = Field(default_factory=list)
    acceptance_criteria: list[AcceptanceCriterion] = Field(default_factory=list)
    constraints: list[TaskConstraint] = Field(default_factory=list)
    assumptions: list[TaskAssumption] = Field(default_factory=list)
    requested_domain: str | None = None
    requested_project_type: str | None = None
    allowed_side_effects: list[str] = Field(default_factory=list)
    forbidden_side_effects: list[str] = Field(default_factory=list)
    validation_strategy: ValidationStrategy = Field(default_factory=ValidationStrategy)
    schema_version: int = 1


class TaskRuntimeState(BaseModel):
    """Mutable task execution state, separate from the task contract."""

    target: Any = None
    workflow: str | None = None
    agent_turns: int = 0
    run_tests: bool = False
    clarification: dict[str, Any] = Field(default_factory=dict)
    final_response: dict[str, Any] | None = None
    final_response_pending: bool = False
    final_status: str | None = None
    final_finished_at: datetime | None = None
    llm_calls: int = 0
    tool_calls: int = 0
    codegraph_queries: int = 0
    project_reads: int = 0
    source_bytes: int = 0
    recovery_attempts: int = 0
    created_project: dict[str, Any] = Field(default_factory=dict)


class ArtifactRef(BaseModel):
    """Immutable reference to an output produced by a task or node."""

    id: str = Field(default_factory=lambda: str(uuid4()))
    kind: ArtifactKind
    description: str = Field(min_length=1, max_length=2000)
    producer_node_id: str
    path: str | None = None
    checksum: str | None = None
    version: int = Field(default=1, ge=1)
    metadata: dict[str, Any] = Field(default_factory=dict)


class ResolvedInput(BaseModel):
    """Result of resolving one declared node input."""

    requested: InputRef
    artifacts: list[ArtifactRef] = Field(default_factory=list)
    missing: bool = False
    reason: str | None = None


class MemoryFact(BaseModel):
    key: str = Field(min_length=1, max_length=255)
    value: Any
    source: str = "system"
    provenance: list[str] = Field(default_factory=list)
    scope: ContractScope = ContractScope.TASK
    confidence_basis: list[str] = Field(default_factory=list)
    valid_until: datetime | None = None


class DecisionRecord(BaseModel):
    decision: str = Field(min_length=1, max_length=2000)
    reason: str = ""
    decided_by: str = "system"
    supersedes: str | None = None
    created_at: datetime = Field(default_factory=contract_now)


class OpenQuestion(BaseModel):
    question: str = Field(min_length=1, max_length=2000)
    required: bool = True
    answer: str | None = None


class WorkingMemory(BaseModel):
    """Typed task-local blackboard shared by execution stages."""

    facts: list[MemoryFact] = Field(default_factory=list)
    decisions: list[DecisionRecord] = Field(default_factory=list)
    assumptions: list[TaskAssumption] = Field(default_factory=list)
    questions: list[OpenQuestion] = Field(default_factory=list)
    artifact_refs: list[ArtifactRef] = Field(default_factory=list)
    schema_version: int = 1


class CriterionResult(BaseModel):
    criterion_id: str
    status: CriterionStatus
    evidence: list[str] = Field(default_factory=list)
    reason: str = ""


class Diagnostic(BaseModel):
    code: str = Field(min_length=1, max_length=100)
    message: str = Field(min_length=1, max_length=4000)
    severity: str = "error"
    retryable: bool = False
    cause: str | None = None
    evidence: list[str] = Field(default_factory=list)
