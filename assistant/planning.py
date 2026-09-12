from __future__ import annotations

from .llm import PlanProposal


_ALLOWED_TYPES = {"OPERATION", "SUBTASK", "DECISION", "VERIFY", "WAIT", "CONDITION"}
_ALLOWED_CONDITION_OPERATORS = {
    "truthy",
    "falsy",
    "equals",
    "not_equals",
    "contains",
    "greater_than",
    "less_than",
}


class PlanQualityError(ValueError):
    """Raised when a syntactically valid plan is not executable enough."""


def validate_plan_quality(proposal: PlanProposal, max_nodes: int) -> None:
    if proposal.answer is not None and (proposal.nodes or proposal.subtasks):
        raise PlanQualityError("direct answers cannot include executable work")
    if not proposal.nodes:
        if proposal.answer is None and not proposal.subtasks:
            raise PlanQualityError("planner returned neither an answer nor executable work")
        if any(not item.strip() for item in proposal.subtasks):
            raise PlanQualityError("planner returned an empty subtask description")
        return
    if len(proposal.nodes) > max_nodes and not proposal.subtasks:
        raise PlanQualityError(
            f"planner returned {len(proposal.nodes)} nodes without bounded subtasks"
        )

    ids = [item.id.strip() for item in proposal.nodes]
    if any(not item for item in ids):
        raise PlanQualityError("planner returned a node without an id")
    if len(set(ids)) != len(ids):
        raise PlanQualityError("planner returned duplicate node ids")
    known_ids = set(ids)
    if not any(item.type in {"OPERATION", "SUBTASK", "VERIFY", "WAIT", "CONDITION"} for item in proposal.nodes):
        raise PlanQualityError("planner returned no executable node")

    for item in proposal.nodes:
        if len(item.description.strip()) < 3:
            raise PlanQualityError(f"node {item.id} has no actionable description")
        if item.type not in _ALLOWED_TYPES:
            raise PlanQualityError(f"node {item.id} has unsupported type {item.type}")
        unknown = set(item.dependencies) - known_ids
        if unknown:
            raise PlanQualityError(
                f"node {item.id} references unknown dependencies: {sorted(unknown)}"
            )
        for branch_key in ("on_false", "on_true"):
            targets = item.metadata.get(branch_key, [])
            if not isinstance(targets, list) or any(target not in known_ids for target in targets):
                raise PlanQualityError(f"node {item.id} has invalid {branch_key} targets")
        if item.type == "CONDITION":
            operator = item.metadata.get("operator", "truthy")
            if operator not in _ALLOWED_CONDITION_OPERATORS:
                raise PlanQualityError(
                    f"condition node {item.id} uses unsupported operator {operator}"
                )
            if "value" not in item.metadata and not item.metadata.get("source_node_id"):
                raise PlanQualityError(
                    f"condition node {item.id} needs value or source_node_id"
                )
            source_id = item.metadata.get("source_node_id")
            if source_id and source_id not in known_ids:
                raise PlanQualityError(
                    f"condition node {item.id} references unknown source {source_id}"
                )
