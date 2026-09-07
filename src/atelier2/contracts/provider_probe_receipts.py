"""What one live provider probe proved, and for exactly how long.

Provider behavior sits beyond deterministic CI: a pinned executor can still lose
its login, drift server-side, or stop surviving the command shape it used to run.
The probe receipt is the small, secret-free fact the live canary leaves behind so
a later start can distinguish a recently proven vector from one it has no current
evidence for. Full output stays with the durable run; this record carries only its
identity and terminal hash, or one bounded problem code when the probe failed.

The same `provider-probe-receipt/v1` shape serves live canaries and CLI-pin
attestations. Its result discriminates the evidence: success carries a terminal
hash, failure carries a problem code, and no receipt can carry both. JSON is
canonical because these records live as `.json` files and are atomically replaced
or checked into the repository; the reader refuses any other spelling rather than
silently normalising bytes a writer did not produce.

A receipt carries two distinct facts about the deployment that proved it (#1124):
`source_commit` is provenance for a human reading the journal -- which exact
checkout ran this probe. `provider_layer_digest` is what a later gate actually
compares (`ports.agent_executions.ProviderProbeReceiptGate`): a content hash of
the files that can change how this deployment talks to a provider
(`host.provider_canary.provider_layer_digest`). A redeploy changes `source_commit`
on every receipt; it changes `provider_layer_digest` only when it touches the
provider layer itself, which is exactly the evidence a live vector's evidence
should survive or not survive.
"""

from __future__ import annotations

import json
import re
from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum

from atelier2.contracts.agents import (
    PROVIDER_PROBE_TOKEN,
    AgentConfigurationRevisionHash,
    ProviderProbeProblemCode,
)
from atelier2.contracts.hashing import Sha256Hash
from atelier2.contracts.runs import RunId, WorkflowRevisionHash
from atelier2.contracts.when import RecordedAt

MAXIMUM_PROVIDER_PROBE_RECEIPT_BYTES = 4_096
"""The complete record is a handful of bounded identifiers and never evidence."""

MAXIMUM_PROVIDER_PROBE_VECTOR_ID_BYTES = 128

PROVIDER_CANARY_HEADLESS_WORKFLOW_NAME = "provider-canary-headless"
PROVIDER_CANARY_WORKSPACE_TOOLS_WORKFLOW_NAME = "provider-canary-workspace-tools"
PROVIDER_CANARY_ATELIER_DOORS_WORKFLOW_NAME = "provider-canary-atelier-doors"
PROVIDER_CANARY_WORKFLOW_NAMES = (
    PROVIDER_CANARY_HEADLESS_WORKFLOW_NAME,
    PROVIDER_CANARY_WORKSPACE_TOOLS_WORKFLOW_NAME,
    PROVIDER_CANARY_ATELIER_DOORS_WORKFLOW_NAME,
)
"""The three catalog names a live canary vector may resolve under.

The one production owner of these tokens: `host/provider_canary.py` maps each
configured executor to one of these names to choose its probe workflow, and
`adapters/dbos/runtime.py` resolves the same three names to their admitted
`WorkflowRevisionHash` to build the reprobe exemption set. Neither module
imports the other -- `atelier2.host` imports `atelier2.adapters.dbos.runtime`,
so the reverse import would close a cycle -- but both already import this
contracts module, which is where a stable protocol token belongs. Drift
between two separately maintained copies would have shrunk the exemption set
silently; one owner makes that impossible instead of merely tested.
"""

_SOURCE_COMMIT = re.compile(r"[0-9a-f]{40}")


@dataclass(frozen=True, slots=True)
class ProviderProbeVectorId:
    """The configured executor vector this probe exercised."""

    value: str

    def __post_init__(self) -> None:
        if not isinstance(self.value, str):
            raise TypeError("a provider probe vector id must be text")
        if (
            PROVIDER_PROBE_TOKEN.fullmatch(self.value) is None
            or len(self.value.encode("ascii")) > MAXIMUM_PROVIDER_PROBE_VECTOR_ID_BYTES
        ):
            raise ValueError(
                "a provider probe vector id must be a bounded lowercase ASCII token"
            )


class ProviderProbeResult(StrEnum):
    """The two terminal facts a provider probe may record."""

    SUCCEEDED = "succeeded"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class ProviderProbeReceipt:
    """One provider vector's result, its durable evidence, and its validity window."""

    vector: ProviderProbeVectorId
    configuration_hash: AgentConfigurationRevisionHash
    workflow_hash: WorkflowRevisionHash
    provider_layer_digest: Sha256Hash
    source_commit: str
    observed_at: RecordedAt
    valid_until: RecordedAt
    result: ProviderProbeResult
    run_reference: RunId
    terminal_hash: Sha256Hash | None = None
    problem_code: ProviderProbeProblemCode | None = None

    def __post_init__(self) -> None:
        self._require_well_formed_fields()
        self._require_result_evidence()

    def _require_well_formed_fields(self) -> None:
        if not isinstance(self.vector, ProviderProbeVectorId):
            raise TypeError("a provider probe receipt names a typed vector")
        if not isinstance(self.configuration_hash, AgentConfigurationRevisionHash):
            raise TypeError("a provider probe receipt names a configuration hash")
        if not isinstance(self.workflow_hash, WorkflowRevisionHash):
            raise TypeError("a provider probe receipt names a workflow hash")
        if not isinstance(self.provider_layer_digest, Sha256Hash):
            raise TypeError("a provider probe receipt names a provider layer digest")
        if not isinstance(self.source_commit, str):
            raise TypeError("a provider probe receipt source commit must be text")
        if _SOURCE_COMMIT.fullmatch(self.source_commit) is None:
            raise ValueError(
                "a provider probe receipt names a full SHA-1 source commit"
            )
        if not isinstance(self.observed_at, RecordedAt) or not isinstance(
            self.valid_until, RecordedAt
        ):
            raise TypeError("a provider probe receipt carries typed recording instants")
        if self.valid_until.value <= self.observed_at.value:
            raise ValueError("a provider probe receipt expires after it was observed")
        if not isinstance(self.result, ProviderProbeResult):
            raise TypeError(
                "a provider probe receipt uses the closed result vocabulary"
            )
        if not isinstance(self.run_reference, RunId):
            raise TypeError("a provider probe receipt names a typed run reference")

    def _require_result_evidence(self) -> None:
        if self.result is ProviderProbeResult.SUCCEEDED:
            if not isinstance(self.terminal_hash, Sha256Hash):
                raise ValueError("a succeeded provider probe carries a terminal hash")
            if self.problem_code is not None:
                raise ValueError("a succeeded provider probe carries no problem code")
        else:
            if not isinstance(self.problem_code, ProviderProbeProblemCode):
                raise ValueError("a failed provider probe carries a problem code")
            if self.terminal_hash is not None:
                raise ValueError("a failed provider probe carries no terminal hash")

    def is_valid_at(self, instant: RecordedAt) -> bool:
        """Whether the proof covers this instant; expiry itself is already stale."""

        if not isinstance(instant, RecordedAt):
            raise TypeError("provider probe validity is asked at a recorded instant")
        return self.observed_at.value <= instant.value < self.valid_until.value

    def canonical_bytes(self) -> bytes:
        """The one JSON spelling of this `provider-probe-receipt/v1`."""

        document: dict[str, str] = {
            "configuration_hash": self.configuration_hash.value,
            "observed_at": self.observed_at.value,
            "provider_layer_digest": self.provider_layer_digest.value,
            "result": self.result.value,
            "run_reference": self.run_reference.value,
            "source_commit": self.source_commit,
            "valid_until": self.valid_until.value,
            "vector": self.vector.value,
            "workflow_hash": self.workflow_hash.value,
        }
        if self.result is ProviderProbeResult.SUCCEEDED:
            assert self.terminal_hash is not None
            document["terminal_hash"] = self.terminal_hash.value
        else:
            assert self.problem_code is not None
            document["problem_code"] = self.problem_code.value
        return json.dumps(document, sort_keys=True, separators=(",", ":")).encode(
            "utf-8"
        )


class ProviderProbeReceiptRefusal(StrEnum):
    """Every named way bytes fail to be a provider probe receipt."""

    DOCUMENT_TOO_LARGE = "document-too-large"
    DOCUMENT_NOT_UTF8 = "document-not-utf8"
    NOT_A_RECEIPT_OBJECT = "not-a-receipt-object"
    DOCUMENT_NOT_CANONICAL = "document-not-canonical"
    UNKNOWN_FIELD = "unknown-field"
    MISSING_FIELD = "missing-field"
    UNKNOWN_RESULT = "unknown-result"
    INVALID_FIELD = "invalid-field"
    INVALID_VALIDITY_WINDOW = "invalid-validity-window"


@dataclass(frozen=True, slots=True)
class ProviderProbeReceiptRefused:
    """Why these bytes are no provider probe receipt this runtime trusts."""

    reason: ProviderProbeReceiptRefusal
    detail: str = ""

    def __str__(self) -> str:
        suffix = f": {self.detail}" if self.detail else ""
        return f"{self.reason.value}{suffix}"


type ProviderProbeReceiptVerdict = ProviderProbeReceipt | ProviderProbeReceiptRefused

_COMMON_FIELDS = frozenset(
    (
        "configuration_hash",
        "observed_at",
        "provider_layer_digest",
        "result",
        "run_reference",
        "source_commit",
        "valid_until",
        "vector",
        "workflow_hash",
    )
)
_EVIDENCE_FIELDS = frozenset(("problem_code", "terminal_hash"))


def read_provider_probe_receipt(document: bytes) -> ProviderProbeReceiptVerdict:
    """Whether these exact canonical bytes record a provider probe."""

    if len(document) > MAXIMUM_PROVIDER_PROBE_RECEIPT_BYTES:
        return ProviderProbeReceiptRefused(
            ProviderProbeReceiptRefusal.DOCUMENT_TOO_LARGE,
            f"{len(document)} bytes exceeds {MAXIMUM_PROVIDER_PROBE_RECEIPT_BYTES}",
        )
    try:
        text = document.decode("utf-8")
    except UnicodeDecodeError as broken:
        return ProviderProbeReceiptRefused(
            ProviderProbeReceiptRefusal.DOCUMENT_NOT_UTF8, broken.reason
        )
    try:
        decoded = json.loads(text)
    except (ValueError, RecursionError) as broken:
        return ProviderProbeReceiptRefused(
            ProviderProbeReceiptRefusal.NOT_A_RECEIPT_OBJECT, str(broken)
        )
    if not isinstance(decoded, dict):
        return ProviderProbeReceiptRefused(
            ProviderProbeReceiptRefusal.NOT_A_RECEIPT_OBJECT,
            f"a provider probe receipt is an object, not {type(decoded).__name__}",
        )
    if _canonical_json(decoded) != document:
        return ProviderProbeReceiptRefused(
            ProviderProbeReceiptRefusal.DOCUMENT_NOT_CANONICAL,
            "a provider probe receipt uses sorted keys and compact separators",
        )
    fields = set(decoded)
    unknown = sorted(fields - _COMMON_FIELDS - _EVIDENCE_FIELDS)
    if unknown:
        return _fields_refused(ProviderProbeReceiptRefusal.UNKNOWN_FIELD, unknown)
    missing = sorted(_COMMON_FIELDS - fields)
    if missing:
        return _fields_refused(ProviderProbeReceiptRefusal.MISSING_FIELD, missing)
    try:
        result = ProviderProbeResult(_text(decoded, "result"))
    except (TypeError, ValueError):
        return ProviderProbeReceiptRefused(
            ProviderProbeReceiptRefusal.UNKNOWN_RESULT,
            f"a provider probe result is one of {', '.join(result.value for result in ProviderProbeResult)}",
        )
    expected_evidence = (
        "terminal_hash" if result is ProviderProbeResult.SUCCEEDED else "problem_code"
    )
    unexpected = sorted((fields & _EVIDENCE_FIELDS) - {expected_evidence})
    if unexpected:
        return _fields_refused(ProviderProbeReceiptRefusal.UNKNOWN_FIELD, unexpected)
    if expected_evidence not in fields:
        return _fields_refused(
            ProviderProbeReceiptRefusal.MISSING_FIELD, [expected_evidence]
        )
    try:
        observed_at = RecordedAt(_text(decoded, "observed_at"))
        valid_until = RecordedAt(_text(decoded, "valid_until"))
        if valid_until.value <= observed_at.value:
            return ProviderProbeReceiptRefused(
                ProviderProbeReceiptRefusal.INVALID_VALIDITY_WINDOW,
                "valid_until must be later than observed_at",
            )
        terminal_hash = (
            Sha256Hash(_text(decoded, "terminal_hash"))
            if result is ProviderProbeResult.SUCCEEDED
            else None
        )
        problem_code = (
            ProviderProbeProblemCode(_text(decoded, "problem_code"))
            if result is ProviderProbeResult.FAILED
            else None
        )
        return ProviderProbeReceipt(
            ProviderProbeVectorId(_text(decoded, "vector")),
            AgentConfigurationRevisionHash(_text(decoded, "configuration_hash")),
            WorkflowRevisionHash(_text(decoded, "workflow_hash")),
            Sha256Hash(_text(decoded, "provider_layer_digest")),
            _text(decoded, "source_commit"),
            observed_at,
            valid_until,
            result,
            RunId(_text(decoded, "run_reference")),
            terminal_hash,
            problem_code,
        )
    except (TypeError, ValueError) as refused:
        return ProviderProbeReceiptRefused(
            ProviderProbeReceiptRefusal.INVALID_FIELD, str(refused)
        )


def _canonical_json(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _text(record: dict[str, object], field: str) -> str:
    value = record[field]
    if not isinstance(value, str):
        raise TypeError(f"provider probe receipt field {field} must be text")
    return value


def _fields_refused(
    reason: ProviderProbeReceiptRefusal, fields: Sequence[str]
) -> ProviderProbeReceiptRefused:
    return ProviderProbeReceiptRefused(reason, ", ".join(fields))
