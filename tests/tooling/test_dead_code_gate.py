"""The dead-code gate refuses what production no longer reaches.

The gate is driven the way CI drives it -- as its own process over a scratch
project -- because the sentence under test is what the whole tool answers, not
how it partitions a list.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).parents[2]
GATE = Path("scripts") / "check_dead_code.py"
BASELINES = Path("scripts") / "baselines"

A_MODULE = "lonely.py"
ANOTHER_MODULE = "elsewhere.py"
A_LONELY_FUNCTION = "a_symbol_nothing_calls"
A_SOURCE_MODULE = f"""
def {A_LONELY_FUNCTION}() -> int:
    return 1
"""
A_TEST_THAT_USES_IT = f"""
from atelier2.lonely import {A_LONELY_FUNCTION}


def test_it() -> None:
    assert {A_LONELY_FUNCTION}() == 1
"""
A_WIRE_FIELD = "served_only_on_the_wire"
A_WIRE_MODEL = f"""
from dataclasses import dataclass


@dataclass(frozen=True)
class Answer:
    {A_WIRE_FIELD}: str


def answer() -> Answer:
    return Answer({A_WIRE_FIELD}="yes")
"""
A_DEAD_PARAMETER = "unused_knob"
A_DEAD_FUNCTION = "dead_but_named_like_a_keyword"
A_MODULE_WHOSE_DEAD_NAMES_ARE_ALSO_KEYWORDS = f"""
def with_a_dead_parameter(value: int, {A_DEAD_PARAMETER}: int) -> int:
    return value


def {A_DEAD_FUNCTION}() -> int:
    return 2


def keyword_taker(**filled: int) -> int:
    return sum(filled.values())


def answer() -> int:
    return with_a_dead_parameter(1, 2) + keyword_taker(
        {A_DEAD_PARAMETER}=1, {A_DEAD_FUNCTION}=1
    )
"""


def a_group(name: str, **stated: str) -> str:
    fields = ", ".join(f'"{key}": "{value}"' for key, value in stated.items())
    return f'{{"names": ("{name}",), {fields}}},'


def allowed(name: str = A_LONELY_FUNCTION, module: str = A_MODULE) -> str:
    return a_group(f"{module}:{name}", why="a site builds the call as text")


def pending_until(
    day: str, name: str = A_LONELY_FUNCTION, module: str = A_MODULE
) -> str:
    return a_group(f"{module}:{name}", why="#1 decides it", expires_on=day)


def frozen(
    name: str = A_LONELY_FUNCTION,
    why: str = "no caller is built yet",
    module: str = A_MODULE,
) -> str:
    """A frozen entry names the module its symbol was built in."""
    return a_group(f"{module}:{name}", why=why, item="#1")


@dataclass(frozen=True)
class Lists:
    """What the three files say about the scratch project's lonely symbols."""

    allowlist: tuple[str, ...] = ()
    pending: tuple[str, ...] = ()
    frozen: tuple[str, ...] = ()

    def files(self) -> dict[Path, str]:
        return {
            BASELINES / "vulture_allowlist.py": _binding(
                "REACHED_BY_A_SITE_VULTURE_CANNOT_SEE", self.allowlist
            ),
            BASELINES / "vulture_pending.py": _binding(
                "WAITING_FOR_A_DECISION", self.pending
            ),
            BASELINES / "vulture_frozen.py": _binding(
                "WAITING_FOR_A_CALLER", self.frozen
            ),
        }


def _binding(name: str, groups: tuple[str, ...]) -> str:
    return f"{name} = (\n" + "\n".join(groups) + "\n)\n"


def scratch_project(
    tmp_path: Path,
    lists: Lists,
    source: str = A_SOURCE_MODULE,
    elsewhere: str | None = None,
) -> Path:
    project = tmp_path / "project"
    (project / BASELINES).mkdir(parents=True)
    package = project / "src" / "atelier2"
    package.mkdir(parents=True)
    shutil.copy2(PROJECT_ROOT / GATE, project / GATE)
    (package / A_MODULE).write_text(source, encoding="utf-8")
    if elsewhere is not None:
        (package / ANOTHER_MODULE).write_text(elsewhere, encoding="utf-8")
    for relative, text in lists.files().items():
        (project / relative).write_text(text, encoding="utf-8")
    return project


def run_gate(project: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(GATE)],
        cwd=project,
        check=False,
        capture_output=True,
        text=True,
    )


def test_an_unreached_production_symbol_turns_the_gate_red(tmp_path: Path) -> None:
    result = run_gate(scratch_project(tmp_path, Lists()))

    assert result.returncode == 1
    assert A_LONELY_FUNCTION in result.stderr


def test_a_symbol_only_a_test_reaches_stays_unreached(tmp_path: Path) -> None:
    project = scratch_project(tmp_path, Lists())
    (project / "tests").mkdir()
    (project / "tests" / "test_lonely.py").write_text(
        A_TEST_THAT_USES_IT, encoding="utf-8"
    )

    result = run_gate(project)

    assert result.returncode == 1
    assert A_LONELY_FUNCTION in result.stderr


def test_a_field_only_ever_passed_by_keyword_counts_as_reached(tmp_path: Path) -> None:
    lists = Lists(allowlist=(allowed("answer"),))

    result = run_gate(scratch_project(tmp_path, lists, source=A_WIRE_MODEL))

    assert result.returncode == 0, result.stderr
    assert A_WIRE_FIELD not in result.stdout + result.stderr


@pytest.mark.parametrize(
    "dead_name",
    [
        pytest.param(A_DEAD_PARAMETER, id="a parameter"),
        pytest.param(A_DEAD_FUNCTION, id="a function"),
    ],
)
def test_a_dead_name_another_call_uses_as_a_keyword_stays_dead(
    tmp_path: Path, dead_name: str
) -> None:
    lists = Lists(allowlist=(allowed("answer"),))

    result = run_gate(
        scratch_project(
            tmp_path, lists, source=A_MODULE_WHOSE_DEAD_NAMES_ARE_ALSO_KEYWORDS
        )
    )

    assert result.returncode == 1
    assert dead_name in result.stderr


@pytest.mark.parametrize(
    "lists",
    [
        pytest.param(Lists(allowlist=(allowed(),)), id="a named dynamic site"),
        pytest.param(
            Lists(pending=(pending_until("2999-01-01"),)),
            id="a decision still in date",
        ),
        pytest.param(Lists(frozen=(frozen(),)), id="frozen ahead of its caller"),
    ],
)
def test_a_justified_name_keeps_the_gate_green(tmp_path: Path, lists: Lists) -> None:
    result = run_gate(scratch_project(tmp_path, lists))

    assert result.returncode == 0, result.stderr


def test_a_frozen_name_stays_visible_without_failing(tmp_path: Path) -> None:
    result = run_gate(scratch_project(tmp_path, Lists(frozen=(frozen(),))))

    assert result.returncode == 0, result.stderr
    assert A_LONELY_FUNCTION in result.stdout


@pytest.mark.parametrize(
    "lists",
    [
        pytest.param(Lists(allowlist=(allowed(),)), id="the allowlist"),
        pytest.param(
            Lists(pending=(pending_until("2999-01-01"),)), id="the pending list"
        ),
        pytest.param(Lists(frozen=(frozen(),)), id="the frozen list"),
    ],
)
def test_an_excused_name_excuses_only_the_module_it_names(
    tmp_path: Path, lists: Lists
) -> None:
    """The same bare name unreached in another module stays its own finding.

    This is the exact confusion a bare-name match once allowed: a caller
    reaching one module's symbol must never silently satisfy the entry for a
    same-named symbol somewhere else.
    """

    project = scratch_project(tmp_path, lists, elsewhere=A_SOURCE_MODULE)

    result = run_gate(project)

    assert result.returncode == 1
    assert ANOTHER_MODULE in result.stderr


@pytest.mark.parametrize(
    "lists",
    [
        pytest.param(
            Lists(allowlist=(a_group(A_LONELY_FUNCTION, why="reached by text"),)),
            id="the allowlist",
        ),
        pytest.param(
            Lists(
                pending=(
                    a_group(
                        A_LONELY_FUNCTION, why="#1 decides it", expires_on="2999-01-01"
                    ),
                )
            ),
            id="the pending list",
        ),
        pytest.param(
            Lists(
                frozen=(
                    a_group(A_LONELY_FUNCTION, why="no caller is built yet", item="#1"),
                )
            ),
            id="the frozen list",
        ),
    ],
)
def test_an_entry_without_its_module_is_refused(tmp_path: Path, lists: Lists) -> None:
    """Every list's entry must name where its symbol lives, not just its name."""

    result = run_gate(scratch_project(tmp_path, lists))

    assert result.returncode == 1
    assert A_LONELY_FUNCTION in result.stderr


def test_a_decision_past_its_expiry_turns_the_gate_red(tmp_path: Path) -> None:
    lists = Lists(pending=(pending_until("2020-01-01"),))

    result = run_gate(scratch_project(tmp_path, lists))

    assert result.returncode == 1
    assert "2020-01-01" in result.stderr


def test_an_orphaned_entry_is_reported_with_its_module_and_name(
    tmp_path: Path,
) -> None:
    lists = Lists(allowlist=(allowed(), allowed("a_symbol_that_left")))

    result = run_gate(scratch_project(tmp_path, lists))

    assert result.returncode == 1
    assert f"{A_MODULE}:a_symbol_that_left" in result.stderr


def test_an_entry_without_a_stated_reason_is_refused(tmp_path: Path) -> None:
    lists = Lists(frozen=(frozen(why="   "),))

    result = run_gate(scratch_project(tmp_path, lists))

    assert result.returncode == 1
    assert "needs a why" in result.stderr
