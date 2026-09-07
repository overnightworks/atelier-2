"""How a red project check's ending is written down for the receipt that keeps it.

The exit code alone is not a reading an operator can repair from, and the
evidence beside it was kept elsewhere. This composes the one sentence that says
what the check answered and where the rest of it stands, so no writer of a
receipt words that ending its own way.
"""

from __future__ import annotations

from atelier2.contracts.tool_grants_v3 import (
    MAXIMUM_RECEIPTED_VERIFICATION_SUMMARY_BYTES,
    ToolRedemptionReceipt,
)
from atelier2.ports.agent_attempts import (
    KeptEvidence,
    ProjectVerificationFailureEvidence,
)


def verification_failure_verdict(
    redemption: ToolRedemptionReceipt,
    evidence: ProjectVerificationFailureEvidence | None,
) -> str:
    """Everything a reader needs to judge a red check without rerunning it.

    The exit code alone is six words that answer nothing: an operator cannot
    tell a broken test from a broken environment from it (#1137). Every real
    redemption failure supplies `evidence`, because the caller that ran the
    check already read what it printed; it is absent only for a caller this
    store cannot assume exists yet, so a missing evidence still names the exit
    code and the command rather than raising.
    """

    words = [f"exit {redemption.exit_code}", " ".join(redemption.command)]
    if evidence is not None:
        words.append(f"after {evidence.duration_seconds:.0f} s")
        if evidence.summary_line is not None:
            words.append(bounded_verification_summary(evidence.summary_line))
        words.extend(named_evidence("output", evidence.output))
        words.extend(named_evidence("candidate diff", evidence.candidate_diff))
    return "; ".join(words)


def named_evidence(name: str, kept: KeptEvidence) -> list[str]:
    """Where one piece of this evidence was kept, or why it was not kept at all.

    Silence is the honest answer for a piece that never existed -- a check that
    printed nothing, an attempt with nothing to diff -- because a receipt saying
    "no artifact" about something that was never there tells a reader nothing.
    A piece that existed and could not be kept says so instead.
    """

    if kept.artifact_hash is not None:
        lines = [f"{name} artifact sha256:{kept.artifact_hash.value}"]
        if kept.redacted:
            lines.append(f"{name} redacted")
        return lines
    if kept.retention_failure is not None:
        return [f"{name} could not be kept: {kept.retention_failure}"]
    return []


def bounded_verification_summary(summary_line: str) -> str:
    """Pytest's own summary line, bounded the way `ProcessExitSignature` bounds free text.

    A project's own test runner is free to compose a summary of any length; a
    receipt is a sentence an operator reads at a glance, not a log (#1137).
    """

    encoded = summary_line.encode("utf-8")
    if len(encoded) <= MAXIMUM_RECEIPTED_VERIFICATION_SUMMARY_BYTES:
        return summary_line
    tail = encoded[-MAXIMUM_RECEIPTED_VERIFICATION_SUMMARY_BYTES:].decode(
        "utf-8", "replace"
    )
    return (
        f"last {MAXIMUM_RECEIPTED_VERIFICATION_SUMMARY_BYTES} of "
        f"{len(encoded)} summary bytes: {tail}"
    )
