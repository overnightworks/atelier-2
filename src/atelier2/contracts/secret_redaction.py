"""Credential shapes taken out of provider bytes before anything durable keeps them.

**Why this is not the CI secret scan.** The `Secret scan` job owns a different
boundary: it reads the repository's own git history with a pinned `gitleaks`
binary, and the rules it matches live inside that Go executable -- the
repository's `.gitleaks.toml` carries reviewed allowlist entries and no patterns
at all. Nothing there can be called on a durable write path: an in-process
redactor cannot shell out to a binary that a serving host is not promised to
have, and a transcript that could only be kept when a Go tool happened to be
installed would be evidence that disappears for an unrelated reason.

So this is the owner for the other boundary -- bytes a provider just produced,
on their way into an artifact nobody can delete. The two owners answer different
questions about different material and neither can stand in for the other.

**Why the set is small and named.** Every shape below is one a reader can judge:
what it matches, and why bytes of that shape are a credential rather than
prose. It is deliberately not a corpus. A match is replaced, never dropped, and
the caller learns that something was replaced -- material this cannot recognise
is still kept, because a transcript that quietly held back what it could not
classify would be exactly the silence this repository is removing.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

REDACTION_MARKER = "[redacted]"
"""What stands where a credential stood, in every surface that reads it back."""

# Where a shape names the credential's own surroundings -- the header it travels
# in, the field it is assigned to -- only this group is replaced, so the reader
# still sees *which* secret was taken out.
_MATCHED_VALUE_GROUP = "value"
MAXIMUM_PRIVATE_KEY_INTERIOR_CHARACTERS = 8_192
"""How much key material the armoured-block shape reads between its markers.

A credential is a token, not a document, and an unbounded span is what turns a
linear scan of one large tool result into a quadratic one.
"""

_PRIVATE_KEY_LABEL_CHARACTERS = 32
"""What may stand between `BEGIN` and `PRIVATE KEY`, as `RSA ` or `OPENSSH ` do."""

_PRIVATE_KEY_BEGIN = "-----BEGIN "
_PRIVATE_KEY_END = "-----END "
_PRIVATE_KEY_MARKER_TAIL = "PRIVATE KEY-----"
_PRIVATE_KEY_LABEL = rf"[A-Z ]{{0,{_PRIVATE_KEY_LABEL_CHARACTERS}}}"
_PRIVATE_KEY_OPENING_PATTERN = (
    rf"{_PRIVATE_KEY_BEGIN}{_PRIVATE_KEY_LABEL}{_PRIVATE_KEY_MARKER_TAIL}"
)
_PRIVATE_KEY_CLOSING_PATTERN = (
    rf"{_PRIVATE_KEY_END}{_PRIVATE_KEY_LABEL}{_PRIVATE_KEY_MARKER_TAIL}"
)
_PRIVATE_KEY_OPENING = re.compile(_PRIVATE_KEY_OPENING_PATTERN)
_PRIVATE_KEY_CLOSING = re.compile(_PRIVATE_KEY_CLOSING_PATTERN)

MAXIMUM_CREDENTIAL_SPAN_CHARACTERS = (
    MAXIMUM_PRIVATE_KEY_INTERIOR_CHARACTERS
    + len(_PRIVATE_KEY_BEGIN)
    + len(_PRIVATE_KEY_END)
    + 2 * (_PRIVATE_KEY_LABEL_CHARACTERS + len(_PRIVATE_KEY_MARKER_TAIL))
)
"""How far past a cut a caller has to read for this owner to see whole tokens.

The longest text any shape here has to see *whole* to recognise it at all: the
armoured block, whose closing marker is what names it, markers included. Every
other shape is recognised by its own opening -- an issuer prefix, a header, a
field name -- so a prefix of it still matches and is still replaced.

A caller that means to show only the first part of some text therefore reads
that part plus this span, redacts what came back, and cuts the result. Cutting
first and replacing afterwards leaves the half before the cut standing as the
token it is.

This counts characters, which is what a regex counts, while a caller bounding a
read counts bytes. Spending it as bytes covers every block written in the ASCII
these shapes are written in, and no unit makes it cover a block padded with
wider characters -- a block whose close fell past a read is a block this owner
never sees at all, and `redact_an_unclosed_credential` is what keeps such a
reading safe to show rather than any width of look-ahead.
"""


@dataclass(frozen=True, slots=True)
class CredentialShape:
    """One recognisable way a credential appears in text a provider produced.

    `minimum_replaced_characters` is the shortest span this shape's own
    pattern can ever hand to `_replacement` for replacement: the `value`
    group's own minimum where the pattern names one (only that group is
    replaced), or the whole match's own minimum otherwise (the whole match is
    replaced). It is hand-verified against the pattern beside it, the same way
    every other quantifier here is a literal count rather than a derived one --
    introspecting an arbitrary compiled regex for its true minimum span is not
    reliable enough to build a safety bound on. A value declared smaller than
    the pattern's true minimum only makes `maximum_redacted_length` more
    conservative, never unsafe; a value declared larger would not.
    """

    name: str
    pattern: re.Pattern[str]
    minimum_replaced_characters: int


CREDENTIAL_SHAPES = (
    CredentialShape(
        # The whole armoured block: its own header names it, and every byte
        # between the markers is key material.
        "private-key-block",
        re.compile(
            _PRIVATE_KEY_OPENING_PATTERN
            + rf".{{0,{MAXIMUM_PRIVATE_KEY_INTERIOR_CHARACTERS}}}?"
            + _PRIVATE_KEY_CLOSING_PATTERN,
            re.DOTALL,
        ),
        minimum_replaced_characters=(
            len(_PRIVATE_KEY_BEGIN)
            + len(_PRIVATE_KEY_END)
            + 2 * len(_PRIVATE_KEY_MARKER_TAIL)
        ),
    ),
    CredentialShape(
        # AWS's own published key-id form: a fixed prefix and a fixed width.
        "aws-access-key-id",
        re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
        minimum_replaced_characters=len("AKIA") + 16,
    ),
    CredentialShape(
        # Issuer-prefixed tokens: the prefix is the issuer's own declaration
        # that what follows is a credential.
        "issued-token",
        re.compile(
            r"\b(?:sk-ant|sk|ghp|gho|ghu|ghs|ghr|github_pat|glpat|xox[abopsr])"
            r"[-_][A-Za-z0-9_-]{16,}"
        ),
        # The shortest issuer prefix is "sk", one separator, 16 value characters.
        minimum_replaced_characters=len("sk") + 1 + 16,
    ),
    CredentialShape(
        # A JSON Web Token: three base64url segments, the first of which is a
        # JSON header and therefore always begins `eyJ`.
        "json-web-token",
        re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}"),
        minimum_replaced_characters=len("eyJ") + 8 + 1 + 8 + 1 + 8,
    ),
    CredentialShape(
        # The credential inside a URL, between the user's name and the host --
        # the form a remote tool echoes back in its own error line. Only the
        # secret is replaced, so the reader still sees which remote refused.
        # The width floor matches the authorization header's, so this shape
        # adds nothing to `maximum_redacted_length`; the ASCII userinfo
        # alphabet keeps that bound's byte arithmetic true.
        "url-userinfo-secret",
        re.compile(
            r"\b[A-Za-z][A-Za-z0-9+.-]*://[A-Za-z0-9._~%!$&'()*+,;=-]+:"
            rf"(?P<{_MATCHED_VALUE_GROUP}>[A-Za-z0-9._~%!$&'()*+,;=-]{{8,}})@"
        ),
        # Only the `value` group is replaced; its own minimum is 8.
        minimum_replaced_characters=8,
    ),
    CredentialShape(
        # The credential in transit, named by the header carrying it.
        "authorization-header",
        re.compile(
            r"(?i:authorization)\s*:\s*(?i:bearer|basic|token)\s+"
            rf"(?P<{_MATCHED_VALUE_GROUP}>[A-Za-z0-9._~+/=-]{{8,}})"
        ),
        # Only the `value` group is replaced; its own minimum is 8.
        minimum_replaced_characters=8,
    ),
    CredentialShape(
        # The credential at rest, named by the field it was assigned to. The
        # width floor keeps ordinary prose -- `password: yes` -- out of it.
        #
        # The name is read as the whole identifier rather than as the credential
        # word alone, because that is how a provider spells it. The measured
        # miss this was widened for is `AWS_SECRET_ACCESS_KEY=`, whose word sits
        # between two other segments and which the narrower shape walked past
        # while still calling itself a redactor. Only the value is replaced, so
        # a generously matched name costs nothing, and the deliberate trade is
        # that a name saying credential is believed even where its value turns
        # out to be a path: a visible replacement rather than a silent leak.
        "assigned-secret",
        re.compile(
            r"(?:[A-Za-z0-9]{1,32}[_-]){0,8}"
            # The plural is inside the case-insensitive group, not after it: an
            # `s` left outside matched only a lowercase one, so a field spelled
            # `CREDENTIALS` was read as prose.
            r"(?i:(?:api[_-]?key|secret|password|passwd|token|credential)s?)"
            r"(?:[_-][A-Za-z0-9]{1,32}){0,8}"
            r"\s*[:=]\s*[\"']?"
            rf"(?P<{_MATCHED_VALUE_GROUP}>[A-Za-z0-9._~+/=-]{{12,}})"
        ),
        # Only the `value` group is replaced; its own minimum is 12.
        minimum_replaced_characters=12,
    ),
)


@dataclass(frozen=True, slots=True)
class RedactedText:
    """Text safe to keep, and whether keeping it safe changed anything."""

    text: str
    redacted: bool


def _replacement(match: re.Match[str]) -> str:
    if _MATCHED_VALUE_GROUP not in match.groupdict():
        return REDACTION_MARKER
    start, end = match.span(_MATCHED_VALUE_GROUP)
    matched = match.group(0)
    offset = match.start()
    return matched[: start - offset] + REDACTION_MARKER + matched[end - offset :]


def redact_credentials(text: str) -> RedactedText:
    """Replace every credential shape this owner recognises, and say whether it did."""

    redacted = text
    for shape in CREDENTIAL_SHAPES:
        redacted = shape.pattern.sub(_replacement, redacted)
    return RedactedText(redacted, redacted != text)


def redact_an_unclosed_credential(text: str) -> RedactedText:
    """Replace what stands behind an opening whose close this text does not carry.

    Run over text `redact_credentials` has already been over, and only where
    that text is a reading someone cut: every armoured opening still standing
    there is one whose closing marker -- the marker that names the block at all
    -- fell outside what was read or outside what a pattern spans. The opening
    alone already says what follows it is key material, so everything from it to
    the end is replaced. Over-broad on purpose: what lies past a cut is unknown
    by definition, and prose after an unclosed opening is a cheaper loss than a
    key's first half handed to a reader.
    """

    opening = _PRIVATE_KEY_OPENING.search(text)
    if opening is None:
        return RedactedText(text, False)
    return RedactedText(text[: opening.start()] + REDACTION_MARKER, True)


def redact_an_unopened_credential(text: str) -> RedactedText:
    """Replace what stands in front of a close this text carries no opening for.

    The mirror of `redact_an_unclosed_credential`, for text cut the other way:
    a reading that kept the last bytes of something longer. A closing marker
    with no opening ahead of it is a block whose opening fell outside what was
    kept, so no shape names it and the material between them stands as the key
    it is. Everything up to and including that close is replaced. Over-broad on
    purpose, on the same terms: what stood before the cut is unknown, and prose
    ahead of an unopened close is a cheaper loss than key material handed to a
    reader.
    """

    closing = _PRIVATE_KEY_CLOSING.search(text)
    if closing is None:
        return RedactedText(text, False)
    opening = _PRIVATE_KEY_OPENING.search(text)
    if opening is not None and opening.start() < closing.start():
        return RedactedText(text, False)
    return RedactedText(REDACTION_MARKER + text[closing.end() :], True)


def maximum_redacted_length(original_length: int) -> int:
    """The longest `redact_credentials(text).text` can ever be, for `len(text) <= original_length`.

    A shape only grows the text where its own `minimum_replaced_characters`
    is shorter than `REDACTION_MARKER`: replacing fewer characters with more
    makes the string longer, by exactly that difference, once per match. A
    match can never recur more often than once per its own
    `minimum_replaced_characters` original characters -- the true minimum
    full match is always at least that many, prefix and suffix included, so
    dividing by the replaced span alone is a safe over-count of how densely
    matches could ever pack a real string, never an undercount. Two different
    shapes cannot both grow the same span of text (each byte is replaced by
    at most one shape's match across the whole pass), so the bound is the
    single worst shape's growth over the whole length, not the sum of every
    shape's growth -- summing would double-count text no real input has
    twice over.

    Callers that hand this the original UTF-8 *byte* count, not the decoded
    text's character count, still get a safe bound on the redacted text's own
    re-encoded byte count -- exactly what a caller bounding an encoded wire
    field wants, and without decoding first to find out. Every shape here
    matches only ASCII, so a matched span's own character count and its own
    byte count are the same number on both sides of a redaction, and those
    bytes are a genuine subset of the original total; unmatched text is
    carried through unchanged, so it contributes exactly the same bytes to
    the result that it already spent from that same total. The growth this
    function counts therefore is the growth in bytes, and the same original
    total already bounds how many matches of any one shape's minimum span
    could ever have been carved out of it -- decoding first could only ever
    shrink that count, by turning some of those original bytes into fewer,
    wider characters, never more of them.
    """

    return original_length + max(
        (
            max(0, len(REDACTION_MARKER) - shape.minimum_replaced_characters)
            * (original_length // shape.minimum_replaced_characters)
            for shape in CREDENTIAL_SHAPES
        ),
        default=0,
    )
