"""The dead-code gate: a production symbol nothing production reaches is a defect.

`vulture` reads `src/atelier2` alone. `tests/` is never a usage source here: a
symbol only its own test reaches is not a symbol the product uses, and letting
the suite vouch for source is how a codebase keeps machinery it retired.

Three files under `scripts/baselines/` carry the names that survive a finding,
and each says something different about the name. Every entry in every list
names its symbol as `module/path.py:symbol`, relative to `src/atelier2`, exactly
as vulture reports it: qualifying by module, not by bare name, is what stops
excusing one vocabulary word in one module from silently vouching for a dead
namesake with the same name in another.

* `vulture_allowlist.py` -- a production site does reach the name, and vulture
  cannot see that site: a program built as text, a vocabulary the wire selects
  by value, a generated `__eq__`, a framework attribute. Permanent; every entry
  names the site.
* `vulture_pending.py` -- the name waits for a decision an open item already
  owns. Every entry carries an expiry, and the gate turns red once it passes, so
  a parked decision cannot be parked forever.
* `vulture_frozen.py` -- the name is built ahead of its caller and is kept
  (operator ruling 04.09.2026: freeze, do not throw away). No expiry; every
  entry names the open item that owns the caller. Reported every run so it
  stays visible, never red.

An entry naming something vulture no longer reports is orphaned and is itself
red: the lists shrink with the code they excuse.
"""

from __future__ import annotations

import ast
import sys
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path

from vulture.core import Item, Vulture

SOURCE_PACKAGE = Path("src") / "atelier2"
MINIMUM_CONFIDENCE = 60
# Decorators that hand the name to a framework's registry, which then calls it
# by route, event, or schema hook rather than by name.
FRAMEWORK_REGISTRATION_DECORATORS = (
    "@router.*",
    "@app.*",
    "@DBOS.*",
    "@model_validator",
    "@field_validator",
    "@model_serializer",
    "@event.listens_for",
)
# Names a framework or this project's own protocol reserves, everywhere they
# appear: pydantic's `model_config`, DBOS's `add_workflow`, and the
# `proves_absence` marker every acceptance report carries.
FRAMEWORK_RESERVED_NAMES = ("proves_absence", "model_config", "add_workflow")
BASELINES_DIRECTORY = Path("scripts") / "baselines"
ALLOWLIST_FILE = BASELINES_DIRECTORY / "vulture_allowlist.py"
PENDING_FILE = BASELINES_DIRECTORY / "vulture_pending.py"
FROZEN_FILE = BASELINES_DIRECTORY / "vulture_frozen.py"
# What a caller can fill by keyword: a field, never a function, a class, or a
# parameter. vulture reports a parameter as a variable it is certain about,
# which is how the two are told apart.
KEYWORD_FILLABLE_TYPES = frozenset({"variable", "attribute"})
CERTAIN_CONFIDENCE = 100
ALLOWLIST_BINDING = "REACHED_BY_A_SITE_VULTURE_CANNOT_SEE"
PENDING_BINDING = "WAITING_FOR_A_DECISION"
FROZEN_BINDING = "WAITING_FOR_A_CALLER"


class DeadCodeGateError(RuntimeError):
    """The gate could not be run as configured."""


@dataclass(frozen=True)
class ExcusedName:
    """One qualified symbol a list excuses, with the sentence that justifies it.

    Excused only in the module that carries it: a bare name would let a
    namesake in another module satisfy the entry it was never written for.
    """

    module: str
    name: str
    why: str

    def excuses(self, name: str, module: str) -> bool:
        return name == self.name and module == self.module

    @property
    def label(self) -> str:
        return f"{self.module}:{self.name}"


@dataclass(frozen=True)
class PendingName(ExcusedName):
    expires_on: date


@dataclass(frozen=True)
class FrozenName(ExcusedName):
    """A frozen name, naming the open item that owns its caller."""

    item: str


def _read_groups(project_root: Path, file: Path, binding: str) -> Iterator[dict]:
    """Read a list file as data: it states names and sentences, never behaviour."""
    path = project_root / file
    module = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for statement in module.body:
        if not isinstance(statement, ast.Assign):
            continue
        if any(
            isinstance(target, ast.Name) and target.id == binding
            for target in statement.targets
        ):
            groups = ast.literal_eval(statement.value)
            if not isinstance(groups, tuple):
                raise DeadCodeGateError(f"{file}: {binding} must be a tuple of groups")
            yield from groups
            return
    raise DeadCodeGateError(f"{file}: no {binding} to read")


def _validated_names(file: Path, group: dict, *required: str) -> tuple[str, ...]:
    """The names of one group, refused unless the group justifies them."""
    names = group.get("names")
    if not isinstance(names, tuple) or not names:
        raise DeadCodeGateError(f"{file}: a group must name at least one symbol")
    for field in ("why", *required):
        stated = group.get(field)
        if not isinstance(stated, str) or not stated.strip():
            raise DeadCodeGateError(f"{file}: {names[0]} needs a {field}")
    return names


def read_allowlist(project_root: Path) -> tuple[ExcusedName, ...]:
    return tuple(
        ExcusedName(*_path_qualified(ALLOWLIST_FILE, entry), group["why"])
        for group in _read_groups(project_root, ALLOWLIST_FILE, ALLOWLIST_BINDING)
        for entry in _validated_names(ALLOWLIST_FILE, group)
    )


def read_pending(project_root: Path) -> tuple[PendingName, ...]:
    return tuple(
        PendingName(
            *_path_qualified(PENDING_FILE, entry),
            group["why"],
            date.fromisoformat(group["expires_on"]),
        )
        for group in _read_groups(project_root, PENDING_FILE, PENDING_BINDING)
        for entry in _validated_names(PENDING_FILE, group, "expires_on")
    )


def read_frozen(project_root: Path) -> tuple[FrozenName, ...]:
    return tuple(
        FrozenName(*_path_qualified(FROZEN_FILE, entry), group["why"], group["item"])
        for group in _read_groups(project_root, FROZEN_FILE, FROZEN_BINDING)
        for entry in _validated_names(FROZEN_FILE, group, "item")
    )


def _path_qualified(file: Path, entry: str) -> tuple[str, str]:
    """Read one `module/path.py:symbol` entry, refusing a bare name."""
    module, separator, name = entry.rpartition(":")
    if not separator or not module.endswith(".py") or not name:
        raise DeadCodeGateError(
            f"{file}: {entry!r} must name the module holding the symbol, "
            "as contracts/agent_permissions.py:COMMAND"
        )
    return module, name


def names_used_as_keyword_arguments(source_root: Path) -> frozenset[str]:
    """Field names production code passes by keyword.

    vulture has no `visit_keyword`, so `RunResourceV3(work_item_reference=...)`
    counts as no use of that field at all. Every wire model and every frozen
    record here is filled exactly that way, so without this pass the gate would
    call the served contract dead.
    """
    used: set[str] = set()
    for path in sorted(source_root.rglob("*.py")):
        module = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        used.update(
            node.arg
            for node in ast.walk(module)
            if isinstance(node, ast.keyword) and node.arg is not None
        )
    return frozenset(used)


def _is_a_field_filled_by_keyword(
    item: Item, passed_by_keyword: frozenset[str]
) -> bool:
    """Whether this finding is a field some call fills by keyword.

    The keyword vocabulary is names, not sites, so it may only excuse what a
    keyword argument can fill. A dead function or a dead parameter that happens
    to share a name with someone else's keyword stays a finding.
    """
    return (
        item.typ in KEYWORD_FILLABLE_TYPES
        and item.confidence < CERTAIN_CONFIDENCE
        and item.name in passed_by_keyword
    )


def production_findings(source_root: Path) -> tuple[Item, ...]:
    scavenger = Vulture(
        ignore_names=FRAMEWORK_RESERVED_NAMES,
        ignore_decorators=FRAMEWORK_REGISTRATION_DECORATORS,
    )
    scavenger.scavenge([str(source_root)])
    passed_by_keyword = names_used_as_keyword_arguments(source_root)
    return tuple(
        item
        for item in scavenger.get_unused_code(min_confidence=MINIMUM_CONFIDENCE)
        if not _is_a_field_filled_by_keyword(item, passed_by_keyword)
    )


def _report(items: Iterable[Item]) -> str:
    return "\n".join(f"  {item.get_report(add_size=True)}" for item in items)


def _module_of(item: Item, source_root: Path) -> str:
    return Path(item.filename).relative_to(source_root).as_posix()


def main() -> int:
    project_root = Path.cwd()
    source_root = project_root / SOURCE_PACKAGE
    try:
        allowlist = read_allowlist(project_root)
        pending = read_pending(project_root)
        frozen = read_frozen(project_root)
        findings = production_findings(source_root)
    except (DeadCodeGateError, OSError, SyntaxError, TypeError, ValueError) as error:
        print(f"Dead-code gate refused: {error}", file=sys.stderr)
        return 1

    excused = allowlist + pending + frozen
    reported = [(item, _module_of(item, source_root)) for item in findings]
    unexpected = [
        item
        for item, module in reported
        if not any(entry.excuses(item.name, module) for entry in excused)
    ]
    today = datetime.now(UTC).date()
    expired = [entry for entry in pending if entry.expires_on <= today]
    stale = tuple(
        entry.label
        for entry in excused
        if not any(entry.excuses(item.name, module) for item, module in reported)
    )

    frozen_findings = [
        item
        for item, module in reported
        if any(entry.excuses(item.name, module) for entry in frozen)
    ]
    if frozen_findings:
        print(
            f"Frozen ahead of a caller ({len(frozen_findings)}, not a failure):\n"
            f"{_report(frozen_findings)}",
            flush=True,
        )

    refusals: list[str] = []
    if unexpected:
        refusals.append(
            f"{len(unexpected)} unreached production symbols:\n{_report(unexpected)}"
        )
    if expired:
        refusals.append(
            "pending decisions past their expiry:\n"
            + "\n".join(
                f"  {entry.name} (due {entry.expires_on}): {entry.why}"
                for entry in expired
            )
        )
    if stale:
        refusals.append(
            "list entries vulture no longer reports -- delete them:\n"
            + "\n".join(f"  {name}" for name in stale)
        )
    if refusals:
        print("Dead-code gate refused:\n" + "\n".join(refusals), file=sys.stderr)
        return 1

    print(
        f"Dead-code gate: {len(findings)} findings over {SOURCE_PACKAGE}, "
        f"{len(allowlist)} reached by a site vulture cannot see, "
        f"{len(pending)} awaiting a decision, {len(frozen)} frozen ahead of a caller",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
