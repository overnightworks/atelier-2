from __future__ import annotations

import ast
import importlib
import io
import sys
import tokenize
import tomllib
import typing
from collections import abc
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from importlinter.api import read_configuration
from importlinter.cli import lint_imports

EXPECTED_SOURCE_MODULE_COUNT = 252
EXPECTED_CONTRACT_NAMES = {
    "layers": "Atelier package layers",
    "root-facade": "Root facade cannot bypass ports",
    "dbos-owner": "DBOS and SQLAlchemy stay inside their adapter",
    "githubkit-owner": "githubkit stays inside the GitHub adapter",
    "wire-projection-split": "Wire schemas name no port type",
    "route-vocabulary": "Routes name no port type",
    "schema-owner": "JSON Schema evaluation stays inside one profile owner",
    "yaml-owner": "PyYAML stays inside its document adapters",
    "httpx-owner": "httpx stays inside its declared transport owners",
}
ROOT_PACKAGE = "atelier2"
PORT_PACKAGE = "atelier2.ports"
APPLICATION_PACKAGE = "atelier2.application"
USE_CASE_RECORD_IMPORT = "atelier2.api.context"
USE_CASE_RECORD_MODULE = "src/atelier2/api/context.py"
USE_CASE_RECORD_NAME = "ApiUseCases"
PORTS_RECORD_NAME = "ApiPorts"


class _UnresolvedOutcome:
    """An outcome whose fields could not be read, so nothing about it is proven."""


_UNRESOLVED_OUTCOME = _UnresolvedOutcome()


SOURCE_PACKAGE_DIRECTORY = "src/atelier2"
PORT_PACKAGE_DIRECTORY = "src/atelier2/ports"
HTTP_SENTENCE_MARKERS = ("API limits", "HTTP", "status code")
API_PACKAGE_DIRECTORY = "src/atelier2/api"
ROUTE_PACKAGE = "src/atelier2/api/routes"
# The cancellation answers a route still matches on, fenced with the route
# exception below and released with it.
PORT_ANSWERS_THE_API_MATCHES = frozenset(
    {"DurableWriteUnavailable", "DurableStateCorrupt"}
)
EXPECTED_LAYER_ROWS = (
    "__main__",
    "host",
    "api | adapters",
    "application",
    "ports",
    "contracts",
)
EXPECTED_LAYER_MEMBERS = frozenset(
    {
        "__main__",
        "host",
        "api",
        "adapters",
        "application",
        "ports",
        "contracts",
    }
)


@dataclass(frozen=True, slots=True)
class ArchitectureConfiguration:
    contracts: tuple[dict[str, Any], ...]
    layer_rows: tuple[str, ...]
    layer_members: frozenset[str]
    dbos_owner: str
    yaml_owners: tuple[str, ...]
    root_facade_owners: tuple[str, ...]


class ArchitecturePreflightError(Exception):
    pass


def read_architecture_configuration(
    configuration_path: Path,
) -> ArchitectureConfiguration:
    configuration = read_configuration(str(configuration_path))
    contracts = tuple(configuration["contracts_options"])
    actual_contract_names = {
        str(contract.get("id", "")): str(contract.get("name", ""))
        for contract in contracts
    }
    if len(actual_contract_names) != len(contracts):
        raise ArchitecturePreflightError("contract identifiers must be unique")
    if actual_contract_names != EXPECTED_CONTRACT_NAMES:
        raise ArchitecturePreflightError(
            "the reviewed contract identifiers or names changed"
        )

    layers_contract = next(
        contract for contract in contracts if contract["id"] == "layers"
    )
    layer_rows = tuple(layers_contract.get("layers", ()))
    layer_members = frozenset(
        member.strip() for row in layer_rows for member in str(row).split("|")
    )
    if layer_rows != EXPECTED_LAYER_ROWS or layer_members != EXPECTED_LAYER_MEMBERS:
        raise ArchitecturePreflightError(
            "the reviewed layer order or member set changed"
        )

    dbos_contract = next(
        contract for contract in contracts if contract["id"] == "dbos-owner"
    )
    dbos_owners = {
        str(import_expression).split(" -> ", 1)[0].removesuffix(".**")
        for import_expression in dbos_contract.get("ignore_imports", ())
    }
    if len(dbos_owners) != 1:
        raise ArchitecturePreflightError("the DBOS external-import owner is ambiguous")

    yaml_contract = next(
        contract for contract in contracts if contract["id"] == "yaml-owner"
    )
    yaml_owners = tuple(
        dict.fromkeys(
            str(import_expression).split(" -> ", 1)[0]
            for import_expression in yaml_contract.get("ignore_imports", ())
        )
    )
    if not yaml_owners:
        raise ArchitecturePreflightError("the YAML external-import owner is missing")

    root_contract = next(
        contract for contract in contracts if contract["id"] == "root-facade"
    )
    root_facade_owners = tuple(
        dict.fromkeys(
            str(module).removeprefix("atelier2.").split(".", 1)[0]
            for module in root_contract.get("forbidden_modules", ())
        )
    )
    return ArchitectureConfiguration(
        contracts,
        layer_rows,
        layer_members,
        dbos_owners.pop(),
        yaml_owners,
        root_facade_owners,
    )


def render_contract_view(configuration: ArchitectureConfiguration) -> str:
    contract_ids = ", ".join(
        str(contract["id"]) for contract in configuration.contracts
    )
    preflight_ids = ", ".join(
        preflight_id for preflight_id, _check in ARCHITECTURE_PREFLIGHTS
    )
    return "\n".join(
        (
            "```text",
            f"contracts: {contract_ids}",
            f"preflights: {preflight_ids}",
            f"layers: {' > '.join(configuration.layer_rows)}",
            f"dbos-owner: {configuration.dbos_owner}",
            f"yaml-owner: {', '.join(configuration.yaml_owners)}",
            f"root-facade-forbids: {', '.join(configuration.root_facade_owners)}",
            "```",
        )
    )


def source_module_count(source_root: Path) -> int:
    return sum(1 for _ in source_root.rglob("*.py"))


def source_module_count_mismatch(found: int) -> str:
    return (
        "source module count mismatch: "
        f"found {found} source modules; expected {EXPECTED_SOURCE_MODULE_COUNT}"
    )


def _parsed(module: Path) -> ast.Module:
    return ast.parse(module.read_text(encoding="utf-8"), filename=str(module))


def _record_under_test(project_root: Path) -> type:
    """The record as Python resolves it, from the tree being checked.

    Reading the annotations as text can only ever compare spellings, and a route
    receives the resolved object rather than its spelling — an alias or a
    re-export defeats any amount of name matching. So the type is resolved by the
    language itself.

    The module's file is checked against the tree under test rather than trusted:
    an editable install that shadowed the copy would otherwise let this check pass
    on a different tree than the one it claims to judge.
    """
    sys.path.insert(0, str(project_root / "src"))
    try:
        module = importlib.import_module(USE_CASE_RECORD_IMPORT)
    except ImportError as error:
        raise ArchitecturePreflightError(
            f"{USE_CASE_RECORD_MODULE} could not be imported to resolve its "
            f"annotations: {error}"
        ) from error
    origin = Path(module.__file__ or "")
    if origin != (project_root / USE_CASE_RECORD_MODULE).resolve():
        raise ArchitecturePreflightError(
            f"{USE_CASE_RECORD_IMPORT} resolved to {origin}, which is not the "
            f"{USE_CASE_RECORD_MODULE} of the tree under test"
        )
    record = getattr(module, USE_CASE_RECORD_NAME, None)
    if not isinstance(record, type):
        raise ArchitecturePreflightError(
            f"{USE_CASE_RECORD_MODULE} declares no {USE_CASE_RECORD_NAME}; "
            "the routes' use-case record is what this check exists for"
        )
    return record


def _declared_in(annotation: Any) -> Iterator[str]:
    """The module every type inside one annotation was declared in."""
    module = getattr(annotation, "__module__", None)
    if isinstance(module, str):
        yield module
    for argument in typing.get_args(annotation):
        if isinstance(argument, list):
            for element in argument:
                yield from _declared_in(element)
        else:
            yield from _declared_in(argument)


def _owned_by(module: str, package: str) -> bool:
    return module == package or module.startswith(f"{package}.")


def _is_a_port_capability(candidate: Any) -> bool:
    """Whether this type is a store a holder could call, rather than data it read.

    The two live side by side under `atelier2.ports`, and only one of them is the
    danger. `RunQueries` is a protocol: whoever holds it can ask the store
    anything. `RunProjection` is a frozen record the store already answered with —
    a route is *supposed* to hold that, because rendering it is the route's job.

    So the discriminator is the protocol, not the package. This is the reading the
    sentence always had — a port is a capability — and not a narrowing to make a
    finding go away: the mutation this rule exists for hands over `RunQueries`.
    That two kinds of thing share one package is a real smell, reported to #87
    rather than fixed here.
    """
    return getattr(candidate, "_is_protocol", False) and _owned_by(
        getattr(candidate, "__module__", ""), PORT_PACKAGE
    )


def _readable_fields(outcome: type) -> dict[str, Any] | None:
    """This type's annotated fields, or `None` when nothing can resolve them.

    A generic declared with PEP 695 type parameters carries them in a lexical
    scope rather than in its module, so a subclass's annotations name something
    `get_type_hints` cannot see from the outside. The parameters are therefore
    offered back from the whole inheritance chain, bound to what they were bound
    to — which is what the annotation meant in the first place.
    """
    parameters: dict[str, Any] = {}
    for base in getattr(outcome, "__mro__", ()):
        for parameter in getattr(base, "__type_params__", ()) or ():
            parameters[parameter.__name__] = (
                getattr(parameter, "__bound__", None) or object
            )
    try:
        return typing.get_type_hints(outcome, localns=parameters)
    except (NameError, TypeError, AttributeError):
        return None


def _carried_by(outcome: Any, seen: set[int]) -> Iterator[Any]:
    """Every type a value of this outcome could carry, however deep it sits.

    Naming an outcome of this application is not enough: the application layer may
    read the ports, so an outcome is free to carry one as a payload and hand it on
    unread by any rule that stops at the outer type. The closure is walked instead
    — union members, generic arguments and the annotated fields of every type
    reached — so a port inside the answer is a port the route was handed.
    """
    if id(outcome) in seen:
        return
    seen.add(id(outcome))
    yield outcome
    for argument in typing.get_args(outcome):
        if isinstance(argument, list):
            for element in argument:
                yield from _carried_by(element, seen)
        else:
            yield from _carried_by(argument, seen)
    value = getattr(outcome, "__value__", None)
    if value is not None:
        yield from _carried_by(value, seen)
    if isinstance(outcome, type) and _owned_by(
        getattr(outcome, "__module__", ""), ROOT_PACKAGE
    ):
        fields = _readable_fields(outcome)
        if fields is None:
            # A type whose own fields cannot be read cannot be shown to be free of
            # ports, and the safe answer to that is no. It is refused wherever it
            # lives: an outcome nobody can read is exactly where one would hide.
            yield _UNRESOLVED_OUTCOME
            return
        for field in fields.values():
            yield from _carried_by(field, seen)


def use_case_record_problems(project_root: Path) -> tuple[str, ...]:
    """Every way the use-case record could hand a port back to a route.

    The record is what the routes hold, so a field of it that resolves to a port
    reopens exactly the call the other locks close. The rule is positive and
    therefore fail-closed: a field is a call into this application, or it is
    refused. A field that is not a callable at all, or whose outcome was declared
    anywhere but `atelier2.application`, fails without anyone having to predict the
    spelling it would have used.

    There is no exception list: the record does not predate this rule, so it never
    legitimately holds a port.
    """
    record = _record_under_test(project_root)
    problems = list(_unannotated_fields(project_root))
    try:
        resolved = typing.get_type_hints(record)
    except (NameError, TypeError, AttributeError) as error:
        # An annotation nobody can resolve is refused rather than skipped: what a
        # route would hold cannot be judged, and the safe answer to that is no.
        # Any other failure still ends the run — this check never reports green
        # for a record it could not read.
        return (
            *problems,
            (
                f"{USE_CASE_RECORD_NAME} carries an annotation that resolves to "
                f"nothing, so what a route would hold cannot be judged: {error}"
            ),
        )
    for field, annotation in resolved.items():
        stated = f"{USE_CASE_RECORD_NAME}.{field} is {annotation}"
        if typing.get_origin(annotation) is not abc.Callable:
            problems.append(
                f"{stated}, which is not a call into {APPLICATION_PACKAGE}; every "
                "field of this record is a use-case the composition already bound"
            )
            continue
        declared = tuple(_declared_in(annotation))
        if any(_owned_by(module, PORT_PACKAGE) for module in declared):
            problems.append(f"{stated}, which resolves to {PORT_PACKAGE}")
            continue
        outcome = typing.get_args(annotation)[1]
        if not all(
            _owned_by(module, APPLICATION_PACKAGE) for module in _declared_in(outcome)
        ):
            problems.append(
                f"{stated}, whose outcome was not declared in "
                f"{APPLICATION_PACKAGE}; a route reads this layer's own answer"
            )
            continue
        carried = tuple(_carried_by(outcome, set()))
        capabilities = [
            carrier for carrier in carried if _is_a_port_capability(carrier)
        ]
        if capabilities:
            problems.append(
                f"{stated}, whose outcome carries {capabilities[0]} inside it; an "
                "answer that hands a port on is the port the route was handed"
            )
        elif any(carrier is _UNRESOLVED_OUTCOME for carrier in carried):
            problems.append(
                f"{stated}, whose outcome carries a type this check could not "
                "read, so it cannot be shown to be free of ports"
            )
    return tuple(problems)


def _unannotated_fields(project_root: Path) -> Iterator[str]:
    """A class-body assignment carrying no annotation is invisible to the resolver.

    `typing.get_type_hints` reports annotated fields only, so an unannotated
    assignment would never reach the rule above. It is refused here rather than
    tolerated, because a field without an annotation is a hole in exactly this
    check.
    """
    module = _parsed(project_root / USE_CASE_RECORD_MODULE)
    for node in ast.walk(module):
        if not isinstance(node, ast.ClassDef) or node.name != USE_CASE_RECORD_NAME:
            continue
        for statement in node.body:
            if isinstance(statement, (ast.Assign, ast.AugAssign)):
                yield (
                    f"{USE_CASE_RECORD_NAME} carries an unannotated assignment; "
                    "a field without an annotation is a hole in this check"
                )


PORTS_RECORD_FIELD = "ports"


def _binds_the_ports_record(node: ast.AST) -> bool:
    """Whether this expression hands over the whole record of ports."""
    return isinstance(node, ast.Attribute) and node.attr == PORTS_RECORD_FIELD


def _port_reached_at(node: ast.AST, aliases: frozenset[str]) -> str | None:
    """The port this one node reaches, if it is the access itself.

    One node, never its subtree: an access nested inside another expression must
    count once, not once per level it sits under.

    An access is recognised by what it reaches, not by how it is spelled. Reading
    `context.ports.run_queries` and binding `ports = context.ports` to read
    `ports.run_queries` off the name are the same reach, so the names a function
    binds the record to are resolved first and counted the same way. Otherwise the
    check measures a spelling, and a spelling is exactly what a caller can change
    without changing what it holds.
    """
    if isinstance(node, ast.Name) and node.id == PORTS_RECORD_NAME:
        return PORTS_RECORD_NAME
    if isinstance(node, ast.Attribute):
        if _binds_the_ports_record(node):
            return PORTS_RECORD_FIELD
        if isinstance(node.value, ast.Attribute) and _binds_the_ports_record(
            node.value
        ):
            return node.attr
        if isinstance(node.value, ast.Name) and node.value.id in aliases:
            return node.attr
    return None


def _ports_record_aliases(nodes: Iterable[ast.AST]) -> frozenset[str]:
    """Every local name these nodes bind the whole record of ports to."""
    bound: set[str] = set()
    for node in nodes:
        if not isinstance(node, (ast.Assign, ast.AnnAssign)):
            continue
        value = node.value
        if value is None or not any(
            _binds_the_ports_record(inner) for inner in ast.walk(value)
        ):
            continue
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        bound.update(target.id for target in targets if isinstance(target, ast.Name))
    return frozenset(bound)


def _ports_reached_in(nodes: Iterable[ast.AST]) -> tuple[str, ...]:
    """Every port these nodes reach, one entry per access.

    A repeated access is a repeated entry: two reads of the same port are two
    decisions, and collapsing them would let one hide behind the other.
    """
    own = list(nodes)
    aliases = _ports_record_aliases(own)
    # `context.ports.run_queries` is one access, not two: the record it reads
    # through is part of the same expression, so only the outermost node counts.
    consumed = {
        id(node.value)
        for node in own
        if isinstance(node, ast.Attribute) and _binds_the_ports_record(node.value)
    }
    return tuple(
        sorted(
            reached
            for node in own
            if id(node) not in consumed
            and (reached := _port_reached_at(node, aliases)) is not None
        )
    )


def _calls_reaching_ports(module: ast.Module) -> dict[str, tuple[str, ...]]:
    """Which ports each named call of one route module reaches, innermost one wins."""
    reaching: dict[str, tuple[str, ...]] = {}
    definitions = [
        node
        for node in ast.walk(module)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    ]
    for definition in definitions:
        nested = {
            id(inner)
            for child in ast.iter_child_nodes(definition)
            for inner in ast.walk(child)
            if isinstance(inner, (ast.FunctionDef, ast.AsyncFunctionDef))
            and inner is not definition
        }
        own = [
            child
            for child in ast.walk(definition)
            if child is not definition and id(child) not in nested
        ]
        reached = _ports_reached_in(own)
        if reached:
            reaching[definition.name] = reached
    outside = [
        statement
        for statement in module.body
        if not isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef))
    ]
    outer = _ports_reached_in(
        child for statement in outside for child in ast.walk(statement)
    )
    if outer:
        reaching["<module>"] = outer
    return reaching


def port_sentence_problems(project_root: Path) -> tuple[str, ...]:
    """Ports that mention one of HTTP_SENTENCE_MARKERS: API limits, HTTP, status code.

    The check is that three-marker heuristic, not every sentence a port could
    write. A plain-English refusal without those markers is outside this gate.
    A port still must not explain a decision it does not make; review catches
    what the markers miss.
    """
    problems: list[str] = []
    for module_path in sorted((project_root / PORT_PACKAGE_DIRECTORY).rglob("*.py")):
        module = _parsed(module_path)
        for node in ast.walk(module):
            if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
                continue
            if any(marker in node.value for marker in HTTP_SENTENCE_MARKERS):
                problems.append(
                    f"{PORT_PACKAGE_DIRECTORY}/{module_path.name} words an answer "
                    f"for the layer above it: {node.value!r}"
                )
    return tuple(problems)


def _port_record_names(project_root: Path) -> dict[str, str]:
    """Every value a port module defines, read from the ports themselves.

    A record is a class a port defines that is not a Protocol: the shape an
    answer carries rather than the seam that answers. The set is derived, not
    listed, so a record added to a port tomorrow is covered without an edit
    here -- a hand-kept list is what let read models settle in the ports.
    """
    records: dict[str, str] = {}
    for module_path in sorted((project_root / PORT_PACKAGE_DIRECTORY).rglob("*.py")):
        for node in _parsed(module_path).body:
            if not isinstance(node, ast.ClassDef):
                continue
            protocol = any(
                (isinstance(base, ast.Name) and base.id == "Protocol")
                or (isinstance(base, ast.Attribute) and base.attr == "Protocol")
                for base in node.bases
            )
            if not protocol:
                records[node.name] = module_path.name
    return records


def api_port_record_problems(project_root: Path) -> tuple[str, ...]:
    """API modules that name a value a port defines rather than the seam itself.

    The API may hold a port -- that is what a composition does -- and it may
    match on the answers a port gives. What it must not do is read a port for
    the shape of the answer: a projection the adapter builds and the use cases
    carry is a shared value, so it lives with the other values and the port
    keeps only its protocol and its outcomes. The two answers below are the ones
    the API matches on directly, because every layer that can fail to write says
    them in the same words.
    """
    records = _port_record_names(project_root)
    problems: list[str] = []
    for module_path in sorted((project_root / API_PACKAGE_DIRECTORY).rglob("*.py")):
        if module_path.match(f"{ROUTE_PACKAGE}/*"):
            continue
        for node in ast.walk(_parsed(module_path)):
            if not isinstance(node, ast.ImportFrom):
                continue
            if not node.module or not node.module.startswith(PORT_PACKAGE):
                continue
            for alias in node.names:
                owner = records.get(alias.name)
                if owner is None or alias.name in PORT_ANSWERS_THE_API_MATCHES:
                    continue
                problems.append(
                    f"{module_path.relative_to(project_root)} names {alias.name} from "
                    f"{PORT_PACKAGE_DIRECTORY}/{owner}: a value the adapter builds and "
                    "the use cases carry belongs with the other shared values, not in "
                    "the seam that returns it"
                )
    return tuple(problems)


def route_port_problems(project_root: Path) -> tuple[str, ...]:
    """Which calls still reach a port -- and none may, now that none does.

    This read against a map of declared exceptions while routes were being
    translated one call at a time. The map shrank to empty and deleted itself
    with its last entry, exactly as it said it would, so the rule is now the
    plain one: a route reads the use-case record the composition bound for it.
    """
    problems: list[str] = []
    route_root = project_root / ROUTE_PACKAGE
    for module_path in sorted(route_root.rglob("*.py")):
        relative = module_path.relative_to(project_root).as_posix()
        for call, reached in sorted(
            _calls_reaching_ports(_parsed(module_path)).items()
        ):
            problems.append(
                f"{relative}: {call} reaches {reached}; "
                "a route reads the use-case record the composition bound for it"
            )
    return tuple(problems)


DUPLICATE_BASELINE_FILE = "duplicate_baseline.toml"
DUPLICATE_BASELINE_TABLE = "pair"
# Five consecutive tokens: long enough that a shared idiom does not match on its
# own, short enough that one edited statement still leaves the rest overlapping.
DUPLICATE_SHINGLE_LENGTH = 5
# A shorter definition says too little to call a second one a copy: a delegation
# of two lines matches every neighbour that delegates the same way.
MINIMUM_DUPLICATE_TOKENS = 40
# Near-identity rather than similarity: at this overlap two definitions differ in
# a token or two, which is a copy someone made, not a family resemblance.
DUPLICATE_JACCARD_THRESHOLD = 0.95
# `True`, `False`, `None` and `...` reach the tokenizer as names and an operator
# rather than as literals, but that is what they are: which one a definition
# names says as little about it as which number it names.
KEYWORD_LITERAL_SPELLINGS = frozenset({"True", "False", "None", "..."})
NORMALISED_KEYWORD_LITERAL = "<constant>"
OVERLOAD_DECORATOR = "overload"
DUPLICATE_BASELINE_SHAPE_REFUSAL = (
    f"{DUPLICATE_BASELINE_FILE}: every [[{DUPLICATE_BASELINE_TABLE}]] names a left "
    "and a right qualified name"
)

FunctionDefinition = ast.FunctionDef | ast.AsyncFunctionDef


@dataclass(frozen=True, slots=True, order=True)
class DuplicatePair:
    """Two qualified names carrying the same code, in one canonical order."""

    left: str
    right: str


def duplicate_pair(one: str, other: str) -> DuplicatePair:
    first, second = sorted((one, other))
    return DuplicatePair(first, second)


@dataclass(frozen=True, slots=True)
class SourceDefinition:
    qualified_name: str
    location: str
    shingles: frozenset[tuple[str, ...]]


def _own_scope_nodes(definition: FunctionDefinition) -> Iterator[ast.AST]:
    """Definition and its descendants, stopping at a nested scope's boundary.

    A nested function, lambda, or class keeps its own parameters and locals,
    including any `global`/`nonlocal` it declares, out of the enclosing
    definition's bindings: only the nested def's own name is a local of the
    enclosing scope, and its body is scanned separately when that nested
    definition is walked on its own.
    """
    stack: list[ast.AST] = [definition]
    while stack:
        node = stack.pop()
        yield node
        if node is not definition and isinstance(
            node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef)
        ):
            continue
        stack.extend(ast.iter_child_nodes(node))


def _names_bound_by(definition: FunctionDefinition) -> dict[str, str]:
    """Every name this definition binds itself, numbered in source order.

    A copy someone renamed is still a copy, so the spelling a definition chose
    for itself, its parameters, its locals, the exceptions it catches and the
    parts it matches out carries no evidence: numbering them by where they are
    bound compares what the code does with them instead. Names it does not bind
    -- imports, attributes, the vocabulary it calls into -- keep their spelling,
    which is what keeps unrelated code apart, and so does a name it declares
    `global` or `nonlocal`, because that one reaches state outside it.
    """
    bound: list[tuple[int, int, str]] = []
    external: set[str] = set()
    for node in _own_scope_nodes(definition):
        if isinstance(node, ast.arg):
            bound.append((node.lineno, node.col_offset, node.arg))
        elif isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
            bound.append((node.lineno, node.col_offset, node.id))
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            bound.append((node.lineno, node.col_offset, node.name))
        elif isinstance(node, (ast.ExceptHandler, ast.MatchAs, ast.MatchStar)):
            if node.name is not None:
                bound.append((node.lineno, node.col_offset, node.name))
        elif isinstance(node, ast.MatchMapping):
            if node.rest is not None:
                bound.append((node.lineno, node.col_offset, node.rest))
        elif isinstance(node, (ast.Global, ast.Nonlocal)):
            external.update(node.names)
    numbered: dict[str, str] = {}
    for _line, _column, name in sorted(bound):
        if name not in external:
            numbered.setdefault(name, f"<name{len(numbered)}>")
    return numbered


def _structural_tokens(definition: FunctionDefinition) -> tuple[str, ...]:
    """The definition as tokens with its literals and its own names normalised.

    Unparsing first drops formatting and comments, so two copies that were
    reflowed differently still yield the same stream.
    """
    numbered = _names_bound_by(definition)
    source = ast.unparse(definition)
    tokens: list[str] = []
    for token in tokenize.generate_tokens(io.StringIO(source).readline):
        if token.type in (
            tokenize.NL,
            tokenize.NEWLINE,
            tokenize.INDENT,
            tokenize.DEDENT,
            tokenize.COMMENT,
            tokenize.ENDMARKER,
        ):
            continue
        if token.string in KEYWORD_LITERAL_SPELLINGS:
            tokens.append(NORMALISED_KEYWORD_LITERAL)
        elif token.type == tokenize.STRING:
            tokens.append("<string>")
        elif token.type == tokenize.NUMBER:
            tokens.append("<number>")
        elif token.type == tokenize.NAME:
            tokens.append(numbered.get(token.string, token.string))
        else:
            tokens.append(token.string)
    return tuple(tokens)


def _shingles(tokens: Sequence[str]) -> frozenset[tuple[str, ...]]:
    length = DUPLICATE_SHINGLE_LENGTH
    return frozenset(
        tuple(tokens[start : start + length])
        for start in range(len(tokens) - length + 1)
    )


def _definitions_under(
    node: ast.AST, prefix: str
) -> Iterator[tuple[str, FunctionDefinition]]:
    """Every function this node holds, under the qualified name it is reached by."""
    for child in ast.iter_child_nodes(node):
        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            qualified_name = f"{prefix}.{child.name}"
            if not isinstance(child, ast.ClassDef):
                yield qualified_name, child
            yield from _definitions_under(child, qualified_name)


def _decorator_name(decorator: ast.expr) -> str:
    if isinstance(decorator, ast.Attribute):
        return decorator.attr
    if isinstance(decorator, ast.Name):
        return decorator.id
    return ""


def _says_nothing(statement: ast.stmt) -> bool:
    return isinstance(statement, ast.Pass) or (
        isinstance(statement, ast.Expr) and isinstance(statement.value, ast.Constant)
    )


def _declares_an_overload(definition: FunctionDefinition) -> bool:
    """A signature of another definition rather than a definition of its own.

    Several `@overload` declarations carry the implementation's qualified name
    on purpose, and a body of `...` says nothing about what the code does, so
    they are neither a second definition of that name nor copies of each other.
    """
    return any(
        _decorator_name(decorator) == OVERLOAD_DECORATOR
        for decorator in definition.decorator_list
    ) and all(_says_nothing(statement) for statement in definition.body)


def _module_name(module_path: Path, source_root: Path) -> str:
    parts = module_path.relative_to(source_root).with_suffix("").parts
    if parts and parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join((ROOT_PACKAGE, *parts))


def source_definitions(project_root: Path) -> tuple[SourceDefinition, ...]:
    """Every function of the source package long enough to be recognised again."""
    source_root = project_root / SOURCE_PACKAGE_DIRECTORY
    definitions: list[SourceDefinition] = []
    seen: set[str] = set()
    for module_path in sorted(source_root.rglob("*.py")):
        module = _parsed(module_path)
        for qualified_name, node in _definitions_under(
            module, _module_name(module_path, source_root)
        ):
            if _declares_an_overload(node):
                continue
            tokens = _structural_tokens(node)
            if len(tokens) < MINIMUM_DUPLICATE_TOKENS:
                continue
            if qualified_name in seen:
                raise ArchitecturePreflightError(
                    f"{qualified_name} is defined twice; the duplicate baseline "
                    "names a definition by that name and could not tell them apart"
                )
            seen.add(qualified_name)
            location = f"{module_path.relative_to(project_root)}:{node.lineno}"
            definitions.append(
                SourceDefinition(qualified_name, location, _shingles(tokens))
            )
    return tuple(definitions)


def found_duplicates(
    definitions: Sequence[SourceDefinition],
) -> frozenset[DuplicatePair]:
    """Every pair of definitions whose token shingles overlap at the threshold.

    Scanning by shingle count and stopping early is exact rather than a
    shortcut: a pair can only reach the threshold when the smaller set holds at
    least that share of the larger one, so a larger partner cannot qualify once
    one has failed.
    """
    by_size = sorted(definitions, key=lambda definition: len(definition.shingles))
    duplicates: set[DuplicatePair] = set()
    for index, smaller in enumerate(by_size):
        for larger in by_size[index + 1 :]:
            if len(smaller.shingles) < DUPLICATE_JACCARD_THRESHOLD * len(
                larger.shingles
            ):
                break
            shared = len(smaller.shingles & larger.shingles)
            union = len(smaller.shingles | larger.shingles)
            if shared / union >= DUPLICATE_JACCARD_THRESHOLD:
                duplicates.add(
                    duplicate_pair(smaller.qualified_name, larger.qualified_name)
                )
    return frozenset(duplicates)


def _baseline_pairs(entries: object) -> Iterator[DuplicatePair]:
    """The listed pairs, refusing anything the baseline's shape does not carry.

    The file is edited by hand, so a table of another shape is as likely as a
    wrong name in it, and reading it as one anyway would answer the ratchet with
    a crash instead of a sentence about the baseline.
    """
    if not isinstance(entries, list):
        raise ArchitecturePreflightError(DUPLICATE_BASELINE_SHAPE_REFUSAL)
    for entry in entries:
        left = entry.get("left") if isinstance(entry, dict) else None
        right = entry.get("right") if isinstance(entry, dict) else None
        if not isinstance(left, str) or not isinstance(right, str):
            raise ArchitecturePreflightError(DUPLICATE_BASELINE_SHAPE_REFUSAL)
        yield duplicate_pair(left, right)


def read_duplicate_baseline(project_root: Path) -> frozenset[DuplicatePair]:
    """The duplicate pairs this tree already knows about, read as data."""
    path = project_root / DUPLICATE_BASELINE_FILE
    try:
        with path.open("rb") as handle:
            document = tomllib.load(handle)
    except tomllib.TOMLDecodeError as error:
        raise ArchitecturePreflightError(
            f"{DUPLICATE_BASELINE_FILE} is not readable as TOML: {error}"
        ) from error
    return frozenset(_baseline_pairs(document.get(DUPLICATE_BASELINE_TABLE, [])))


def duplicate_problems(project_root: Path) -> tuple[str, ...]:
    """Copied code the baseline does not already carry -- and entries it outlived.

    The ratchet holds in both directions on purpose: a new pair is red because
    the tree grew a copy, and a baseline entry whose pair is gone is red because
    a list that only ever grows stops describing anything.
    """
    definitions = source_definitions(project_root)
    located = {
        definition.qualified_name: definition.location for definition in definitions
    }
    baseline = read_duplicate_baseline(project_root)
    duplicates = found_duplicates(definitions)
    problems = [
        f"{pair.left} ({located[pair.left]}) is a copy of "
        f"{pair.right} ({located[pair.right]}); give the two one owner, or record "
        f"the pair in {DUPLICATE_BASELINE_FILE}"
        for pair in sorted(duplicates - baseline)
    ]
    problems.extend(
        f"{pair.left} and {pair.right} are no longer a duplicate pair: "
        f"orphan baseline entry, remove it from {DUPLICATE_BASELINE_FILE}"
        for pair in sorted(baseline - duplicates)
    )
    return tuple(problems)


ARCHITECTURE_PREFLIGHTS = (
    ("port-sentence-problems", port_sentence_problems),
    ("api-port-record-problems", api_port_record_problems),
    ("use-case-record-problems", use_case_record_problems),
    ("route-port-problems", route_port_problems),
    ("duplicate-problems", duplicate_problems),
)


def architecture_preflight(project_root: Path) -> ArchitectureConfiguration:
    source_count = source_module_count(project_root / SOURCE_PACKAGE_DIRECTORY)
    if source_count != EXPECTED_SOURCE_MODULE_COUNT:
        raise ArchitecturePreflightError(source_module_count_mismatch(source_count))
    problems: list[str] = []
    for preflight_id, check in ARCHITECTURE_PREFLIGHTS:
        try:
            found = check(project_root)
        except ArchitecturePreflightError as error:
            problems.append(f"{preflight_id}: {error}")
            continue
        problems.extend(f"{preflight_id}: {problem}" for problem in found)
    if problems:
        raise ArchitecturePreflightError(
            "architecture preflights failed:\n  " + "\n  ".join(problems)
        )
    configuration = read_architecture_configuration(project_root / "pyproject.toml")
    print(
        "Architecture preflight: "
        f"{source_count} source modules, {len(configuration.contracts)} contracts, "
        f"{len(configuration.layer_members)} layer members, "
        f"{len(ARCHITECTURE_PREFLIGHTS)} architecture preflights",
        flush=True,
    )
    return configuration


def main() -> int:
    project_root = Path.cwd()
    try:
        architecture_preflight(project_root)
    except (
        ArchitecturePreflightError,
        FileNotFoundError,
        KeyError,
        TypeError,
        ValueError,
    ) as error:
        print(f"Architecture preflight refused: {error}", file=sys.stderr)
        return 1
    sys.path.insert(0, str(project_root / "src"))
    return lint_imports(
        config_filename=str(project_root / "pyproject.toml"),
        no_cache=True,
        show_timings=True,
    )


if __name__ == "__main__":
    raise SystemExit(main())
