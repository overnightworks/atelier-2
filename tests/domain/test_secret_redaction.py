"""Which credential shapes never reach durable evidence, and what stands there."""

from __future__ import annotations

import pytest

from atelier2.contracts.secret_redaction import (
    REDACTION_MARKER,
    maximum_redacted_length,
    redact_credentials,
)
from tests.scenarios.credentials import armoured_key, assembled


@pytest.mark.parametrize(
    ("secret", "surroundings"),
    [
        pytest.param(armoured_key(), "{secret}", id="armoured private key"),
        pytest.param(
            assembled("AKIA", "7QF3NOTAREALKEY0"),
            "aws_access_key_id={secret}\n",
            id="aws access key id",
        ),
        pytest.param(
            assembled("sk-ant", "-", "notarealkeyvalue0123456789"),
            "export ANTHROPIC_KEY={secret}",
            id="issuer-prefixed token",
        ),
        pytest.param(
            assembled(
                "eyJ", "hbGciOiJIUzI1NiJ9", ".", "eyJzdWIiOiI0MiJ9", ".", "bm90LXJlYWw"
            ),
            "session={secret} (expired)",
            id="json web token",
        ),
        pytest.param(
            assembled("wJalrXUtnFEMI", "K7MDENGbPxRfiCYEXAMPLEKEY"),
            "AWS_SECRET_ACCESS_KEY={secret}\n",
            id="a provider name whose credential word is not its first segment",
        ),
        pytest.param(
            assembled("notarealservice", "accountkey0123456789"),
            "GOOGLE_APPLICATION_CREDENTIALS_JSON: {secret}",
            id="a provider name whose credential word is not its last segment",
        ),
        pytest.param(
            assembled("notareal", "remotepassword"),
            "fatal: unable to access 'https://deploy:{secret}@example.invalid/"
            "org/repo.git/': The requested URL returned error: 403",
            id="a secret inside a remote url a tool echoed",
        ),
    ],
)
def test_a_recognised_credential_is_replaced_where_it_stood(
    secret: str, surroundings: str
) -> None:
    redacted = redact_credentials(surroundings.format(secret=secret))

    assert secret not in redacted.text
    assert REDACTION_MARKER in redacted.text
    assert redacted.redacted


@pytest.mark.parametrize(
    ("carrier", "expected"),
    [
        pytest.param(
            "Authorization: Bearer {secret}",
            f"Authorization: Bearer {REDACTION_MARKER}",
            id="the header keeps its name",
        ),
        pytest.param(
            "api_key = {secret}",
            f"api_key = {REDACTION_MARKER}",
            id="the field keeps its name",
        ),
        pytest.param(
            "https://deploy:{secret}@example.invalid/org/repo.git",
            f"https://deploy:{REDACTION_MARKER}@example.invalid/org/repo.git",
            id="the url keeps its user and host",
        ),
    ],
)
def test_the_reader_still_sees_which_credential_was_taken_out(
    carrier: str, expected: str
) -> None:
    secret = assembled("notarealcredential", "0123456789")

    assert redact_credentials(carrier.format(secret=secret)).text == expected


@pytest.mark.parametrize(
    "prose",
    [
        pytest.param("The password: yes answer is not a credential.", id="short value"),
        pytest.param("Read the token from the operator's own keyring.", id="no value"),
        pytest.param("git commit -m 'begin private key rotation'", id="prose about it"),
        pytest.param(
            "fetch https://github.com/overnightworks/atelier-2.git failed",
            id="a url without userinfo",
        ),
        pytest.param(
            "ssh://git@github.com:22/overnightworks/atelier-2.git",
            id="a url whose userinfo names no secret",
        ),
    ],
)
def test_text_carrying_no_credential_is_kept_exactly_and_says_so(prose: str) -> None:
    redacted = redact_credentials(prose)

    assert redacted.text == prose
    assert not redacted.redacted


def test_maximum_redacted_length_never_understates_the_input_itself() -> None:
    """The bound this feeds a wire field must never claim a value the input already exceeds."""
    for length in (0, 1, 8, 12, 100, 49_152):
        assert maximum_redacted_length(length) >= length


def test_a_minimal_authorization_header_grows_by_marker_minus_value_length() -> None:
    """The one shape that ever grows text (#664's re-review): the shortest case, worked by hand.

    `REDACTION_MARKER` is ten characters; the shape's own declared minimum
    `value` is eight, so replacing the shortest possible header value grows
    the text by exactly two characters -- and the declared bound has to be at
    least that much, for the shortest input that could ever produce it.
    """
    header = "Authorization: Basic xxxxxxxx"

    redacted = redact_credentials(header)

    assert len(redacted.text) - len(header) == len(REDACTION_MARKER) - 8
    assert maximum_redacted_length(len(header)) >= len(redacted.text)


def test_maximum_redacted_length_bounds_a_worst_case_credential_pack() -> None:
    """The wire's own use of this bound (#664) needs it to hold for real input, not one match.

    A text packed edge to edge with the shortest growing shape is the closest
    a real string can come to the theoretical worst case this function
    computes -- so this is the case that would have caught a bound loose in
    the unsafe direction.
    """
    unit = "Authorization: Basic xxxxxxxx "
    text = unit * 2_000

    redacted = redact_credentials(text)

    assert redacted.redacted
    assert len(redacted.text) > len(text)
    assert len(redacted.text) <= maximum_redacted_length(len(text))
