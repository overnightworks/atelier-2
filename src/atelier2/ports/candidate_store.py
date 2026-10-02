"""Where the work an attempt made is kept once, unchanged, past its workspace.

An attempt works in a leased directory that is deleted when it ends. Everything
the attempt produced lives only there, so between the last verified state and the
release of that directory there is exactly one moment in which the work can be
kept -- and if it is not kept then, no later owner can invent it back.

This port is that keeping. A capture answers with the tree the work now *is*:
content-addressed, so nothing can change it afterwards without changing its name,
and anchored under the attempt that made it, so a reader with nothing but the
attempt's identity can find it again. Capture is repeatable rather than
once-only: the same attempt capturing the same work twice is the same fact
stated twice, while the same attempt claiming two different trees is a
contradiction and is refused instead of overwritten.

Nothing here deletes. What an attempt made outlives the run that made it, and no
owner in this slice prunes a candidate -- a named gap, not an oversight.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from atelier2.contracts.agent_attempts import AgentAttemptId
from atelier2.contracts.candidate_reports import ReadPatch
from atelier2.contracts.project_sources import CandidateTree, ProjectSourcePin
from atelier2.contracts.secret_redaction import MAXIMUM_CREDENTIAL_SPAN_CHARACTERS
from atelier2.ports.agent_executions import AgentAttemptWorkspaceLease

MAXIMUM_CANDIDATE_DIFF_BYTES = 65_536
"""How much of an attempt's own patch a reader is given, once it is safe to show.

Sibling to `MAXIMUM_VERIFICATION_OUTPUT_TAIL_BYTES`, and the same width for the
same reason: a check that said no and a diff that says what was done are the two
halves of one question an operator answers without rerunning anything. Kept from
the *start* of the patch, unlike a console tail: a patch read from its beginning
is a patch, while a patch read from its end begins inside a hunk of whichever
path happened to sort last.

This is the bound on what a *presenter* shows, and the cut belongs to whoever
redacts, never to the store: a patch cut here and scrubbed afterwards would keep
the first half of a credential that straddled the cut, because the shape the
redactor recognises no longer stands whole in what it is handed.
"""


CANDIDATE_DIFF_READ_BYTES = (
    MAXIMUM_CANDIDATE_DIFF_BYTES + MAXIMUM_CREDENTIAL_SPAN_CHARACTERS
)
"""How much of a patch this port reads at all, so nothing here grows unbounded.

More than any reader is given, by exactly the look-ahead
`MAXIMUM_CREDENTIAL_SPAN_CHARACTERS` names, spent here as bytes: a credential
written in the ASCII its shapes are written in stands complete in what the
redactor sees even when it begins just before the presenter's cut, so the
marker lands where the token was and the patch around it is still shown. It is
a look-ahead, not the safety bound -- a block padded wide enough closes past
any read of it, and what the presenter does with an opening whose close is
missing is what makes a cut patch safe. What is read past the look-ahead is
dropped inside the store, so the process never holds a whole patch of an
arbitrarily large tree.
"""


@dataclass(frozen=True, slots=True)
class LeasedWorkingTree:
    """What one lease holds right now, named the way a candidate would be.

    Written into the store, because a tree has to be written before it has a
    name at all -- and anchored under nothing, because "did this attempt change
    anything" must be answerable without thereby keeping work no ending has
    decided to keep. The objects it names stay unreferenced until a capture
    anchors them.

    It carries the pin it was measured against, so no caller has to remember
    which pin an answer belongs to in order to read it.
    """

    pin: ProjectSourcePin
    tree: str

    @property
    def changed_the_pinned_tree(self) -> bool:
        """Whether this attempt left anything the pin did not already carry."""

        return self.tree != self.pin.tree


class CandidateNotKept(Exception):
    """This capture ended with no candidate, whatever the reason was.

    The caller of a capture has exactly one decision to make about every way it
    can fail -- the attempt must not be allowed to succeed -- so the ways share a
    name it can catch. Without it that caller would list the reasons it happens
    to know today, and the next reason added here would escape it silently,
    leaving the attempt armed and its replay reporting that it possibly ran.
    """


class CandidateStoreUnavailable(CandidateNotKept):
    """The store could not answer, so nothing is claimed about the work."""


class CandidateCaptureConflict(CandidateNotKept):
    """This attempt is already anchored at other work than the work offered.

    One attempt is one piece of work. Two different trees under one attempt would
    mean the store had been told two incompatible truths, and the second one is
    refused rather than allowed to overwrite the first.
    """


class CandidateTreeUnrepresentable(CandidateNotKept):
    """The workspace holds something no tree of this store can carry.

    A nested repository is the case that exists: it would be recorded as a link
    to a commit the store has never seen, so the candidate would name work that
    is nowhere -- a tree that lies rather than a tree that is missing something.
    """


class CandidateTreeStore(Protocol):
    """The provider-neutral owner of every candidate one project ever captured."""

    def capture(
        self, pin: ProjectSourcePin, lease: AgentAttemptWorkspaceLease
    ) -> CandidateTree:
        """Keep what stands in this lease as the tree the pinned source became."""
        ...

    def read(self, attempt_id: AgentAttemptId) -> CandidateTree | None:
        """The candidate this attempt captured, or nothing if it captured none."""
        ...

    def attest(self, candidate: CandidateTree) -> None:
        """Refuse a candidate this store cannot hand out whole, unpacking nothing.

        Whole is the word that matters: the tree still anchored under the
        attempt that made it, and every object beneath that tree readable here.
        The anchor is what keeps those objects from being collected, so a tree
        found without its anchor is one nothing promises to still hold.

        Asked by whoever is about to begin work in a candidate, before that
        work costs anything. It leaves through `CandidateNotKept` like the rest
        of this port: work that cannot be named cannot be continued either.
        """
        ...

    def materialize(
        self, candidate: CandidateTree, lease: AgentAttemptWorkspaceLease
    ) -> None:
        """Unpack this candidate into the directory an attempt leased.

        The mirror of a capture: what `capture` read out of a lease is what this
        writes back into one, so an attempt handed a candidate it then leaves
        alone captures that very tree again. Material travels and no repository
        does -- the same stated limit the pinned tree's own unpacking carries.
        """
        ...

    def written(
        self, pin: ProjectSourcePin, lease: AgentAttemptWorkspaceLease
    ) -> LeasedWorkingTree:
        """Name what stands in this lease now, without keeping it as a candidate.

        The same reading a capture does, stopped one step earlier: an attempt
        asks this before it pays for anything the work is worth paying for, and
        an answer equal to the pin means the attempt changed nothing. Failing
        here is failing to name the work at all, so it leaves through
        `CandidateNotKept` like a capture does -- work that cannot be named now
        cannot be kept later either.
        """
        ...

    def changes(self, written: LeasedWorkingTree) -> ReadPatch:
        """The patch from the pinned tree to this one, read under this port's bound.

        Evidence, never a candidate: a rejected attempt's work must not survive
        as something a later run could take, but what it did has to be readable
        by whoever judges the rejection. An unchanged tree answers with nothing,
        because there is no patch to read.

        What comes back is `CANDIDATE_DIFF_READ_BYTES` at most, says whether that
        bound stopped the reading, and is not yet safe to show: the caller hands
        it to `patch_safe_to_show`, which redacts before it cuts.
        """
        ...
