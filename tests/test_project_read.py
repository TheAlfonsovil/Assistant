"""project.read bounds: a whole huge file is refused, a slice of it is not.

The size gate exists so a minified bundle or a generated file cannot land in the
context in one call. Applied to line ranges it did the opposite of its job: a
source file over 200 KB could not be looked at *at all*, not even forty lines of
it, which is precisely the retrieval ladder the codegraph and `types.symbols`
exist to feed.
"""

from __future__ import annotations

import pytest

from assistant.devices.computer.actions.project_read import read_project

BIG_LINES = 12_000


@pytest.fixture()
def project(tmp_path):
    source = tmp_path / "big.py"
    source.write_text(
        "".join(f"value_{index} = {index}\n" for index in range(BIG_LINES)),
        encoding="utf-8",
    )
    assert source.stat().st_size > 200_000
    return tmp_path


async def test_a_whole_file_over_the_gate_is_refused(project):
    result = await read_project({"root": str(project), "files": ["big.py"]})

    assert result.success is False
    assert "too large" in result.metadata["file_errors"][0]["error"]


async def test_a_bounded_range_of_the_same_file_is_read(project):
    result = await read_project(
        {
            "root": str(project),
            "files": [{"path": "big.py", "start_line": 10, "end_line": 12}],
        }
    )

    assert result.success is True, result.error
    assert result.output["files"]["big.py"] == "value_9 = 9\nvalue_10 = 10\nvalue_11 = 11\n"
    assert result.output["file_metadata"]["big.py"]["returned_lines"] == 3


async def test_a_range_without_an_end_line_still_respects_the_gate(project):
    """An open range runs to the end of the file, so it is not a bounded read."""
    result = await read_project(
        {"root": str(project), "files": [{"path": "big.py", "start_line": 10}]}
    )

    assert result.success is False
    assert "too large" in result.metadata["file_errors"][0]["error"]


async def test_the_character_budget_still_bounds_a_range(project):
    result = await read_project(
        {
            "root": str(project),
            "files": [{"path": "big.py", "start_line": 1, "end_line": 8_000}],
            "max_chars": 100,
        }
    )

    assert result.success is True
    assert len(result.output["files"]["big.py"]) <= 100
    assert result.output["file_metadata"]["big.py"]["truncated"] is True
