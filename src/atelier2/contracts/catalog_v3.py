from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum

from atelier2.contracts.hashing import SHA256_HEX_DIGEST, Sha256Hash, frame
from atelier2.contracts.revisions_v3 import (
    PublishedRevision,
    PublishedRevisionHash,
    RevisionKind,
)

MAXIMUM_LINEAGE_DISPLAY_NAME_CHARACTERS = 128
# An actor reaches the catalog from outside and is written into an append-only
# event, so its bound belongs to the catalog rather than to whichever caller
# happens to arrive first.
MAXIMUM_CATALOG_ACTOR_CHARACTERS = 128
CATALOG_ACTIVATED_AT_PATTERN = (
    r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$"
)
_LINEAGE_DISPLAY_NAME = re.compile(r"[a-z][a-z0-9._-]*")
_CATALOG_ACTIVATED_AT = re.compile(
    r"(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})Z",
    re.ASCII,
)


class CatalogLineageId(Sha256Hash):
    """The store-stable identity derived from one kind and founding revision."""


@dataclass(frozen=True)
class CatalogLineageDisplayName:
    """One unambiguous operator-authored name for a catalog lineage."""

    value: str

    def __post_init__(self) -> None:
        if not isinstance(self.value, str):
            raise TypeError("a catalog lineage display name must be text")
        if not 1 <= len(self.value) <= MAXIMUM_LINEAGE_DISPLAY_NAME_CHARACTERS:
            raise ValueError(
                "a catalog lineage display name must contain 1 to "
                f"{MAXIMUM_LINEAGE_DISPLAY_NAME_CHARACTERS} characters"
            )
        if _LINEAGE_DISPLAY_NAME.fullmatch(self.value) is None:
            raise ValueError(
                "a catalog lineage display name must match [a-z][a-z0-9._-]*"
            )
        if SHA256_HEX_DIGEST.fullmatch(self.value) is not None:
            raise ValueError(
                "a catalog lineage display name cannot look like a lineage id"
            )


@dataclass(frozen=True)
class CatalogActor:
    """The attributed actor on one catalog alias or retirement event."""

    value: str

    def __post_init__(self) -> None:
        if not isinstance(self.value, str):
            raise TypeError("a catalog actor must be text")
        if not 1 <= len(self.value) <= MAXIMUM_CATALOG_ACTOR_CHARACTERS:
            raise ValueError(
                "a catalog actor must contain 1 to "
                f"{MAXIMUM_CATALOG_ACTOR_CHARACTERS} characters"
            )


@dataclass(frozen=True)
class CatalogActivatedAt:
    """The attributed activation instant of one catalog event, RFC 3339 UTC."""

    value: str

    def __post_init__(self) -> None:
        if not isinstance(self.value, str):
            raise TypeError("a catalog event time must be text")
        matched = _CATALOG_ACTIVATED_AT.fullmatch(self.value)
        if matched is None:
            raise ValueError(
                "a catalog event time must be RFC 3339 UTC at second precision"
            )
        try:
            datetime(
                int(matched[1]),
                int(matched[2]),
                int(matched[3]),
                int(matched[4]),
                int(matched[5]),
                int(matched[6]),
                tzinfo=UTC,
            )
        except ValueError:
            raise ValueError(
                "a catalog event time must be RFC 3339 UTC at second precision"
            ) from None


class CatalogRetirementState(StrEnum):
    """The closed set of retirement-history tokens. `retired` is today's only one."""

    RETIRED = "retired"


type CatalogLineageQuery = CatalogLineageId | CatalogLineageDisplayName
"""How an author names one lineage: by its id, or by the name it carries.

The union of two contract types is a contract concept. It lived in the port
that first answered such a query, which made every caller naming an authoring
query name a port -- including the API's own use-case record, where that is the
evasion `check_architecture.py` reads annotations for.
"""


def catalog_lineage_query(
    value: str,
) -> CatalogLineageQuery:
    """Split an authoring query by the ADR 0007 syntactic rule.

    A 64-hex input is a lineage id and can be nothing else; every other input is
    a display name. The name type itself refuses a 64-hex string, so the two
    readings cannot collide.
    """

    if not isinstance(value, str):
        raise TypeError("a catalog name query must be text")
    if SHA256_HEX_DIGEST.fullmatch(value) is not None:
        return CatalogLineageId(value)
    return CatalogLineageDisplayName(value)


@dataclass(frozen=True)
class CatalogLineage:
    """The immutable identity-bearing root of one revision lineage."""

    kind: RevisionKind
    founding_revision_hash: PublishedRevisionHash
    lineage_id: CatalogLineageId = field(init=False)

    def __post_init__(self) -> None:
        if not isinstance(self.kind, RevisionKind):
            raise TypeError("a catalog lineage names its kind through the contract")
        if not isinstance(self.founding_revision_hash, PublishedRevisionHash):
            raise TypeError(
                "a catalog lineage names its founding revision through the contract"
            )
        object.__setattr__(
            self,
            "lineage_id",
            CatalogLineageId.of(
                frame(
                    "catalog-lineage/v1",
                    self.kind.value.encode("ascii"),
                    self.founding_revision_hash.value.encode("ascii"),
                )
            ),
        )


@dataclass(frozen=True)
class CatalogLineageFounded:
    lineage: CatalogLineage
    revision: PublishedRevision
    display_name: CatalogLineageDisplayName


@dataclass(frozen=True)
class CatalogMemberAdmitted:
    lineage: CatalogLineage
    revision: PublishedRevision
    revision_number: int
    display_name: CatalogLineageDisplayName


@dataclass(frozen=True)
class CatalogAdmissionExisting:
    lineage: CatalogLineage
    revision: PublishedRevision
    revision_number: int
    display_name: CatalogLineageDisplayName


@dataclass(frozen=True)
class CatalogLineageIdMismatch:
    claimed: CatalogLineageId
    derived: CatalogLineageId


@dataclass(frozen=True)
class CatalogAdmissionUnpublished:
    revision_hash: PublishedRevisionHash


@dataclass(frozen=True)
class CatalogAdmissionNameHeld:
    name: CatalogLineageDisplayName
    holder: CatalogLineageId


@dataclass(frozen=True)
class CatalogAdmissionRevisionOwned:
    revision_hash: PublishedRevisionHash
    owner: CatalogLineageId


@dataclass(frozen=True)
class CatalogAdmissionRetired:
    lineage_id: CatalogLineageId


@dataclass(frozen=True)
class CatalogAdmissionLineageMissing:
    lineage_id: CatalogLineageId


@dataclass(frozen=True)
class CatalogLineageRetired:
    lineage_id: CatalogLineageId


@dataclass(frozen=True)
class CatalogRetirementExisting:
    lineage_id: CatalogLineageId


@dataclass(frozen=True)
class CatalogAdmissionKindMismatch:
    lineage_id: CatalogLineageId
    expected: RevisionKind
    actual: RevisionKind
