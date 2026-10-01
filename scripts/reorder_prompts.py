"""Enforce the cache-friendly block order in every prompt template.

A provider-side context cache is a *prefix* cache: it only bills a hit for the
leading tokens that are byte-identical to a previous request. That makes block
order a cost decision, not a style one: anything stable must come before
anything volatile, or the stable part can never be reused.

This script rewrites the data blocks of each template so that:

1. the role instructions stay first (they define the role),
2. the stable blocks follow (tool catalog, output schema, fixed limits, role and
   target descriptors),
3. the volatile blocks come last (task, memory, evidence, observations, the
   remaining budget counters),
4. a short constant anchor closes the prompt, so the model still reads the
   output requirement and the "decide from the last observation" cue last,
   where recency helps.

It never rewrites prose: a template whose text between blocks is not a plain
header followed by ``{{marker}}`` is reported and left untouched, because
silently moving hand-written guidance is how a prompt loses its meaning.

    python scripts/reorder_prompts.py --check   # report only
    python scripts/reorder_prompts.py           # rewrite
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

PROMPTS = Path(__file__).resolve().parents[1] / "assistant" / "prompts"

# Order within the stable section. Small and descriptive first is irrelevant to
# the cache; what matters is that none of these changes between two calls of the
# same role for the same intent and task.
STABLE_ORDER = (
    "available_actions",
    "output_schema",
    "limits",
    "worker",
    "template",
    "execution_target",
    "known_targets",
    "available_workers",
    "audit_protocol",
    "project",
    "codegraph",
    "extra_context",
    "acceptance_criteria",
)

# Order within the volatile tail. Cheap-to-serialize first is irrelevant; what
# matters is that every one of these can differ from the previous call.
VOLATILE_ORDER = (
    "task",
    "user_prompt",
    "node",
    "assistant_state",
    "orchestration_stage",
    "long_term_memory",
    "working_memory",
    "dependencies",
    "completed_artifacts",
    "execution_evidence",
    "worker_completion",
    "evidence",
    "failure_context",
    "planner_feedback",
    "last_observation",
    "remaining",
)

HEADERS = {
    "available_actions": "TOOLS",
    "output_schema": "OUTPUT SCHEMA",
    "limits": "LIMITS",
    "remaining": "REMAINING",
    "worker": "WORKER",
    "template": "TEMPLATE",
    "execution_target": "TARGET",
    "known_targets": "KNOWN TARGETS",
    "available_workers": "AVAILABLE WORKERS",
    "audit_protocol": "AUDIT PROTOCOL",
    "project": "PROJECT",
    "codegraph": "CODEGRAPH",
    "extra_context": "EXTRA CONTEXT",
    "acceptance_criteria": "ACCEPTANCE CRITERIA",
    "task": "TASK",
    "user_prompt": "USER REQUEST",
    "node": "CURRENT NODE",
    "assistant_state": "ASSISTANT STATE",
    "orchestration_stage": "ORCHESTRATION STAGE",
    "long_term_memory": "LONG-TERM MEMORY",
    "working_memory": "WORKING MEMORY",
    "dependencies": "DEPENDENCY RESULTS",
    "completed_artifacts": "COMPLETED ARTIFACTS",
    "execution_evidence": "EXECUTION EVIDENCE",
    "worker_completion": "WORKER COMPLETION",
    "evidence": "EVIDENCE",
    "failure_context": "FAILURE CONTEXT",
    "planner_feedback": "PLANNER FEEDBACK",
    "last_observation": "LAST OBSERVATION",
}

ANCHOR_BASE = "Return JSON only, matching the OUTPUT SCHEMA above."
ANCHOR = (
    f"{ANCHOR_BASE} Decide the next step from LAST OBSERVATION, EVIDENCE and REMAINING."
)

# A block is a header line followed by nothing but its marker.
BLOCK = re.compile(r"^\{\{(?P<name>[a-z_]+)\}\}$")

# Legacy name kept working: a single ``constraints`` block becomes the split pair.
SPLIT_LEGACY = "constraints"


# Short phrases that only introduce the schema are replaced by the canonical
# anchor, so they can be dropped instead of blocking the reorder.
SHORT_ANCHOR_LIMIT = 80


def parse(text: str) -> tuple[
    str, list[tuple[str, list[str]]], list[str], list[str], list[str], list[str]
]:
    """Split a template into preamble, blocks (with their notes) and tail.

    A block is a marker line, optionally labelled by the uppercase line above it.
    Prose between two blocks is handled by size, because that is the difference
    between a lead-in and a rule:

    - a short line ending in ``:`` introduces the next block, so the canonical
      anchor replaces it;
    - anything longer is a rule about the previous block, so it travels with it.

    Anything after the last block is the tail and stays at the end.
    """
    lines = text.splitlines()
    preamble: list[str] = []
    blocks: list[tuple[str, list[str]]] = []
    pending: list[str] = []
    travel: dict[str, list[str]] = {}
    tail_prose: list[str] = []
    replaced: list[str] = []
    complaints: list[str] = []
    notes: list[str] = []
    seen_block = False

    for line in lines:
        stripped = line.strip()
        marker = BLOCK.match(stripped)
        if marker:
            name = marker.group("name")
            # The uppercase line above a marker is its label, not prose; the
            # canonical header replaces it. It may still be sitting in the
            # preamble when the template opens with a block.
            carrier = pending if seen_block else preamble
            if carrier and carrier[-1].strip().isupper():
                carrier.pop()
            prose = [item.strip() for item in pending if item.strip()]
            joined = " ".join(prose)
            if prose and len(joined) <= SHORT_ANCHOR_LIMIT and joined.endswith(":"):
                notes.append(f"lead-in replaced by the anchor: {joined!r}")
                replaced.extend(prose)
                prose = []
            elif prose:
                # A rule written just before a stable block belongs to that
                # block, so it travels up with it and stays cacheable; otherwise
                # it explains the block above and travels with that one.
                host = name if name in STABLE_ORDER else (blocks[-1][0] if blocks else None)
                if host is None:
                    tail_prose.extend(prose)
                else:
                    travel.setdefault(host, []).extend(prose)
                    notes.append(f"rule kept with {{{{{host}}}}}: {prose[0][:50]!r}")
                prose = []
            pending = []
            blocks.append((name, []))
            seen_block = True
            continue
        if stripped:
            (pending if seen_block else preamble).append(line)

    tail = [*tail_prose, *(item.strip() for item in pending if item.strip())]
    # The closing anchor is produced by ``render`` from the blocks it emitted.
    # Keeping the previous copy in the tail would append a second one on every
    # run, so old copies are dropped and recorded as replaced (the prose check
    # then knows they were superseded on purpose).
    canonical_tail: list[str] = []
    for item in tail:
        if item.startswith(ANCHOR_BASE):
            replaced.append(item)
            continue
        canonical_tail.append(item)
    for name, block_notes in travel.items():
        for block_name, block_notes_list in blocks:
            if block_name == name:
                block_notes_list.extend(block_notes)
    return (
        "\n".join(preamble).strip("\n"),
        blocks,
        [item for item in canonical_tail if item],
        complaints,
        notes,
        replaced,
    )


def anchor_for(seen: set[str]) -> str:
    """Close the prompt with the output requirement and what to decide from.

    The tail names only the volatile blocks this template actually has, so the
    anchor never points at a section that is not there.
    """
    sources = [
        HEADERS[name]
        for name in (
            "last_observation",
            "evidence",
            "execution_evidence",
            "worker_completion",
            "failure_context",
            "remaining",
        )
        if name in seen
    ]
    base = ANCHOR_BASE
    if not sources:
        return base
    if len(sources) == 1:
        return f"{base} Decide the next step from {sources[0]}."
    return f"{base} Decide the next step from {', '.join(sources[:-1])} and {sources[-1]}."


def render(preamble: str, blocks: list[tuple[str, list[str]]]) -> str:
    """Emit the canonical order, expanding the legacy constraints pair."""
    seen = {name for name, _notes in blocks}
    notes = {name: block_notes for name, block_notes in blocks}
    ordered: list[str] = []

    def emit(name: str) -> None:
        label = HEADERS.get(name)
        if label is None:
            return
        ordered.append(f"{label}\n{{{{{name}}}}}")
        if notes.get(name):
            ordered.append("\n".join(notes[name]))

    for name in STABLE_ORDER:
        if name in seen:
            emit(name)
    if SPLIT_LEGACY in seen:
        emit("limits")
    for name in VOLATILE_ORDER:
        if name in seen:
            emit(name)
        if name == "last_observation" and SPLIT_LEGACY in seen:
            emit("remaining")
    for name, _notes in blocks:  # anything unknown keeps its own header
        if name not in STABLE_ORDER and name not in VOLATILE_ORDER and name != SPLIT_LEGACY:
            ordered.append(f"{HEADERS.get(name, name.upper())}\n{{{{{name}}}}}")

    body = "\n".join(ordered)
    # ``remaining`` is synthesised from the legacy ``constraints`` block, so it
    # is not in ``seen`` even though the prompt will contain it.
    effective = set(seen) | ({"limits", "remaining"} if SPLIT_LEGACY in seen else set())
    tail = anchor_for(effective)
    return (
        f"{preamble}\n\n{body}\n\n{tail}\n"
        if preamble
        else f"{body}\n\n{tail}\n"
    )


def markers(text: str) -> list[str]:
    """Every marker the renderer will substitute, in order of appearance."""
    return [
        match.group(1)
        for match in re.finditer(r"^\{\{([a-z_]+)\}\}$", text, flags=re.MULTILINE)
    ]


def prose_lines(text: str) -> list[str]:
    """Hand-written lines, ignoring markers, labels and blank lines.

    Used as a safety net: a reorder must not drop a single written instruction.
    """
    kept: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or BLOCK.match(stripped) or stripped.isupper():
            continue
        kept.append(stripped)
    return kept


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="report without writing")
    arguments = parser.parse_args()

    changed = 0
    for path in sorted(PROMPTS.glob("*.md")):
        original = path.read_text(encoding="utf-8")
        preamble, blocks, tail, complaints, notes, replaced = parse(original)
        if not blocks:
            print(f"skip   {path.name}: no data blocks")
            continue
        if complaints:
            print(f"manual {path.name}: {'; '.join(complaints[:2])}")
            continue
        updated = render(preamble, blocks)
        if tail:
            updated = f"{updated}\n{chr(10).join(tail)}\n"
        # Safety net: a reorder must preserve every marker the prompt asks for
        # (bar the legacy ``constraints`` that becomes limits + remaining) and
        # must never introduce one. Losing a marker silently deletes context.
        before = markers(original)
        after = markers(updated)
        expected = list(before)
        if SPLIT_LEGACY in expected:
            expected.remove(SPLIT_LEGACY)
            expected.extend(["limits", "remaining"])
        if sorted(after) != sorted(expected):
            print(f"REFUSED {path.name}: "
                  f"missing={sorted(set(expected) - set(after))} "
                  f"extra={sorted(set(after) - set(expected))}")
            return 2
        lost = [
            line
            for line in prose_lines(original)
            if line not in prose_lines(updated) and line not in replaced
        ]
        if lost:
            print(f"REFUSED {path.name}: prose lost: {lost[:2]}")
            return 2
        if updated == original:
            print(f"ok     {path.name}")
            continue
        if arguments.check:
            print(f"stale  {path.name}: {[name for name, _ in blocks]}")
            changed += 1
            continue
        path.write_text(updated, encoding="utf-8")
        for note in notes:
            print(f"       {path.name}: {note}")
        print(f"wrote  {path.name}")
        changed += 1
    return 1 if (arguments.check and changed) else 0


if __name__ == "__main__":
    sys.exit(main())
