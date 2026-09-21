from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any
from uuid import uuid4

from pydantic import AliasChoices, BaseModel, ConfigDict, Field


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
    priority: int = 0
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

    def __init__(self, **data: Any) -> None:
        super().__init__(**data)
        if self.root_task_id is None:
            self.root_task_id = self.id


class TaskNode(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid4()))
    task_id: str
    parent_node_id: str | None = None
    type: NodeType = NodeType.TASK
    description: str
    status: NodeStatus = NodeStatus.CREATED
    priority: int = 0
    input_data: dict[str, Any] = Field(default_factory=dict)
    output_data: dict[str, Any] = Field(default_factory=dict)
    retry_count: int = 0
    max_retries: int = 3
    created_at: datetime = Field(default_factory=utcnow)
    started_at: datetime | None = None
    finished_at: datetime | None = None
    error: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class GraphEdge(BaseModel):
    from_node: str
    to_node: str
    dependency_type: DependencyType = DependencyType.SUCCESS
    condition: str | None = None


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
    priority: int = 0
    deadline: datetime | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


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
