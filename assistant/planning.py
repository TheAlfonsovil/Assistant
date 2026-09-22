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
    "dime", "decir", "parece", "parecer", "proyecto", "project", "audita",
    "auditar", "audit", "audite", "revisa", "revisar", "review", "analiza",
    "analizar", "inspect", "inspecciona", "inspeccionar", "ejecuta", "ejecutar",
}


def plan_coverage_warnings(proposal: PlanProposal, goal: str) -> list[str]:
    if proposal.answer is not None or not proposal.nodes:
        return []
    descriptions = " ".join(item.description.lower() for item in proposal.nodes)
    terms = {
        term for term in re.findall(r"[a-zA-Z0-9_]{4,}", goal.lower())
        if term not in _COVERAGE_STOPWORDS
    }
    missing = sorted(term for term in terms if term not in descriptions)
    warnings = []
    if missing and len(missing) >= max(2, len(terms) // 2):
        warnings.append(f"plan does not mention goal terms: {', '.join(missing[:8])}")
    # Coverage is supplementary metadata, not a second plan contract. An
    # executable plan without it is valid and should not produce noisy warnings.
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
            targets = getattr(item.branch_config, branch_key)
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
        for criterion in item.acceptance_criteria:
            if isinstance(criterion, str) and not criterion.strip():
                raise PlanQualityError(f"node {item.id} has an empty acceptance criterion")
            if not isinstance(criterion, str) and not criterion.description.strip():
                raise PlanQualityError(f"node {item.id} has an empty acceptance criterion")
        if any(not value.strip() for value in item.allowed_tools):
            raise PlanQualityError(f"node {item.id} has an empty allowed tool")
        output_names: set[str] = set()
        for output in item.outputs:
            name = output.name.strip()
            if not name:
                raise PlanQualityError(f"node {item.id} has an output without a name")
            if name in output_names:
                raise PlanQualityError(
                    f"node {item.id} declares duplicate output name {name!r}"
                )
            output_names.add(name)
        for input_ref in item.inputs:
            reference = input_ref.ref.strip()
            if reference != input_ref.ref:
                raise PlanQualityError(
                    f"node {item.id} has whitespace around input reference {input_ref.ref!r}"
                )
            if reference.startswith("artifact:"):
                if not reference.removeprefix("artifact:").strip():
                    raise PlanQualityError(
                        f"node {item.id} has an empty artifact input reference"
                    )
            elif reference.startswith("node:"):
                parts = reference.split(":")
                if len(parts) not in {2, 3} or not parts[1].strip():
                    raise PlanQualityError(
                        f"node {item.id} has an invalid node input reference {reference!r}"
                    )
                if len(parts) == 3 and not parts[2].strip():
                    raise PlanQualityError(
                        f"node {item.id} has an empty node output name"
                    )
                if parts[1] not in known_ids:
                    raise PlanQualityError(
                        f"node {item.id} references unknown input node {parts[1]!r}"
                    )
            else:
                raise PlanQualityError(
                    f"node {item.id} has unsupported input reference {reference!r}"
                )
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
            operator = item.branch_config.operator
            if operator not in _ALLOWED_CONDITION_OPERATORS:
                raise PlanQualityError(
                    f"{item.type.lower()} node {item.id} uses unsupported operator {operator}"
                )
            if item.branch_config.value is None and not item.branch_config.source_node_id:
                raise PlanQualityError(
                    f"{item.type.lower()} node {item.id} needs value or source_node_id"
                )
            source_id = item.branch_config.source_node_id
            if source_id and source_id not in known_ids:
                raise PlanQualityError(
                    f"{item.type.lower()} node {item.id} references unknown source {source_id}"
                )
