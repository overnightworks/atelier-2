from __future__ import annotations

import base64
import binascii
import re
from dataclasses import dataclass
from typing import Annotated

from fastapi import Path, Query

from atelier2.contracts.agents import (
    MAXIMUM_AGENT_OUTPUT_BYTES_V2,
    PROVIDER_PROBE_TOKEN,
)
from atelier2.contracts.definition_sources import (
    MAXIMUM_GIT_OBJECT_NAME_CHARACTERS,
    MINIMUM_GIT_OBJECT_NAME_CHARACTERS,
    DefinitionSourceId,
)
from atelier2.contracts.hashing import SHA256_HEX_DIGEST
from atelier2.contracts.host_configuration import (
    MAXIMUM_PROJECT_ID_CHARACTERS,
    ProjectId,
    ProjectSourceId,
    ProjectUnknown,
)
from atelier2.contracts.pages import MAXIMUM_PAGE_ITEMS
from atelier2.contracts.runs import RunId, WorkflowRevisionHash
from atelier2.contracts.secret_redaction import maximum_redacted_length

MAX_SIGNED_INT64 = 9_223_372_036_854_775_807
# The wire's own bound: no durable owner caps how many roles one run binds, so
# the edge decides it, once, rather than twice in two request shapes.
MAXIMUM_RUN_AGENT_BINDINGS = 100
# The wire's own bound: a published V3 node preview carries a start of the
# authored instruction, never the instruction. The durable owner bounds the
# whole instruction in UTF-8 bytes; this glance is a character count the
# edge decides once, so two shapes cannot pick two lengths.
MAXIMUM_NODE_INSTRUCTION_PREVIEW_CHARACTERS = 120
# The wire's own bound: no durable owner caps how many orders one run can be
# started with, so the read edge decides it, once -- an admitted projection
# that somehow carries more is a refusal here rather than an unbounded page.
MAXIMUM_RUN_ORDERS = 100
# Wire-owned: a validation loc and its reason have no durable owner, so the
# problem object decides the glance once.
MAXIMUM_INVALID_FIELD_PATH_CHARACTERS = 256
MAXIMUM_INVALID_FIELD_REASON_CHARACTERS = 512
# The wire's own default: no durable owner picks how many items an unspecified
# page holds, so the query parameter decides it, once, at the size every
# listing already served before this bound was named.
DEFAULT_PAGE_LIMIT = 50
PageLimitParameter = Annotated[int, Query(ge=1, le=MAXIMUM_PAGE_ITEMS)]
"""How many items a page-bounded listing may be asked to return.

`MAXIMUM_PAGE_ITEMS` is `PageLimit`'s own upper bound (`contracts.pages`); a
lower request is honoured as asked, and FastAPI refuses an out-of-range or
non-integer value itself (`invalid-request`, the same code and status a
route's own parsing used to raise for the identical refusal).
"""


def base64_characters_for(payload_bytes: int) -> int:
    """The exact base64 length of a payload of this many bytes.

    Base64 encodes three source bytes as four characters and pads the final
    group, so a payload occupies four characters per started group of three:
    4 * ceil(payload_bytes / 3), in exact integer arithmetic. Lives here, not
    beside `ApiLimits` in `api.limits`, because a wire schema (`api.wire.resources`)
    needs it to state a field bound too, and `api.limits` already depends on
    `api.wire.resources` through `api.problems` -- naming it on that side would
    close a cycle rather than share one fact.
    """
    started_groups_of_three = (payload_bytes + 2) // 3
    return 4 * started_groups_of_three


MAXIMUM_REFUSED_OUTPUT_BASE64_CHARACTERS = base64_characters_for(
    maximum_redacted_length(MAXIMUM_AGENT_OUTPUT_BYTES_V2)
)
"""The wire's own name for a bound `NodeDetail.refusal_output` already keeps.

Only a V3 agent node's own schema-refused output ever reaches that field
(#664), and every executor adapter already refuses to hand the domain more
than `MAXIMUM_AGENT_OUTPUT_BYTES_V2` bytes before any schema judgment even
happens -- so the byte count this rests on is not a new limit, it is that
existing invariant. What travels on the wire is not those exact bytes, though:
`queries.py` redacts credential shapes out of them first (#664), and the
redaction owner's own `maximum_redacted_length` names how much longer that can
ever make them -- so this bound is the agent output cap *after* the one
transform this field's value is guaranteed to have been through, restated in
the encoding the browser reads it under, once, so the Pydantic resource and
its Zod mirror cannot each pick a different one.
"""

MAXIMUM_RUN_TERMINAL_ANSWER_BYTES = MAXIMUM_AGENT_OUTPUT_BYTES_V2
"""The run list's own byte bound on the terminal `answer` a row may carry.

`NodeDetail.answer` stays unbounded on the single-node route (#238: it is
served for every answer-bearing node kind, agent, wait, action, subworkflow,
which share no one byte bound) -- but `RunResourceV3.answer` (#1045) is
served on every listed run, repeated once per row, so it needs its own. This
reuses the agent output cap every executor already holds rather than
inventing a second number: `run_resource` never asks for a value larger than
this, so it is the honest ceiling to name here too, and a projected answer
over it is nulled, never truncated mid-byte (see `run_resource`).
"""
MAXIMUM_RUN_TERMINAL_ANSWER_BASE64_CHARACTERS = base64_characters_for(
    MAXIMUM_RUN_TERMINAL_ANSWER_BYTES
)
SHA256_HASH_PATTERN = f"^{SHA256_HEX_DIGEST.pattern}$"
ArtifactHashPathParameter = Annotated[
    str, Path(json_schema_extra={"pattern": SHA256_HASH_PATTERN})
]
AgentAttemptIdPathParameter = Annotated[
    str, Path(json_schema_extra={"pattern": SHA256_HASH_PATTERN})
]
REVISION_HASH_PATTERN = SHA256_HASH_PATTERN
RevisionHashPathParameter = Annotated[
    str, Path(json_schema_extra={"pattern": REVISION_HASH_PATTERN})
]
RevisionHashQueryParameter = Annotated[
    str, Query(json_schema_extra={"pattern": REVISION_HASH_PATTERN})
]
CATALOG_LINEAGE_ID_PATTERN = SHA256_HASH_PATTERN
PROVIDER_PROBE_PROBLEM_CODE_PATTERN = f"^{PROVIDER_PROBE_TOKEN.pattern}$"
SOURCE_COMMIT_PATTERN = (
    f"^[0-9a-f]{{{MINIMUM_GIT_OBJECT_NAME_CHARACTERS},"
    f"{MAXIMUM_GIT_OBJECT_NAME_CHARACTERS}}}$"
)
"""A git object name as its durable owner bounds it: SHA-1 or SHA-256, lowercase."""
PUBLIC_RUN_REFERENCE_PATTERN = r"^run1\.[A-Za-z0-9_-]+$"
PublicRunReferencePathParameter = Annotated[
    str, Path(json_schema_extra={"pattern": PUBLIC_RUN_REFERENCE_PATTERN})
]
PublicRunReferenceQueryParameter = Annotated[
    str, Query(json_schema_extra={"pattern": PUBLIC_RUN_REFERENCE_PATTERN})
]
PUBLIC_PROJECT_REFERENCE_PATTERN = r"^project1\.[A-Za-z0-9_-]+$"
PUBLIC_SOURCE_REFERENCE_PATTERN = r"^source1\.[A-Za-z0-9_-]+$"
EVENT_CURSOR_PATTERN = r"^event1\.[A-Za-z0-9_-]+\.[1-9][0-9]*$"
_PUBLIC_REFERENCE_PREFIX = "run1."
_PUBLIC_PROJECT_REFERENCE_PREFIX = "project1."
_PUBLIC_SOURCE_REFERENCE_PREFIX = "source1."
_EVENT_CURSOR_PREFIX = "event1."
_UNPADDED_BASE64URL = re.compile(r"[A-Za-z0-9_-]+")
_POSITIVE_DECIMAL = re.compile(r"[1-9][0-9]*")


class InvalidPublicRunReference(ValueError):
    """A public run reference is malformed or not canonically encoded."""


class InvalidPublicProjectReference(ValueError):
    """A public project reference is malformed or not canonically encoded."""


class InvalidPublicSourceReference(ValueError):
    """A public source reference is malformed or not canonically encoded."""


class InvalidEventCursor(ValueError):
    """An event cursor is malformed or outside the durable sequence range."""


class InvalidRevisionHash(ValueError):
    """A workflow revision hash is not an exact lowercase SHA-256 digest."""


@dataclass(frozen=True)
class EventCursor:
    run_id: RunId
    sequence: int

    def __post_init__(self) -> None:
        if type(self.sequence) is not int or not 1 <= self.sequence <= MAX_SIGNED_INT64:
            raise InvalidEventCursor("event sequence must be a positive signed int64")


def encode_public_run_reference(run_id: RunId) -> str:
    try:
        payload = run_id.value.encode("utf-8")
    except UnicodeEncodeError as error:
        raise InvalidPublicRunReference("run id is not exact UTF-8") from error
    encoded = base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")
    return _PUBLIC_REFERENCE_PREFIX + encoded


def decode_public_run_reference(reference: str) -> RunId:
    if not reference.startswith(_PUBLIC_REFERENCE_PREFIX):
        raise InvalidPublicRunReference("public run reference has the wrong version")
    encoded = reference.removeprefix(_PUBLIC_REFERENCE_PREFIX)
    if _UNPADDED_BASE64URL.fullmatch(encoded) is None:
        raise InvalidPublicRunReference(
            "public run reference is not canonical base64url"
        )
    try:
        payload = base64.b64decode(
            encoded + "=" * (-len(encoded) % 4), altchars=b"-_", validate=True
        )
        run_id = RunId(payload.decode("utf-8"))
    except (binascii.Error, UnicodeDecodeError, ValueError) as error:
        raise InvalidPublicRunReference(
            "public run reference has invalid payload"
        ) from error
    if encode_public_run_reference(run_id) != reference:
        raise InvalidPublicRunReference("public run reference is not canonical")
    return run_id


def encode_public_project_reference(project_id: ProjectId) -> str:
    payload = project_id.value.encode("utf-8")
    encoded = base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")
    return _PUBLIC_PROJECT_REFERENCE_PREFIX + encoded


def encode_public_source_reference(source_id: ProjectSourceId) -> str:
    encoded = base64.urlsafe_b64encode(source_id.value.encode("ascii")).decode("ascii")
    return _PUBLIC_SOURCE_REFERENCE_PREFIX + encoded.rstrip("=")


MAXIMUM_PUBLIC_SOURCE_REFERENCE_CHARACTERS = len(
    encode_public_source_reference(
        ProjectSourceId("ffffffff-ffff-ffff-ffff-ffffffffffff")
    )
)
PublicSourceReferencePathParameter = Annotated[
    str,
    Path(
        json_schema_extra={
            "pattern": PUBLIC_SOURCE_REFERENCE_PATTERN,
            "maxLength": MAXIMUM_PUBLIC_SOURCE_REFERENCE_CHARACTERS,
        }
    ),
]


def encode_public_definition_source_reference(source_id: DefinitionSourceId) -> str:
    """The reference a reader is given for a registered definition source.

    The `source1.` shape a project source connection already answers with,
    because both name "a source this instance is wired to" to the same reader,
    and the durable id stays off the wire either way. The two payloads cannot
    be confused -- a project source id is a UUID, a definition source id a
    SHA-256 digest -- and no decoder joins this one because no request names a
    definition source: the command line takes the durable id, and the wire only
    ever answers with this.
    """

    encoded = base64.urlsafe_b64encode(source_id.value.encode("ascii")).decode("ascii")
    return _PUBLIC_SOURCE_REFERENCE_PREFIX + encoded.rstrip("=")


MAXIMUM_PUBLIC_DEFINITION_SOURCE_REFERENCE_CHARACTERS = len(
    encode_public_definition_source_reference(DefinitionSourceId("f" * 64))
)


def decode_public_source_reference(reference: str) -> ProjectSourceId:
    if len(reference) > MAXIMUM_PUBLIC_SOURCE_REFERENCE_CHARACTERS:
        raise InvalidPublicSourceReference("public source reference is too long")
    if not reference.startswith(_PUBLIC_SOURCE_REFERENCE_PREFIX):
        raise InvalidPublicSourceReference(
            "public source reference has the wrong version"
        )
    encoded = reference.removeprefix(_PUBLIC_SOURCE_REFERENCE_PREFIX)
    if _UNPADDED_BASE64URL.fullmatch(encoded) is None:
        raise InvalidPublicSourceReference(
            "public source reference is not canonical base64url"
        )
    try:
        payload = base64.b64decode(
            encoded + "=" * (-len(encoded) % 4), altchars=b"-_", validate=True
        )
        source_id = ProjectSourceId(payload.decode("ascii"))
    except (binascii.Error, UnicodeDecodeError, ValueError) as error:
        raise InvalidPublicSourceReference(
            "public source reference has invalid payload"
        ) from error
    if encode_public_source_reference(source_id) != reference:
        raise InvalidPublicSourceReference("public source reference is not canonical")
    return source_id


# Longest project1. encoding of a UTF-8-encodable ProjectId at the durable
# character bound. One four-byte scalar is the widest UTF-8 a Python character
# can occupy, so this is the bound the codec, project doors, and OpenAPI
# must share.
MAXIMUM_PUBLIC_PROJECT_REFERENCE_CHARACTERS = len(
    encode_public_project_reference(
        ProjectId("\U00010000" * MAXIMUM_PROJECT_ID_CHARACTERS)
    )
)
PublicProjectReferencePathParameter = Annotated[
    str,
    Path(
        json_schema_extra={
            "pattern": PUBLIC_PROJECT_REFERENCE_PATTERN,
            "maxLength": MAXIMUM_PUBLIC_PROJECT_REFERENCE_CHARACTERS,
        }
    ),
]


def decode_public_project_reference(reference: str) -> ProjectId:
    if len(reference) > MAXIMUM_PUBLIC_PROJECT_REFERENCE_CHARACTERS:
        raise InvalidPublicProjectReference(
            "public project reference exceeds its character limit"
        )
    if not reference.startswith(_PUBLIC_PROJECT_REFERENCE_PREFIX):
        raise InvalidPublicProjectReference(
            "public project reference has the wrong version"
        )
    encoded = reference.removeprefix(_PUBLIC_PROJECT_REFERENCE_PREFIX)
    if _UNPADDED_BASE64URL.fullmatch(encoded) is None:
        raise InvalidPublicProjectReference(
            "public project reference is not canonical base64url"
        )
    try:
        payload = base64.b64decode(
            encoded + "=" * (-len(encoded) % 4), altchars=b"-_", validate=True
        )
        project_id = ProjectId(payload.decode("utf-8"))
    except (binascii.Error, UnicodeDecodeError, ProjectUnknown, ValueError) as error:
        raise InvalidPublicProjectReference(
            "public project reference has invalid payload"
        ) from error
    if encode_public_project_reference(project_id) != reference:
        raise InvalidPublicProjectReference("public project reference is not canonical")
    return project_id


def encode_event_cursor(run_id: RunId, sequence: int) -> str:
    cursor = EventCursor(run_id, sequence)
    encoded_run_id = encode_public_run_reference(cursor.run_id).removeprefix(
        _PUBLIC_REFERENCE_PREFIX
    )
    return f"{_EVENT_CURSOR_PREFIX}{encoded_run_id}.{cursor.sequence}"


def parse_event_cursor(value: str) -> EventCursor:
    if not value.startswith(_EVENT_CURSOR_PREFIX):
        raise InvalidEventCursor("event cursor has the wrong version")
    body = value.removeprefix(_EVENT_CURSOR_PREFIX)
    try:
        encoded_run_id, encoded_sequence = body.rsplit(".", maxsplit=1)
    except ValueError as error:
        raise InvalidEventCursor("event cursor fields are missing") from error
    if _POSITIVE_DECIMAL.fullmatch(encoded_sequence) is None:
        raise InvalidEventCursor("event cursor sequence is not canonical")
    sequence = int(encoded_sequence)
    if sequence > MAX_SIGNED_INT64:
        raise InvalidEventCursor("event cursor sequence exceeds signed int64")
    try:
        run_id = decode_public_run_reference(_PUBLIC_REFERENCE_PREFIX + encoded_run_id)
    except InvalidPublicRunReference as error:
        raise InvalidEventCursor("event cursor run id is invalid") from error
    cursor = EventCursor(run_id, sequence)
    if encode_event_cursor(cursor.run_id, cursor.sequence) != value:
        raise InvalidEventCursor("event cursor is not canonical")
    return cursor


def parse_revision_hash(value: str) -> WorkflowRevisionHash:
    if SHA256_HEX_DIGEST.fullmatch(value) is None:
        raise InvalidRevisionHash("revision hash must be lowercase SHA-256")
    return WorkflowRevisionHash(value)


def decode_canonical_base64(value: str) -> bytes:
    if any(character.isspace() for character in value):
        raise ValueError("base64 must contain no whitespace")
    try:
        decoded = base64.b64decode(value, validate=True)
    except (ValueError, binascii.Error) as error:
        raise ValueError("base64 must use the standard alphabet and padding") from error
    if base64.b64encode(decoded).decode("ascii") != value:
        raise ValueError("base64 encoding is not canonical")
    return decoded


def encode_canonical_base64(value: bytes) -> str:
    return base64.b64encode(value).decode("ascii")
