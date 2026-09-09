from __future__ import annotations

import os
import re
import subprocess
from collections.abc import Callable
from pathlib import Path

import pytest

from tests.tooling.architecture_test_support import (
    PROJECT_ROOT,
    append_to,
    copied_project,
    load_architecture_script,
    run_gate,
)

CONTRACT_VIEW_START = "<!-- architecture-contract-view:start -->"
CONTRACT_VIEW_END = "<!-- architecture-contract-view:end -->"


def broken_contract_named(contract_id: str) -> str:
    name = load_architecture_script().EXPECTED_CONTRACT_NAMES[contract_id]
    return f"{name} BROKEN"


def assert_named_contract_broken(
    result: subprocess.CompletedProcess[str], contract_id: str
) -> None:
    assert result.returncode != 0, result.stdout + result.stderr
    assert broken_contract_named(contract_id) in result.stdout, (
        result.stdout + result.stderr
    )


def assert_named_preflight_failed(
    result: subprocess.CompletedProcess[str], fragment: str
) -> None:
    assert result.returncode != 0, result.stdout + result.stderr
    assert "architecture preflights failed:" in result.stderr
    assert fragment in result.stderr, result.stderr


def assert_unnameable_source_refused(
    result: subprocess.CompletedProcess[str], refusal: str
) -> None:
    script = load_architecture_script()
    assert result.returncode != 0, result.stdout + result.stderr
    assert script.UNNAMEABLE_SOURCE_HEADLINE in result.stderr, result.stderr
    assert refusal in result.stderr, result.stderr


def add_contract_to_host_import(project: Path) -> None:
    append_to(
        project,
        "src/atelier2/contracts/workflow_documents.py",
        "\nfrom atelier2.host import main\n",
    )


def add_root_to_application_package_import(project: Path) -> None:
    append_to(project, "src/atelier2/__init__.py", "import atelier2.application\n")


def add_root_to_application_leaf_import(project: Path) -> None:
    append_to(
        project,
        "src/atelier2/__init__.py",
        "from atelier2.application.answer_wait import answer_wait_result\n",
    )


def add_api_to_dbos_import(project: Path) -> None:
    append_to(project, "src/atelier2/api/__init__.py", "\nfrom dbos import DBOS\n")


def add_empty_rogue_package(project: Path) -> None:
    rogue = project / "src/atelier2/rogue"
    rogue.mkdir()
    (rogue / "__init__.py").touch()


def add_wire_to_port_import(project: Path) -> None:
    append_to(
        project,
        "src/atelier2/api/wire/__init__.py",
        "\nfrom atelier2.ports.run_queries import RunProjection\n",
    )


def add_port_reexport_on_application(project: Path) -> None:
    append_to(
        project,
        "src/atelier2/application/__init__.py",
        "from atelier2.ports.run_queries import RunPage as RunPage\n",
    )


def add_wire_to_port_reexport_through_application(project: Path) -> None:
    add_port_reexport_on_application(project)
    append_to(
        project,
        "src/atelier2/api/wire/__init__.py",
        "\nfrom atelier2.application import RunPage\n",
    )


def add_application_to_yaml_import(project: Path) -> None:
    append_to(project, "src/atelier2/application/__init__.py", "import yaml\n")


def add_third_adapter_to_yaml_import(project: Path) -> None:
    append_to(project, "src/atelier2/adapters/loopback.py", "\nimport yaml\n")


@pytest.mark.parametrize(
    ("violation", "contract_id"),
    [
        (add_contract_to_host_import, "layers"),
        (add_root_to_application_package_import, "root-facade"),
        (add_root_to_application_leaf_import, "root-facade"),
        (add_api_to_dbos_import, "dbos-owner"),
        (add_empty_rogue_package, "layers"),
    ],
    ids=[
        "contracts-to-host",
        "root-to-application-package",
        "root-to-application-deep-leaf",
        "api-to-dbos",
        "empty-rogue",
    ],
)
def test_forbidden_inward_and_dbos_owner_imports_fail(
    tmp_path: Path, violation: Callable[[Path], None], contract_id: str
) -> None:
    project = copied_project(tmp_path)
    violation(project)

    result = run_gate(project)

    assert_named_contract_broken(result, contract_id)


@pytest.mark.proves("wire-schemas-name-no-port-type")
def test_a_wire_schema_module_that_names_a_port_fails(tmp_path: Path) -> None:
    project = copied_project(tmp_path)
    add_wire_to_port_import(project)

    result = run_gate(project)

    assert_named_contract_broken(result, "wire-projection-split")


@pytest.mark.proves("wire-schemas-name-no-port-type")
def test_a_wire_schema_that_names_a_port_reexported_through_application_fails(
    tmp_path: Path,
) -> None:
    project = copied_project(tmp_path)
    add_wire_to_port_reexport_through_application(project)

    result = run_gate(project)

    assert_named_contract_broken(result, "wire-projection-split")


def test_an_application_module_that_imports_yaml_fails(tmp_path: Path) -> None:
    project = copied_project(tmp_path)
    add_application_to_yaml_import(project)

    result = run_gate(project)

    assert_named_contract_broken(result, "yaml-owner")


def test_a_third_existing_adapter_that_imports_yaml_fails(tmp_path: Path) -> None:
    project = copied_project(tmp_path)
    add_third_adapter_to_yaml_import(project)

    result = run_gate(project)

    assert_named_contract_broken(result, "yaml-owner")


def test_green_gate_reports_positive_source_contract_layer_and_native_graph_counts(
    tmp_path: Path,
) -> None:
    result = run_gate(copied_project(tmp_path))

    assert result.returncode == 0, result.stdout + result.stderr
    preflight_counts = re.search(
        r"Architecture preflight: (\d+) source modules, (\d+) contracts, "
        r"(\d+) layer members, (\d+) architecture preflights",
        result.stdout,
    )
    assert preflight_counts is not None
    source_count, contract_count, layer_count, preflight_count = map(
        int, preflight_counts.groups()
    )
    script = load_architecture_script()
    landed = script.source_module_count(PROJECT_ROOT / "src/atelier2")
    assert source_count == landed
    assert (contract_count, layer_count, preflight_count) == (
        len(script.EXPECTED_CONTRACT_NAMES),
        len(script.EXPECTED_LAYER_MEMBERS),
        len(script.ARCHITECTURE_PREFLIGHTS),
    )

    native_counts = re.search(
        r"Analyzed (\d+) files, (\d+) dependencies\.", result.stdout
    )
    assert native_counts is not None
    assert all(count > 0 for count in map(int, native_counts.groups()))
    reviewed = len(script.EXPECTED_CONTRACT_NAMES)
    assert f"Contracts: {reviewed} kept, 0 broken." in result.stdout


def empty_source_scan(project: Path) -> None:
    for source in (project / "src/atelier2").rglob("*.py"):
        source.unlink()


def delete_a_declared_layer(project: Path) -> None:
    (project / "src/atelier2/__main__.py").unlink()


def add_a_source_module(project: Path) -> None:
    (project / "src/atelier2/contracts/extra.py").write_text("", encoding="utf-8")


def add_a_module_beside_the_package(project: Path) -> None:
    beside = project / "src/tooling"
    beside.mkdir()
    (beside / "__init__.py").touch()
    (beside / "helper.py").touch()


def add_a_module_under_a_directory_python_cannot_name(project: Path) -> None:
    unnameable = project / "src/atelier2/api/route-group"
    unnameable.mkdir()
    (unnameable / "__init__.py").touch()
    (unnameable / "health.py").touch()


def add_a_module_under_a_directory_that_is_no_package(project: Path) -> None:
    loose = project / "src/atelier2/api/group"
    loose.mkdir()
    (loose / "health.py").touch()


def remove_contract(project: Path) -> None:
    configuration = project / "pyproject.toml"
    text = configuration.read_text(encoding="utf-8")
    start = text.index('id = "dbos-owner"')
    block_start = text.rfind("[[tool.importlinter.contracts]]", 0, start)
    configuration.write_text(text[:block_start], encoding="utf-8")


def change_layer(project: Path) -> None:
    configuration = project / "pyproject.toml"
    text = configuration.read_text(encoding="utf-8")
    configuration.write_text(
        text.replace('"application",\n    "ports",', '"ports",\n    "application",'),
        encoding="utf-8",
    )


@pytest.mark.parametrize(
    ("unnameable", "refusal"),
    [
        (
            add_a_module_beside_the_package,
            "src/tooling/helper.py: it lies outside the atelier2 package",
        ),
        (
            add_a_module_under_a_directory_python_cannot_name,
            (
                "src/atelier2/api/route-group/health.py: "
                "the directory 'route-group' is not a Python name"
            ),
        ),
        (
            add_a_module_under_a_directory_that_is_no_package,
            (
                "src/atelier2/api/group/health.py: "
                "atelier2/api/group carries no __init__.py"
            ),
        ),
    ],
    ids=["beside-the-package", "unnameable-directory", "no-package"],
)
def test_a_source_file_the_analysis_cannot_see_is_refused_with_its_path_and_reason(
    tmp_path: Path, unnameable: Callable[[Path], None], refusal: str
) -> None:
    project = copied_project(tmp_path)
    unnameable(project)

    result = run_gate(project)

    assert_unnameable_source_refused(result, refusal)


def test_a_new_module_inside_a_declared_layer_keeps_the_gate_green(
    tmp_path: Path,
) -> None:
    project = copied_project(tmp_path)
    add_a_source_module(project)

    result = run_gate(project)

    assert result.returncode == 0, result.stdout + result.stderr


def test_an_emptied_source_tree_is_refused_instead_of_judged_elsewhere(
    tmp_path: Path,
) -> None:
    """An empty `src` leaves the package resolvable from wherever it is installed.

    The gate then reports on a tree it never read, so what it must say is that the
    module it resolved is not this tree's module.
    """
    project = copied_project(tmp_path)
    empty_source_scan(project)
    script = load_architecture_script()

    result = run_gate(project)

    assert result.returncode != 0, result.stdout + result.stderr
    assert f"{script.USE_CASE_RECORD_MODULE} of the tree under test" in result.stderr


def test_a_missing_contract_fails_as_a_reviewed_set_change(tmp_path: Path) -> None:
    project = copied_project(tmp_path)
    remove_contract(project)

    result = run_gate(project)

    assert result.returncode != 0, result.stdout + result.stderr
    assert "the reviewed contract identifiers or names changed" in result.stderr


def test_a_changed_layer_order_fails_as_a_reviewed_set_change(tmp_path: Path) -> None:
    project = copied_project(tmp_path)
    change_layer(project)

    result = run_gate(project)

    assert result.returncode != 0, result.stdout + result.stderr
    assert "the reviewed layer order or member set changed" in result.stderr


def test_deleting_a_declared_layer_fails_as_a_missing_layer(tmp_path: Path) -> None:
    project = copied_project(tmp_path)
    delete_a_declared_layer(project)
    script = load_architecture_script()

    result = run_gate(project)

    assert result.returncode != 0, result.stdout + result.stderr
    assert f"{script.ROOT_PACKAGE}.__main__" in result.stdout + result.stderr


def test_architecture_decision_and_executable_contract_share_the_exact_layers_and_owners(
    tmp_path: Path,
) -> None:
    result = run_gate(copied_project(tmp_path))
    assert result.returncode == 0, result.stdout + result.stderr
    decision = (
        PROJECT_ROOT / "docs/decisions/0005-enforced-package-boundaries.md"
    ).read_text(encoding="utf-8")
    documented_view = decision.split(CONTRACT_VIEW_START, 1)[1].split(
        CONTRACT_VIEW_END, 1
    )[0]
    script = load_architecture_script()

    expected_view = script.render_contract_view(
        script.read_architecture_configuration(PROJECT_ROOT / "pyproject.toml")
    )

    assert documented_view.strip() == expected_view.strip()


def test_gate_runs_with_minimal_environment_and_no_network_or_service(
    tmp_path: Path,
) -> None:
    environment = {
        "PATH": os.environ["PATH"],
        "PYTHONIOENCODING": "utf-8",
    }

    result = run_gate(copied_project(tmp_path), environment)

    assert result.returncode == 0, result.stdout + result.stderr


def replace_use_case_field(project: Path, field: str, imported: str = "") -> None:
    """Add one field to the record, with the import a real evasion would need.

    The field is appended after the record's own fields, because a field carrying
    a default in front of them is a dataclass error and would end the run before
    the rule under test is ever reached. Writing the import too is what makes
    these cases the evasions they claim to be rather than typos.
    """
    context = project / "src/atelier2/api/context.py"
    source = context.read_text(encoding="utf-8")
    marker = "class ApiUseCases:\n"
    assert source.count(marker) == 1
    if imported:
        source = source.replace(
            "from atelier2.api.limits import ApiLimits\n",
            f"{imported}\n"
            "def _unbound() -> object:\n    raise NotImplementedError\n\n"
            "from atelier2.api.limits import ApiLimits\n",
            1,
        )
    body_start = source.index(marker) + len(marker)
    body_end = source.index("\n\n\n", body_start)
    context.write_text(
        source[:body_end] + f"\n    {field}" + source[body_end:],
        encoding="utf-8",
    )


def rename_the_use_case_record(project: Path) -> None:
    context = project / "src/atelier2/api/context.py"
    context.write_text(
        context.read_text(encoding="utf-8").replace("ApiUseCases", "ApiCalls"),
        encoding="utf-8",
    )


def add_route_reaching_a_port(project: Path) -> None:
    (project / "src/atelier2/api/routes/health.py").write_text(
        "from atelier2.api.context import ApiPorts\n\n\n"
        "def leak(ports: ApiPorts) -> object:\n    return ports\n",
        encoding="utf-8",
    )


def add_nested_route_reaching_a_port(project: Path) -> None:
    nested = project / "src/atelier2/api/routes/group"
    nested.mkdir()
    (nested / "__init__.py").touch()
    (nested / "health.py").write_text(
        "from atelier2.api.context import ApiPorts\n\n\n"
        "def leak(ports: ApiPorts) -> object:\n    return ports\n",
        encoding="utf-8",
    )


RUN_QUERIES_IMPORT = "from atelier2.ports.run_queries import RunQueries"
CANCELLATION_IMPORT = (
    "from atelier2.ports.agent_attempts import AgentAttemptCancellationResult"
)
QUARANTINED_IMPORT = (
    "from typing import TYPE_CHECKING\n"
    "\nif TYPE_CHECKING:\n"
    "    from atelier2.ports.run_queries import RunQueries"
)


@pytest.mark.parametrize(
    ("field", "imported"),
    [
        ("run_queries: RunQueries", RUN_QUERIES_IMPORT),
        ('run_queries: "RunQueries"', RUN_QUERIES_IMPORT),
        ("run_queries: ports.RunQueries", ""),
        ("run_queries: RunQueries", QUARANTINED_IMPORT),
        (
            "cancel: Callable[[str], AgentAttemptCancellationResult]",
            CANCELLATION_IMPORT,
        ),
        ("run_queries = None", ""),
    ],
    ids=[
        "plain-port-type",
        "forward-reference",
        "dotted-port-path",
        "quarantined-under-type-checking",
        "port-union-behind-a-bound-call",
        "unannotated",
    ],
)
@pytest.mark.proves("the-use-case-record-cannot-hand-a-route-a-port")
def test_a_use_case_field_that_resolves_to_a_port_fails(
    tmp_path: Path, field: str, imported: str
) -> None:
    project = copied_project(tmp_path)
    replace_use_case_field(project, field, imported)

    result = run_gate(project)

    assert_named_preflight_failed(result, "use-case-record-problems")


@pytest.mark.proves("the-use-case-record-cannot-hand-a-route-a-port")
def test_a_use_case_record_that_disappears_fails(tmp_path: Path) -> None:
    project = copied_project(tmp_path)
    rename_the_use_case_record(project)

    result = run_gate(project)

    assert_named_preflight_failed(result, "use-case-record-problems")
    assert "declares no ApiUseCases" in result.stderr


@pytest.mark.proves("no-route-reaches-a-port-and-the-verification-says-so")
def test_a_translated_route_module_that_reaches_a_port_again_fails(
    tmp_path: Path,
) -> None:
    project = copied_project(tmp_path)
    add_route_reaching_a_port(project)

    result = run_gate(project)

    assert_named_preflight_failed(
        result,
        "route-port-problems: src/atelier2/api/routes/health.py: leak reaches "
        "('ApiPorts',)",
    )


@pytest.mark.proves("no-route-reaches-a-port-and-the-verification-says-so")
def test_a_nested_route_module_that_reaches_a_port_fails(tmp_path: Path) -> None:
    project = copied_project(tmp_path)
    add_nested_route_reaching_a_port(project)

    result = run_gate(project)

    assert_named_preflight_failed(
        result,
        "route-port-problems: src/atelier2/api/routes/group/health.py: leak reaches "
        "('ApiPorts',)",
    )


ALIAS_IMPORT = (
    "from atelier2.ports.run_queries import RunQueries\nPortAlias = RunQueries"
)
REEXPORT_IMPORT = "from atelier2.application import RunPage"


def reach_a_port_from_a_read_of_an_allowlisted_module(project: Path) -> None:
    """Hand the port back inside a read of a module allowlisted for another call.

    Behaviour-preserving and type-correct: the composition lambda and this call
    take the same arguments. `events` legitimately keeps a port for its stream, so
    a check that records one boolean per module cannot see that this *read* stopped
    going through its use-case.
    """
    events = project / "src/atelier2/api/routes/events.py"
    source = events.read_text(encoding="utf-8")
    replaced = source.replace(
        "        lambda: context.use_cases.runs.prepare_run_events(run_id, after_sequence),",
        "        lambda: prepare_run_events(\n"
        "            run_id, after_sequence, context.ports.run_event_queries\n"
        "        ),",
    )
    assert replaced != source
    events.write_text(
        replaced.replace(
            "from atelier2.application.prepare_run_events import (",
            "from atelier2.application.prepare_run_events import (\n    prepare_run_events,",
            1,
        ),
        encoding="utf-8",
    )


@pytest.mark.proves("the-use-case-record-cannot-hand-a-route-a-port")
def test_a_use_case_field_behind_a_local_alias_fails(tmp_path: Path) -> None:
    project = copied_project(tmp_path)
    replace_use_case_field(
        project, "leaked_port: PortAlias | None = None", ALIAS_IMPORT
    )

    result = run_gate(project)

    assert_named_preflight_failed(result, "use-case-record-problems")


@pytest.mark.proves("the-use-case-record-cannot-hand-a-route-a-port")
def test_a_use_case_field_behind_a_re_export_of_another_layer_fails(
    tmp_path: Path,
) -> None:
    project = copied_project(tmp_path)
    add_port_reexport_on_application(project)
    replace_use_case_field(project, "leaked: RunPage | None = None", REEXPORT_IMPORT)

    result = run_gate(project)

    assert_named_preflight_failed(result, "use-case-record-problems")


@pytest.mark.proves("no-route-reaches-a-port-and-the-verification-says-so")
def test_a_read_that_reaches_a_port_inside_an_allowlisted_module_fails(
    tmp_path: Path,
) -> None:
    project = copied_project(tmp_path)
    reach_a_port_from_a_read_of_an_allowlisted_module(project)

    result = run_gate(project)

    assert_named_preflight_failed(result, "route-port-problems")
    assert "prepare_events" in result.stderr


# The outcome fixtures need any application module that already imports
# `dataclass`; naming that one canvas keeps their four references together.
OUTCOME_CANVAS_MODULE = "atelier2.application.evaluate_executability"
OUTCOME_CANVAS_PATH = "src/atelier2/application/evaluate_executability.py"
LEAKING_OUTCOME_IMPORT = f"from {OUTCOME_CANVAS_MODULE} import LeakedOutcome"


def add_outcome_carrying_a_port(project: Path) -> None:
    """An outcome of the application layer whose payload is a durable port.

    The field is a bound call and its result was declared where the rule demands,
    so the shape is legal in every way the rule can see from the outside. Only
    reading what the outcome carries shows the port travelling inside it.
    """
    append_to(
        project,
        OUTCOME_CANVAS_PATH,
        "\nfrom atelier2.ports.run_queries import RunQueries\n\n"
        "@dataclass(frozen=True)\n"
        "class LeakedOutcome:\n"
        "    port: RunQueries\n",
    )


def reach_a_port_twice_inside_one_allowlisted_call(project: Path) -> None:
    """A second, undeclared port read inside a call allowlisted for its first.

    `event_stream_route` legitimately carries the stream's port until B3 lands, so
    a check that answers only *whether* the call reaches one cannot see a second
    reach appear beside it.
    """
    events = project / "src/atelier2/api/routes/events.py"
    source = events.read_text(encoding="utf-8")
    replaced = source.replace(
        "    async for event in stream_server_events(",
        "    context.ports.run_event_queries.prepare_run_event_stream(prepared.run_id, 0)\n"
        "    async for event in stream_server_events(",
    )
    assert replaced != source
    events.write_text(replaced, encoding="utf-8")


@pytest.mark.proves("the-use-case-record-cannot-hand-a-route-a-port")
def test_a_use_case_outcome_that_carries_a_port_inside_it_fails(
    tmp_path: Path,
) -> None:
    project = copied_project(tmp_path)
    add_outcome_carrying_a_port(project)
    replace_use_case_field(
        project,
        "leaked: Callable[[], LeakedOutcome] = _unbound",
        LEAKING_OUTCOME_IMPORT,
    )

    result = run_gate(project)

    assert_named_preflight_failed(result, "use-case-record-problems")


@pytest.mark.proves("no-route-reaches-a-port-and-the-verification-says-so")
def test_a_second_port_read_inside_an_allowlisted_call_fails(tmp_path: Path) -> None:
    project = copied_project(tmp_path)
    reach_a_port_twice_inside_one_allowlisted_call(project)

    result = run_gate(project)

    assert_named_preflight_failed(result, "route-port-problems")
    assert "event_stream_route" in result.stderr


def reach_a_port_through_an_alias_in_a_translated_call(project: Path) -> None:
    """Take the port back inside a translated call, through a local name.

    Spelling the access literally is caught; binding the record to a name first
    and reading the port off that name is the same reach with a different
    spelling, and a check that reads only the literal form cannot tell them apart.
    """
    events = project / "src/atelier2/api/routes/events.py"
    source = events.read_text(encoding="utf-8")
    replaced = source.replace(
        "    async for event in stream_server_events(",
        "    ports = context.ports\n"
        "    ports.run_event_queries\n"
        "    async for event in stream_server_events(",
    )
    assert replaced != source
    events.write_text(replaced, encoding="utf-8")


def add_outcome_hiding_a_port_behind_an_unreadable_annotation(project: Path) -> None:
    """An outcome that really carries a port, behind an annotation nothing resolves."""
    append_to(
        project,
        OUTCOME_CANVAS_PATH,
        "\nfrom typing import TYPE_CHECKING\n"
        "if TYPE_CHECKING:\n"
        "    from atelier2.ports.run_queries import RunQueries\n\n"
        "@dataclass(frozen=True)\n"
        "class HiddenOutcome:\n"
        "    port: 'RunQueries'\n",
    )


@pytest.mark.proves("no-route-reaches-a-port-and-the-verification-says-so")
def test_a_translated_call_that_takes_a_port_back_through_an_alias_fails(
    tmp_path: Path,
) -> None:
    project = copied_project(tmp_path)
    reach_a_port_through_an_alias_in_a_translated_call(project)

    result = run_gate(project)

    assert_named_preflight_failed(result, "route-port-problems")
    assert "event_stream_route" in result.stderr


@pytest.mark.proves("the-use-case-record-cannot-hand-a-route-a-port")
def test_an_outcome_this_check_cannot_read_fails_rather_than_being_reported(
    tmp_path: Path,
) -> None:
    project = copied_project(tmp_path)
    add_outcome_hiding_a_port_behind_an_unreadable_annotation(project)
    replace_use_case_field(
        project,
        "leaked: Callable[[], HiddenOutcome] = _unbound",
        f"from {OUTCOME_CANVAS_MODULE} import HiddenOutcome",
    )

    result = run_gate(project)

    assert_named_preflight_failed(result, "use-case-record-problems")


def make_a_port_word_an_http_answer(project: Path) -> None:
    """Give a durable port a sentence about the caller's own bound again."""
    revisions = project / "src/atelier2/ports/workflow_revisions.py"
    source = revisions.read_text(encoding="utf-8")
    revisions.write_text(
        source.replace(
            "@dataclass(frozen=True)\nclass ProjectionTooLarge:",
            'PROJECTION_LIMIT_DETAIL = "Durable projection exceeds configured API limits."\n\n\n'
            "@dataclass(frozen=True)\nclass ProjectionTooLarge:",
            1,
        ),
        encoding="utf-8",
    )


@pytest.mark.proves("a-port-refuses-by-type-and-the-api-words-the-answer")
def test_a_port_that_words_an_http_answer_fails(tmp_path: Path) -> None:
    project = copied_project(tmp_path)
    make_a_port_word_an_http_answer(project)

    result = run_gate(project)

    assert_named_preflight_failed(result, "port-sentence-problems")
    assert "words an answer" in result.stderr


def make_an_api_module_name_a_port_record(project: Path) -> None:
    """Put a read model back where this head took it from."""

    port = project / "src/atelier2/ports/run_queries.py"
    port.write_text(
        port.read_text(encoding="utf-8")
        + "\n\n@dataclass(frozen=True)\nclass RunShapeForReaders:\n    run: object\n",
        encoding="utf-8",
    )
    module = project / "src/atelier2/api/projection/runs.py"
    module.write_text(
        module.read_text(encoding="utf-8")
        + "\n\nfrom atelier2.ports.run_queries import RunShapeForReaders\n"
        "\nSHAPE = RunShapeForReaders\n",
        encoding="utf-8",
    )


def test_an_api_module_that_names_a_port_record_fails(tmp_path: Path) -> None:
    project = copied_project(tmp_path)
    make_an_api_module_name_a_port_record(project)

    result = run_gate(project)

    assert_named_preflight_failed(result, "api-port-record-problems")
    assert "belongs with the other shared values" in result.stderr


def test_an_api_module_that_names_a_port_protocol_stays_green(tmp_path: Path) -> None:
    """Holding the seam is what a composition does; only the shape moved."""

    project = copied_project(tmp_path)
    module = project / "src/atelier2/api/projection/runs.py"
    module.write_text(
        module.read_text(encoding="utf-8")
        + "\n\nfrom atelier2.ports.run_queries import RunQueries\n"
        "\nSEAM = RunQueries\n",
        encoding="utf-8",
    )

    result = run_gate(project)

    assert result.returncode == 0, result.stdout + result.stderr
