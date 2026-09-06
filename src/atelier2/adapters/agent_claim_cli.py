"""The pinned ``agent-claim`` 0.12.0 JSON command adapter."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path, PurePosixPath

from atelier2.adapters.bounded_processes import bounded_process_streams
from atelier2.contracts.effect_requests import HeadBranch
from atelier2.contracts.runs import RunId
from atelier2.ports.work_item_claims import (
    Abandoned,
    ClaimReceipt,
    ClaimRefusal,
    ClaimRefusalReason,
    ClaimReleaseOutcome,
    ClaimState,
    ClaimTouch,
    Merged,
)

AGENT_CLAIM_TIMEOUT_SECONDS = 30.0
MAXIMUM_AGENT_CLAIM_OUTPUT_BYTES = 65_536
_JSON_FLAG = "--json"
_BUILDER_ROLE = "builder"

_CLAIM_FIELDS = frozenset(
    {
        "issue",
        "lane",
        "claim_id",
        "url",
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
_STATUS_FIELDS = frozenset({"ledger", "issue", "state", "claims", "unreadable"})
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
    }
)
_STATUS_UNREADABLE_FIELDS = frozenset({"claim_id", "comment_url", "fields", "note"})
_TOUCH_FIELDS = frozenset({"issue", "lane", "claim_id", "agent", "scope"})
_OVERLAP_FIELDS = frozenset({"issue", "lane", "claim_id", "agent"})
_CHECK_FIELDS = frozenset({"level", "check", "text", "slice", "issue"})
_RELEASE_FIELDS = frozenset(
    {"issue", "lane", "branch", "claim_id", "agent", "role", "reason"}
)


class AgentClaimCli:
    """Runs the claim command in the checkout whose ledger it owns."""

    def __init__(
        self,
        executable: Path,
        working_directory: Path,
        run_id: RunId,
        branch: HeadBranch,
        *,
        timeout_seconds: float = AGENT_CLAIM_TIMEOUT_SECONDS,
    ) -> None:
        self._executable = executable
        self._working_directory = working_directory
        self._run_id = run_id
        self._branch = branch
        self._timeout_seconds = timeout_seconds

    def claim(
        self,
        item: int,
        agent: RunId,
        branch: HeadBranch,
        scope: tuple[PurePosixPath, ...],
        out_of_order_reason: str | None,
    ) -> ClaimReceipt | ClaimRefusal:
        if agent != self._run_id or branch != self._branch:
            return ClaimRefusal(ClaimRefusalReason.UNKNOWN)
        arguments = [
            "claim",
            str(item),
            "--agent",
            _agent_name(agent),
            "--role",
            _BUILDER_ROLE,
            "--branch",
            branch.value,
        ]
        for path in scope:
            arguments.extend(("--scope", path.as_posix()))
        if out_of_order_reason is not None:
            arguments.extend(("--out-of-order", out_of_order_reason))
        payload, diagnostics = self._command(*arguments, _JSON_FLAG)
        if payload is None:
            return ClaimRefusal(_diagnostic_refusal(diagnostics))
        if _is_claim_refusal(payload):
            return ClaimRefusal(_claim_refusal_reason(payload))
        try:
            _require_fields(payload, _CLAIM_FIELDS)
            claimed_item = _integer(payload["issue"])
            claim_id = _text(payload["claim_id"])
            claimed_branch = HeadBranch(_text(payload["branch"]))
            _text(payload["url"])
            claimed_agent = _text(payload["agent"])
            claimed_role = _text(payload["role"])
            _text(payload["base"])
            claimed_scope = _scope(payload["scope"])
            _identity(payload["issue"], payload["lane"])
            _resource(payload["resource"], payload["resource_value"])
            _integer(payload["versioned_files"])
            _integer(payload["versioned_files_total"])
            _number(payload["share"])
            for check in _list(payload["checks"]):
                _check(check)
            touches = tuple(_touch(value) for value in _list(payload["touches"]))
        except (TypeError, ValueError):
            return ClaimRefusal(ClaimRefusalReason.UNKNOWN)
        if (
            claimed_item != item
            or claimed_branch != branch
            or claimed_agent != _agent_name(agent)
            or claimed_role != _BUILDER_ROLE
            or claimed_scope != scope
        ):
            return ClaimRefusal(ClaimRefusalReason.UNKNOWN)
        return ClaimReceipt(claimed_item, claim_id, claimed_branch, touches)

    def status(self, branch: HeadBranch) -> ClaimState:
        payload, _diagnostics = self._command("status", _JSON_FLAG)
        if payload is None:
            return ClaimState.UNKNOWN
        try:
            _require_fields(payload, _STATUS_FIELDS)
            _integer(payload["ledger"])
            if payload["issue"] is not None:
                _integer(payload["issue"])
            ClaimState(_text(payload["state"]))
            claims = _list(payload["claims"])
            unreadable = _list(payload["unreadable"])
            for value in unreadable:
                _unreadable(value)
            matching_states = tuple(
                state
                for value in claims
                if (state := _status_claim(value, branch)) is not None
            )
        except (TypeError, ValueError):
            return ClaimState.UNKNOWN
        if unreadable:
            return ClaimState.LEDGER_UNREADABLE
        if not matching_states:
            return ClaimState.UNCLAIMED
        if ClaimState.CONFLICT in matching_states:
            return ClaimState.CONFLICT
        return ClaimState.CLAIMED

    def release(
        self, item: int, claim_id: str, outcome: ClaimReleaseOutcome
    ) -> ClaimRefusal | None:
        arguments = [
            "release",
            str(item),
            "--agent",
            _agent_name(self._run_id),
            "--claim-id",
            claim_id,
        ]
        if isinstance(outcome, Merged):
            arguments.extend(("--merged", str(outcome.pull_request)))
        elif isinstance(outcome, Abandoned):
            arguments.extend(("--abandoned", outcome.reason))
        payload, diagnostics = self._command(*arguments, _JSON_FLAG)
        if payload is None:
            return ClaimRefusal(_diagnostic_refusal(diagnostics))
        try:
            _require_fields(payload, _RELEASE_FIELDS)
            if (
                _integer(payload["issue"]) != item
                or _text(payload["claim_id"]) != claim_id
            ):
                return ClaimRefusal(ClaimRefusalReason.UNKNOWN)
            _identity(payload["issue"], payload["lane"])
            released_branch = HeadBranch(_text(payload["branch"]))
            released_agent = _text(payload["agent"])
            released_role = _text(payload["role"])
            released_reason = _text(payload["reason"])
        except (TypeError, ValueError):
            return ClaimRefusal(ClaimRefusalReason.UNKNOWN)
        if (
            released_branch != self._branch
            or released_agent != _agent_name(self._run_id)
            or released_role != _BUILDER_ROLE
            or released_reason != _release_reason(outcome)
        ):
            return ClaimRefusal(ClaimRefusalReason.UNKNOWN)
        return None

    def _command(self, *arguments: str) -> tuple[dict[str, object] | None, str]:
        try:
            process = subprocess.Popen(
                (str(self._executable), *arguments),
                cwd=self._working_directory,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                start_new_session=True,
            )
            _return_code, standard_output, standard_error = bounded_process_streams(
                process, self._timeout_seconds, MAXIMUM_AGENT_CLAIM_OUTPUT_BYTES
            )
        except (OSError, ValueError):
            return None, ""
        diagnostics = standard_error.decode("utf-8", errors="replace")
        try:
            return _object(json.loads(standard_output.decode("utf-8"))), diagnostics
        except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError):
            return None, diagnostics


def _agent_name(agent: RunId) -> str:
    return f"atelier2 run {agent.value}"


def _object(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        raise TypeError("agent-claim returned a non-object JSON value")
    return value


def _list(value: object) -> list[object]:
    if not isinstance(value, list):
        raise TypeError("agent-claim returned a non-list JSON field")
    return value


def _text(value: object) -> str:
    if not isinstance(value, str) or not value:
        raise TypeError("agent-claim returned a nonempty text field")
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
        raise ValueError("agent-claim returned an invalid resource pair")


def _integer(value: object) -> int:
    if type(value) is not int:
        raise TypeError("agent-claim returned an integer field")
    return value


def _number(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError("agent-claim returned a numeric field")
    return float(value)


def _require_fields(value: dict[str, object], expected: frozenset[str]) -> None:
    if frozenset(value) != expected:
        raise ValueError("agent-claim returned fields outside its pinned contract")


def _scope(value: object) -> tuple[PurePosixPath, ...]:
    paths = _list(value)
    return tuple(PurePosixPath(_text(path)) for path in paths)


def _identity(issue: object, lane: object) -> int | None:
    if issue is not None:
        if lane is not None:
            raise TypeError("agent-claim returned an invalid claim identity")
        return _integer(issue)
    if lane is not True:
        raise TypeError("agent-claim returned an invalid claim identity")
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


def _diagnostic_refusal(diagnostics: str) -> ClaimRefusalReason:
    if "unreadable" in diagnostics or "upgrade the installed tool" in diagnostics:
        return ClaimRefusalReason.LEDGER_UNREADABLE
    return ClaimRefusalReason.UNKNOWN


def _release_reason(outcome: ClaimReleaseOutcome) -> str:
    if isinstance(outcome, Merged):
        return f"merged #{outcome.pull_request}"
    return f"abandoned: {outcome.reason}"


def _claim_refusal_reason(value: dict[str, object]) -> ClaimRefusalReason:
    try:
        _require_fields(value, _CLAIM_REFUSAL_FIELDS)
        _integer(value["issue"])
        checks = _list(value["checks"])
        texts = tuple(_check(check) for check in checks)
    except (TypeError, ValueError):
        return ClaimRefusalReason.UNKNOWN
    if any("out-of-order" in text or "priority" in text for text in texts):
        return ClaimRefusalReason.PRIORITY
    if any(
        "unreadable" in text or "upgrade the installed tool" in text for text in texts
    ):
        return ClaimRefusalReason.LEDGER_UNREADABLE
    return ClaimRefusalReason.UNKNOWN


def _status_claim(value: object, branch: HeadBranch) -> ClaimState | None:
    claim = _object(value)
    fields = frozenset(claim)
    if fields not in (_STATUS_CLAIM_FIELDS, _STATUS_CLAIM_FIELDS | {"whole"}):
        raise ValueError("agent-claim returned fields outside its pinned contract")
    _identity(claim["issue"], claim["lane"])
    claimed_branch = _text(claim["branch"])
    _text(claim["agent"])
    _text(claim["role"])
    _text(claim["base"])
    _text(claim["claim_id"])
    _scope(claim["scope"])
    _resource(claim["resource"], claim["resource_value"])
    if "whole" in claim:
        _text(claim["whole"])
    for overlap in _list(claim["overlaps"]):
        overlap_fields = _object(overlap)
        _require_fields(overlap_fields, _OVERLAP_FIELDS)
        _identity(overlap_fields["issue"], overlap_fields["lane"])
        _text(overlap_fields["claim_id"])
        _text(overlap_fields["agent"])
    _text(claim["age"])
    if type(claim["old"]) is not bool:
        raise TypeError("agent-claim returned an invalid old marker")
    state = ClaimState(_text(claim["state"]))
    if claimed_branch != branch.value:
        return None
    return state


def _check(value: object) -> str:
    check = _object(value)
    _require_fields(check, _CHECK_FIELDS)
    _text(check["level"])
    _text(check["check"])
    text = _text(check["text"])
    if check["slice"] is not None:
        _integer(check["slice"])
    if check["issue"] is not None:
        _integer(check["issue"])
    return text


def _unreadable(value: object) -> None:
    unreadable = _object(value)
    _require_fields(unreadable, _STATUS_UNREADABLE_FIELDS)
    if unreadable["claim_id"] is not None:
        _text(unreadable["claim_id"])
    _text(unreadable["comment_url"])
    for field in _list(unreadable["fields"]):
        _text(field)
    _text(unreadable["note"])
