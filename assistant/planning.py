from __future__ import annotations

import re

from .llm import PlanProposal

_ALLOWED_TYPES = {"OPERATION", "SUBTASK", "DECISION", "VERIFY", "WAIT", "CONDITION", "NOTIFY"}
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


_COVERAGE_STOPWORDS = {
    "para", "como", "este", "esta", "that", "this", "with", "from", "into",
    "the", "and", "que", "una", "uno", "los", "las", "del", "por", "con",
}


def plan_coverage_warnings(proposal: PlanProposal, goal: str) -> list[str]:
    if proposal.answer is not None or not proposal.nodes:
        return []
    declared = {item.strip().lower() for item in proposal.coverage if item.strip()}
    descriptions = " ".join(item.description.lower() for item in proposal.nodes)
    terms = {
        term for term in re.findall(r"[a-zA-Z0-9_]{4,}", goal.lower())
        if term not in _COVERAGE_STOPWORDS
    }
    missing = sorted(term for term in terms if term not in descriptions)
    warnings = []
    if missing and len(missing) >= max(2, len(terms) // 2):
        warnings.append(f"plan does not mention goal terms: {', '.join(missing[:8])}")
    if terms and not declared:
        warnings.append("planner did not declare coverage items")
    return warnings


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
    if not any(
        item.type in {"OPERATION", "SUBTASK", "VERIFY", "WAIT", "CONDITION", "DECISION", "NOTIFY"}
        for item in proposal.nodes
    ):
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
        for branch_key in ("skip_on_false", "skip_on_true"):
            targets = item.metadata.get(branch_key, [])
            if not isinstance(targets, list) or any(target not in known_ids for target in targets):
                raise PlanQualityError(f"node {item.id} has invalid {branch_key} targets")
            if item.id in targets:
                raise PlanQualityError(f"node {item.id} cannot branch to itself")
        unknown_dependency_types = set(item.dependency_types) - set(item.dependencies)
        if unknown_dependency_types:
            raise PlanQualityError(
                f"node {item.id} declares dependency types for unknown dependencies: "
                f"{sorted(unknown_dependency_types)}"
            )
        if not isinstance(item.acceptance, dict):
            raise PlanQualityError(f"node {item.id} has invalid acceptance evidence")
        acceptance = item.acceptance
        if "fields" in acceptance and not isinstance(acceptance["fields"], dict):
            raise PlanQualityError(f"node {item.id} has invalid field evidence")
        for key in ("exists", "not_exists"):
            if key in acceptance and (
                not isinstance(acceptance[key], list)
                or any(not isinstance(path, str) or not path for path in acceptance[key])
            ):
                raise PlanQualityError(f"node {item.id} has invalid {key} evidence")
        for key in ("contains", "output_contains"):
            if key in acceptance and not isinstance(acceptance[key], (str, list)):
                raise PlanQualityError(f"node {item.id} has invalid {key} evidence")
        if item.type in {"CONDITION", "DECISION"}:
            operator = item.metadata.get("operator", "truthy")
            if operator not in _ALLOWED_CONDITION_OPERATORS:
                raise PlanQualityError(
                    f"{item.type.lower()} node {item.id} uses unsupported operator {operator}"
                )
            if "value" not in item.metadata and not item.metadata.get("source_node_id"):
                raise PlanQualityError(
                    f"{item.type.lower()} node {item.id} needs value or source_node_id"
                )
            source_id = item.metadata.get("source_node_id")
            if source_id and source_id not in known_ids:
                raise PlanQualityError(
                    f"{item.type.lower()} node {item.id} references unknown source {source_id}"
                )
