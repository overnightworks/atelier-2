"""The operator's command line: serve, run, resolve, migrate, connect, or speak MCP."""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import assert_never

from atelier2.adapters.agent_workspaces import (
    AgentScratchRootRefused,
    LocalAgentAttemptWorkspaceOwner,
)
from atelier2.adapters.claude_subscription import (
    MANAGED_POLICY_ROOTS,
    ClaudeExecutableUnsupported,
    ClaudeManagedPolicyPresent,
    ClaudeSubscriptionSettings,
    attest_atelier_doors_invocation,
    attest_no_managed_policy,
    attest_workspace_tool_invocation,
    verify_claude_capability,
)
from atelier2.adapters.codex_subscription import (
    CodexContainmentUnattested,
    CodexExecutableUnsupported,
    CodexSandboxMode,
    CodexSubscriptionSettings,
    attest_codex_containment,
    verify_codex_capability,
)
from atelier2.adapters.dbos.host_configuration import DbosHostConfigurationChannel
from atelier2.adapters.dbos.runtime import create_canonical_engine
from atelier2.adapters.dbos.schema import (
    StoreMigrationRefused,
    UnsupportedSchemaVersion,
    initialize_schema,
)
from atelier2.adapters.github import GitHubCredentialUnresolvable
from atelier2.adapters.grok_subscription import (
    GrokExecutableUnsupported,
    GrokSubscriptionSettings,
    attest_grok_workspace_tool_invocation,
    verify_grok_capability,
)
from atelier2.adapters.project_verification import refuse_unusable_project_checkout
from atelier2.application.project_connections import (
    ConnectionProjectUnknown,
    ProjectSourceConnectionCollision,
    ProjectSourceConnectionConflict,
    ProjectSourceConnectionMoved,
    ProjectSourceConnectionPublished,
    ProjectSourceConnectionUnchanged,
    UnpublishableConnection,
    connect_project_source,
)
from atelier2.application.refusals import (
    DurableStateCorrupt,
    WriteUnavailable,
)
from atelier2.contracts.host_configuration import (
    PROJECT_UNKNOWN,
    HostConfigurationUnreadable,
    ProjectId,
    ProjectRootMissing,
    ProjectUnknown,
    SourceReference,
)
from atelier2.host.address import DEFAULT_HOST, DEFAULT_PORT, DEFAULT_SERVICE_URL
from atelier2.host.definition_source_command import (
    add_definition_source_parser,
    execute_definition_source,
)
from atelier2.host.mcp_command import execute_mcp
from atelier2.host.migrate_command import describe_migration, execute_migrate
from atelier2.host.provider_canary import (
    PROVIDER_CANARY_TERMINAL_TIMEOUT_SECONDS,
    ProviderCanaryAnswerUnreadable,
    ProviderCanaryDiscoveryFailed,
    ProviderCanaryHttpRefused,
    ProviderCanaryProcessTimedOut,
    ProviderCanaryServerUnavailable,
    ProviderCanarySettings,
    ProviderLayerReceiptOutcome,
    ProviderLayerReceiptStatus,
    default_provider_canary_state_directory,
    execute_provider_canaries,
)
from atelier2.host.run_command import (
    DEFAULT_CATALOG_POSITION,
    AgentBindingSource,
    NamedRunOrder,
    NameOrder,
    RunCommandRefusal,
    RunOrder,
    SuppliedOrder,
    describe_receipt,
    describe_resolution,
    execute_named_run,
    execute_run,
    resolve_published_name,
)
from atelier2.host.seat_command import (
    add_seat_arguments,
    add_seat_parser,
    declared_seat,
    execute_seat,
)
from atelier2.host.serving import (
    HostSettings,
    # `serving` owns how the doors vector is composed -- door tools, server
    # name, this instance's own loopback address -- so the command line borrows
    # that composition for its attest instead of re-spelling the vector, which
    # would let the attested and the launched invocation drift apart.
    _atelier_doors_settings,
    api_limits,
    event_poll_backoff,
    serve,
)
from atelier2.ports.project_source import ProjectSourceUnavailable
from atelier2.ports.project_verification import ProjectVerificationUndeclared

CONNECT_DESCRIPTION = """\
Connect a configured project to its external source.

This command is offline, like migrate: it does not serve and does not create
a store. It appends one immutable connection revision on the host
configuration channel, binding the project to a source kind, an opaque
source address the connected platform adapter interprets, an optional
adapter-owned source ref that is not connection identity, a credential-directory
reference, the chosen auth method, and the connecting actor. GitHub requires a
branchless owner/name address and a separate nonempty source ref. The credential
value itself never enters the record; the host resolves it from the named
directory at composition. Repeating the exact same connect changes nothing.

An active connection of the same source kind at a different address refuses
by default, to catch a typo. `--move` instead publishes two revisions: the
old address continues as `DISCONNECTED`, and the new address is `CONNECTED`
-- history stays, nothing is deleted. A running serve reads the connection
only at startup, so a moved connection needs one restart to take effect; the
auto-redeploy performs that restart on its next deploy.
"""

MIGRATE_DESCRIPTION = """\
Raise an existing canonical store to the current product schema.

This command is offline. It does not start a server, does not open a runtime,
and does not create a store. Stop the process that owns the file first. A
write lock the command can see is refused; an idle reader is not always
visible, so stopping the serve is the operator's gate, not this process's.

The file is inspected, then raised one published step at a time. Each step
ends with the fingerprint ADR 0001 names. Any doubt rolls the transaction
back, so a failed hop leaves the predecessor unaltered. The built steps run
from schema version 13 upward, one published version at a time. Older
published predecessors, and unknown or future versions, are refused by name.

A store already on the current schema is left unaltered and said to be
already current.
"""


RESOLVE_DESCRIPTION = """\
Ask a served Atelier which published revision a workflow name holds, and print
the lineage, the member number and the exact revision hash.

This command starts nothing, which is the whole of what separates it from `run
--name`: that one asks this same question and then runs the answer, so use this
one to look before you leap. Every refusal is the service's own - an unadmitted
name, a retired lineage, a position the lineage does not hold - and each one ends
this command unsuccessfully, there and in `run --name` alike.
"""

RUN_DESCRIPTION = """\
Run a workflow on a served Atelier API and wait for its end -- either the
document named by --workflow, or the one a catalog name holds via --name. Every
agent output the run produced is written to standard output, as the exact bytes
its hash covers and with no separator added, so a piped output is the output;
the run, its revision, its terminal hash and one hash per output are written to
standard error. The exit code is 0 only for a run whose whole event history this
command read and whose terminal event it saw.

The command owns nothing. It publishes the workflow document and each binding
file through the public API of the service named by --service, and starts the
run there, exactly as any other client would. All three publications are
idempotent, and the run identity is derived from the published hashes, so the
same command run twice reports one run instead of paying for two.

With --name nothing is published for the workflow: the service is asked which
revision the name holds -- the same question `resolve` asks, and its refusals are
handed on unchanged -- and that revision is what starts. --position picks the
member of the lineage, so a name can be run at an exact revision rather than
only at its head.

--input NAME=VALUE and --input-file NAME=PATH fill the graph_inputs the
workflow declared. VALUE and the file are exact JSON text; the command
publishes each one as an artifact through POST /artifacts before the start,
and the run names only the address that publication answered -- never the
bytes themselves. A name the document never declared, a declared name that
is missing, and a value that is not valid JSON for the schema the document
pinned are each refused by name. A typed 422 from the service is handed on
in the service's own words.

Not supported yet, and refused rather than faked:

  a wait        a run that stops for a human ends this command unsuccessfully
                and says which capability is missing; answering a wait from here
                is not built.

There is no verdict exit code: output contracts (issue #57) do not exist yet,
so the exit code reports the run's disposition and nothing more.
"""


MCP_DESCRIPTION = """\
Speak MCP on standard input and standard output against a served Atelier API.

Messages are newline-delimited JSON-RPC: one object per line, no header.

This command starts no listener and invents no credential. It is a child
process a client launches, and it talks to the public HTTP API of --service
exactly as the browser and `run` already do. The API has no caller
authentication today: #82 is human OIDC, ADR 0009 (machine credentials) is
not landed, so this child refuses any service that is not a literal
loopback address rather than pretending a token exists.

The five tools are list_workflows, start_run, run_status, answer_wait and
publish_artifact. MCP start_run accepts artifact and work-item orders only;
publish inline material first, then start with its returned artifact hash.
Each tool calls an existing door. A typed problem from the service is the
tool's own answer, field pointers included.
"""

PROVIDER_CANARY_DESCRIPTION = """\
Run each configured live provider vector once through the served Atelier API.

The command first reads a bounded startable agent-configuration list and resolves
every matching admitted provider-canary workflow. Any failure in that discovery
phase exits nonzero and leaves all vector receipts byte-identical. It then starts
one fresh run per vector with the exact configuration hash and waits for a
terminal state. Once a vector enters execution, its outcome atomically replaces
that vector's provider-probe-receipt/v1 beneath the XDG state directory, including
a failure replacing a still-live success. The process and every vector have hard
deadlines. Provider output stays with the durable run and never enters the receipt.
"""

BINDING_SEPARATOR = "="


@dataclass(frozen=True)
class _DeclaredSubscription[SettingsT]:
    """A fully declared provider deployment, startable or named-unstartable.

    Incomplete flags still refuse the command. A pin or attest failure keeps
    the declaration so the house can serve, and names why this executor cannot
    be bound.
    """

    settings: SettingsT | None
    start_refusal: str | None = None
    workspace_tools_start_refusal: str | None = None


def main(arguments: Sequence[str] | None = None) -> int:
    parser = _argument_parser()
    parsed = parser.parse_args(arguments)
    if parsed.command == "serve":
        return _serve(parser, parsed)
    if parsed.command == "run":
        return _run(parser, parsed)
    if parsed.command == "resolve":
        return _resolve(parser, parsed)
    if parsed.command == "migrate":
        return _migrate(parsed)
    if parsed.command == "connect":
        return _connect(parsed)
    if parsed.command == "definition-source":
        return execute_definition_source(parsed)
    if parsed.command == "mcp":
        return execute_mcp(parsed.service, sys.stdin.buffer, sys.stdout.buffer)
    if parsed.command == "seat":
        return execute_seat(parsed)
    if parsed.command == "provider-canary":
        return _provider_canary(parser, parsed)
    parser.error("a command is required")


def _given[ValueT](**flags: ValueT | None) -> dict[str, ValueT]:
    """Only the answers the operator actually gave.

    A flag nobody passed is not an answer, so it is left out and the field's own
    default stands. That keeps one named place for every default instead of
    repeating each of them here as `or DEFAULT`.
    """

    return {name: value for name, value in flags.items() if value is not None}


def _serve(parser: argparse.ArgumentParser, parsed: argparse.Namespace) -> int:
    try:
        limits = api_limits(
            **_given(
                event_page_size=parsed.event_page_size,
                maximum_control_queries=parsed.maximum_control_queries,
                maximum_event_poll_queries=parsed.maximum_event_poll_queries,
                maximum_query_admission_wait_milliseconds=(
                    parsed.query_admission_wait_milliseconds
                ),
            )
        )
        backoff = event_poll_backoff(
            **_given(
                initial_delay_seconds=parsed.initial_event_poll_delay_seconds,
                maximum_delay_seconds=parsed.maximum_event_poll_delay_seconds,
                multiplier=parsed.event_poll_delay_multiplier,
            )
        )
        claude = _claude_subscription_settings(parser, parsed)
        grok = _grok_subscription_settings(parser, parsed)
        codex = _codex_subscription_settings(parser, parsed)
        settings = HostSettings(
            limits=limits,
            event_poll_backoff=backoff,
            **_given(
                sqlite_lock_timeout_seconds=parsed.sqlite_lock_timeout_seconds,
                agent_termination_grace_seconds=(
                    parsed.agent_termination_grace_seconds
                ),
                model_inspection_timeout_seconds=(
                    parsed.model_inspection_timeout_seconds
                ),
            ),
            database_path=parsed.database,
            effect_store_path=parsed.effect_store,
            effect_adapter_revision=parsed.effect_adapter_revision,
            effect_destination=parsed.effect_destination,
            application_version=parsed.application_version,
            source_commit=parsed.source_commit,
            source_tree=parsed.source_tree,
            frontend_dist=parsed.frontend_dist,
            host=parsed.host,
            port=parsed.port,
            agent_scratch_root=_attested_agent_scratch_root(parser, parsed),
            project_id=_declared_project_id(parser, parsed),
            project_root=_declared_project_root(parser, parsed),
            aco_executable=parsed.aco_executable,
            claude_subscription=claude.settings,
            claude_workspace_tools=parsed.claude_workspace_tools,
            claude_atelier_doors=parsed.claude_atelier_doors,
            claude_start_refusal=claude.start_refusal,
            claude_workspace_tools_start_refusal=claude.workspace_tools_start_refusal,
            grok_subscription=grok.settings,
            grok_workspace_tools=parsed.grok_workspace_tools,
            grok_start_refusal=grok.start_refusal,
            grok_workspace_tools_start_refusal=grok.workspace_tools_start_refusal,
            codex_subscription=codex.settings,
            codex_start_refusal=codex.start_refusal,
            terminal_seat=declared_seat(parsed),
        )
        settings = _atelier_doors_attested(settings)
    except ValueError as refusal:
        parser.error(str(refusal))
    try:
        serve(settings)
    except KeyboardInterrupt:
        # The operator's interrupt is how the house stops; it is not a failure.
        pass
    except ValueError as refusal:
        parser.error(str(refusal))
    except GitHubCredentialUnresolvable as refusal:
        # The live-GitHub token is read once by reference when the effect adapter
        # opens at startup; a missing, empty, or unreadable file fails the whole
        # start rather than serving open-pr silently disabled (`#430`).
        parser.error(str(refusal))
    except (
        ProjectUnknown,
        ProjectRootMissing,
        HostConfigurationUnreadable,
    ) as refusal:
        parser.error(str(refusal))
    return 0


def _run(parser: argparse.ArgumentParser, parsed: argparse.Namespace) -> int:
    bindings = tuple(_binding_source(parser, declared) for declared in parsed.binding)
    orders = _supplied_orders(parser, parsed)
    if parsed.position is not None and parsed.name is None:
        # A position without a name would be read and then ignored, which is the
        # quietest way for a command to disagree with the operator.
        parser.error("--position selects a member of --name, so it needs one")
    try:
        if parsed.name is not None:
            report = execute_named_run(
                NamedRunOrder(
                    service_url=parsed.service,
                    name=parsed.name,
                    bindings=bindings,
                    run_id=parsed.run_id,
                    position=parsed.position or DEFAULT_CATALOG_POSITION,
                    orders=orders,
                )
            )
        else:
            report = execute_run(
                RunOrder(
                    service_url=parsed.service,
                    workflow_document=_file_bytes(parser, parsed.workflow),
                    bindings=bindings,
                    run_id=parsed.run_id,
                    orders=orders,
                )
            )
    except RunCommandRefusal as refusal:
        print(refusal, file=sys.stderr)
        return 1
    for output in report.outputs:
        sys.stdout.buffer.write(output.output)
    sys.stdout.buffer.flush()
    print(describe_receipt(report), file=sys.stderr)
    return 0


def _migrate(parsed: argparse.Namespace) -> int:
    try:
        report = execute_migrate(parsed.database)
    except StoreMigrationRefused as refusal:
        print(refusal, file=sys.stderr)
        return 1
    print(describe_migration(report))
    return 0


def _describe_provider_layer_receipt_status(status: ProviderLayerReceiptStatus) -> str:
    """The one journal line naming whether existing receipts still apply (#1124)."""

    match status.outcome:
        case ProviderLayerReceiptOutcome.NO_READABLE_PRIOR_RECEIPT:
            return (
                "no readable prior receipt (this run's provider layer: "
                f"{status.current_digest.value[:8]})"
            )
        case ProviderLayerReceiptOutcome.RECEIPTS_KEPT:
            return "receipts kept (provider layer unchanged)"
        case ProviderLayerReceiptOutcome.RECEIPTS_INVALIDATED:
            assert status.previous_digest is not None
            return (
                "receipts invalidated (provider layer changed: "
                f"{status.previous_digest.value[:8]} → {status.current_digest.value[:8]})"
            )
        case _ as unreachable:
            assert_never(unreachable)


def _provider_canary(
    parser: argparse.ArgumentParser, parsed: argparse.Namespace
) -> int:
    try:
        settings = ProviderCanarySettings(
            service_url=parsed.service,
            workflow_directory=parsed.workflow_directory,
            state_directory=parsed.state_directory,
            terminal_timeout_seconds=parsed.terminal_timeout_seconds,
        )
    except ValueError as refusal:
        parser.error(str(refusal))
    try:
        report = execute_provider_canaries(
            settings,
            on_provider_layer_status=lambda status: print(
                _describe_provider_layer_receipt_status(status)
            ),
        )
    except (
        OSError,
        ProviderCanaryAnswerUnreadable,
        ProviderCanaryDiscoveryFailed,
        ProviderCanaryHttpRefused,
        ProviderCanaryProcessTimedOut,
        ProviderCanaryServerUnavailable,
    ) as refusal:
        print(f"provider canary refused: {refusal}", file=sys.stderr)
        return 1
    for failure in report.failures:
        print(
            f"provider canary {failure.vector.value} failed with "
            f"{failure.problem_code.value}: {failure.detail}",
            file=sys.stderr,
        )
    if report.failed:
        return 1
    print(f"provider canary: {report.attempted} vector(s) succeeded")
    return 0


def _connect(parsed: argparse.Namespace) -> int:
    if not parsed.database.is_file() or parsed.database.stat().st_size == 0:
        print(
            f"{parsed.database} is not a database file; "
            "this command does not create a store",
            file=sys.stderr,
        )
        return 1
    if parsed.source_kind == "github":
        try:
            SourceReference(parsed.source_ref)
        except (TypeError, ValueError):
            print(
                "a github connection requires a nonempty --source-ref",
                file=sys.stderr,
            )
            return 1
        if "@" in parsed.source_address:
            print(
                "a github connection requires branchless owner/name in "
                "--source-address; put the branch in --source-ref",
                file=sys.stderr,
            )
            return 1
    engine = create_canonical_engine(parsed.database)
    try:
        try:
            initialize_schema(engine)
        except UnsupportedSchemaVersion as refusal:
            print(refusal, file=sys.stderr)
            return 1
        channel = DbosHostConfigurationChannel(engine)
        result = connect_project_source(
            parsed.project_id,
            parsed.source_kind,
            parsed.source_address,
            parsed.credential_directory,
            parsed.auth_method,
            parsed.actor,
            channel,
            channel,
            source_ref=parsed.source_ref,
            move=parsed.move,
        )
    finally:
        engine.dispose()
    match result:
        case ProjectSourceConnectionPublished(revision):
            print(
                f"connected project {revision.project_id.value!r} to "
                f"{revision.source_kind.value} source "
                f"{revision.source_address.value!r} as revision "
                f"{revision.revision_number}"
            )
            return 0
        case ProjectSourceConnectionUnchanged(revision):
            print(
                f"project {revision.project_id.value!r} is already connected to "
                f"{revision.source_kind.value} source "
                f"{revision.source_address.value!r}; revision "
                f"{revision.revision_number} is unchanged"
            )
            return 0
        case ProjectSourceConnectionMoved(disconnected, connected):
            print(
                f"disconnected {disconnected.source_kind.value} source "
                f"{disconnected.source_address.value!r} from project "
                f"{disconnected.project_id.value!r} as revision "
                f"{disconnected.revision_number}"
            )
            print(
                f"connected project {connected.project_id.value!r} to "
                f"{connected.source_kind.value} source "
                f"{connected.source_address.value!r} as revision "
                f"{connected.revision_number}"
            )
            return 0
        case ConnectionProjectUnknown():
            print(
                f"{PROJECT_UNKNOWN}: the project id is malformed or has no "
                "configured root",
                file=sys.stderr,
            )
            return 1
        case UnpublishableConnection():
            print(
                "the given values do not make one connection revision",
                file=sys.stderr,
            )
            return 1
        case ProjectSourceConnectionConflict() | ProjectSourceConnectionCollision():
            print(
                "the connection revision collides with one already recorded",
                file=sys.stderr,
            )
            return 1
        case WriteUnavailable(detail):
            print(
                detail or "the configuration channel could not be written",
                file=sys.stderr,
            )
            return 1
        case DurableStateCorrupt():
            print("the configuration channel is corrupt", file=sys.stderr)
            return 1
        case _ as unreachable:
            assert_never(unreachable)


def _resolve(parser: argparse.ArgumentParser, parsed: argparse.Namespace) -> int:
    """Answer which revision a name holds. Start nothing, and say nothing else."""

    del parser
    order = NameOrder(
        service_url=parsed.service, name=parsed.name, position=parsed.position
    )
    try:
        resolution = resolve_published_name(order)
    except RunCommandRefusal as refusal:
        print(refusal, file=sys.stderr)
        return 1
    print(describe_resolution(resolution))
    return 0


def _binding_source(
    parser: argparse.ArgumentParser, declared: str
) -> AgentBindingSource:
    role, separator, path = declared.partition(BINDING_SEPARATOR)
    if not separator or not role or not path:
        parser.error(
            f"--binding takes role{BINDING_SEPARATOR}agent-file.json, not {declared!r}"
        )
    return AgentBindingSource(role, _file_bytes(parser, Path(path)))


def _named_assignment(
    parser: argparse.ArgumentParser, flag: str, declared: str
) -> tuple[str, str]:
    name, separator, value = declared.partition(BINDING_SEPARATOR)
    if not separator or not name or not value:
        parser.error(f"{flag} takes NAME{BINDING_SEPARATOR}VALUE, not {declared!r}")
    return name, value


def _supplied_orders(
    parser: argparse.ArgumentParser, parsed: argparse.Namespace
) -> tuple[SuppliedOrder, ...]:
    collected: list[tuple[str, bytes]] = []
    for declared in parsed.input:
        name, text = _named_assignment(parser, "--input", declared)
        collected.append((name, text.encode()))
    for declared in parsed.input_file:
        name, path = _named_assignment(parser, "--input-file", declared)
        collected.append((name, _file_bytes(parser, Path(path))))
    seen: set[str] = set()
    orders: list[SuppliedOrder] = []
    for name, value in collected:
        if name in seen:
            parser.error(f"input {name!r} was supplied twice")
        seen.add(name)
        try:
            json.loads(value)
        except (json.JSONDecodeError, UnicodeDecodeError):
            parser.error(f"input {name!r} is not valid JSON for the pinned schema")
        orders.append(SuppliedOrder(name, value))
    return tuple(orders)


def _file_bytes(parser: argparse.ArgumentParser, path: Path) -> bytes:
    try:
        return path.read_bytes()
    except OSError as unreadable:
        parser.error(f"cannot read {path}: {unreadable.strerror}")


def _attested_agent_scratch_root(
    parser: argparse.ArgumentParser, parsed: argparse.Namespace
) -> Path | None:
    """Refuse an unusable scratch root before the server exists.

    A root that is a git worktree, is shared, or belongs to somebody else is
    refused here rather than at the first run, where the refusal would cost a
    started run and reach nobody but a log.
    """

    root: Path | None = parsed.agent_scratch_root
    if root is None:
        return None
    try:
        LocalAgentAttemptWorkspaceOwner(root).close()
    except AgentScratchRootRefused as refusal:
        parser.error(str(refusal))
    return root


def _declared_project_id(
    parser: argparse.ArgumentParser, parsed: argparse.Namespace
) -> ProjectId | None:
    """The project this process serves; the root is read from the channel."""

    raw: str | None = parsed.project_id
    if raw is None:
        if parsed.project_root is not None:
            parser.error(
                "--project-root writes the host configuration channel, so it "
                "needs --project-id"
            )
        return None
    try:
        return ProjectId(raw)
    except ProjectUnknown as refusal:
        parser.error(str(refusal))


def _declared_project_root(
    parser: argparse.ArgumentParser, parsed: argparse.Namespace
) -> Path | None:
    """Refuse a bootstrap root that cannot be pinned or declares no verification.

    `--project-root` writes the channel. The runtime then reads the mapping
    back. A root that is no repository of its own, and a root whose manifest
    states nothing, are both refused here -- where the operator who named it is
    still reading -- rather than at the first run that binds a node.
    """

    root: Path | None = parsed.project_root
    if root is None:
        return None
    try:
        refuse_unusable_project_checkout(root)
    except (ProjectSourceUnavailable, ProjectVerificationUndeclared) as refusal:
        parser.error(str(refusal))
    return root


def _claude_subscription_settings(
    parser: argparse.ArgumentParser, parsed: argparse.Namespace
) -> _DeclaredSubscription[ClaudeSubscriptionSettings]:
    """Compose the Claude subscription declaration only when fully named."""

    declared = (parsed.claude_executable, parsed.claude_credential_directory)
    if all(value is None for value in declared):
        if parsed.claude_workspace_tools:
            parser.error(
                "--claude-workspace-tools arms a second executor of the Claude "
                "deployment, so it needs --claude-executable and "
                "--claude-credential-directory beside it"
            )
        if parsed.claude_atelier_doors:
            parser.error(
                "--claude-atelier-doors arms a third executor of the Claude "
                "deployment, so it needs --claude-executable and "
                "--claude-credential-directory beside it"
            )
        return _DeclaredSubscription(None)
    if any(value is None for value in declared):
        parser.error(
            "serving Claude subscription agents requires --claude-executable "
            "and --claude-credential-directory together"
        )
    search_path = os.environ.get("PATH")
    if search_path is None:
        parser.error(
            "serving Claude subscription agents requires PATH in the server "
            "environment, because the launched provider inherits nothing else"
        )
    settings = ClaudeSubscriptionSettings(
        parsed.claude_executable, parsed.claude_credential_directory, search_path
    )
    # Pin and attest stay. A failure names this executor unstartable; serve
    # continues. Binding it is refused before any process.
    try:
        attest_no_managed_policy(settings.credential_directory, MANAGED_POLICY_ROOTS)
        verify_claude_capability(settings.executable)
    except (ClaudeExecutableUnsupported, ClaudeManagedPolicyPresent) as error:
        return _DeclaredSubscription(settings, start_refusal=str(error))
    tools_refusal = None
    if parsed.claude_workspace_tools:
        # Startability, not a version answer: the tool-bearing invocation is
        # the one whose flags decide what a node's process may touch, so the
        # deployment starts that exact vector once, here, rather than
        # discovering at the first bound node that it never spawns.
        try:
            attest_workspace_tool_invocation(settings)
        except ClaudeExecutableUnsupported as error:
            tools_refusal = str(error)
    return _DeclaredSubscription(settings, workspace_tools_start_refusal=tools_refusal)


def _atelier_doors_attested(settings: HostSettings) -> HostSettings:
    """Arming the doors executor always attests its exact launch vector.

    The doors twin of the workspace-tool attest above: startability, not a
    version answer, probed once at every serve rather than at the first bound
    node. It runs after `HostSettings` because the attested vector embeds this
    instance's own bound address, whose composition `serving` owns. A
    deployment already named unstartable is not probed again, and a failure
    names this one executor unstartable while the house still serves.
    """

    deployment = settings.claude_subscription
    if (
        not settings.claude_atelier_doors
        or deployment is None
        or settings.claude_start_refusal is not None
    ):
        return settings
    try:
        attest_atelier_doors_invocation(_atelier_doors_settings(deployment, settings))
    except ClaudeExecutableUnsupported as error:
        return replace(settings, claude_atelier_doors_start_refusal=str(error))
    return settings


def _grok_subscription_settings(
    parser: argparse.ArgumentParser, parsed: argparse.Namespace
) -> _DeclaredSubscription[GrokSubscriptionSettings]:
    """Compose the Grok subscription declaration only when fully named."""

    declared = (
        parsed.grok_executable,
        parsed.grok_workspace,
        parsed.grok_credential_directory,
    )
    if all(value is None for value in declared):
        if parsed.grok_workspace_tools:
            parser.error(
                "--grok-workspace-tools arms a second executor of the Grok "
                "deployment, so it needs --grok-executable, --grok-workspace "
                "and --grok-credential-directory beside it"
            )
        return _DeclaredSubscription(None)
    if any(value is None for value in declared):
        parser.error(
            "serving Grok subscription agents requires --grok-executable, "
            "--grok-workspace and --grok-credential-directory together"
        )
    search_path = os.environ.get("PATH")
    if search_path is None:
        parser.error(
            "serving Grok subscription agents requires PATH in the server "
            "environment, because the launched provider inherits nothing else"
        )
    settings = GrokSubscriptionSettings(
        parsed.grok_executable,
        parsed.grok_workspace,
        parsed.grok_credential_directory,
        search_path,
    )
    try:
        verify_grok_capability(settings.executable)
    except GrokExecutableUnsupported as error:
        return _DeclaredSubscription(settings, start_refusal=str(error))
    tools_refusal = None
    if parsed.grok_workspace_tools:
        # Startability, not a version answer: the tool-bearing invocation
        # is the one whose flags decide what a node's process may touch, so
        # the deployment starts that exact vector once, here, rather than
        # discovering at the first bound node that it never spawns.
        try:
            attest_grok_workspace_tool_invocation(settings)
        except GrokExecutableUnsupported as error:
            tools_refusal = str(error)
    return _DeclaredSubscription(settings, workspace_tools_start_refusal=tools_refusal)


def _codex_subscription_settings(
    parser: argparse.ArgumentParser, parsed: argparse.Namespace
) -> _DeclaredSubscription[CodexSubscriptionSettings]:
    """Compose the Codex subscription declaration only when fully named."""

    declared = (parsed.codex_executable, parsed.codex_credential_directory)
    if all(value is None for value in declared):
        return _DeclaredSubscription(None)
    if any(value is None for value in declared):
        parser.error(
            "serving Codex subscription agents requires --codex-executable "
            "and --codex-credential-directory together"
        )
    search_path = os.environ.get("PATH")
    if search_path is None:
        parser.error(
            "serving Codex subscription agents requires PATH in the server "
            "environment, because the launched provider inherits nothing else"
        )
    try:
        settings = CodexSubscriptionSettings(
            parsed.codex_executable,
            parsed.codex_credential_directory,
            search_path,
            CodexSandboxMode(parsed.codex_sandbox),
        )
    except ValueError as refusal:
        parser.error(str(refusal))
    # Pin and attest stay. A failure names this executor unstartable; serve
    # continues. Binding it is refused before any process.
    try:
        verify_codex_capability(settings.executable, settings.search_path)
        attest_codex_containment(settings)
    except (CodexExecutableUnsupported, CodexContainmentUnattested) as error:
        return _DeclaredSubscription(settings, start_refusal=str(error))
    return _DeclaredSubscription(settings)


def _argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="atelier2")
    commands = parser.add_subparsers(dest="command")
    serve_parser = commands.add_parser("serve", help="serve the local cockpit")
    serve_parser.add_argument("--database", type=Path, required=True)
    serve_parser.add_argument("--effect-store", type=Path, required=True)
    serve_parser.add_argument("--effect-adapter-revision", required=True)
    serve_parser.add_argument("--effect-destination", required=True)
    serve_parser.add_argument("--application-version", required=True)
    serve_parser.add_argument("--source-commit", required=True)
    serve_parser.add_argument("--source-tree", required=True)
    serve_parser.add_argument("--frontend-dist", type=Path, required=True)
    # The instance's own answers. Everything above this line says which store,
    # which port, which executable; these say how this instance behaves once
    # those are settled, and they are the values a second machine honestly wants
    # differently. Each is refused by its owner when it is out of range.
    serve_parser.add_argument("--event-page-size", type=int)
    serve_parser.add_argument("--maximum-control-queries", type=int)
    serve_parser.add_argument("--maximum-event-poll-queries", type=int)
    serve_parser.add_argument("--query-admission-wait-milliseconds", type=int)
    serve_parser.add_argument("--initial-event-poll-delay-seconds", type=float)
    serve_parser.add_argument("--maximum-event-poll-delay-seconds", type=float)
    serve_parser.add_argument("--event-poll-delay-multiplier", type=float)
    serve_parser.add_argument("--sqlite-lock-timeout-seconds", type=float)
    serve_parser.add_argument("--agent-termination-grace-seconds", type=float)
    serve_parser.add_argument("--model-inspection-timeout-seconds", type=float)
    serve_parser.add_argument("--host", default=DEFAULT_HOST)
    serve_parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    serve_parser.add_argument("--agent-scratch-root", type=Path)
    serve_parser.add_argument("--project-id")
    serve_parser.add_argument("--project-root", type=Path)
    serve_parser.add_argument("--aco-executable", type=Path)
    serve_parser.add_argument("--claude-executable", type=Path)
    serve_parser.add_argument("--claude-credential-directory", type=Path)
    serve_parser.add_argument(
        "--claude-workspace-tools",
        action="store_true",
        help=(
            "also serve the Claude executor whose invocation may read, write "
            "and run commands where the attempt stands. It runs as this user "
            "and is no sandbox; only a node whose binding requests the "
            "headless_with_tools capability reaches it"
        ),
    )
    serve_parser.add_argument(
        "--claude-atelier-doors",
        action="store_true",
        help=(
            "also serve the Claude executor whose invocation may choose, "
            "start and observe catalog runs through this instance's own "
            "loopback MCP door -- real billed children behind one node. "
            "Arming it always attests the exact door-bearing invocation; "
            "routine use additionally waits on the billed conformance probe "
            "the executor names"
        ),
    )
    serve_parser.add_argument("--grok-executable", type=Path)
    serve_parser.add_argument("--grok-workspace", type=Path)
    serve_parser.add_argument("--grok-credential-directory", type=Path)
    serve_parser.add_argument(
        "--grok-workspace-tools",
        action="store_true",
        help=(
            "also serve the Grok executor whose invocation may read, write "
            "and run commands where the attempt stands. It runs as this user "
            "and is no sandbox; only a node whose binding requests the "
            "headless_with_tools capability reaches it"
        ),
    )
    add_seat_arguments(serve_parser, required=False)
    serve_parser.add_argument("--codex-executable", type=Path)
    serve_parser.add_argument("--codex-credential-directory", type=Path)
    serve_parser.add_argument(
        "--codex-sandbox",
        choices=tuple(mode.value for mode in CodexSandboxMode),
        default=CodexSandboxMode.READ_ONLY.value,
    )
    migrate_parser = commands.add_parser(
        "migrate",
        help="raise an existing store to the current schema, offline",
        description=MIGRATE_DESCRIPTION,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    migrate_parser.add_argument("--database", type=Path, required=True)
    connect_parser = commands.add_parser(
        "connect",
        help="connect a configured project to its external source, offline",
        description=CONNECT_DESCRIPTION,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    connect_parser.add_argument("--database", type=Path, required=True)
    connect_parser.add_argument("--project-id", required=True)
    connect_parser.add_argument(
        "--source-kind",
        required=True,
        help="which platform adapter family interprets the source address",
    )
    connect_parser.add_argument(
        "--source-address",
        required=True,
        help=(
            "the source's address inside its platform; GitHub requires "
            "branchless owner/name"
        ),
    )
    connect_parser.add_argument(
        "--source-ref",
        help=(
            "an adapter-owned operating detail, required for GitHub's base "
            "branch and never part of source identity"
        ),
    )
    connect_parser.add_argument(
        "--credential-directory",
        type=Path,
        required=True,
        help="where the host resolves the credential; never the credential itself",
    )
    connect_parser.add_argument("--auth-method", required=True)
    connect_parser.add_argument(
        "--actor",
        required=True,
        help="the operator accountable for this connect",
    )
    connect_parser.add_argument(
        "--move",
        action="store_true",
        help=(
            "when an active connection of the same source kind names a "
            "different address, disconnect it and connect the given address "
            "instead, publishing both revisions; without this flag that "
            "conflict is refused"
        ),
    )
    add_definition_source_parser(commands)
    add_seat_parser(commands)
    resolve_parser = commands.add_parser(
        "resolve",
        help="ask a served Atelier which revision a workflow name holds",
        description=RESOLVE_DESCRIPTION,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    resolve_parser.add_argument(
        "--name",
        required=True,
        metavar="NAME",
        help="the catalog name to resolve, or a 64-hex lineage id",
    )
    resolve_parser.add_argument(
        "--position",
        default=DEFAULT_CATALOG_POSITION,
        metavar="head|N",
        help=(
            "which member of the lineage to answer with: head, or an exact "
            f"member number (default {DEFAULT_CATALOG_POSITION})"
        ),
    )
    resolve_parser.add_argument(
        "--service",
        default=DEFAULT_SERVICE_URL,
        help=f"the served Atelier API to ask (default {DEFAULT_SERVICE_URL})",
    )
    run_parser = commands.add_parser(
        "run",
        help="run one workflow document on a served Atelier API",
        description=RUN_DESCRIPTION,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    # Exactly one source for the revision. Two would leave the operator guessing
    # which one the run used; none would leave the command with nothing to run.
    run_source = run_parser.add_mutually_exclusive_group(required=True)
    run_source.add_argument(
        "--workflow",
        type=Path,
        metavar="DOCUMENT.yaml",
        help="the workflow document to publish and run",
    )
    run_source.add_argument(
        "--name",
        metavar="NAME",
        help=(
            "run the workflow this catalog name holds, instead of publishing a "
            "document; the name is resolved by the service before anything starts"
        ),
    )
    run_parser.add_argument(
        "--position",
        default=None,
        metavar="head|N",
        help=(
            "which member of the named lineage to run: head, or an exact member "
            f"number (default {DEFAULT_CATALOG_POSITION}); only with --name"
        ),
    )
    run_parser.add_argument(
        "--binding",
        action="append",
        default=[],
        metavar="ROLE=AGENT.json",
        help=(
            "bind one agent role of a format-2 workflow to the agent described "
            "by that file; repeatable"
        ),
    )
    run_parser.add_argument(
        "--input",
        action="append",
        default=[],
        metavar="NAME=VALUE",
        help=(
            "one graph_input the workflow declared, as exact JSON text; "
            "repeatable; the command publishes it as an artifact before the "
            "run starts"
        ),
    )
    run_parser.add_argument(
        "--input-file",
        action="append",
        default=[],
        metavar="NAME=PATH",
        help=(
            "one graph_input the workflow declared, read as exact JSON bytes "
            "from that file; repeatable"
        ),
    )
    run_parser.add_argument(
        "--service",
        default=DEFAULT_SERVICE_URL,
        help=f"the served Atelier API to run this on (default {DEFAULT_SERVICE_URL})",
    )
    run_parser.add_argument(
        "--run-id",
        help=(
            "this run's own identity; without it the identity is derived from "
            "the published workflow and bindings, so repeating the command "
            "reports the same run instead of starting another"
        ),
    )
    mcp_parser = commands.add_parser(
        "mcp",
        help="speak MCP on standard input against a loopback Atelier API",
        description=MCP_DESCRIPTION,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    mcp_parser.add_argument(
        "--service",
        default=DEFAULT_SERVICE_URL,
        help=(
            "the served Atelier API to call (default "
            f"{DEFAULT_SERVICE_URL}); must be a literal loopback address"
        ),
    )
    provider_canary_parser = commands.add_parser(
        "provider-canary",
        help="run each configured provider vector and write its live receipt",
        description=PROVIDER_CANARY_DESCRIPTION,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    provider_canary_parser.add_argument(
        "--service",
        default=DEFAULT_SERVICE_URL,
        help=(f"the served Atelier API to probe (default {DEFAULT_SERVICE_URL})"),
    )
    provider_canary_parser.add_argument(
        "--workflow-directory",
        type=Path,
        default=Path("workflows"),
        help="directory containing the three provider-canary workflow documents",
    )
    provider_canary_parser.add_argument(
        "--state-directory",
        type=Path,
        default=default_provider_canary_state_directory(),
        help=(
            "receipt directory (default "
            "${XDG_STATE_HOME:-~/.local/state}/atelier2/provider-probes/live)"
        ),
    )
    provider_canary_parser.add_argument(
        "--terminal-timeout-seconds",
        type=float,
        default=PROVIDER_CANARY_TERMINAL_TIMEOUT_SECONDS,
        help=(
            "maximum wait for each started run "
            f"(default {PROVIDER_CANARY_TERMINAL_TIMEOUT_SECONDS:g})"
        ),
    )
    return parser
