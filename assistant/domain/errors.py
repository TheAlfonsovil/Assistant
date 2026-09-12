class AssistantError(Exception):
    """Base exception for expected Assistant failures."""


class GraphCycleError(AssistantError):
    pass


class GraphValidationError(AssistantError):
    pass


class LeaseConflictError(AssistantError):
    pass


class ToolNotFoundError(AssistantError):
    pass


class ToolMethodNotFoundError(AssistantError):
    pass


class PolicyViolationError(AssistantError):
    pass
