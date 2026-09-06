"""What a command reads when the operator names a file, and what it refuses."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest

from atelier2.host.operator_files import OperatorFileRefused, read_operator_file

DOCUMENT = b"format_version: 3\n"


@pytest.fixture
def working_directory(tmp_path: Path) -> Path:
    inside = tmp_path / "inside"
    inside.mkdir()
    return inside


@pytest.fixture
def outside(tmp_path: Path) -> Path:
    elsewhere = tmp_path / "outside"
    elsewhere.mkdir()
    (elsewhere / "workflow.yaml").write_bytes(DOCUMENT)
    return elsewhere


def a_relative_name(working_directory: Path, outside: Path) -> Path:
    del outside
    (working_directory / "workflow.yaml").write_bytes(DOCUMENT)
    return Path("workflow.yaml")


def an_absolute_name_beneath(working_directory: Path, outside: Path) -> Path:
    del outside
    document = working_directory / "documents" / "workflow.yaml"
    document.parent.mkdir()
    document.write_bytes(DOCUMENT)
    return document


def a_name_climbing_out(working_directory: Path, outside: Path) -> Path:
    del working_directory
    return Path("..") / outside.name / "workflow.yaml"


def an_absolute_name_outside(working_directory: Path, outside: Path) -> Path:
    del working_directory
    return outside / "workflow.yaml"


def a_link_pointing_out(working_directory: Path, outside: Path) -> Path:
    (working_directory / "workflow.yaml").symlink_to(outside / "workflow.yaml")
    return Path("workflow.yaml")


@pytest.mark.parametrize(
    "name",
    [a_relative_name, an_absolute_name_beneath],
    ids=["a relative name", "an absolute name beneath it"],
)
def test_a_file_beneath_the_working_directory_is_read(
    working_directory: Path, outside: Path, name: Callable[[Path, Path], Path]
) -> None:
    named = name(working_directory, outside)

    assert read_operator_file(named, working_directory) == DOCUMENT


@pytest.mark.parametrize(
    "name",
    [a_name_climbing_out, an_absolute_name_outside, a_link_pointing_out],
    ids=["a name climbing out", "an absolute name outside it", "a link pointing out"],
)
def test_a_file_outside_the_working_directory_is_refused_by_name(
    working_directory: Path, outside: Path, name: Callable[[Path, Path], Path]
) -> None:
    named = name(working_directory, outside)

    with pytest.raises(OperatorFileRefused, match="outside the working directory"):
        read_operator_file(named, working_directory)


def test_a_file_that_cannot_be_read_is_refused_with_the_reason(
    working_directory: Path,
) -> None:
    with pytest.raises(OperatorFileRefused, match="cannot read absent.yaml"):
        read_operator_file(Path("absent.yaml"), working_directory)
