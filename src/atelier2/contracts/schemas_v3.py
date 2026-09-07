"""What a document published as a `schema` revision must be to mean anything.

A V3 document binds a node output to `outputs[*].schema`, and the resolution of
that reference proves the registry carries the pinned revision. It cannot prove
the revision says anything: bytes published under the name `schema` are a schema
only because someone called them one. This module is the proof that was missing,
and it is deliberately a pure function over bytes so that both the reference
resolution and the instance evaluation ask exactly one owner. Both halves live
here: `read_schema_document` says whether published bytes are a schema this
product can enforce, and `read_instance_document` says whether exact bytes are a
value one of those schemas admits -- always read as JSON, because a produced
value already comes back under a promise its own executor made.
`read_authored_instance_document` asks the same question of a value a caller
authored instead of one an executor produced: it reads bytes as raw text
rather than a second JSON encoding when the schema's own top-level `type` is
`"string"`, through `instance_for_schema`, the one owner of that decision.

The profile is a closed subset of JSON Schema Draft 2020-12, and every bound in
it exists to keep evaluation decidable, local and cheap:

- the document is bounded in bytes, in container depth and in value count, so a
  published revision cannot cost unbounded work to read;
- `$id`, `$anchor`, `$dynamicAnchor` and `$dynamicRef` are refused, because each
  one moves the meaning of a reference somewhere the pinned revision hash does
  not cover;
- a `$ref` is local **and resolvable**, or it is refused: what a schema means
  travels with its own bytes, never over the network, and never to a target the
  document does not carry;
- a reference cycle no instance can break is refused, while an ordinary
  recursive schema is not — the rule is whether the cycle passes through an
  applicator that descends into the instance, because that is what makes the
  recursion end. `{"$ref": "#"}` alone never ends; a tree whose child is
  `{"$ref": "#"}` under `properties` always does;
- `$schema` is absent or exactly Draft 2020-12, so a document announcing another
  dialect is refused rather than silently read under rules it never asked for;
- `format` stays the draft's annotation and is never an assertion, so a schema
  that needs format validation for safety is not expressible here rather than
  silently unenforced.

Resolvability and termination are decided **here, over the published bytes**,
and never left to the evaluator: a document that passes this profile and then
raises `NoSuchAnchor`, `PointerToNowhere` or `RecursionError` would not be a
refusal but an outage, which is the defect this profile's first version carried.

Retrieval is off by construction rather than by trust: every evaluation runs
against a registry whose only retrieval path is `refuse_retrieval`, which raises.
The forbidden-vocabulary scan runs first, so a document naming a non-local
reference is refused by name before any evaluator sees it and nothing ever
reaches that seam.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from math import gcd
from typing import NoReturn

import jsonschema.validators
from jsonschema import Draft202012Validator, TypeChecker
from jsonschema.exceptions import ValidationError
from jsonschema.protocols import Validator
from jsonschema_specifications import REGISTRY as SPECIFICATION_REGISTRY
from referencing import Registry
from referencing.jsonschema import Schema

MAXIMUM_SCHEMA_DOCUMENT_BYTES = 16_384
# One value's own bound. It is the same number as a schema's today and still its
# own decision: a value is bounded because a run must not be asked to carry an
# order nobody can read, a schema because a published revision must not cost
# unbounded work to read.
#
# What this height protects is the *inline* route and only that: a value written
# into a start request is copied through the wire body, the request record and
# the durable order row, so it is held several times over before anything reads
# it, and it is the one route a caller can flood without publishing anything
# first. Sixteen kilobytes is what a hand-written order is; material larger than
# that is published as an artifact and ordered by its address
# (`atelier2.contracts.artifacts`), which is why this bound stays strict instead
# of growing with the largest payload anybody has.
MAXIMUM_INSTANCE_DOCUMENT_BYTES = 16_384
MAXIMUM_SCHEMA_CONTAINER_DEPTH = 32
MAXIMUM_SCHEMA_VALUES = 4_096

FORBIDDEN_KEYWORDS = ("$id", "$anchor", "$dynamicAnchor", "$dynamicRef")
SUPPORTED_DIALECT = "https://json-schema.org/draft/2020-12/schema"
_LOCAL_REFERENCE_PREFIX = "#"
_BYTE_ORDER_MARK = "﻿"

# Keywords whose subschemas are applied to the *same* instance the parent saw.
# A reference cycle made only of these never consumes input, so it cannot end;
# every other applicator descends into a strictly smaller instance and is what
# makes an ordinary recursive schema terminate.
_IN_PLACE_SUBSCHEMAS = ("not", "if", "then", "else")
_IN_PLACE_SUBSCHEMA_LISTS = ("allOf", "anyOf", "oneOf")
_IN_PLACE_SUBSCHEMA_MAPS = ("dependentSchemas",)

# What `json.loads` produces, written down once so both halves can say it. The
# decoder refuses every non-canonical literal, so nothing outside this shape
# reaches a caller.
type JsonValue = (
    None | bool | int | Decimal | str | list["JsonValue"] | dict[str, "JsonValue"]
)
type _ExactNumber = int | Decimal


def _is_integer(checker: TypeChecker, instance: object) -> bool:
    if isinstance(instance, bool):
        return False
    if isinstance(instance, int):
        return True
    return isinstance(instance, Decimal) and instance == instance.to_integral_value()


def _number_parts(value: _ExactNumber) -> tuple[int, int]:
    """The finite coefficient and exponent, without expanding the exponent."""
    if isinstance(value, int):
        return abs(value), 0
    decomposed = value.as_tuple()
    if not isinstance(decomposed.exponent, int):
        raise TypeError("a JSON number must be finite")
    coefficient = 0
    for digit in decomposed.digits:
        coefficient = coefficient * 10 + digit
    return coefficient, decomposed.exponent


def _without_factor(value: int, factor: int, limit: int) -> tuple[int, int]:
    """Remove one prime at most `limit` times, stopping when it is absent."""
    removed = 0
    while removed < limit:
        quotient, remainder = divmod(value, factor)
        if remainder != 0:
            break
        value = quotient
        removed += 1
    return value, removed


def _is_exact_multiple(instance: _ExactNumber, divisor: _ExactNumber) -> bool:
    """Base-10 divisibility bounded by coefficient digits, not exponent size."""
    numerator, numerator_exponent = _number_parts(instance)
    denominator, denominator_exponent = _number_parts(divisor)
    if numerator == 0:
        return denominator != 0
    if denominator == 0:
        return False

    common = gcd(numerator, denominator)
    numerator //= common
    denominator //= common
    exponent_delta = numerator_exponent - denominator_exponent
    if exponent_delta >= 0:
        for factor in (2, 5):
            denominator, _ = _without_factor(denominator, factor, exponent_delta)
        return denominator == 1

    if denominator != 1:
        return False
    required = -exponent_delta
    for factor in (2, 5):
        numerator, removed = _without_factor(numerator, factor, required)
        if removed != required:
            return False
    return True


def _validate_exact_multiple_of(
    _validator: Validator, divisor: JsonValue, instance: JsonValue, _schema: Schema
) -> Iterator[ValidationError]:
    """The draft's `multipleOf` decision for this exact-number vocabulary."""
    if isinstance(instance, bool) or not isinstance(instance, (int, Decimal)):
        return
    if isinstance(divisor, bool) or not isinstance(divisor, (int, Decimal)):
        raise TypeError("an accepted multipleOf divisor must be an exact number")
    if not _is_exact_multiple(instance, divisor):
        yield ValidationError(f"{instance!r} is not a multiple of {divisor}")


# Numbers are decoded exactly rather than through float64, because two JSON
# numbers that differ must stay different: a value whose hash is stored and whose
# schema is enforced cannot be collapsed onto the nearest double. The evaluator
# is taught the same vocabulary so `number` and `integer` still mean what the
# draft says they mean.
_EXACT_NUMBER_TYPES = Draft202012Validator.TYPE_CHECKER.redefine("integer", _is_integer)
_ExactNumberValidator = jsonschema.validators.extend(
    Draft202012Validator,
    validators={"multipleOf": _validate_exact_multiple_of},
    type_checker=_EXACT_NUMBER_TYPES,
)


class SchemaDocumentRefusal(StrEnum):
    """Every named way a published `schema` revision fails this closed profile."""

    DOCUMENT_TOO_LARGE = "document-too-large"
    DOCUMENT_NOT_UTF8 = "document-not-utf8"
    DOCUMENT_CARRIES_BYTE_ORDER_MARK = "document-carries-byte-order-mark"
    DOCUMENT_NOT_JSON = "document-not-json"
    NON_CANONICAL_NUMBER = "non-canonical-number"
    DUPLICATE_OBJECT_KEY = "duplicate-object-key"
    DOCUMENT_TOO_DEEP = "document-too-deep"
    TOO_MANY_VALUES = "too-many-values"
    FORBIDDEN_KEYWORD = "forbidden-keyword"
    NONLOCAL_REFERENCE = "nonlocal-reference"
    UNRESOLVABLE_REFERENCE = "unresolvable-reference"
    NON_TERMINATING_REFERENCE_CYCLE = "non-terminating-reference-cycle"
    UNSUPPORTED_DIALECT = "unsupported-dialect"
    NOT_A_SCHEMA = "not-a-schema"


class InstanceRefusal(StrEnum):
    """Every named way exact bytes fail to be a value one schema admits."""

    INSTANCE_TOO_LARGE = "instance-too-large"
    INSTANCE_NOT_UTF8 = "instance-not-utf8"
    INSTANCE_CARRIES_BYTE_ORDER_MARK = "instance-carries-byte-order-mark"
    INSTANCE_NOT_JSON = "instance-not-json"
    NON_CANONICAL_NUMBER = "non-canonical-number"
    DUPLICATE_OBJECT_KEY = "duplicate-object-key"
    INSTANCE_TOO_DEEP = "instance-too-deep"
    TOO_MANY_VALUES = "too-many-values"
    SCHEMA_VIOLATED = "schema-violated"


@dataclass(frozen=True, slots=True)
class SchemaAccepted:
    """The document is a Draft 2020-12 schema inside this profile."""

    schema: Schema


@dataclass(frozen=True, slots=True)
class SchemaRefused:
    """One named refusal, with the exact thing it is about when there is one."""

    refusal: SchemaDocumentRefusal
    subject: str | None = None

    def __str__(self) -> str:
        named = "" if self.subject is None else f": {self.subject}"
        return f"{self.refusal.value}{named}"


type SchemaDocumentVerdict = SchemaAccepted | SchemaRefused


@dataclass(frozen=True, slots=True)
class InstanceAccepted:
    """The bytes are a value the schema admits, decoded once for the caller."""

    value: JsonValue


@dataclass(frozen=True, slots=True)
class InstanceSchemaViolation:
    """Where one `SCHEMA_VIOLATED` refusal is about, and why -- for a caller that
    points an author at a field rather than reading `subject`'s prose.

    `pointer` is the RFC 6901 pointer `_first_violation` already located the
    violation at, or `None` when the earliest violation is not about one
    addressable field -- a `required` property missing at the value's own root
    has no pointer, because RFC 6901 has none for a key that does not exist.
    """

    pointer: str | None
    reason: str


@dataclass(frozen=True, slots=True)
class InstanceRefused:
    """One named refusal, with the exact thing it is about when there is one."""

    refusal: InstanceRefusal
    subject: str | None = None
    violation: InstanceSchemaViolation | None = None

    def __str__(self) -> str:
        named = "" if self.subject is None else f": {self.subject}"
        return f"{self.refusal.value}{named}"


type InstanceVerdict = InstanceAccepted | InstanceRefused


class SchemaRetrievalAttempted(Exception):
    """A schema evaluation reached for a resource outside its own bytes."""


def refuse_retrieval(uri: str) -> NoReturn:
    """The only retrieval path any schema evaluation has, and it never returns."""
    raise SchemaRetrievalAttempted(uri)


def schema_registry() -> Registry[Schema]:
    """The bundled specifications, and nothing else reachable."""
    return SPECIFICATION_REGISTRY.combine(Registry(retrieve=refuse_retrieval))


def read_schema_document(document: bytes) -> SchemaDocumentVerdict:
    """Whether these exact published bytes are a schema this product can enforce."""
    if len(document) > MAXIMUM_SCHEMA_DOCUMENT_BYTES:
        return SchemaRefused(
            SchemaDocumentRefusal.DOCUMENT_TOO_LARGE,
            f"{len(document)} bytes exceeds {MAXIMUM_SCHEMA_DOCUMENT_BYTES}",
        )
    try:
        text = document.decode("utf-8")
    except UnicodeDecodeError as broken:
        return SchemaRefused(SchemaDocumentRefusal.DOCUMENT_NOT_UTF8, broken.reason)
    if text.startswith(_BYTE_ORDER_MARK):
        return SchemaRefused(SchemaDocumentRefusal.DOCUMENT_CARRIES_BYTE_ORDER_MARK)
    decoded = _decoded_json(text)
    if isinstance(decoded, SchemaRefused):
        return decoded
    # The cheap bounds and the closed vocabulary run before anything reads the
    # document as a schema, so hostile input is refused by size and by keyword
    # rather than by an evaluator walking it.
    scanned = _scanned_vocabulary(decoded.value)
    if scanned is not None:
        return scanned
    if not isinstance(decoded.value, (bool, dict)):
        return SchemaRefused(
            SchemaDocumentRefusal.NOT_A_SCHEMA,
            f"a schema is an object or a boolean, not {type(decoded.value).__name__}",
        )
    unsupported = _refused_dialect(decoded.value)
    if unsupported is not None:
        return unsupported
    unusable = _refused_reference_graph(decoded.value)
    if unusable is not None:
        return unusable
    return _validated_against_the_draft(decoded.value)


def read_instance_document(
    instance: bytes,
    schema: SchemaAccepted,
    maximum_bytes: int = MAXIMUM_INSTANCE_DOCUMENT_BYTES,
) -> InstanceVerdict:
    """Whether these exact bytes are a value that accepted schema admits.

    It takes an accepted schema rather than bytes on purpose: evaluating against
    a revision nobody vetted is the outage this module exists to prevent, and a
    caller that cannot name an accepted schema has nothing to evaluate against.

    The order is the schema half's, for the schema half's reasons: the byte
    bound first, then decoding, then the depth and value bounds, and only then
    the evaluation. Hostile input costs a length check rather than a walk, and
    nothing reaches the evaluator that has not already been measured.

    `maximum_bytes` is the caller's, because the byte bound belongs to the route
    the value arrived by rather than to this evaluation: a value written inline
    is bounded at `MAXIMUM_INSTANCE_DOCUMENT_BYTES`, one published as an artifact
    at `MAXIMUM_ARTIFACT_BYTES`, and a caller that has already refused past its
    own door would otherwise have to refuse a second time under a bound it does
    not own. What keeps the evaluation itself decidable is the depth and value
    count below, which no caller may raise.
    """
    if len(instance) > maximum_bytes:
        return InstanceRefused(
            InstanceRefusal.INSTANCE_TOO_LARGE,
            f"{len(instance)} bytes exceeds {maximum_bytes}",
        )
    text = _decoded_text(instance)
    if isinstance(text, InstanceRefused):
        return text
    decoded = _decoded_json(text)
    if isinstance(decoded, SchemaRefused):
        return _as_instance_refusal(decoded)
    bounded = _bounded_instance(decoded.value)
    if bounded is not None:
        return bounded
    violation = _first_violation(decoded.value, schema.schema)
    if violation is not None:
        return violation
    return InstanceAccepted(decoded.value)


def read_authored_instance_document(
    instance: bytes,
    schema: SchemaAccepted,
    maximum_bytes: int = MAXIMUM_INSTANCE_DOCUMENT_BYTES,
) -> InstanceVerdict:
    """Whether these exact bytes are a value a caller authored, that the schema admits.

    A produced value (`read_instance_document`) always arrives JSON-encoded: it
    comes back from an executor whose own structured-output help already
    promised the schema it would honour. An authored value -- an order a start
    supplies, or an answer a person types at a wait -- carries no such promise,
    so this is the one door that lets a schema whose own top-level `type` is
    exactly `"string"` read the artifact's raw UTF-8 text as its value
    directly, rather than demanding it JSON-encoded a second time
    (`instance_for_schema` is the one decision). Every other schema still
    demands a JSON-encoded instance: an object or an array has no text of its
    own to be one.

    The bound, decode and bounds order otherwise matches `read_instance_document`
    for the same reasons; see that function's docstring.
    """
    if len(instance) > maximum_bytes:
        return InstanceRefused(
            InstanceRefusal.INSTANCE_TOO_LARGE,
            f"{len(instance)} bytes exceeds {maximum_bytes}",
        )
    decoded = instance_for_schema(instance, schema.schema)
    if isinstance(decoded, InstanceRefused):
        return decoded
    bounded = _bounded_instance(decoded)
    if bounded is not None:
        return bounded
    violation = _first_violation(decoded, schema.schema)
    if violation is not None:
        return violation
    return InstanceAccepted(decoded)


def instance_for_schema(instance: bytes, schema: Schema) -> JsonValue | InstanceRefused:
    """The one JSON value or plain text `instance`'s bytes are, judged by `schema`'s own top type.

    A schema whose top-level `type` is exactly `"string"` reads these bytes as
    the string directly: the published bytes ARE the value, so a
    JSON-encoded string arrives as a string that still carries its own
    quotes -- there is no second, compatibility-preserving reading, because
    Atelier is a prototype that owes no encoding it never promised (see
    `docs/PRODUCT.md`). Every other schema -- object, array, number, boolean,
    a nested schema, or one that names no top-level `type` at all -- keeps
    demanding a JSON-encoded instance, decoded by the same rules a schema
    document itself is decoded by: canonical numbers, unique keys.

    UTF-8 decoding and the byte-order-mark refusal are judged here rather than
    by a caller, because the `"string"` branch has no JSON decode step left to
    judge them.
    """
    text = _decoded_text(instance)
    if isinstance(text, InstanceRefused):
        return text
    if isinstance(schema, dict) and schema.get("type") == "string":
        return text
    decoded = _decoded_json(text)
    if isinstance(decoded, SchemaRefused):
        return _as_instance_refusal(decoded)
    return decoded.value


def _decoded_text(instance: bytes) -> str | InstanceRefused:
    """The exact UTF-8 text `instance`'s bytes are, or the refusal naming where they break.

    Shared by every instance reader that must decode bytes to text before doing
    anything else with them (`read_instance_document`, `instance_for_schema`):
    invalid UTF-8 names the exact byte offset it broke at
    (`UnicodeDecodeError.start`), because the ruled sentence for this refusal
    is the place, not only the reason -- a person cannot fix "invalid start
    byte" without knowing where in the artifact it is. A byte-order mark is
    refused by its own name, never silently stripped.
    """
    try:
        text = instance.decode("utf-8")
    except UnicodeDecodeError as broken:
        return InstanceRefused(
            InstanceRefusal.INSTANCE_NOT_UTF8,
            f"{broken.reason} at byte {broken.start}",
        )
    if text.startswith(_BYTE_ORDER_MARK):
        return InstanceRefused(InstanceRefusal.INSTANCE_CARRIES_BYTE_ORDER_MARK)
    return text


# Where a JSON document may begin. A declared value the profile admits is an
# object or an array, so these two characters are the only places an embedded
# one can start.
_JSON_DOCUMENT_OPENERS = "{["


def declared_instance_in_answer(
    answer: bytes,
    schema: SchemaAccepted,
    maximum_bytes: int = MAXIMUM_INSTANCE_DOCUMENT_BYTES,
) -> bytes | None:
    """The bytes of one free-text answer that are a value this schema admits.

    A provider asked in prose for JSON answers in prose: the same brief comes
    back bare, wrapped in a Markdown fence, or introduced by a sentence, and the
    first of those three is the only one `read_instance_document` accepts. That
    made an episode's success a coin flip (#663), so the question "which bytes
    of this answer are the declared value" gets an owner here, beside the rule
    that judges them, rather than a guess in each adapter.

    Exactly two places are looked in, in this order: the whole answer, and the
    one JSON document that starts at the answer's first opener. Both candidates
    are judged by `read_instance_document`, so extraction only proposes a span
    and the profile alone decides -- canonical numbers, unique keys, depth and
    value bounds included. Nothing is stripped, repaired or re-serialized: the
    answer is narrowed to bytes it really carries, or nothing is returned.

    Answering `None` rather than a refusal is deliberate. This says only that
    the answer carries no value the schema admits; naming *why* an output is
    refused belongs to the seam that records the refusal durably, and a second
    voice for it would let two owners disagree about the same bytes.

    `maximum_bytes` bounds the candidates, not the answer: an answer is prose
    around a value and is legitimately larger than the value it carries, so its
    own size stays bounded by the route it arrived on -- for a provider answer,
    the executor's durable output bound -- exactly as `read_instance_document`
    says about the same argument.
    """

    if isinstance(
        read_instance_document(answer, schema, maximum_bytes), InstanceAccepted
    ):
        return answer
    embedded = _first_json_document_in(answer)
    if embedded is None:
        return None
    if isinstance(
        read_instance_document(embedded, schema, maximum_bytes), InstanceAccepted
    ):
        return embedded
    return None


def _first_json_document_in(answer: bytes) -> bytes | None:
    """The one JSON document starting at this answer's first opener, if there is one.

    One decode at one offset, never a scan over every opener: a rule that tried
    each of them would read a different document out of the same answer as
    prose around it changed, and the cost would grow with the square of an
    answer's length.
    """

    try:
        text = answer.decode("utf-8")
    except UnicodeDecodeError:
        return None
    openers = [
        found
        for found in (text.find(opener) for opener in _JSON_DOCUMENT_OPENERS)
        if found >= 0
    ]
    if not openers:
        return None
    start = min(openers)
    try:
        _value, end = json.JSONDecoder().raw_decode(text, start)
    except json.JSONDecodeError:
        return None
    return text[start:end].encode("utf-8")


_DECODING_REFUSALS = {
    SchemaDocumentRefusal.DOCUMENT_NOT_JSON: InstanceRefusal.INSTANCE_NOT_JSON,
    SchemaDocumentRefusal.NON_CANONICAL_NUMBER: InstanceRefusal.NON_CANONICAL_NUMBER,
    SchemaDocumentRefusal.DUPLICATE_OBJECT_KEY: InstanceRefusal.DUPLICATE_OBJECT_KEY,
}


def _as_instance_refusal(refused: SchemaRefused) -> InstanceRefused:
    """The decoder is shared, so its three refusals arrive under instance names.

    One decoder for both halves means a value and a schema are read by the same
    rules -- canonical numbers, unique keys -- and a caller still hears which of
    the two it handed in.
    """
    return InstanceRefused(_DECODING_REFUSALS[refused.refusal], refused.subject)


def _bounded_instance(value: JsonValue) -> InstanceRefused | None:
    """Depth and value count, bounded exactly as the schema half bounds them.

    The two walk together during evaluation, so one unbounded side would make
    the pair unbounded; the bounds are therefore the same decision, taken once.
    """
    counted = 0
    pending: list[tuple[object, int]] = [(value, 1)]
    while pending:
        current, depth = pending.pop()
        counted += 1
        if counted > MAXIMUM_SCHEMA_VALUES:
            return InstanceRefused(
                InstanceRefusal.TOO_MANY_VALUES,
                f"more than {MAXIMUM_SCHEMA_VALUES} JSON values",
            )
        if not isinstance(current, (dict, list)):
            continue
        if depth > MAXIMUM_SCHEMA_CONTAINER_DEPTH:
            return InstanceRefused(
                InstanceRefusal.INSTANCE_TOO_DEEP,
                f"more than {MAXIMUM_SCHEMA_CONTAINER_DEPTH} container levels",
            )
        entries = current if isinstance(current, list) else list(current.values())
        pending.extend((entry, depth + 1) for entry in entries)
    return None


def _first_violation(value: JsonValue, schema: Schema) -> InstanceRefused | None:
    """The one violation this refusal names, chosen by a stated rule.

    The evaluator promises no order over its errors, so naming "the first" would
    hand the operator a different answer for the same bytes. The rule is the
    earliest place in the value, then the message, so one input always reads
    back one refusal.
    """
    validator = _ExactNumberValidator(schema, registry=schema_registry())
    errors = sorted(
        validator.iter_errors(value),
        key=lambda error: (_ordered_path(error.absolute_path), error.message),
    )
    if not errors:
        return None
    first = errors[0]
    located = _pointer(first.absolute_path)
    place = "the value itself" if located == "" else located
    return InstanceRefused(
        InstanceRefusal.SCHEMA_VIOLATED,
        f"{place}: {first.message}",
        InstanceSchemaViolation(None if located == "" else located, first.message),
    )


def _ordered_path(path: Iterable[object]) -> tuple[tuple[int, int, str], ...]:
    """A path order a reader expects: index 2 before index 10, keys by name.

    Ordering the parts as text puts `10` before `2`, so the refusal an operator
    reads first is not the earliest place in their value.
    """
    ordered: list[tuple[int, int, str]] = []
    for part in path:
        if isinstance(part, int) and not isinstance(part, bool):
            ordered.append((0, part, ""))
        else:
            ordered.append((1, 0, str(part)))
    return tuple(ordered)


def _pointer(path: Iterable[object]) -> str:
    """The place as an RFC 6901 JSON pointer, so a key can never be misread.

    A key containing `/` or `~` is ambiguous when the parts are simply joined;
    escaped, `a/b~c` reads as `a~1b~0c` and names exactly one place.
    """
    parts = [str(part).replace("~", "~0").replace("/", "~1") for part in path]
    return "".join(f"/{part}" for part in parts)


def _refused_dialect(schema: Schema) -> SchemaRefused | None:
    """A document declaring another dialect is refused rather than reinterpreted.

    The evaluator is pinned to Draft 2020-12, so a document announcing draft-07
    would be read under rules it never asked for — the divergence is silent
    exactly where `$ref` siblings behave differently between the drafts. An
    absent `$schema` is the draft's own default and stays accepted.
    """
    if not isinstance(schema, dict):
        return None
    declared = schema.get("$schema")
    if declared is None:
        return None
    if declared == SUPPORTED_DIALECT or declared == f"{SUPPORTED_DIALECT}#":
        return None
    return SchemaRefused(
        SchemaDocumentRefusal.UNSUPPORTED_DIALECT,
        declared if isinstance(declared, str) else type(declared).__name__,
    )


class _NonCanonicalNumber(Exception):
    def __init__(self, literal: str) -> None:
        super().__init__(literal)
        self.literal = literal


class _DuplicateObjectKey(Exception):
    def __init__(self, key: str) -> None:
        super().__init__(key)
        self.key = key


def _refuse_constant(literal: str) -> NoReturn:
    raise _NonCanonicalNumber(literal)


def _read_exact_decimal(literal: str) -> Decimal:
    try:
        return Decimal(literal)
    except InvalidOperation as refused:
        raise _NonCanonicalNumber(literal) from refused


def _refuse_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    seen: dict[str, object] = {}
    for key, value in pairs:
        if key in seen:
            raise _DuplicateObjectKey(key)
        seen[key] = value
    return seen


@dataclass(frozen=True, slots=True)
class _Decoded:
    """One decoded JSON value, before anything has claimed it is a schema."""

    value: JsonValue


def _decoded_json(text: str) -> _Decoded | SchemaRefused:
    try:
        value: JsonValue = json.loads(
            text,
            parse_constant=_refuse_constant,
            parse_float=_read_exact_decimal,
            object_pairs_hook=_refuse_duplicate_keys,
        )
    except _NonCanonicalNumber as refused:
        return SchemaRefused(
            SchemaDocumentRefusal.NON_CANONICAL_NUMBER, refused.literal
        )
    except _DuplicateObjectKey as refused:
        return SchemaRefused(SchemaDocumentRefusal.DUPLICATE_OBJECT_KEY, refused.key)
    except json.JSONDecodeError as broken:
        return SchemaRefused(SchemaDocumentRefusal.DOCUMENT_NOT_JSON, broken.msg)
    return _Decoded(value)


def _scanned_vocabulary(schema: object) -> SchemaRefused | None:
    """The closed-vocabulary and locality walk, which also bounds depth and size."""
    counted = 0
    pending: list[tuple[object, int]] = [(schema, 1)]
    while pending:
        value, depth = pending.pop()
        counted += 1
        if counted > MAXIMUM_SCHEMA_VALUES:
            return SchemaRefused(
                SchemaDocumentRefusal.TOO_MANY_VALUES,
                f"more than {MAXIMUM_SCHEMA_VALUES} JSON values",
            )
        if not isinstance(value, (dict, list)):
            continue
        if depth > MAXIMUM_SCHEMA_CONTAINER_DEPTH:
            return SchemaRefused(
                SchemaDocumentRefusal.DOCUMENT_TOO_DEEP,
                f"more than {MAXIMUM_SCHEMA_CONTAINER_DEPTH} container levels",
            )
        if isinstance(value, list):
            pending.extend((entry, depth + 1) for entry in value)
            continue
        refused = _refused_keyword(value)
        if refused is not None:
            return refused
        pending.extend((entry, depth + 1) for entry in value.values())
    return None


def _refused_keyword(value: dict[str, object]) -> SchemaRefused | None:
    for keyword in FORBIDDEN_KEYWORDS:
        if keyword in value:
            return SchemaRefused(SchemaDocumentRefusal.FORBIDDEN_KEYWORD, keyword)
    reference = value.get("$ref")
    if isinstance(reference, str) and not reference.startswith(_LOCAL_REFERENCE_PREFIX):
        return SchemaRefused(SchemaDocumentRefusal.NONLOCAL_REFERENCE, reference)
    return None


def _resolved_pointer(root: object, fragment: str) -> object | None:
    """The value one local JSON pointer names, or `None` when it names nothing."""
    if fragment == "":
        return root
    if not fragment.startswith("/"):
        # A plain-name fragment addresses an `$anchor`, and this profile forbids
        # declaring one, so no document can ever carry the target.
        return None
    current: object = root
    for raw in fragment[1:].split("/"):
        token = raw.replace("~1", "/").replace("~0", "~")
        if isinstance(current, dict):
            mapping: dict[str, object] = current
            if token not in mapping:
                return None
            current = mapping[token]
        elif isinstance(current, list):
            entries: list[object] = current
            if not token.isdigit() or int(token) >= len(entries):
                return None
            current = entries[int(token)]
        else:
            return None
    return current


def _in_place_subschemas(schema: object) -> Iterator[tuple[str, object]]:
    """Every subschema applied to the same instance, with the path that reaches it."""
    if not isinstance(schema, dict):
        return
    for keyword in _IN_PLACE_SUBSCHEMAS:
        if keyword in schema:
            yield f"/{keyword}", schema[keyword]
    for keyword in _IN_PLACE_SUBSCHEMA_LISTS:
        entries = schema.get(keyword)
        if not isinstance(entries, list):
            continue
        for index, entry in enumerate(entries):
            yield f"/{keyword}/{index}", entry
    for keyword in _IN_PLACE_SUBSCHEMA_MAPS:
        entries = schema.get(keyword)
        if not isinstance(entries, dict):
            continue
        for name, entry in entries.items():
            yield f"/{keyword}/{name}", entry


def _every_position(value: object, pointer: str = "") -> Iterator[str]:
    """The JSON pointer of every object and array position in the document."""
    yield pointer
    if isinstance(value, dict):
        for key, entry in value.items():
            token = key.replace("~", "~0").replace("/", "~1")
            yield from _every_position(entry, f"{pointer}/{token}")
    elif isinstance(value, list):
        for index, entry in enumerate(value):
            yield from _every_position(entry, f"{pointer}/{index}")


def _refused_reference_graph(root: Schema) -> SchemaRefused | None:
    """Every local reference resolves, and no cycle of them can outlast an instance.

    Two different faults, two different names. A reference resolving to nothing
    is unusable bytes, refused where the bytes are read rather than where an
    evaluator would trip over it. A cycle whose every edge stays on the same
    instance never terminates whatever it is given -- `{"$ref": "#"}` is exactly
    that -- while the ordinary recursive schema, whose cycle passes through
    `properties` or `items`, descends into a smaller instance each time and is
    deliberately left legal, because a tree is the shape this workshop's own
    outputs most want to declare.
    """
    for pointer in _every_position(root):
        node = _resolved_pointer(root, pointer)
        reference = node.get("$ref") if isinstance(node, dict) else None
        if isinstance(reference, str) and (
            _resolved_pointer(root, reference[len(_LOCAL_REFERENCE_PREFIX) :]) is None
        ):
            return SchemaRefused(
                SchemaDocumentRefusal.UNRESOLVABLE_REFERENCE, reference
            )
    settled: set[str] = set()
    for pointer in _every_position(root):
        if isinstance(_resolved_pointer(root, pointer), dict):
            refused = _non_terminating_cycle(root, pointer, frozenset(), settled)
            if refused is not None:
                return refused
    return None


def _non_terminating_cycle(
    root: Schema, pointer: str, on_path: frozenset[str], settled: set[str]
) -> SchemaRefused | None:
    """Walk the in-place edges from this position; `settled` are positions that end."""
    if pointer in on_path:
        return SchemaRefused(
            SchemaDocumentRefusal.NON_TERMINATING_REFERENCE_CYCLE,
            f"#{pointer}" if pointer else "#",
        )
    if pointer in settled:
        return None
    node = _resolved_pointer(root, pointer)
    reached = on_path | {pointer}
    next_pointers = [pointer + step for step, _ in _in_place_subschemas(node)]
    reference = node.get("$ref") if isinstance(node, dict) else None
    if isinstance(reference, str):
        next_pointers.append(reference[len(_LOCAL_REFERENCE_PREFIX) :])
    for next_pointer in next_pointers:
        refused = _non_terminating_cycle(root, next_pointer, reached, settled)
        if refused is not None:
            return refused
    settled.add(pointer)
    return None


def _validated_against_the_draft(schema: Schema) -> SchemaDocumentVerdict:
    validator = Draft202012Validator(
        Draft202012Validator.META_SCHEMA, registry=schema_registry()
    )
    for error in validator.iter_errors(schema):
        return SchemaRefused(SchemaDocumentRefusal.NOT_A_SCHEMA, error.message)
    return SchemaAccepted(schema)
