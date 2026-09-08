"""The pinned ``aco`` 1.0.0 JSON command adapter."""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from atelier2.adapters.bounded_processes import bounded_process_streams
from atelier2.contracts.effect_requests import ClaimReasons, HeadBranch
from atelier2.contracts.runs import RunId
from atelier2.ports.work_item_claims import (
    Abandoned,
    ClaimAbsent,
    ClaimReadback,
    ClaimReceipt,
    ClaimRefusal,
    ClaimRefusalReason,
    ClaimReleaseOutcome,
    ClaimTouch,
    Merged,
)

AGENT_CLAIM_ADAPTER_REVISION = "agent-claim-cli/1.0.0"
"""Which command contract this adapter speaks, as the revision an intent binds."""

AGENT_CLAIM_TIMEOUT_SECONDS = 30.0
MAXIMUM_AGENT_CLAIM_OUTPUT_BYTES = 65_536
_JSON_FLAG = "--json"
_BUILDER_ROLE = "builder"
_OUT_OF_ORDER_CHECK = "out-of-order"
_ERROR_CHECK_LEVEL = "error"
_ERROR_LINE_BANNER = "ERROR:"

_CLAIM_FIELDS = frozenset(
    {
        "issue",
        "lane",
        "claim_id",
        "agent",
        "role",
        "base",
        "branch",
        "scope",
        "resource",
        "resource_value",
        "versioned_files",
        "versioned_files_total",
        "share",
        "touches",
        "checks",
    }
)
_CLAIM_REFUSAL_FIELDS = frozenset({"refused", "issue", "checks"})
_STATUS_FIELDS = frozenset({"issue", "state", "claims"})
_STATUS_CLAIM_FIELDS = frozenset(
    {
        "issue",
        "lane",
        "agent",
        "role",
        "base",
        "branch",
        "claim_id",
        "scope",
        "resource",
        "resource_value",
        "overlaps",
        "state",
        "age",
        "old",
        "whole",
    }
)
_TOUCH_FIELDS = frozenset({"issue", "lane", "claim_id", "agent", "scope"})
_OVERLAP_FIELDS = frozenset({"issue", "lane", "claim_id", "agent"})
_CHECK_FIELDS = frozenset({"level", "check", "text", "slice", "issue"})
_RELEASE_FIELDS = frozenset(
    {"issue", "lane", "branch", "claim_id", "agent", "role", "reason"}
)


@dataclass(frozen=True, slots=True)
class _ClaimPeer:
    """A claim the store names as overlapping another, without its scope."""

    item: int | None
    claim_id: str
    agent: str


@dataclass(frozen=True, slots=True)
class _SliceCheck:
    """One structured finding of a claim answer: its id, level and sentence."""

    name: str
    level: str
    text: str


@dataclass(frozen=True, slots=True)
class _StandingClaim:
    """One live claim of the store, read back in full."""

    item: int | None
    claim_id: str
    agent: str
    branch: HeadBranch
    scope: tuple[PurePosixPath, ...]
    overlaps: tuple[_ClaimPeer, ...]


class AgentClaimCli:
    """Runs the claim command in the run's claim checkout.

    `working_directory` is the project checkout whose store the command owns;
    a release runs there, a claim and its read-back in the checkout they are
    given.
    """

    def __init__(
        self,
        executable: Path,
        working_directory: Path,
        *,
        timeout_seconds: float = AGENT_CLAIM_TIMEOUT_SECONDS,
    ) -> None:
        self._executable = executable
        self._working_directory = working_directory
        self._timeout_seconds = timeout_seconds

    def claim(
        self,
        item: int,
        agent: RunId,
        branch: HeadBranch,
        scope: tuple[PurePosixPath, ...],
        claim_id: str,
        reasons: ClaimReasons,
        checkout: Path,
    ) -> ClaimReceipt | ClaimRefusal:
        arguments = [
            "claim",
            str(item),
            "--agent",
            _agent_name(agent),
            "--role",
            _BUILDER_ROLE,
            "--branch",
            branch.value,
            "--claim-id",
            claim_id,
        ]
        for path in scope:
            arguments.extend(("--scope", path.as_posix()))
        arguments.extend(("--whole", reasons.whole))
        if reasons.out_of_order is not None:
            arguments.extend(("--out-of-order", reasons.out_of_order))
        payload = self._json_payload(*arguments, _JSON_FLAG, cwd=checkout)
        if isinstance(payload, ClaimRefusal):
            return payload
        if _is_claim_refusal(payload):
            return _claim_refusal(payload)
        try:
            acquired = _acquired_claim(payload)
        except (TypeError, ValueError) as violation:
            return ClaimRefusal(ClaimRefusalReason.UNKNOWN, str(violation))
        if (
            acquired.item != item
            or acquired.claim_id != claim_id
            or acquired.branch != branch
            or acquired.agent != _agent_name(agent)
        ):
            return ClaimRefusal(
                ClaimRefusalReason.UNKNOWN,
                "aco posted a claim other than the one requested",
            )
        return acquired

    def read_back(self, item: int, claim_id: str, checkout: Path) -> ClaimReadback:
        payload = self._json_payload("status", _JSON_FLAG, cwd=checkout)
        if isinstance(payload, ClaimRefusal):
            return payload
        try:
            _require_fields(payload, _STATUS_FIELDS)
            if payload["issue"] is not None:
                _integer(payload["issue"])
            _text(payload["state"])
            claims = tuple(_status_claim(value) for value in _list(payload["claims"]))
        except (TypeError, ValueError) as violation:
            return ClaimRefusal(ClaimRefusalReason.UNKNOWN, str(violation))
        held = tuple(claim for claim in claims if claim.claim_id == claim_id)
        if not held:
            return ClaimAbsent()
        if len(held) != 1 or held[0].item != item:
            return ClaimRefusal(
                ClaimRefusalReason.UNKNOWN,
                "the store holds this claim id under another item or more than once",
            )
        standing = held[0]
        scopes = {claim.claim_id: claim for claim in claims}
        return ClaimReceipt(
            item,
            standing.claim_id,
            standing.agent,
            standing.branch,
            standing.scope,
            tuple(
                ClaimTouch(
                    peer.item, peer.claim_id, peer.agent, scopes[peer.claim_id].scope
                )
                for peer in standing.overlaps
                if peer.claim_id in scopes
            ),
        )

    def release(
        self, item: int, agent: RunId, claim_id: str, outcome: ClaimReleaseOutcome
    ) -> ClaimRefusal | None:
        arguments = [
            "release",
            str(item),
            "--agent",
            _agent_name(agent),
            "--claim-id",
            claim_id,
        ]
        if isinstance(outcome, Merged):
            arguments.extend(("--merged", str(outcome.pull_request)))
        elif isinstance(outcome, Abandoned):
            arguments.extend(("--abandoned", outcome.reason))
        payload = self._json_payload(
            *arguments, _JSON_FLAG, cwd=self._working_directory
        )
        if isinstance(payload, ClaimRefusal):
            return payload
        other_release = ClaimRefusal(
            ClaimRefusalReason.UNKNOWN,
            "aco released a claim other than the one requested",
        )
        try:
            _require_fields(payload, _RELEASE_FIELDS)
            if (
                _integer(payload["issue"]) != item
                or _text(payload["claim_id"]) != claim_id
            ):
                return other_release
            _identity(payload["issue"], payload["lane"])
            HeadBranch(_text(payload["branch"]))
            released_agent = _text(payload["agent"])
            released_role = _text(payload["role"])
            released_reason = _text(payload["reason"])
        except (TypeError, ValueError) as violation:
            return ClaimRefusal(ClaimRefusalReason.UNKNOWN, str(violation))
        if (
            released_agent != _agent_name(agent)
            or released_role != _BUILDER_ROLE
            or released_reason != _release_reason(outcome)
        ):
            return other_release
        return None

    def _json_payload(
        self, *arguments: str, cwd: Path
    ) -> dict[str, object] | ClaimRefusal:
        """Stdout of a `--json` command, or the refusal that command stated.

        `_command` carries the child's exit status here. A non-zero exit that
        printed `{"ok": false, "error": "<sentence>"}` is that sentence, never
        a success payload; a non-zero exit with nothing parseable falls back
        to the last `ERROR:` line on stderr. A zero exit is unchanged.
        """

        return_code, payload, diagnostics = self._command(*arguments, cwd=cwd)
        if return_code != 0:
            json_refusal = _json_error_refusal(payload)
            if json_refusal is not None:
                return json_refusal
        if payload is None:
            return _diagnostic_refusal(diagnostics)
        return payload

    def _command(
        self, *arguments: str, cwd: Path
    ) -> tuple[int | None, dict[str, object] | None, str]:
        try:
            process = subprocess.Popen(
                (str(self._executable), *arguments),
                cwd=cwd,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                start_new_session=True,
            )
            return_code, standard_output, standard_error = bounded_process_streams(
                process, self._timeout_seconds, MAXIMUM_AGENT_CLAIM_OUTPUT_BYTES
            )
        except (OSError, ValueError):
            return None, None, ""
        diagnostics = standard_error.decode("utf-8", errors="replace")
        try:
            payload = _object(json.loads(standard_output.decode("utf-8")))
        except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError):
            payload = None
        return return_code, payload, diagnostics


def _acquired_claim(payload: dict[str, object]) -> ClaimReceipt:
    """The claim `aco claim --json` says it posted, read in full."""

    _require_fields(payload, _CLAIM_FIELDS)
    item = _integer(payload["issue"])
    claim_id = _text(payload["claim_id"])
    branch = HeadBranch(_text(payload["branch"]))
    agent = _text(payload["agent"])
    if _text(payload["role"]) != _BUILDER_ROLE:
        raise ValueError("aco posted a claim under another role")
    _text(payload["base"])
    scope = _scope(payload["scope"])
    _identity(payload["issue"], payload["lane"])
    _resource(payload["resource"], payload["resource_value"])
    _integer(payload["versioned_files"])
    _integer(payload["versioned_files_total"])
    _number(payload["share"])
    for check in _list(payload["checks"]):
        _check(check)
    touches = tuple(_touch(value) for value in _list(payload["touches"]))
    return ClaimReceipt(item, claim_id, agent, branch, scope, touches)


def _agent_name(agent: RunId) -> str:
    return f"atelier2 run {agent.value}"


def _object(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        raise TypeError("aco returned a non-object JSON value")
    return value


def _list(value: object) -> list[object]:
    if not isinstance(value, list):
        raise TypeError("aco returned a non-list JSON field")
    return value


def _text(value: object) -> str:
    if not isinstance(value, str) or not value:
        raise TypeError("aco returned a nonempty text field")
    return value


def _optional_text(value: object) -> str | None:
    if value is None:
        return None
    return _text(value)


def _optional_integer(value: object) -> int | None:
    if value is None:
        return None
    return _integer(value)


def _resource(resource: object, resource_value: object) -> None:
    name = _optional_text(resource)
    value = _optional_integer(resource_value)
    if (name is None) != (value is None) or (value is not None and value <= 0):
        raise ValueError("aco returned an invalid resource pair")


def _integer(value: object) -> int:
    if type(value) is not int:
        raise TypeError("aco returned an integer field")
    return value


def _number(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError("aco returned a numeric field")
    return float(value)


def _require_fields(value: dict[str, object], expected: frozenset[str]) -> None:
    if frozenset(value) != expected:
        raise ValueError("aco returned fields outside its pinned contract")


def _scope(value: object) -> tuple[PurePosixPath, ...]:
    paths = _list(value)
    return tuple(PurePosixPath(_text(path)) for path in paths)


def _identity(issue: object, lane: object) -> int | None:
    if issue is not None:
        if lane is not None:
            raise TypeError("aco returned an invalid claim identity")
        return _integer(issue)
    if lane is not True:
        raise TypeError("aco returned an invalid claim identity")
    return None


def _touch(value: object) -> ClaimTouch:
    touch = _object(value)
    _require_fields(touch, _TOUCH_FIELDS)
    issue = _identity(touch["issue"], touch["lane"])
    return ClaimTouch(
        issue,
        _text(touch["claim_id"]),
        _text(touch["agent"]),
        _scope(touch["scope"]),
    )


def _is_claim_refusal(value: dict[str, object]) -> bool:
    return value.get("refused") is True


def _json_error_refusal(payload: dict[str, object] | None) -> ClaimRefusal | None:
    """The sentence a refusing `--json` command printed on stdout, if it did.

    The tool writes `{"ok": false, "error": "<sentence>"}` and the same
    sentence under `ERROR:` on stderr. The stdout object is the refusal,
    not a success payload.
    """

    if payload is None:
        return None
    error = payload.get("error")
    if payload.get("ok") is not False or not isinstance(error, str) or not error:
        return None
    return ClaimRefusal(ClaimRefusalReason.UNKNOWN, error)


def _diagnostic_refusal(diagnostics: str) -> ClaimRefusal:
    """The refusal a command that printed no JSON left on its standard error.

    Checkout preconditions fail as `ClaimError` before any `--json` payload
    exists, and the top-level handler prints that one sentence under an
    `ERROR:` banner. The last such line is the refusal's own detail.
    """

    error_lines = [
        line.removeprefix(_ERROR_LINE_BANNER).strip()
        for line in diagnostics.splitlines()
        if line.startswith(_ERROR_LINE_BANNER)
    ]
    detail = error_lines[-1] if error_lines else ""
    return ClaimRefusal(ClaimRefusalReason.UNKNOWN, detail)


def _release_reason(outcome: ClaimReleaseOutcome) -> str:
    if isinstance(outcome, Merged):
        return f"merged #{outcome.pull_request}"
    return f"abandoned: {outcome.reason}"


def _claim_refusal(value: dict[str, object]) -> ClaimRefusal:
    """The refusal a structured `--json` claim answer states, with its first
    failing check's sentence as the detail."""

    try:
        _require_fields(value, _CLAIM_REFUSAL_FIELDS)
        _integer(value["issue"])
        checks = tuple(_check(check) for check in _list(value["checks"]))
    except (TypeError, ValueError) as violation:
        return ClaimRefusal(ClaimRefusalReason.UNKNOWN, str(violation))
    failed = [check.text for check in checks if check.level == _ERROR_CHECK_LEVEL]
    detail = failed[0] if failed else ""
    if any(check.name == _OUT_OF_ORDER_CHECK for check in checks):
        return ClaimRefusal(ClaimRefusalReason.PRIORITY, detail)
    return ClaimRefusal(ClaimRefusalReason.UNKNOWN, detail)


def _status_claim(value: object) -> _StandingClaim:
    """One live store claim as `aco status --json` states it."""
    claim = _object(value)
    if "whole" not in claim:
        claim = {**claim, "whole": None}
    _require_fields(claim, _STATUS_CLAIM_FIELDS)
    item = _identity(claim["issue"], claim["lane"])
    branch = HeadBranch(_text(claim["branch"]))
    agent = _text(claim["agent"])
    _text(claim["role"])
    _text(claim["base"])
    claim_id = _text(claim["claim_id"])
    scope = _scope(claim["scope"])
    _resource(claim["resource"], claim["resource_value"])
    _optional_text(claim["whole"])
    overlaps: list[_ClaimPeer] = []
    for overlap in _list(claim["overlaps"]):
        overlap_fields = _object(overlap)
        _require_fields(overlap_fields, _OVERLAP_FIELDS)
        overlaps.append(
            _ClaimPeer(
                _identity(overlap_fields["issue"], overlap_fields["lane"]),
                _text(overlap_fields["claim_id"]),
                _text(overlap_fields["agent"]),
            )
        )
    _text(claim["age"])
    if type(claim["old"]) is not bool:
        raise TypeError("aco returned an invalid old marker")
    _text(claim["state"])
    return _StandingClaim(item, claim_id, agent, branch, scope, tuple(overlaps))


def _check(value: object) -> _SliceCheck:
    """Validate one structured refusal finding; `name` and `level` are what a
    reader decides on, `text` is what it shows."""
    check = _object(value)
    _require_fields(check, _CHECK_FIELDS)
    slice_check = _SliceCheck(
        _text(check["check"]), _text(check["level"]), _text(check["text"])
    )
    if check["slice"] is not None:
        _integer(check["slice"])
    if check["issue"] is not None:
        _integer(check["issue"])
    return slice_check
