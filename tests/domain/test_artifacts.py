"""What material must be to be kept, and what its address promises."""

from __future__ import annotations

import hashlib

import pytest

from atelier2.contracts.artifacts import (
    MAXIMUM_ARTIFACT_BYTES,
    Artifact,
    ArtifactHash,
    ArtifactRefusal,
    ArtifactRefused,
    read_artifact_content,
)


def accepted(content: bytes) -> Artifact:
    verdict = read_artifact_content(content)
    assert isinstance(verdict, Artifact), verdict
    return verdict


def test_an_artifacts_address_is_the_digest_of_its_exact_bytes() -> None:
    content = b"diff --git a/one b/one\n"

    assert accepted(content).artifact_hash == ArtifactHash(
        hashlib.sha256(content).hexdigest()
    )


def test_the_same_bytes_are_the_same_artifact_and_different_bytes_are_not() -> None:
    assert accepted(b"one").artifact_hash == ArtifactHash(
        hashlib.sha256(b"one").hexdigest()
    )
    assert accepted(b"one").artifact_hash != accepted(b"one ").artifact_hash


@pytest.mark.parametrize(
    ("content", "expected"),
    [
        pytest.param(b"", ArtifactRefusal.ARTIFACT_EMPTY, id="empty"),
        pytest.param(
            b"x" * (MAXIMUM_ARTIFACT_BYTES + 1),
            ArtifactRefusal.ARTIFACT_TOO_LARGE,
            id="over the bound",
        ),
    ],
)
def test_material_this_store_does_not_keep_is_refused_by_its_own_name(
    content: bytes, expected: ArtifactRefusal
) -> None:
    verdict = read_artifact_content(content)

    assert isinstance(verdict, ArtifactRefused)
    assert verdict.refusal is expected


def test_the_largest_admitted_artifact_is_exactly_the_bound() -> None:
    assert (
        len(accepted(b"x" * MAXIMUM_ARTIFACT_BYTES).content) == MAXIMUM_ARTIFACT_BYTES
    )
