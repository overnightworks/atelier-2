"""A git repository the catalog takes definitions out of, and never writes back to.

Authoring lives in the operator's own repository (ADR 0007 decision 2). A
definition source is the wire to it: which repository, which ref, and which
paths carry which kind of document. Nothing here reads git and nothing here
writes a store -- this module owns only what a source *is*, so the identities
below cannot drift apart in the adapter, the store, and the command at once.

Four identities stay separate on purpose (ADR 0007):

* the registered source is `source_id`, minted from the repository and the ref
  it was configured with, so connecting the same repository at the same ref a
  second time is the same source rather than a second copy of it;
* what the operator configured is a `DefinitionSourceConfiguration`, compared as
  a whole and ordered so that the same claim written differently is the same
  claim; the store decides which `revision_number` it lands under, and
  `revision_hash` names that pairing;
* authored continuity is `(source_id, repository path)`, which is why a rename
  in the repository founds a new lineage instead of quietly joining an old one;
* a published revision is the SHA-256 of the exact file bytes, and the commit a
  scan resolved is provenance beside it, never identity.

A selection names its kind rather than deriving one from the layout: ADR 0018
rules that the kind is configured, never inferred. Workflows, schemas and
budget policies are the kinds with a reader; another kind joins when a reader
for it exists.
"""

from __future__ import annotations

import re
import struct
from dataclasses import dataclass, field
from enum import StrEnum

from atelier2.contracts.agents import MAXIMUM_SIGNED_INT64
from atelier2.contracts.catalog_v3 import CatalogActivatedAt
from atelier2.contracts.hashing import Sha256Hash, frame
from atelier2.contracts.revisions_v3 import PublishedRevisionHash, RevisionKind

MAXIMUM_REPOSITORY_LOCATION_CHARACTERS = 1_024
MAXIMUM_REPOSITORY_REF_CHARACTERS = 256
MAXIMUM_REPOSITORY_PATH_CHARACTERS = 1_024
MAXIMUM_SELECTION_PATTERN_CHARACTERS = 1_024
MAXIMUM_DEFINITION_SOURCE_ACTOR_CHARACTERS = 1_024
MAXIMUM_DEFINITION_SOURCE_SELECTIONS = 64
"""How many patterns one configuration may carry.

Not a product ceiling on how much a source may deliver -- one pattern matches
any number of files -- but the bound on the operator-typed list itself, so a
configuration cannot grow past what a person wrote.
"""

MINIMUM_GIT_OBJECT_NAME_CHARACTERS = 40
MAXIMUM_GIT_OBJECT_NAME_CHARACTERS = 64
"""A git object name is a SHA-1 or a SHA-256 digest, and this product reads both."""

_HEXADECIMAL = re.compile(r"[0-9a-f]+")
_WILDCARD = "*"
_PATTERN_WILDCARD = re.compile(r"(\*)")
_SELECTION_KINDS = frozenset(
    {RevisionKind.WORKFLOW, RevisionKind.SCHEMA, RevisionKind.BUDGET_POLICY}
)
"""The kinds a selection may declare: exactly those an intake can read.

ADR 0018 keeps the kind configured rather than inferred, so widening this set
is what admits agents, skills or MCP servers -- not a new layout heuristic.
"""


class DefinitionSourceKind(StrEnum):
    """Which family of source a registration names."""

    GIT = "git"


class DefinitionSourceAccess(StrEnum):
    """How a scan reaches the source.

    Only anonymous access exists while private repositories wait on account
    authentication; a credential reference joins here, never a secret.
    """

    ANONYMOUS = "anonymous"


class DefinitionSourceRefusal(StrEnum):
    """Every reason a connect or a scan stops, said in one closed vocabulary.

    A refusal named here is answered before anything is written. Refusals about
    a document's *content* are not here: those are the publication door's own
    words, passed through as it said them.
    """

    UNREACHABLE = "definition_source_unreachable"
    REF_UNRESOLVED = "definition_source_ref_unresolved"
    LAYOUT_UNRECOGNIZED = "definition_source_layout_unrecognized"
    SELECTION_AMBIGUOUS = "definition_source_selection_ambiguous"
    PATH_ESCAPES_REPOSITORY = "definition_source_path_escapes_repository"
    NO_SELECTED_FILES = "definition_source_no_selected_files"
    SYMLINK_SELECTED = "definition_source_symlink_selected"
    GITLINK_SELECTED = "definition_source_gitlink_selected"
    KIND_CHANGED = "definition_source_kind_changed"


class AmbiguousSelection(ValueError):
    """Two configured selections claim the same repository path."""


class DefinitionSourceId(Sha256Hash):
    """The durable identity of one registered source, across its revisions."""


class DefinitionSourceRevisionHash(Sha256Hash):
    """The identity of one exact configuration of a registered source."""


def _require_bounded_text(value: object, maximum: int, what: str) -> None:
    if type(value) is not str or not 1 <= len(value) <= maximum:
        raise ValueError(f"{what} must contain 1..{maximum} exact characters")


@dataclass(frozen=True)
class RepositoryLocation:
    """Where the repository is, in the words the git family understands."""

    value: str

    def __post_init__(self) -> None:
        _require_bounded_text(
            self.value,
            MAXIMUM_REPOSITORY_LOCATION_CHARACTERS,
            "a repository location",
        )


@dataclass(frozen=True)
class RepositoryRef:
    """The ref a scan resolves, configured once and resolved fresh every scan."""

    value: str

    def __post_init__(self) -> None:
        _require_bounded_text(
            self.value, MAXIMUM_REPOSITORY_REF_CHARACTERS, "a repository ref"
        )


@dataclass(frozen=True)
class SourceCommit:
    """The exact commit one scan resolved the configured ref to."""

    value: str

    def __post_init__(self) -> None:
        if (
            type(self.value) is not str
            or not MINIMUM_GIT_OBJECT_NAME_CHARACTERS
            <= len(self.value)
            <= MAXIMUM_GIT_OBJECT_NAME_CHARACTERS
            or _HEXADECIMAL.fullmatch(self.value) is None
        ):
            raise ValueError(
                "a source commit must be a lowercase hexadecimal git object name of "
                f"{MINIMUM_GIT_OBJECT_NAME_CHARACTERS}.."
                f"{MAXIMUM_GIT_OBJECT_NAME_CHARACTERS} characters"
            )


def _require_repository_relative(value: str, what: str) -> None:
    """Refuse anything that names a file outside the tree the operator selected.

    One rule for a path and for the pattern that claims it: an absolute
    spelling, a `..` segment, an empty or `.` segment, and a backslash all
    reach past the selection, and refusing them here means no reader ever has
    to resolve one to find out where it points.
    """

    if (
        value.startswith("/")
        or any(segment in {"", ".", ".."} for segment in value.split("/"))
        or "\\" in value
    ):
        raise ValueError(
            f"{DefinitionSourceRefusal.PATH_ESCAPES_REPOSITORY.value}: "
            f"{value!r} is not {what}"
        )


@dataclass(frozen=True)
class RepositoryPath:
    """One repository-relative path, in the single form the store keeps it in.

    Normalized so that two spellings of one file are one lineage: no leading
    `./`, no empty or `.` segment, no repeated separator. A path that would
    leave the repository -- absolute, or with a `..` segment -- is refused
    rather than resolved, because the resolution would name a file outside the
    tree the operator selected.
    """

    value: str

    def __post_init__(self) -> None:
        _require_bounded_text(
            self.value, MAXIMUM_REPOSITORY_PATH_CHARACTERS, "a repository path"
        )
        _require_repository_relative(
            self.value, "a normalized repository-relative path"
        )


@dataclass(frozen=True)
class SelectionPattern:
    """Which repository paths one selection claims.

    The one wildcard is `*`, matching any run of characters inside a single
    path segment, so a pattern spans exactly as many segments as it names.
    Every other character matches itself. Deliberately smaller than a shell's
    glob: character classes, negation and a recursive `**` would let one
    pattern's reach depend on a reading nobody declared, and the operator's
    real patterns -- `workflows/*.yaml`, `skills/*/SKILL.md` -- do not need
    them.
    """

    value: str
    _claim: re.Pattern[str] = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        _require_bounded_text(
            self.value, MAXIMUM_SELECTION_PATTERN_CHARACTERS, "a selection pattern"
        )
        _require_repository_relative(
            self.value, "a repository-relative selection pattern"
        )
        object.__setattr__(self, "_claim", _compiled(self.value))

    def matches(self, path: RepositoryPath) -> bool:
        return self._claim.fullmatch(path.value) is not None


def _compiled(pattern: str) -> re.Pattern[str]:
    """The pattern as the one expression that decides what it claims.

    Built once with the pattern rather than per comparison, because a scan asks
    every selection about every path in the tree.
    """

    return re.compile(
        "".join(
            r"[^/]*" if piece == _WILDCARD else re.escape(piece)
            for piece in _PATTERN_WILDCARD.split(pattern)
        )
    )


@dataclass(frozen=True)
class DefinitionSourceActor:
    """The operator accountable for connecting one definition source."""

    value: str

    def __post_init__(self) -> None:
        _require_bounded_text(
            self.value,
            MAXIMUM_DEFINITION_SOURCE_ACTOR_CHARACTERS,
            "a definition source actor",
        )


@dataclass(frozen=True)
class DefinitionSourceSelection:
    """One configured claim: which paths carry which kind of document."""

    pattern: SelectionPattern
    kind: RevisionKind

    def __post_init__(self) -> None:
        if not isinstance(self.pattern, SelectionPattern):
            raise TypeError("a selection pattern must use its typed contract")
        if not isinstance(self.kind, RevisionKind):
            raise TypeError("a selection kind must use its typed contract")
        if self.kind not in _SELECTION_KINDS:
            raise ValueError(
                f"a selection may declare only {sorted(kind.value for kind in _SELECTION_KINDS)}; "
                f"{self.kind!r} has no reader in this build"
            )


def _ordered(
    selections: tuple[DefinitionSourceSelection, ...],
) -> tuple[DefinitionSourceSelection, ...]:
    """The selections in the one order this product stores and hashes them in.

    Sorted rather than kept as typed, so the same claim written in a different
    order is the same configuration rather than a second revision of it.
    """

    return tuple(
        sorted(
            selections,
            key=lambda selection: (selection.pattern.value, selection.kind.value),
        )
    )


@dataclass(frozen=True)
class DefinitionSourceConfiguration:
    """Everything the operator configured about one source, and nothing else.

    Separate from the revision that keeps it because only the store knows which
    number a configuration lands under: a caller that named one would be saying
    what it cannot know, and two callers naming different numbers for the same
    claim would look like two different configurations.
    """

    kind: DefinitionSourceKind
    location: RepositoryLocation
    ref: RepositoryRef
    access: DefinitionSourceAccess
    connected_by: DefinitionSourceActor
    selections: tuple[DefinitionSourceSelection, ...]
    source_id: DefinitionSourceId = field(init=False)

    def __post_init__(self) -> None:
        if not isinstance(self.kind, DefinitionSourceKind):
            raise TypeError("a definition source kind must use its typed contract")
        if not isinstance(self.location, RepositoryLocation):
            raise TypeError("a repository location must use its typed contract")
        if not isinstance(self.ref, RepositoryRef):
            raise TypeError("a repository ref must use its typed contract")
        if not isinstance(self.access, DefinitionSourceAccess):
            raise TypeError("a definition source access must use its typed contract")
        if not isinstance(self.connected_by, DefinitionSourceActor):
            raise TypeError("a definition source actor must use its typed contract")
        selections = _ordered(tuple(self.selections))
        if not 1 <= len(selections) <= MAXIMUM_DEFINITION_SOURCE_SELECTIONS:
            raise ValueError(
                "a definition source must configure 1.."
                f"{MAXIMUM_DEFINITION_SOURCE_SELECTIONS} selections"
            )
        patterns = [selection.pattern.value for selection in selections]
        if len(set(patterns)) != len(patterns):
            raise ValueError(
                f"{DefinitionSourceRefusal.SELECTION_AMBIGUOUS.value}: "
                "one pattern is configured twice"
            )
        object.__setattr__(self, "selections", selections)
        object.__setattr__(
            self,
            "source_id",
            DefinitionSourceId.of(
                frame(
                    "definition-source/v1",
                    self.kind.value.encode("ascii"),
                    self.location.value.encode("utf-8"),
                    self.ref.value.encode("utf-8"),
                )
            ),
        )

    def selection_for(self, path: RepositoryPath) -> DefinitionSourceSelection | None:
        """The one selection claiming this path, or nothing when none does.

        Two selections claiming one path is a configuration whose meaning
        depends on which is read first, so the caller refuses it rather than
        picking; that is why this answers the claim set, not the first match.
        """

        claiming = [
            selection
            for selection in self.selections
            if selection.pattern.matches(path)
        ]
        if len(claiming) > 1:
            raise AmbiguousSelection(
                f"{DefinitionSourceRefusal.SELECTION_AMBIGUOUS.value}: "
                f"{path.value!r} is claimed by "
                f"{sorted(selection.pattern.value for selection in claiming)}"
            )
        return claiming[0] if claiming else None


@dataclass(frozen=True)
class DefinitionSourceRevision:
    """One configuration of a source, under the number the store gave it."""

    configuration: DefinitionSourceConfiguration
    revision_number: int
    revision_hash: DefinitionSourceRevisionHash = field(init=False)

    def __post_init__(self) -> None:
        if not isinstance(self.configuration, DefinitionSourceConfiguration):
            raise TypeError("a source configuration must use its typed contract")
        if (
            type(self.revision_number) is not int
            or not 1 <= self.revision_number <= MAXIMUM_SIGNED_INT64
        ):
            raise ValueError(
                "a definition source revision number must be a positive signed int64"
            )
        object.__setattr__(self, "revision_hash", self._hashed())

    def _hashed(self) -> DefinitionSourceRevisionHash:
        configured = self.configuration
        return DefinitionSourceRevisionHash.of(
            frame(
                "definition-source-revision/v1",
                configured.source_id.value.encode("ascii"),
                struct.pack(">Q", self.revision_number),
                configured.access.value.encode("ascii"),
                configured.connected_by.value.encode("utf-8"),
                *(
                    field_bytes
                    for selection in configured.selections
                    for field_bytes in (
                        selection.pattern.value.encode("utf-8"),
                        selection.kind.value.encode("ascii"),
                    )
                ),
            )
        )


@dataclass(frozen=True)
class SourceIntake:
    """One durable record of a source path having entered the catalog.

    `intake_number` is dense per `(source_id, path)`: ADR 0007 speaks of the
    latest intake, and a tuple with no ordering key cannot answer which that
    is. A scan reads the highest one; writing it is the intake's own act.
    """

    source_id: DefinitionSourceId
    path: RepositoryPath
    intake_number: int
    revision_kind: RevisionKind
    revision_hash: PublishedRevisionHash
    source_commit: SourceCommit

    def __post_init__(self) -> None:
        if not isinstance(self.source_id, DefinitionSourceId):
            raise TypeError("a source id must use its typed contract")
        if not isinstance(self.path, RepositoryPath):
            raise TypeError("a repository path must use its typed contract")
        if (
            type(self.intake_number) is not int
            or not 1 <= self.intake_number <= MAXIMUM_SIGNED_INT64
        ):
            raise ValueError("an intake number must be a positive signed int64")
        if not isinstance(self.revision_kind, RevisionKind):
            raise TypeError("a revision kind must use its typed contract")
        if not isinstance(self.revision_hash, PublishedRevisionHash):
            raise TypeError("a published revision hash must use its typed contract")
        if not isinstance(self.source_commit, SourceCommit):
            raise TypeError("a source commit must use its typed contract")


@dataclass(frozen=True)
class RevisionProvenance:
    """Where one published revision's bytes first came in from, as it was then.

    Beside the identity, never part of it (ADR 0007): the same bytes stay one
    revision however many sources carry them, and this says which delivery
    brought them into the catalog first.

    Only what was true at that intake. The source's location and ref are
    deliberately absent: they are configuration a later connect may change, so
    carrying today's pair beside an old commit would name a repository that
    never delivered these bytes. Which repository a source stands for now is
    the source's own question, and answering it is a later slice.
    """

    source_id: DefinitionSourceId
    commit: SourceCommit
    path: RepositoryPath
    intaken_at: CatalogActivatedAt

    def __post_init__(self) -> None:
        if not isinstance(self.source_id, DefinitionSourceId):
            raise TypeError("a source id must use its typed contract")
        if not isinstance(self.commit, SourceCommit):
            raise TypeError("a source commit must use its typed contract")
        if not isinstance(self.path, RepositoryPath):
            raise TypeError("a repository path must use its typed contract")
        if not isinstance(self.intaken_at, CatalogActivatedAt):
            raise TypeError("an intake instant must use its typed contract")
