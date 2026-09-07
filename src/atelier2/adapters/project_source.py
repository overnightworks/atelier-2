"""The project's own git repository, and the one boundary every git call crosses.

The pin is what makes a run repeatable: a commit is resolved once, and every later
question -- does this pin still resolve, what does its manifest declare, what does
the attempt work in -- is asked of that commit rather than of whatever the
operator's checkout happens to hold at the time.

What is unpacked into a lease is the tree alone. No `.git` travels with it, so the
directory is material rather than a repository, and a provider that wants history
has none. It is the *whole* tree, and it is the tree as the pin holds it: the
material is checked out of a temporary index rather than exported, because an
export honours `export-ignore` and would hand the attempt a directory that is
quietly missing paths the pin carries -- paths whose absence a later reader could
only read as the attempt having deleted them. The checkout runs inside a
repository this boundary makes for it, so no `filter` the operator's own
repository declares can rewrite a byte on the way in.

Nothing here is isolation: the unpacking runs as this process's own user, into the
blank directory the attempt leased, and that directory's own sentence about not
being a sandbox is left standing.

The tree is entered through the identity the lease attested rather than through the
path it was named by, because unpacking happens after the lease is taken and before
the provider starts -- exactly the window in which a peer of this user could move
its own directory into that path.
"""

from __future__ import annotations

import os
import subprocess
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import IO

from atelier2.adapters.leased_directory import entered_leased_directory
from atelier2.contracts.project_sources import GitObjectFormat, ProjectSourcePin
from atelier2.ports.agent_executions import AgentAttemptWorkspaceLease
from atelier2.ports.project_source import ProjectSourceUnavailable

_GIT_EXECUTABLE_NAME = "git"
"""Resolved from the serving process's own path, like every command a project names."""

_HEAD_REVISION = "HEAD"
"""What git calls the commit a checkout currently stands on."""

_MATERIALIZED_INDEX_NAME = "materialize.index"
_PLUMBING_REPOSITORY_NAME = "materialize.git"

_ALTERNATE_OBJECTS_FILE = ("objects", "info", "alternates")
"""Where git is told the other object database a repository may read through."""

NO_GIT_TEMPLATE = "--template="
"""What keeps a repository this product creates free of anybody else's files.

`git init` copies a template directory into every repository it makes, and the
default one on a machine can be replaced -- with hooks, or with an
`info/attributes` naming a filter. An empty name copies nothing at all.
"""

_INHERITED_ENVIRONMENT_NAMES = ("PATH", "LANG", "LC_ALL", "TZ")
"""The only names a git child keeps from this process: where git is, and who reads it.

An allowlist rather than a scrub list, because the environment of a serving
process is not a fixed set: a variable nobody here named is a variable nobody
here reasoned about, and the list of ways git reads one only grows. What that
keeps out is not hypothetical -- `GIT_OBJECT_DIRECTORY` redirects where a
candidate's objects land, `GIT_INDEX_FILE` and `GIT_COMMON_DIR` replace the state
a capture stages into, `GIT_NAMESPACE` moves the refs out from under the store,
`GIT_TEMPLATE_DIR` installs hooks into a repository this product creates,
`GIT_ASKPASS` and the credential variables hand a secret to a program of
somebody else's choosing, and `GIT_TRACE*` writes this boundary's traffic to a
file nobody asked for. A provider token in any variable at all has no business
in a subprocess that writes a project's content.

The locale is kept because a refusal at this boundary is read by the operator,
and their own git speaks their language.
"""

_DECLARED_GIT_ENVIRONMENT = {
    "HOME": os.devnull,
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_SYSTEM": os.devnull,
    "GIT_ATTR_NOSYSTEM": "1",
    "GIT_CONFIG_COUNT": "0",
    "GIT_TERMINAL_PROMPT": "0",
}
"""What every git child is told instead, whatever the host would have said.

The machine's own git configuration is not part of any project: it can declare a
`clean` filter, a git-lfs smudge, an author identity or a credential helper, and
each would change what this boundary reads or writes for reasons no run ever
declared. So there is no system, no global and no inherited parameter
configuration left to read, no system attributes file, and no home directory
under which either could be found -- `/dev/null` is not a directory, so nothing
resolves beneath it. A call that would have asked a human for a credential fails
instead of hanging on a terminal nobody is watching.
"""

_HOOK_FREE_ARGUMENTS = ("-c", f"core.hooksPath={os.devnull}")
"""What is prepended to every git call here, so no repository's scripts run.

Hooks are the one host opinion configuration cannot switch off: they live in the
repository rather than in a file this boundary can redirect. A `pre-push` in the
operator's checkout can rewrite what a candidate carries, a `reference-transaction`
in the store can veto one outright, and both would run as this process's user for
reasons no run declared. Pointed at a path that is not a directory, git finds
none. Command-line configuration outranks every file, so no repository can undo it.
"""


class GitRefused(Exception):
    """A git call did not answer, so its caller says what that means for it.

    Deliberately not a port failure: the same call is a source being unavailable
    to one owner and a candidate not being captured to another, and only the
    caller knows which sentence its own users were promised.
    """


def isolated_git_environment(**declared: str) -> dict[str, str]:
    """The whole environment a git child gets, built rather than inherited."""

    return {
        **{
            name: os.environ[name]
            for name in _INHERITED_ENVIRONMENT_NAMES
            if name in os.environ
        },
        **_DECLARED_GIT_ENVIRONMENT,
        **declared,
    }


MAXIMUM_REFUSAL_WORDS_BYTES = 8_192
"""How much of a failed git child's own words one refusal quotes.

What a child may write to its standard error is decided by the repository it
reads, not by this product: a single `.gitattributes` line git dislikes is
warned about once per path it touches. That stream is written to a file rather
than to a pipe, so the child never blocks on it; what a refusal *carries* is
bounded here, because a refusal is one sentence an operator reads.
"""


@dataclass(frozen=True, slots=True)
class GitOutputUnderBound:
    """What one git child wrote, and whether its caller's bound stopped it short.

    The second half is a fact only this boundary holds: past the bound the child
    is killed, so nothing downstream can tell a patch that ended there from one
    that was cut, and a reader shown a prefix without being told so reads it as
    the whole.
    """

    written: bytes
    stopped_at_the_bound: bool


def answered_git(
    arguments: Sequence[str],
    *,
    working_directory: str,
    environment: Mapping[str, str],
    passed_descriptors: tuple[int, ...] = (),
    standard_input: int | IO[bytes] = subprocess.DEVNULL,
) -> bytes:
    """Run one git command where it was told to, and answer with what it wrote.

    For every answer whose size this repository's own shape bounds -- a name, a
    listing, an index. An answer the working tree decides the size of is read
    through `answered_git_under_bound` instead.
    """

    return answered_git_under_bound(
        arguments,
        working_directory=working_directory,
        environment=environment,
        passed_descriptors=passed_descriptors,
        standard_input=standard_input,
        maximum_output_bytes=None,
    ).written


def answered_git_under_bound(
    arguments: Sequence[str],
    *,
    working_directory: str,
    environment: Mapping[str, str],
    passed_descriptors: tuple[int, ...] = (),
    standard_input: int | IO[bytes] = subprocess.DEVNULL,
    maximum_output_bytes: int | None,
) -> GitOutputUnderBound:
    """Run one git command, reading no more of its answer than a caller declared.

    The working directory is entered by the child rather than named to git with
    `-C`, so a caller holding an open descriptor can pass `/proc/self/fd/<n>`
    together with that descriptor and have the child land in the very directory
    its owner checked.

    What a caller feeds in is an open file rather than bytes, because one of the
    things fed to git here is a pack of a whole project tree, and holding that in
    this process's memory buys nothing.

    `maximum_output_bytes` is what a caller says when the answer is a document
    whose size the working tree decides -- a patch of an arbitrarily large tree.
    Then exactly that many bytes are read and the child is stopped, so no answer
    can grow this process past a bound its caller declared, and the caller reads
    a prefix and is told that is what it is. No bound at all reads the answer
    whole.

    What git writes to its standard error goes to a temporary file, never to a
    pipe: a bounded read stops reading standard output while the child is still
    running, and a child filling a pipe nobody is draining blocks on that write
    for as long as the parent waits for it.
    """

    argv = (_GIT_EXECUTABLE_NAME, *_HOOK_FREE_ARGUMENTS, *arguments)
    with tempfile.TemporaryFile() as refused:
        try:
            started = subprocess.Popen(
                argv,
                cwd=working_directory,
                env=dict(environment),
                pass_fds=passed_descriptors,
                stdin=standard_input,
                stdout=subprocess.PIPE,
                stderr=refused,
            )
        except OSError as error:
            raise GitRefused(
                f"git {' '.join(arguments)} could not be started in "
                f"{working_directory}: {error}"
            ) from error
        with started as child:
            return _what_git_wrote(
                child, refused, arguments, working_directory, maximum_output_bytes
            )


def _what_git_wrote(
    child: subprocess.Popen[bytes],
    refused: IO[bytes],
    arguments: Sequence[str],
    working_directory: str,
    maximum_output_bytes: int | None,
) -> GitOutputUnderBound:
    """Read one git child out, and let its exit speak only where it finished.

    One byte past the caller's bound is read on purpose: reading exactly the
    bound cannot tell a child that stopped there from one that had more to say,
    and that difference is the whole reason the exit code is not consulted in
    the second case. A child this call killed answers a code it did not choose,
    and reading that as a refusal would turn every large patch into an error.
    """

    assert child.stdout is not None
    if maximum_output_bytes is None:
        written = child.stdout.read()
    else:
        written = child.stdout.read(maximum_output_bytes + 1)
        if len(written) > maximum_output_bytes:
            child.kill()
            child.wait()
            return GitOutputUnderBound(written[:maximum_output_bytes], True)
    child.wait()
    _refuse_a_failed_git(child, arguments, working_directory, refused)
    return GitOutputUnderBound(written, False)


def _refuse_a_failed_git(
    child: subprocess.Popen[bytes],
    arguments: Sequence[str],
    working_directory: str,
    refused: IO[bytes],
) -> None:
    """Say what a git call that ran to its own end refused, in its own words."""

    if child.returncode == 0:
        return
    refused.seek(0)
    words = refused.read(MAXIMUM_REFUSAL_WORDS_BYTES)
    # The whole argument list is named, not just the subcommand: what a
    # refusal is about is the revision or path that was asked for.
    raise GitRefused(
        f"git {' '.join(arguments)} answered {child.returncode} in "
        f"{working_directory}: {words.decode('utf-8', 'replace').strip()}"
    )


@dataclass(frozen=True, slots=True)
class LeasedIndex:
    """A lease entered by its attested identity, and the index staged inside it.

    The three travel together because none of them means anything alone: the
    descriptor is what makes the entered path an identity rather than a name, and
    the index is what keeps one lease's staging out of every other one's.
    """

    entered: str
    descriptor: int
    index: Path


def answered_in_lease(
    arguments: Sequence[str], *, leased: LeasedIndex, git_directory: str
) -> bytes:
    """Run one git command inside a lease, against the repository that owns it.

    The index is always the lease's own temporary one, never the repository's:
    reading a tree into the index an operator works with would throw away
    whatever they had staged there.
    """

    return answered_git(
        arguments,
        working_directory=leased.entered,
        environment=isolated_git_environment(
            GIT_DIR=git_directory,
            GIT_WORK_TREE=".",
            GIT_INDEX_FILE=str(leased.index),
        ),
        passed_descriptors=(leased.descriptor,),
    )


def object_format_of(repository: str) -> GitObjectFormat:
    """How this repository names its objects, in git's own words.

    Asked rather than assumed, because two repositories of different formats
    cannot hold each other's objects at all: the name of a tree *is* its hash.
    """

    named = (
        answered_git(
            ("rev-parse", "--show-object-format"),
            working_directory=repository,
            environment=isolated_git_environment(),
        )
        .decode("utf-8", "replace")
        .strip()
    )
    try:
        return GitObjectFormat(named)
    except ValueError as error:
        raise GitRefused(
            f"the repository at {repository} names its objects as {named!r}, "
            "which is no format this product can keep work in"
        ) from error


def _tree_of(revision: str) -> str:
    return f"{revision}^{{tree}}"


class LocalGitProjectSource:
    """One local git repository as the source of every tree an attempt works in."""

    def __init__(self, project_root: Path) -> None:
        self._project_root = project_root.resolve()

    def head(self) -> ProjectSourcePin:
        """Pin what this repository stands on, refusing a root that is not one.

        A root below its repository's top level is refused rather than pinned:
        git answers for the whole repository, so a tree pinned there would carry
        everything beside the project too, and the manifest read at that commit
        would be the enclosing repository's rather than this project's.
        """

        top_level = Path(self._line(("rev-parse", "--show-toplevel")))
        if top_level != self._project_root:
            raise ProjectSourceUnavailable(
                f"the project source {self._project_root} is not the top level of "
                f"the repository it lies in ({top_level}), so a tree pinned here "
                "would carry that whole repository rather than this project"
            )
        commit = self._object_name(_HEAD_REVISION)
        tree = self._object_name(_tree_of(commit))
        try:
            return ProjectSourcePin(commit, tree)
        except ValueError as error:
            raise ProjectSourceUnavailable(
                f"the source at {self._project_root} answered {commit!r} and "
                f"{tree!r} for what it stands on, which name no commit and tree"
            ) from error

    def attest(self, pin: ProjectSourcePin) -> None:
        standing = self._object_name(_tree_of(pin.commit))
        if standing != pin.tree:
            raise ProjectSourceUnavailable(
                f"commit {pin.commit} in {self._project_root} names the tree "
                f"{standing}, not the tree {pin.tree} this attempt was pinned to"
            )

    def read(self, pin: ProjectSourcePin, path: PurePosixPath) -> bytes:
        return self._answered(("show", f"{pin.commit}:{path}"))

    def materialize(
        self, pin: ProjectSourcePin, lease: AgentAttemptWorkspaceLease
    ) -> None:
        """Check the pinned tree out into the leased directory, whole and unfiltered.

        A checkout rather than an export: `git archive` drops every path a
        `.gitattributes` marks `export-ignore`, so the attempt would work on less
        than it was pinned to -- and whoever compared its work against the pin
        afterwards could only read those missing paths as the attempt having
        deleted them.

        And out of a repository this boundary made rather than out of the
        operator's own, because a checkout carries its own `.git/config` too. That
        configuration can declare a `filter` driver that the project's own
        `.gitattributes` points paths at, and the driver's smudge would write
        content into the lease that the pinned tree does not carry. What comes
        back is captured under no filter at all, so that content would come home
        as work the attempt never did.
        """

        with tempfile.TemporaryDirectory() as staging:
            unfiltered = self._plumbing_repository(Path(staging))
            index = Path(staging) / _MATERIALIZED_INDEX_NAME
            with entered_leased_directory(
                lease.working_directory, lease.device, lease.inode
            ) as (entered, descriptor):
                self._checked_out(
                    LeasedIndex(entered, descriptor, index),
                    str(unfiltered),
                    pin,
                    lease,
                )

    def _plumbing_repository(self, staging: Path) -> Path:
        """A bare repository of this boundary's own making, borrowing the source's
        objects.

        It declares no filter and copies no template, so the only thing a
        `.gitattributes` in the pinned tree can name is a driver that does not
        exist -- which git skips. The pinned objects are read through an alternate
        rather than copied, so nothing is duplicated to gain that.
        """

        borrowed = self._line(
            ("rev-parse", "--path-format=absolute", "--git-path", "objects")
        )
        made = staging / _PLUMBING_REPOSITORY_NAME
        try:
            answered_git(
                (
                    "init",
                    "--bare",
                    "--quiet",
                    NO_GIT_TEMPLATE,
                    f"--object-format={object_format_of(str(self._project_root)).value}",
                    str(made),
                ),
                working_directory=str(staging),
                environment=isolated_git_environment(),
            )
            made.joinpath(*_ALTERNATE_OBJECTS_FILE).write_text(
                f"{borrowed}\n", encoding="utf-8"
            )
        except (GitRefused, OSError) as error:
            raise ProjectSourceUnavailable(
                f"no filter-free repository could be made to check "
                f"{self._project_root} out of: {error}"
            ) from error
        return made

    def _checked_out(
        self,
        leased: LeasedIndex,
        git_directory: str,
        pin: ProjectSourcePin,
        lease: AgentAttemptWorkspaceLease,
    ) -> None:
        try:
            for arguments in (
                ("read-tree", pin.tree),
                ("checkout-index", "--all", "--force"),
            ):
                answered_in_lease(arguments, leased=leased, git_directory=git_directory)
        except GitRefused as error:
            raise ProjectSourceUnavailable(
                f"the tree {pin.tree} of {self._project_root} could not be checked "
                f"out into the workspace of attempt {lease.attempt_id.value}: {error}"
            ) from error

    def _object_name(self, revision: str) -> str:
        return self._line(("rev-parse", "--verify", revision))

    def _line(self, arguments: tuple[str, ...]) -> str:
        return self._answered(arguments).decode("utf-8", "replace").strip()

    def _answered(self, arguments: tuple[str, ...]) -> bytes:
        try:
            return answered_git(
                arguments,
                working_directory=str(self._project_root),
                environment=isolated_git_environment(),
            )
        except GitRefused as error:
            raise ProjectSourceUnavailable(
                f"the project source at {self._project_root} could not be read: {error}"
            ) from error
