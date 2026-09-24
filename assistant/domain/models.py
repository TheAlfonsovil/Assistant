from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any
from uuid import uuid4

from pydantic import AliasChoices, BaseModel, ConfigDict, Field, field_validator, model_validator

from .contracts import (
    ContractScope,
    DecisionRecord,
    MemoryFact,
    NodeContract,
    NodeRuntimeState,
    TaskContract,
    TaskRuntimeState,
    WorkingMemory,
    split_registered_tool_name,
)


def utcnow() -> datetime:
    return datetime.now(UTC)


class TaskStatus(StrEnum):
    CREATED = "CREATED"
    QUEUED = "QUEUED"
    PLANNING = "PLANNING"
    READY = "READY"
    RUNNING = "RUNNING"
    WAITING = "WAITING"
    BLOCKED = "BLOCKED"
    VERIFYING = "VERIFYING"
    FINALIZING = "FINALIZING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class TaskPriority(StrEnum):
    INMEDIATE = "Inmediate"
    HIGH = "High"
    MEDIUM = "Medium"
    LOW = "Low"


TASK_PRIORITY_VALUES = {
    TaskPriority.LOW: 0,
    TaskPriority.MEDIUM: 1,
    TaskPriority.HIGH: 2,
    TaskPriority.INMEDIATE: 3,
}


def priority_label(priority: int) -> str:
    return max(
        TASK_PRIORITY_VALUES,
        key=lambda label: TASK_PRIORITY_VALUES[label] if TASK_PRIORITY_VALUES[label] <= priority else -1,
    ).value


class NodeType(StrEnum):
    TASK = "TASK"
    OPERATION = "OPERATION"
    SUBTASK = "SUBTASK"
    DECISION = "DECISION"
    CONDITION = "CONDITION"
    VERIFY = "VERIFY"
    WAIT = "WAIT"
    NOTIFY = "NOTIFY"


class NodeStatus(StrEnum):
    CREATED = "CREATED"
    READY = "READY"
    RUNNING = "RUNNING"
    WAITING = "WAITING"
    BLOCKED = "BLOCKED"
    VERIFYING = "VERIFYING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class DependencyType(StrEnum):
    SUCCESS = "SUCCESS"
    FAILURE = "FAILURE"
    ALWAYS = "ALWAYS"


class ErrorType(StrEnum):
    TRANSIENT = "TRANSIENT"
    TIMEOUT = "TIMEOUT"
    INVALID_ARGUMENT = "INVALID_ARGUMENT"
    AUTH = "AUTH"
    NOT_FOUND = "NOT_FOUND"
    TOOL_FAILURE = "TOOL_FAILURE"
    DEPENDENCY_FAILURE = "DEPENDENCY_FAILURE"
    CONFLICT = "CONFLICT"
    UNKNOWN = "UNKNOWN"
    USER_REQUIRED = "USER_REQUIRED"


class VerificationDecision(StrEnum):
    SUCCESS = "SUCCESS"
    RETRY = "RETRY"
    REPLAN = "REPLAN"
    BLOCK = "BLOCK"
    WAIT_USER = "WAIT_USER"
    FAIL = "FAIL"


class TaskBudget(BaseModel):
    max_llm_calls: int = 20
    max_retries: int = 3
    max_recovery_attempts: int = 2
    max_execution_time: float = 86400.0
    max_tool_calls: int = 50
    max_codegraph_queries: int = 100
    max_project_reads: int = 100
    max_source_bytes: int = 20_000_000
    max_plan_nodes: int = 100


class Project(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid4()))
    name: str = Field(min_length=1, max_length=255)
    path: str
    description: str = ""
    project_type: str = "code"
    audit_prompt: str = "Audit the project and report findings with evidence"
    enabled: bool = True
    is_default: bool = False
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)
    last_used_at: datetime | None = None
    last_audited_at: datetime | None = None
    codegraph: dict[str, Any] | None = None
    codegraph_updated_at: datetime | None = None
    codegraph_version: int = 0


class Operation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tool: str
    method: str
    args: dict[str, Any] = Field(
        default_factory=dict,
        validation_alias=AliasChoices("args", "arguments"),
    )
    timeout: float = 60.0
    retry_policy: dict[str, Any] = Field(default_factory=dict)
    idempotency_key: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="before")
    @classmethod
    def normalize_dotted_tool(cls, payload: Any) -> Any:
        return split_registered_tool_name(payload)


class AgentDecisionType(StrEnum):
    """Actions an incremental agent may request from its runtime."""

    EXECUTE = "EXECUTE"
    DELEGATE = "DELEGATE"
    WAIT = "WAIT"
    ASK_USER = "ASK_USER"
    COMPLETE = "COMPLETE"
    FAIL = "FAIL"


class AgentBudget(BaseModel):
    """Limits available to one incremental agent run."""

    max_steps: int = Field(default=100, ge=0)
    max_llm_calls: int = Field(default=20, ge=0)
    max_tool_calls: int = Field(default=50, ge=0)
    max_execution_time: float = Field(default=86400.0, ge=0.0)
    max_tokens: int | None = Field(default=None, ge=0)


class WorkingMemoryPatch(BaseModel):
    """Bounded, explicit changes a worker may make to task-local memory."""

    facts: list[MemoryFact] = Field(default_factory=list, max_length=16)
    decisions: list[DecisionRecord] = Field(default_factory=list, max_length=16)


class AgentDecision(BaseModel):
    """Typed, runtime-neutral decision emitted by an incremental agent."""

    decision_type: AgentDecisionType = Field(
        default=AgentDecisionType.WAIT,
        validation_alias=AliasChoices("decision_type", "type", "action"),
    )
    reason: str = Field(default="", max_length=4000)
    operation: Operation | None = None
    subtasks: list[str] = Field(default_factory=list)
    budget: AgentBudget | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    working_memory_updates: WorkingMemoryPatch = Field(default_factory=WorkingMemoryPatch)

    @property
    def type(self) -> AgentDecisionType:
        return self.decision_type

    @model_validator(mode="after")
    def validate_payload(self) -> "AgentDecision":
        if self.decision_type is AgentDecisionType.EXECUTE and self.operation is None:
            raise ValueError("EXECUTE decisions require an operation")
        if self.decision_type is AgentDecisionType.DELEGATE and not self.subtasks:
            raise ValueError("DELEGATE decisions require at least one subtask")
        if any(not item.strip() for item in self.subtasks):
            raise ValueError("subtasks must not contain blank values")
        return self


class WorkerEvidence(BaseModel):
    """A durable, typed piece of evidence emitted by an agent worker.

    Evidence deliberately contains only JSON-compatible values so it can be
    stored in task/event payloads and reconstructed with ``model_validate``.
    """

    id: str = Field(default_factory=lambda: str(uuid4()), min_length=1, max_length=100)
    kind: str = Field(default="observation", min_length=1, max_length=100)
    source: str = Field(default="worker", min_length=1, max_length=500)
    content: Any = Field(
        default=None,
        validation_alias=AliasChoices("content", "value", "payload"),
    )
    path: str | None = None
    line_start: int | None = Field(default=None, ge=1)
    line_end: int | None = Field(default=None, ge=1)
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=utcnow)

    @model_validator(mode="after")
    def validate_location(self) -> "WorkerEvidence":
        if self.line_start is not None and self.line_end is not None:
            if self.line_end < self.line_start:
                raise ValueError("line_end must be greater than or equal to line_start")
        return self


class RetryDirective(BaseModel):
    """A persisted instruction describing whether worker execution may retry."""

    action: str = Field(default="stop", pattern="^(retry|stop|replan|wait)$")
    reason: str = Field(default="", max_length=4000)
    attempt: int = Field(default=0, ge=0)
    max_attempts: int = Field(default=1, ge=1)
    delay_seconds: float = Field(default=0.0, ge=0.0)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @property
    def should_retry(self) -> bool:
        return self.action == "retry" and self.attempt < self.max_attempts

    @model_validator(mode="after")
    def validate_retry_budget(self) -> "RetryDirective":
        if self.action == "retry" and self.attempt >= self.max_attempts:
            raise ValueError("retry directives require attempt to be below max_attempts")
        return self


class AgentSubtask(BaseModel):
    """A durable unit of work returned by a worker for later scheduling."""

    id: str = Field(default_factory=lambda: str(uuid4()), min_length=1, max_length=100)
    description: str = Field(min_length=1, max_length=10000)
    status: str = Field(default="PENDING", min_length=1, max_length=32)
    priority: int = Field(default=1, ge=0)
    dependencies: list[str] = Field(default_factory=list)
    input: dict[str, Any] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_dependencies(self) -> "AgentSubtask":
        if any(not dependency.strip() for dependency in self.dependencies):
            raise ValueError("dependencies must not contain blank values")
        if len(set(self.dependencies)) != len(self.dependencies):
            raise ValueError("dependencies must not contain duplicates")
        if self.id in self.dependencies:
            raise ValueError("a subtask cannot depend on itself")
        return self


class WorkerResult(BaseModel):
    """The complete JSON-persistible result of one agent worker execution."""

    success: bool
    output: Any = None
    summary: str = Field(default="", max_length=10000)
    error: str | None = Field(default=None, max_length=4000)
    error_type: ErrorType | None = None
    evidence: list[WorkerEvidence] = Field(default_factory=list)
    retry: RetryDirective | None = Field(
        default=None,
        validation_alias=AliasChoices("retry", "retry_directive"),
    )
    subtasks: list[AgentSubtask] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)
    started_at: datetime = Field(default_factory=utcnow)
    finished_at: datetime = Field(default_factory=utcnow)
    duration: float = Field(default=0.0, ge=0.0)

    @model_validator(mode="after")
    def validate_result(self) -> "WorkerResult":
        if self.success and self.error is not None:
            raise ValueError("successful worker results must not contain an error")
        if self.success and self.retry is not None and self.retry.should_retry:
            raise ValueError("successful worker results cannot request a retry")
        if self.finished_at < self.started_at:
            raise ValueError("finished_at must be greater than or equal to started_at")
        return self


class OperationResult(BaseModel):
    success: bool
    output: Any = None
    error: str | None = None
    error_type: ErrorType | None = None
    retryable: bool = False
    artifacts: list[dict[str, Any]] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)
    started_at: datetime = Field(default_factory=utcnow)
    finished_at: datetime = Field(default_factory=utcnow)
    duration: float = 0.0
    side_effects: list[str] = Field(default_factory=list)


class Task(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid4()))
    parent_task_id: str | None = None
    root_task_id: str | None = None
    source: str = "USER"
    project_id: str | None = None
    goal: str
    description: str = ""
    status: TaskStatus = TaskStatus.CREATED
    priority: int = 1
    created_at: datetime = Field(default_factory=utcnow)
    started_at: datetime | None = None
    finished_at: datetime | None = None
    deadline: datetime | None = None
    retry_count: int = 0
    max_retries: int = 3
    metadata: dict[str, Any] = Field(default_factory=dict)
    result_summary: str | None = None
    failure_reason: str | None = None
    budget: TaskBudget = Field(default_factory=TaskBudget)
    contract: TaskContract | None = None
    working_memory: WorkingMemory = Field(default_factory=WorkingMemory)
    runtime: TaskRuntimeState = Field(default_factory=TaskRuntimeState)

    def __init__(self, **data: Any) -> None:
        super().__init__(**data)
        if self.root_task_id is None:
            self.root_task_id = self.id

    def apply_worker_decision(self, decision: AgentDecision) -> None:
        """Apply only the explicitly typed, task-local worker memory patch."""
        patch = decision.working_memory_updates
        for fact in patch.facts:
            self.working_memory.facts = [
                existing for existing in self.working_memory.facts if existing.key != fact.key
            ]
            self.working_memory.facts.append(fact)
        if patch.decisions:
            self.working_memory.decisions.extend(patch.decisions)
        self.working_memory.facts = self.working_memory.facts[-100:]
        self.working_memory.decisions = self.working_memory.decisions[-100:]

class TaskNode(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid4()))
    task_id: str
    parent_node_id: str | None = None
    type: NodeType = NodeType.TASK
    description: str
    status: NodeStatus = NodeStatus.CREATED
    priority: int = 1
    input_data: dict[str, Any] = Field(default_factory=dict)
    output_data: dict[str, Any] = Field(default_factory=dict)
    retry_count: int = 0
    max_retries: int = 3
    created_at: datetime = Field(default_factory=utcnow)
    started_at: datetime | None = None
    finished_at: datetime | None = None
    error: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    contract: NodeContract = Field(default_factory=NodeContract)
    runtime: NodeRuntimeState = Field(default_factory=NodeRuntimeState)

class GraphEdge(BaseModel):
    from_node: str
    to_node: str
    dependency_type: DependencyType = DependencyType.SUCCESS
    condition: str | None = None


class RecoveryExpansion(BaseModel):
    """Persistent identity and lifecycle for one recovery subgraph."""

    id: str = Field(default_factory=lambda: str(uuid4()))
    task_id: str
    target_node_id: str
    attempt: int = Field(ge=1)
    strategy: str = "FIX"
    status: str = "CREATED"
    branch_name: str | None = None
    node_ids: list[str] = Field(default_factory=list)
    final_node_id: str | None = None
    reason: str = ""
    created_at: datetime = Field(default_factory=utcnow)
    finished_at: datetime | None = None


class TaskEvent(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid4()))
    task_id: str
    node_id: str | None = None
    event_type: str
    payload: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=utcnow)


class MemoryRecord(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid4()))
    kind: str
    key: str
    value: Any
    source: str = "USER"
    confidence: float = 1.0
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)
    usage_count: int = 0
    expires_at: datetime | None = None
    scope: ContractScope = ContractScope.GLOBAL
    scope_id: str | None = None

    @model_validator(mode="after")
    def validate_scope(self) -> "MemoryRecord":
        if self.scope is ContractScope.GLOBAL and self.scope_id is not None:
            raise ValueError("global memory must not have a scope_id")
        if self.scope is not ContractScope.GLOBAL and not self.scope_id:
            raise ValueError("scoped memory requires a scope_id")
        return self


class UserProfile(BaseModel):
    """Durable user context explicitly supplied by the user."""

    name: str
    birth_date: str | None = None
    profession: str | None = None
    degrees: list[str] = Field(default_factory=list)
    expertise: list[str] = Field(default_factory=list)


class TaskRequest(BaseModel):
    goal: str = Field(min_length=1, max_length=10000)
    description: str = ""
    source: str = "USER"
    project_id: str | None = None
    project_name: str | None = None
    target_type: str | None = None
    target_id: str | None = None
    priority: int = 1
    deadline: datetime | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("priority", mode="before")
    @classmethod
    def normalize_priority(cls, value: Any) -> int:
        if value is None:
            return TASK_PRIORITY_VALUES[TaskPriority.MEDIUM]
        if isinstance(value, str):
            normalized = value.strip().casefold()
            for label, numeric_value in TASK_PRIORITY_VALUES.items():
                if normalized == label.value.casefold():
                    return numeric_value
            raise ValueError("priority must be Inmediate, High, Medium, or Low")
        return value


class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=10000)
    project_id: str | None = None
    target_type: str | None = None
    target_id: str | None = None
    confirm: bool = False


class ChatFastRequest(ChatRequest):
    """Request accepted by the dashboard's low-latency SSE entry point."""

    stream_timeout: float = Field(default=2.0, ge=0.0, le=10.0)


class IdleConfigurationRequest(BaseModel):
    enabled: bool


class TaskInputRequest(BaseModel):
    node_id: str | None = None
    input: dict[str, Any] = Field(default_factory=dict)


class TaskRedefinitionRequest(BaseModel):
    goal: str = Field(min_length=1, max_length=10000)
    description: str = ""
    metadata: dict[str, Any] = Field(default_factory=dict)


class ProjectRequest(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    path: str
    description: str = ""
    project_type: str = "code"
    audit_prompt: str = "Audit the project and report findings with evidence"
    enabled: bool = True
    is_default: bool = False
