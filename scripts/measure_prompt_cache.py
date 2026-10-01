"""Measure how much of each prompt is a *stable prefix*.

DeepSeek's context cache is a prefix cache: a hit is only counted for the
leading tokens that are byte-identical to a previous request. So the useful
question is not "how big is the prompt" but "how many leading characters are the
same between two calls of the same role".

This renders real prompts through the same code path production uses (the
context builder plus the provider's ``prepare_request``) and reports:

- the identical prefix of two consecutive turns of the same role,
- the size of the blocks that could live in that prefix (instructions, tool
  catalog, output schema),
- and the first characters of the prompt, so the variable part is visible.

Run it after changing a prompt to see whether cacheability improved:

    python scripts/measure_prompt_cache.py
"""

from __future__ import annotations

import asyncio
import json
import tempfile
from pathlib import Path

from assistant.config import Settings
from assistant.domain.models import TaskRequest
from assistant.llm import (
    DeepSeekLLMProvider,
    NodeDecision,
    OrchestratorDecision,
    PlanProposal,
    _compact_schema,
)
from assistant.startup.bootstrap import create_context


def common_prefix(left: str, right: str) -> int:
    """Length of the longest identical leading run of two strings."""
    limit = min(len(left), len(right))
    index = 0
    while index < limit and left[index] == right[index]:
        index += 1
    return index


def tokens(text: str) -> int:
    """Rough token count. Good enough to compare two prompts of the same kind."""
    return round(len(text) / 4)


def size(value: object) -> int:
    return len(json.dumps(value, ensure_ascii=False, default=str, separators=(",", ":")))


def report(
    label: str,
    first: str,
    second: str,
    stable_blocks: dict[str, object],
) -> dict[str, object]:
    prefix = common_prefix(first, second)
    stable_chars = sum(size(block) for block in stable_blocks.values())
    return {
        "role": label,
        "prompt_chars": [len(first), len(second)],
        "identical_prefix_chars": prefix,
        "identical_prefix_pct": round(100 * prefix / max(1, len(first)), 1),
        "cacheable_if_reordered_chars": stable_chars,
        "cacheable_if_reordered_pct": round(100 * stable_chars / max(1, len(first)), 1),
        "blocks": {name: size(block) for name, block in stable_blocks.items()},
        "head": first[:220].replace("\n", "\\n"),
    }


async def main() -> None:
    with tempfile.TemporaryDirectory() as folder:
        settings = Settings(
            database_url=f"sqlite+aiosqlite:///{Path(folder) / 'probe.db'}",
            deepseek_api_key="",
            workspace_root=folder,
            projects_root=folder,
            idle_enabled=False,
        )
        context = await create_context(use_mock=True, settings=settings)
        try:
            service = context.service
            builder = service.context_builder
            # Only ``prepare_request`` is used here: it renders the prompt and
            # never touches the network, so a placeholder key is enough.
            llm = DeepSeekLLMProvider(
                settings.deepseek_url, settings.deepseek_model, "probe"
            )
            task = await service.create_task(
                TaskRequest(goal="revisa el proyecto y ejecuta los tests")
            )

            # Two consecutive turns of the same task differ only in what the
            # last tool call produced, which is the realistic repeated call.
            turn_one = await builder.for_agent_decision(
                task,
                {"tool": "filesystem", "method": "list", "output": {"entries": 12}},
            )
            turn_two = await builder.for_agent_decision(
                task,
                {
                    "tool": "shell",
                    "method": "run",
                    "output": {"exit_code": 0, "stdout": "238 passed"},
                },
            )
            agent_first = llm.prepare_request("AGENT", turn_one, NodeDecision)[
                "rendered_instructions"
            ]
            agent_second = llm.prepare_request("AGENT", turn_two, NodeDecision)[
                "rendered_instructions"
            ]

            orchestrator_one = await builder.for_orchestrator(task)
            other = await service.create_task(TaskRequest(goal="audita el proyecto"))
            orchestrator_two = await builder.for_orchestrator(other)
            route_first = llm.prepare_request(
                "ORCHESTRATOR", orchestrator_one, OrchestratorDecision
            )["rendered_instructions"]
            route_second = llm.prepare_request(
                "ORCHESTRATOR", orchestrator_two, OrchestratorDecision
            )["rendered_instructions"]

            plan_first = llm.prepare_request(
                "PLANNER", await builder.for_planner(task), PlanProposal
            )["rendered_instructions"]
            plan_second = llm.prepare_request(
                "PLANNER", await builder.for_planner(other), PlanProposal
            )["rendered_instructions"]

            template = (
                Path(__file__).resolve().parents[1]
                / "assistant"
                / "prompts"
                / "general_worker.md"
            ).read_text(encoding="utf-8")
            header = template.split("WORKER", 1)[0]
            agent_blocks = {
                "role header": header,
                "tool catalog": turn_one.get("available_actions", []),
                "output schema": _compact_schema(NodeDecision.model_json_schema()),
            }
            route_blocks = {
                "role header": header,
                "tool catalog": orchestrator_one.get("available_actions", []),
                "output schema": _compact_schema(OrchestratorDecision.model_json_schema()),
            }
            planner_blocks = {
                "role header": header,
                "tool catalog": (await builder.for_planner(task)).get("available_actions", []),
                "output schema": _compact_schema(PlanProposal.model_json_schema()),
            }

            rows = [
                report("AGENT (two turns of one task)", agent_first, agent_second, agent_blocks),
                report(
                    "ORCHESTRATOR (two different tasks)",
                    route_first,
                    route_second,
                    route_blocks,
                ),
                report("PLANNER (two different tasks)", plan_first, plan_second, planner_blocks),
            ]

            print(
                f"{'role':34} {'chars A/B':>13} {'prefix':>8} {'%':>6} "
                f"{'reordered':>10} {'%':>6}"
            )
            for row in rows:
                chars = "/".join(str(value) for value in row["prompt_chars"])
                print(
                    f"{row['role']:34} {chars:>13} {row['identical_prefix_chars']:>8} "
                    f"{row['identical_prefix_pct']:>6} "
                    f"{row['cacheable_if_reordered_chars']:>10} "
                    f"{row['cacheable_if_reordered_pct']:>6}"
                )
            print()
            print("Tokens (chars/4):")
            for row in rows:
                print(f"  {row['role']}")
                print(
                    f"    prompt ~{tokens('x' * int(row['prompt_chars'][0]))} tok | "
                    f"identical prefix ~{tokens('x' * int(row['identical_prefix_chars']))} tok | "
                    f"reorderable ~{tokens('x' * int(row['cacheable_if_reordered_chars']))} tok"
                )
                print(f"    blocks: {row['blocks']}")
            print()
            print("First 220 characters of the AGENT prompt:")
            print("  " + str(rows[0]["head"]))
        finally:
            await context.close()
            await llm.close()


if __name__ == "__main__":
    asyncio.run(main())
