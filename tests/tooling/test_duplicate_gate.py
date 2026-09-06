"""The duplicate ratchet: copied code is red unless the baseline already names it.

The three sentences the ratchet answers with -- a new copy is red and says where
both stand, a listed copy is quiet, a listed pair that is gone is red -- are
driven here the way CI drives them, by running the gate over a copy of the real
tree. What counts as a copy is driven over scratch trees of a few functions
instead: the sentence under test is the rule, and the real tree would only ever
restate today's baseline.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.tooling.architecture_test_support import (
    DUPLICATE_BASELINE,
    append_to,
    copied_project,
    load_architecture_script,
    recalibrate_copied_source_module_count,
    run_gate,
)

SOURCE_PACKAGE = Path("src") / "atelier2"
# A leaf layer: a module written here reaches nothing, so a copy planted for the
# ratchet cannot break another contract on its way to the duplicate check.
COPY_CANVAS_PACKAGE = SOURCE_PACKAGE / "contracts"
COPY_CANVAS_MODULES = ("copied_first", "copied_second")
COPIED_PAIR = (
    "atelier2.contracts.copied_first.summed",
    "atelier2.contracts.copied_second.summed",
)


def a_summing_function(
    name: str = "summed", counter: str = "total", refusal: str = "negative"
) -> str:
    """A function long enough to be recognised again, spelled as asked."""
    return f"""
def {name}(values: list[int]) -> int:
    {counter} = 0
    for value in values:
        if value < 0:
            raise ValueError("{refusal}")
        {counter} = {counter} + value * 2
    return {counter}
"""


A_SHORT_FUNCTION = """
def doubled(value: int) -> int:
    return value * 2
"""
A_DIFFERENT_FUNCTION = """
def averaged(values: list[float]) -> float:
    if not values:
        raise ValueError("no values")
    total = 0.0
    for value in values:
        total = total + value
    return total / len(values)
"""
AN_OVERLOADED_FUNCTION = """
from typing import overload


@overload
def formatted(value: int, width: int, fill: str, prefix: str, suffix: str) -> str: ...
@overload
def formatted(value: str, width: int, fill: str, prefix: str, suffix: str) -> str: ...
def formatted(value, width, fill, prefix, suffix):
    return f"{prefix}{value:{fill}>{width}}{suffix}"
"""

# The overlap of the pair below is arithmetic on tokens: the header is fourteen,
# every shared line three, the return two, and the extra line three more, so the
# two definitions overlap by (14 + 3 x lines - 4) / (14 + 3 x lines + 3). Forty-one
# shared lines make that exactly 0.95 and forty leave it just under, which is why
# the case at the threshold and the case below it differ by a single line.
SHARED_LINES_AT_THE_THRESHOLD = 41
SHARED_LINES_BELOW_THE_THRESHOLD = 40


def a_function_of(shared_lines: int, extra_line: bool = False) -> str:
    body = [f"    value{index} = step" for index in range(shared_lines)]
    if extra_line:
        body.append("    extra = step")
    return "\n".join(
        ["def counted(start: int, step: int) -> int:", *body, "    return start"]
    )


def a_nonlocal_counting_function(
    function_name: str, local_name: str, helper_name: str
) -> str:
    """An outer function whose own local a nested helper reaches with `nonlocal`."""
    return f"""
def {function_name}(values: list[int]) -> int:
    {local_name} = 0
    def {helper_name}(step: int) -> None:
        nonlocal {local_name}
        {local_name} = {local_name} + step
    for value in values:
        if value < 0:
            raise ValueError("negative")
        {helper_name}(value * 2)
    return {local_name}
"""


def a_global_touching_function(function_name: str, global_name: str) -> str:
    """A module-level global and a function that reaches it with `global`."""
    return f"""
{global_name} = 0


def {function_name}(values: list[int]) -> int:
    global {global_name}
    for value in values:
        if value < 0:
            raise ValueError("negative")
        {global_name} = {global_name} + value * 2
    return {global_name}
"""


def scratch_project(
    tmp_path: Path, modules: dict[str, str], baseline: str = ""
) -> Path:
    project = tmp_path / "project"
    (project / SOURCE_PACKAGE).mkdir(parents=True)
    for module, source in modules.items():
        (project / SOURCE_PACKAGE / f"{module}.py").write_text(source, encoding="utf-8")
    (project / DUPLICATE_BASELINE.parent).mkdir(parents=True)
    (project / DUPLICATE_BASELINE).write_text(baseline, encoding="utf-8")
    return project


def a_baseline_of(*pairs: tuple[str, str]) -> str:
    return "\n".join(
        f'[[pair]]\nleft = "{left}"\nright = "{right}"\n' for left, right in pairs
    )


def problems_of(project: Path) -> tuple[str, ...]:
    return load_architecture_script().duplicate_problems(project)


def project_carrying_a_copy(tmp_path: Path) -> Path:
    """The real tree with one function written into two of its leaf modules."""
    project = copied_project(tmp_path)
    for module in COPY_CANVAS_MODULES:
        (project / COPY_CANVAS_PACKAGE / f"{module}.py").write_text(
            a_summing_function(), encoding="utf-8"
        )
    recalibrate_copied_source_module_count(project)
    return project


def test_the_gate_refuses_a_copy_the_baseline_does_not_name_and_says_where_both_stand(
    tmp_path: Path,
) -> None:
    result = run_gate(project_carrying_a_copy(tmp_path))

    assert result.returncode == 1, result.stdout + result.stderr
    assert "duplicate-problems" in result.stderr
    for module in COPY_CANVAS_MODULES:
        assert str(COPY_CANVAS_PACKAGE / f"{module}.py") in result.stderr, result.stderr


def test_the_gate_passes_when_the_baseline_names_the_copy(tmp_path: Path) -> None:
    project = project_carrying_a_copy(tmp_path)
    append_to(project, str(DUPLICATE_BASELINE), a_baseline_of(COPIED_PAIR))

    result = run_gate(project)

    assert result.returncode == 0, result.stdout + result.stderr


def test_the_gate_refuses_a_baseline_entry_whose_copy_is_gone(tmp_path: Path) -> None:
    project = copied_project(tmp_path)
    append_to(project, str(DUPLICATE_BASELINE), a_baseline_of(COPIED_PAIR))

    result = run_gate(project)

    assert result.returncode == 1, result.stdout + result.stderr
    assert "orphan baseline entry" in result.stderr


def test_a_copy_the_baseline_does_not_name_is_refused_with_both_locations(
    tmp_path: Path,
) -> None:
    project = scratch_project(
        tmp_path, {"first": a_summing_function(), "second": a_summing_function()}
    )

    problems = problems_of(project)

    assert len(problems) == 1
    assert "atelier2.first.summed" in problems[0]
    assert "atelier2.second.summed" in problems[0]
    assert str(SOURCE_PACKAGE / "first.py") in problems[0]
    assert str(SOURCE_PACKAGE / "second.py") in problems[0]


@pytest.mark.parametrize(
    "baseline_pair",
    [
        pytest.param(
            ("atelier2.first.summed", "atelier2.second.summed"), id="as found"
        ),
        pytest.param(
            ("atelier2.second.summed", "atelier2.first.summed"),
            id="the other way round",
        ),
    ],
)
def test_a_copy_the_baseline_names_keeps_the_gate_quiet(
    tmp_path: Path, baseline_pair: tuple[str, str]
) -> None:
    project = scratch_project(
        tmp_path,
        {"first": a_summing_function(), "second": a_summing_function()},
        baseline=a_baseline_of(baseline_pair),
    )

    assert problems_of(project) == ()


def test_a_baseline_entry_whose_copy_is_gone_is_refused(tmp_path: Path) -> None:
    project = scratch_project(
        tmp_path,
        {"first": a_summing_function(), "second": A_DIFFERENT_FUNCTION},
        baseline=a_baseline_of(("atelier2.first.summed", "atelier2.second.averaged")),
    )

    problems = problems_of(project)

    assert len(problems) == 1
    assert "orphan baseline entry, remove it" in problems[0]
    assert "atelier2.second.averaged" in problems[0]


def test_renaming_one_side_of_a_baseline_pair_is_an_orphan_and_a_copy_again(
    tmp_path: Path,
) -> None:
    project = scratch_project(
        tmp_path,
        {"first": a_summing_function(), "second": a_summing_function(name="added")},
        baseline=a_baseline_of(("atelier2.first.summed", "atelier2.second.summed")),
    )

    problems = problems_of(project)

    assert len(problems) == 2
    assert any(
        "atelier2.second.added" in problem and "is a copy of" in problem
        for problem in problems
    )
    assert any(
        "atelier2.second.summed" in problem and "orphan baseline entry" in problem
        for problem in problems
    )


@pytest.mark.parametrize(
    ("shared_lines", "expected_problems"),
    [
        pytest.param(SHARED_LINES_AT_THE_THRESHOLD, 1, id="exactly at the threshold"),
        pytest.param(SHARED_LINES_BELOW_THE_THRESHOLD, 0, id="just below it"),
    ],
)
def test_the_threshold_decides_a_pair_differing_by_one_line(
    tmp_path: Path, shared_lines: int, expected_problems: int
) -> None:
    project = scratch_project(
        tmp_path,
        {
            "first": a_function_of(shared_lines),
            "second": a_function_of(shared_lines, extra_line=True),
        },
    )

    assert len(problems_of(project)) == expected_problems


@pytest.mark.parametrize(
    "second_module",
    [
        pytest.param(
            a_summing_function(name="added", counter="carried", refusal="below zero"),
            id="renamed, with other locals and other messages",
        ),
        pytest.param(a_summing_function(), id="copied verbatim"),
    ],
)
def test_a_copy_stays_a_copy_however_its_own_names_are_spelled(
    tmp_path: Path, second_module: str
) -> None:
    project = scratch_project(
        tmp_path, {"first": a_summing_function(), "second": second_module}
    )

    assert len(problems_of(project)) == 1


def test_a_nested_nonlocal_does_not_block_normalising_the_outer_local(
    tmp_path: Path,
) -> None:
    """A nested `nonlocal` is the nested scope's business, not the outer one's.

    Renaming the outer local a nested helper reaches with `nonlocal` is still
    just renaming -- the pair stays a copy -- while two functions that each
    `global` a *different* name at their own level are reaching different
    state, so that name difference must still keep them apart.
    """
    project = scratch_project(
        tmp_path,
        {
            "first": a_nonlocal_counting_function("gathered", "count", "add_step"),
            "second": a_nonlocal_counting_function("totalled", "total", "bump"),
            "global_first": a_global_touching_function("accumulate_one", "counter_one"),
            "global_second": a_global_touching_function(
                "accumulate_two", "counter_two"
            ),
        },
    )

    problems = problems_of(project)

    assert len(problems) == 1
    assert "atelier2.first.gathered" in problems[0]
    assert "atelier2.second.totalled" in problems[0]


@pytest.mark.parametrize(
    ("modules", "why"),
    [
        pytest.param(
            {"first": a_summing_function(), "second": A_DIFFERENT_FUNCTION},
            "two functions that do different work",
            id="different work",
        ),
        pytest.param(
            {"first": A_SHORT_FUNCTION, "second": A_SHORT_FUNCTION},
            "two functions too short to recognise again",
            id="below the minimum length",
        ),
        pytest.param(
            {"first": AN_OVERLOADED_FUNCTION},
            "several @overload signatures of one function, not one function twice",
            id="overload declarations",
        ),
    ],
)
def test_what_is_not_a_copy_keeps_the_gate_quiet(
    tmp_path: Path, modules: dict[str, str], why: str
) -> None:
    project = scratch_project(tmp_path, modules)

    assert problems_of(project) == (), why


@pytest.mark.parametrize(
    "baseline",
    [
        pytest.param('[[pair]]\nleft = "atelier2.first.summed"\n', id="a name missing"),
        pytest.param('pair = ["not a table"]\n', id="a list of strings"),
        pytest.param('pair = "atelier2.first.summed"\n', id="a bare string"),
        pytest.param("[[pair]]\nleft = 1\nright = 2\n", id="names that are numbers"),
        pytest.param("[[pair\n", id="not readable as TOML"),
    ],
)
def test_a_baseline_that_is_not_a_list_of_pairs_is_refused_by_name(
    tmp_path: Path, baseline: str
) -> None:
    project = copied_project(tmp_path)
    (project / DUPLICATE_BASELINE).write_text(baseline, encoding="utf-8")

    result = run_gate(project)

    assert result.returncode == 1, result.stdout + result.stderr
    assert str(DUPLICATE_BASELINE) in result.stderr, result.stderr


def test_two_definitions_sharing_a_qualified_name_are_refused(tmp_path: Path) -> None:
    script = load_architecture_script()
    project = scratch_project(
        tmp_path, {"first": a_summing_function() + a_summing_function()}
    )

    with pytest.raises(script.ArchitecturePreflightError):
        script.duplicate_problems(project)
