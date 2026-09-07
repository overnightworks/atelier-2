"""Run one workflow document on a served Atelier API with a single command.

This module is a client of the product, not a second way into it. Every effect
it has travels through the published HTTP API of a service someone is already
serving, and every shape it writes or reads is the wire contract that API
publishes, so this command can neither do nor claim anything an operator could
not obtain from the same service by hand.

The API exposes no lookup for a published auth profile or agent configuration,
so `run` publishes each of them from the operator's own files on every
invocation. A workflow revision can be looked up by the name its lineage
carries, which is what `resolve` asks and what `run --name` runs, through the
same question so the two cannot disagree; `run --workflow` publishes the document
it was handed instead, and when those bytes are a V3 document whose authored
name the catalog can hold it then names that revision through
`POST /catalog-lineages` — publication and admission stay two HTTP acts.
A title the catalog grammar refuses is left unpublished-to-the-catalog so the
hash start still happens. All three publications are idempotent: identical
bytes answer with the same hash and change nothing. The run identity is derived
from those hashes and from the orders `--input` / `--input-file` supplied, so
repeating the same command reports the first run again instead of starting -
and paying for - a second one. Every order this command hands to a start is
published first, as a content-addressed artifact: `--input NAME=VALUE` and
`--input-file NAME=PATH` both reach `POST /artifacts` before `POST /runs` ever
sees the order, and the start names only the address the artifact door
answered. A ten-byte order and a hundred-kilobyte diff therefore take the same
door — the wire form no longer depends on how large the material is, and
publishing the same bytes twice is the same artifact, so a repeated command
publishes nothing new either.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Final
from urllib.parse import quote

from pydantic import BaseModel, ConfigDict, TypeAdapter, ValidationError

from atelier2.api.references import decode_canonical_base64
from atelier2.api.wire.events import (
    ActionReconciliationRequiredEventResourceV3,
    AgentCompletedEventResourceV3,
    AgentExecutorBindingUnavailableEventResourceV3,
    AgentFailedEventResourceV3,
    WaitingInputEventResourceV3,
)
from atelier2.api.wire.requests import (
    AdmitCatalogMemberRequestResource,
    AnyStartRunOrderResource,
    ArtifactOrderResource,
    FoundCatalogLineageRequestResource,
    InlineOrderResource,
    PublishAgentConfigurationRevisionRequestResource,
    PublishAuthProfileRevisionRequestResource,
    StartRunAgentBindingResourceV2,
    StartRunRequestResource,
    StartRunRequestResourceV2,
    StartRunRequestResourceV3,
    WorkItemOrderResource,
)
from atelier2.api.wire.resources import (
    AgentConfigurationRevisionResource,
    ArtifactResource,
    AuthProfileRevisionResource,
    CatalogAdmissionResource,
    CatalogNameResolutionResource,
    NodeDetailResource,
    ProblemResource,
    RunResourceV3,
    StreamFailureResource,
    WorkflowRevisionDetailResource,
)
from atelier2.contracts.catalog_v3 import CatalogLineageDisplayName
from atelier2.contracts.executions import RunEventKind
from atelier2.contracts.hashing import Sha256Hash, frame
from atelier2.contracts.revisions_v3 import RevisionKind
from atelier2.contracts.runs import RunState
from atelier2.host.atelier_api_client import (
    AtelierApi,
    AtelierApiAddressUnusable,
    AtelierApiTransportFailure,
    opened_api,
)

REQUEST_TIMEOUT_SECONDS = 30.0

AUTH_PROFILE_PATH = "/auth-profile-revisions"
AGENT_CONFIGURATION_PATH = "/agent-configuration-revisions"
WORKFLOW_REVISION_PATH = "/workflow-revisions"
CATALOG_LINEAGE_PATH = "/catalog-lineages"
CATALOG_NAME_PATH = "/catalog-revisions/by-name"
RUN_PATH = "/runs"
ARTIFACT_PATH = "/artifacts"
DEFAULT_CATALOG_POSITION: Final = "head"
COMMAND_CATALOG_ACTOR: Final = "atelier2-run"
PROBLEM_TYPE_PREFIX: Final = "urn:atelier2:problem:v1:"

JSON_MEDIA_TYPE = "application/json"
YAML_MEDIA_TYPE = "application/yaml"
EVENT_STREAM_MEDIA_TYPE = "text/event-stream"
OCTET_STREAM_MEDIA_TYPE = "application/octet-stream"

RUN_IDENTITY_DOMAIN = "atelier2-command-line-run"
_RUN_ENDED = "; this run has ended; a new run continues the work"

ActedEventResource = (
    AgentCompletedEventResourceV3
    | AgentFailedEventResourceV3
    | AgentExecutorBindingUnavailableEventResourceV3
    | WaitingInputEventResourceV3
    | ActionReconciliationRequiredEventResourceV3
)
"""Every event form this command must read to report a run it started.

The resources are the served wire's own, imported rather than restated: a second
vocabulary in the host is how this command came to refuse what the service had
already learned to say. It is narrower than the served event union on purpose --
only the kinds in `ACTED_EVENT_NAMES` reach the decoder, so a form this command
passes over needs no entry here, and a kind it cannot act on alone reaches the
operator as a refusal rather than as a silent pass.
"""
ACTED_EVENT_NAMES = frozenset(
    {
        RunEventKind.AGENT_COMPLETED,
        RunEventKind.AGENT_FAILED,
        RunEventKind.WAITING_INPUT,
        RunEventKind.ACTION_RECONCILIATION_REQUIRED,
    }
)

STREAM_FAILURE_NAME: Final[str] = StreamFailureResource.model_fields["event"].default
"""The name of the stream's own problem frame, read from the model that owns it."""

_auth_profile_resource = TypeAdapter(AuthProfileRevisionResource)
_agent_configuration_resource = TypeAdapter(AgentConfigurationRevisionResource)
_workflow_revision_resource = TypeAdapter(WorkflowRevisionDetailResource)
_catalog_name_resolution_resource = TypeAdapter(CatalogNameResolutionResource)
_catalog_admission_resource = TypeAdapter(CatalogAdmissionResource)
_run_resource = TypeAdapter[RunResourceV3](RunResourceV3)
_acted_event_resource = TypeAdapter[ActedEventResource](ActedEventResource)
_stream_failure_resource = TypeAdapter(StreamFailureResource)
_node_detail_resource = TypeAdapter(NodeDetailResource)
_artifact_resource = TypeAdapter(ArtifactResource)


class RunCommandRefusal(Exception):
    """This command cannot honestly report a run that ended."""


class UnusableRunOrder(RunCommandRefusal):
    """The order names something the public API could never accept."""


class ServiceUnreachable(RunCommandRefusal):
    """No Atelier service answered at the named address."""


class ServiceRefused(RunCommandRefusal):
    """The service answered a typed problem instead of the resource asked for."""

    def __init__(self, message: str, problem: ProblemResource | None = None) -> None:
        super().__init__(message)
        self.problem = problem


class UnreadableServiceAnswer(RunCommandRefusal):
    """The service answered something this command cannot read as its contract."""


class RunNeedsAnotherActor(RunCommandRefusal):
    """The run stopped on a decision this command is not allowed to make."""


class RunUnfinished(RunCommandRefusal):
    """The event history ended while the run had not."""


class AgentBindingDocument(BaseModel):
    """One operator file naming the agent an unbound workflow role needs.

    It carries what the two publication routes ask for, because this command
    publishes the pair rather than looking up hashes the API cannot serve. What
    each field may hold is the publication request's own business, so this
    document declares no bound of its own and hands the values to the resource
    that owns them.

    A refusal quotes the failing value, which is what makes it usable. This file
    may therefore never carry a credential: the published request resources it
    is made of carry none today, and a field that ever does belongs in a secret
    channel rather than here.
    """

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    auth_profile: PublishAuthProfileRevisionRequestResource
    model: str
    executor_revision: str
    requested_capability: str | None = None

    def publication(
        self, auth_profile_revision_hash: str
    ) -> PublishAgentConfigurationRevisionRequestResource:
        return PublishAgentConfigurationRevisionRequestResource.model_validate(
            {
                **self.model_dump(exclude_unset=True, exclude={"auth_profile"}),
                "auth_profile_revision_hash": auth_profile_revision_hash,
            }
        )


@dataclass(frozen=True)
class CarriedEvent:
    """What one stream frame says it is, before it is read as its own resource."""

    kind: str
    cursor: str | None


@dataclass(frozen=True)
class AgentBindingSource:
    role: str
    document: bytes


@dataclass(frozen=True)
class AgentRoleBinding:
    role: str
    agent_configuration_revision_hash: str


@dataclass(frozen=True)
class AgentOutput:
    node_id: str
    output: bytes
    output_hash: str
    attempt_id: str | None


@dataclass(frozen=True)
class SuppliedOrder:
    """One order the operator handed this command, as exact bytes."""

    name: str
    value: bytes


@dataclass(frozen=True)
class SuppliedArtifactOrder:
    """One order the caller named by a published artifact's address."""

    name: str
    artifact_hash: str


@dataclass(frozen=True)
class SuppliedWorkItemOrder:
    """One order the caller named by an item in the project's own tracker."""

    name: str
    work_item: str


type SuppliedStartOrder = SuppliedOrder | SuppliedArtifactOrder | SuppliedWorkItemOrder


@dataclass(frozen=True)
class RunOrder:
    service_url: str
    workflow_document: bytes
    bindings: tuple[AgentBindingSource, ...]
    run_id: str | None
    orders: tuple[SuppliedOrder, ...] = ()
    catalog_actor: str = COMMAND_CATALOG_ACTOR
    catalog_activated_at: str | None = None


@dataclass(frozen=True)
class RunHistory:
    """What one reading of a run's event stream saw, and how far it got."""

    outputs: tuple[AgentOutput, ...]
    last_cursor: str | None


@dataclass(frozen=True)
class RunReport:
    run_id: str
    public_run_reference: str
    workflow_revision_hash: str
    terminal_hash: str
    outputs: tuple[AgentOutput, ...]
    resolved_name: NameResolution | None = None
    """The name this run was started by, where a name is how it was asked for.

    A run started from a document has none, and the receipt says nothing about
    one rather than inventing a label for bytes the operator handed over.
    """


@dataclass(frozen=True)
class NamedRunOrder:
    """Run the workflow a catalog name holds, without naming a document at all."""

    service_url: str
    name: str
    bindings: tuple[AgentBindingSource, ...]
    run_id: str | None
    position: str = "head"
    orders: tuple[SuppliedOrder, ...] = ()


@dataclass(frozen=True)
class NameOrder:
    """One question for the catalog: which revision does this name hold?"""

    service_url: str
    name: str
    position: str = "head"


@dataclass(frozen=True)
class NameResolution:
    display_name: str
    lineage_id: str
    revision_hash: str
    revision_number: int


def resolve_published_name(order: NameOrder) -> NameResolution:
    """Ask the service which revision one catalog name holds. Start nothing.

    The service owns the answer and every refusal in it: an unadmitted name, a
    retired lineage, a position it does not hold. This command adds no judgment
    of its own -- it asks, and hands the service's own words on.
    """

    with service_api(order.service_url) as api:
        asked = catalog_name_path(RevisionKind.WORKFLOW, order.name)
        if order.position != DEFAULT_CATALOG_POSITION:
            asked = f"{asked}?position={quote(order.position, safe='')}"
        resolved = decoded(
            _catalog_name_resolution_resource, _get(api, asked), "a catalog name"
        )
        return NameResolution(
            resolved.display_name,
            resolved.lineage_id,
            resolved.catalog_revision_hash,
            resolved.revision_number,
        )


def catalog_name_path(kind: RevisionKind, name: str) -> str:
    """Where one catalog name is read: the kind is part of that address."""

    return f"{CATALOG_NAME_PATH}/{kind.value}/{quote(name, safe='')}"


def describe_resolution(resolution: NameResolution) -> str:
    """What the name resolved to, in the terms the operator asked in."""

    return (
        f"{resolution.display_name} is revision {resolution.revision_number} "
        f"of lineage {resolution.lineage_id}: {resolution.revision_hash}"
    )


def catalog_activated_at(now: datetime | None = None) -> str:
    """The catalog's activation instant, RFC 3339 UTC at second precision."""

    instant = datetime.now(UTC) if now is None else now.astimezone(UTC)
    return instant.strftime("%Y-%m-%dT%H:%M:%SZ")


def execute_run(order: RunOrder) -> RunReport:
    """Publish what the run binds, name a V3 revision, start it, and report it."""

    with service_api(order.service_url) as api:
        bindings = tuple(_published_binding(api, source) for source in order.bindings)
        activated_at = order.catalog_activated_at or catalog_activated_at()
        revision_hash = _published_workflow_revision(
            api,
            order.workflow_document,
            actor=order.catalog_actor,
            activated_at=activated_at,
        )
        return _run_published_revision(
            api, revision_hash, bindings, order.run_id, order.orders
        )


def execute_named_run(order: NamedRunOrder) -> RunReport:
    """Run the workflow a name holds: ask the catalog, then start what it answered.

    The name is resolved before anything is written, and by the same question
    `resolve` asks -- one owner for what a name means, so this command cannot
    disagree with that one about which revision an operator meant. Nothing is
    published: the revision the name holds is already in the store, and
    republishing it would mint a second identity for the same bytes.
    """

    resolution = resolve_published_name(
        NameOrder(order.service_url, order.name, order.position)
    )
    with service_api(order.service_url) as api:
        bindings = tuple(_published_binding(api, source) for source in order.bindings)
        report = _run_published_revision(
            api, resolution.revision_hash, bindings, order.run_id, order.orders
        )
        return replace(report, resolved_name=resolution)


def _run_published_revision(
    api: AtelierApi,
    revision_hash: str,
    bindings: tuple[AgentRoleBinding, ...],
    asked_run_id: str | None,
    orders: tuple[SuppliedOrder, ...] = (),
) -> RunReport:
    """Start one published revision and report the run that ended.

    Both ways in share this: a run is the same thing whether its revision was
    published from a document or resolved from a name, and one owner of the start,
    the reading and the refusals is what keeps the two from drifting apart.
    """

    run_id = asked_run_id or derived_run_id(revision_hash, bindings, orders)
    published_orders = _published_orders(api, orders)
    started = decoded(
        _run_resource,
        _post(
            api,
            RUN_PATH,
            start_request_body(run_id, revision_hash, bindings, published_orders),
        ),
        "a run",
    )
    reference = started.public_run_reference
    history = _read_history(api, reference)
    ended = decoded(_run_resource, _get(api, f"{RUN_PATH}/{reference}"), "a run")
    if (
        ended.state not in {RunState.COMPLETED, RunState.FAILED}
        or ended.terminal_hash is None
    ):
        raise RunUnfinished(
            f"the event history of run {reference} ended while the run was still "
            f"{ended.state}"
        )
    if history.last_cursor != ended.latest_event_cursor:
        raise RunUnfinished(
            f"the event history of run {reference} broke off at "
            f"{history.last_cursor or 'no event at all'} while the run's own "
            f"latest event is {ended.latest_event_cursor}, so what this command "
            "read is not the whole output; nothing was started twice, so running "
            "this command again is safe"
        )
    return RunReport(
        run_id=run_id,
        public_run_reference=reference,
        workflow_revision_hash=ended.workflow_revision_hash,
        terminal_hash=ended.terminal_hash,
        outputs=history.outputs,
    )


def derived_run_id(
    revision_hash: str,
    bindings: tuple[AgentRoleBinding, ...],
    orders: tuple[SuppliedOrder, ...] = (),
) -> str:
    """Derive the identity that makes repeating the same command harmless.

    `POST /runs` is idempotent over the caller's own run identity, so an
    identity derived from everything the run is made of turns a repeated
    command into a second report of the first run instead of a second run.
    The order is part of what the run is made of: two starts of the same
    revision with different material are two runs.
    """

    bound = (
        binding.role.encode()
        + b"="
        + binding.agent_configuration_revision_hash.encode()
        for binding in sorted(bindings, key=lambda binding: binding.role)
    )
    ordered = (
        order.name.encode() + b"=" + Sha256Hash.of(order.value).value.encode()
        for order in sorted(orders, key=lambda supplied: supplied.name)
    )
    preimage = frame(RUN_IDENTITY_DOMAIN, revision_hash.encode(), *bound, *ordered)
    return Sha256Hash.of(preimage).value


def describe_receipt(report: RunReport) -> str:
    """Render what binds the printed output to the run that produced it."""

    lines = [
        f"run: {report.public_run_reference}",
        *(
            []
            if report.resolved_name is None
            else [f"name: {describe_resolution(report.resolved_name)}"]
        ),
        f"run identity: {report.run_id}",
        f"workflow revision: {report.workflow_revision_hash}",
        f"terminal hash: {report.terminal_hash}",
    ]
    lines.extend(
        f"output of node {output.node_id}: {output.output_hash}"
        f"{'' if output.attempt_id is None else ' from attempt ' + output.attempt_id}"
        for output in report.outputs
    )
    return "\n".join(lines)


@contextmanager
def service_api(service_url: str) -> Iterator[AtelierApi]:
    """The one client every command opens from an operator-named service."""

    try:
        with opened_api(service_url) as api:
            yield api
    except AtelierApiAddressUnusable as invalid:
        raise UnusableRunOrder(str(invalid)) from invalid


def _published_binding(api: AtelierApi, source: AgentBindingSource) -> AgentRoleBinding:
    try:
        document = AgentBindingDocument.model_validate_json(source.document)
    except ValidationError as error:
        raise _unpublishable_binding(source.role, error) from error
    profile = decoded(
        _auth_profile_resource,
        _post(api, AUTH_PROFILE_PATH, document.auth_profile.model_dump_json().encode()),
        "an auth profile revision",
    )
    try:
        publication = document.publication(profile.auth_profile_revision_hash)
    except ValidationError as error:
        raise _unpublishable_binding(source.role, error) from error
    configuration = decoded(
        _agent_configuration_resource,
        _post(api, AGENT_CONFIGURATION_PATH, publication.model_dump_json().encode()),
        "an agent configuration revision",
    )
    return AgentRoleBinding(
        source.role, configuration.agent_configuration_revision_hash
    )


def _unpublishable_binding(role: str, error: ValidationError) -> UnusableRunOrder:
    return UnusableRunOrder(
        f"the binding of role {role} is not an agent this API could publish: {error}"
    )


def _published_orders(
    api: AtelierApi, orders: tuple[SuppliedOrder, ...]
) -> tuple[SuppliedArtifactOrder, ...]:
    """Publish every order's exact bytes as an artifact before a start names it.

    One door carries every order regardless of size: a start never again says
    the bytes themselves, only the address `POST /artifacts` answered.
    Publishing is idempotent, so a repeated command republishes nothing new.
    """

    return tuple(
        SuppliedArtifactOrder(order.name, _published_artifact_hash(api, order.value))
        for order in orders
    )


def _published_artifact_hash(api: AtelierApi, content: bytes) -> str:
    published = decoded(
        _artifact_resource,
        _post(api, ARTIFACT_PATH, content, media_type=OCTET_STREAM_MEDIA_TYPE),
        "an artifact",
    )
    return published.artifact_hash


def _published_workflow_revision(
    api: AtelierApi, document: bytes, *, actor: str, activated_at: str
) -> str:
    revision = decoded(
        _workflow_revision_resource,
        _post(api, WORKFLOW_REVISION_PATH, document, media_type=YAML_MEDIA_TYPE),
        "a workflow revision",
    )
    _admit_published_v3(api, revision, actor=actor, activated_at=activated_at)
    return revision.workflow_revision_hash


def _admit_published_v3(
    api: AtelierApi,
    revision: WorkflowRevisionDetailResource,
    *,
    actor: str,
    activated_at: str,
) -> None:
    """Name a just-published V3 revision through the existing admission door.

    Publication does not found a lineage. This is the second act: POST
    /catalog-lineages, or POST …/members when that authored name is already
    held. A title the catalog grammar refuses is skipped so the hash start
    still happens.
    """

    if revision.graph.workflow_format_version != 3:
        return
    try:
        CatalogLineageDisplayName(revision.graph.name)
    except (TypeError, ValueError):
        return
    founding = FoundCatalogLineageRequestResource(
        kind=RevisionKind.WORKFLOW,
        catalog_revision_hash=revision.workflow_revision_hash,
        actor=actor,
        activated_at=activated_at,
    )
    try:
        _admitted(
            api, CATALOG_LINEAGE_PATH, founding.model_dump_json(exclude_none=True)
        )
        return
    except ServiceRefused as refused:
        code = _problem_code(refused)
        if code == "catalog-revision-owned":
            return
        if code != "catalog-name-held":
            raise
    resolution = decoded(
        _catalog_name_resolution_resource,
        _get(api, catalog_name_path(RevisionKind.WORKFLOW, revision.graph.name)),
        "a catalog name",
    )
    member = AdmitCatalogMemberRequestResource(
        kind=RevisionKind.WORKFLOW,
        catalog_revision_hash=revision.workflow_revision_hash,
        actor=actor,
        activated_at=activated_at,
    )
    try:
        _admitted(
            api,
            f"{CATALOG_LINEAGE_PATH}/{resolution.lineage_id}/members",
            member.model_dump_json(),
        )
    except ServiceRefused as refused:
        if _problem_code(refused) == "catalog-revision-owned":
            return
        raise


def _admitted(api: AtelierApi, path: str, body: str) -> CatalogAdmissionResource:
    return decoded(
        _catalog_admission_resource,
        _post(api, path, body.encode()),
        "a catalog admission",
    )


def _problem_code(refused: ServiceRefused) -> str | None:
    if refused.problem is None:
        return None
    if refused.problem.type.startswith(PROBLEM_TYPE_PREFIX):
        return refused.problem.type.removeprefix(PROBLEM_TYPE_PREFIX)
    return refused.problem.type


def start_request_body(
    run_id: str,
    revision_hash: str,
    bindings: tuple[AgentRoleBinding, ...],
    orders: tuple[SuppliedStartOrder, ...] = (),
) -> bytes:
    """The POST /runs body. One owner for `run` and the MCP start tool."""
    if orders:
        requested = StartRunRequestResourceV3(
            workflow_format_version=3,
            run_id=run_id,
            workflow_revision_hash=revision_hash,
            agent_bindings=tuple(
                StartRunAgentBindingResourceV2(
                    role=binding.role,
                    agent_configuration_revision_hash=(
                        binding.agent_configuration_revision_hash
                    ),
                )
                for binding in bindings
            ),
            orders=tuple(_wire_start_order(order) for order in orders),
        )
    elif bindings:
        requested = StartRunRequestResourceV2(
            workflow_format_version=2,
            run_id=run_id,
            workflow_revision_hash=revision_hash,
            agent_bindings=tuple(
                StartRunAgentBindingResourceV2(
                    role=binding.role,
                    agent_configuration_revision_hash=(
                        binding.agent_configuration_revision_hash
                    ),
                )
                for binding in bindings
            ),
        )
    else:
        requested = StartRunRequestResource(
            run_id=run_id, workflow_revision_hash=revision_hash
        )
    return requested.model_dump_json().encode()


def _wire_start_order(order: SuppliedStartOrder) -> AnyStartRunOrderResource:
    if isinstance(order, SuppliedArtifactOrder):
        return ArtifactOrderResource(name=order.name, artifact_hash=order.artifact_hash)
    if isinstance(order, SuppliedWorkItemOrder):
        return WorkItemOrderResource(name=order.name, work_item=order.work_item)
    return InlineOrderResource(name=order.name, value=order.value.decode("utf-8"))


def _read_history(api: AtelierApi, public_run_reference: str) -> RunHistory:
    """Read the run's own event history, which is where its output lives.

    The stream ends itself when the run reaches its terminal event, so this
    waits exactly as long as the run takes. A run that stops on a decision this
    command cannot make would otherwise keep it waiting forever, so those
    events are refusals rather than silence.

    The stream may also end early - it says so with a failure frame, or by
    simply stopping when the service wants the client to reconnect. Neither is
    an ended run, so this reports how far the reading got and lets the caller
    weigh that against the run's own latest event.
    """

    path = f"{RUN_PATH}/{public_run_reference}/events"
    outputs: list[AgentOutput] = []
    last_cursor: str | None = None
    try:
        for data in _server_sent_data(
            api.event_lines(path, accept=EVENT_STREAM_MEDIA_TYPE)
        ):
            carried = _carried_event(data)
            if carried.kind == STREAM_FAILURE_NAME:
                raise ServiceRefused(_failed_stream(api.base_url + path, data))
            if carried.cursor is not None:
                last_cursor = carried.cursor
            if carried.kind not in ACTED_EVENT_NAMES:
                continue
            event = decoded(_acted_event_resource, data.encode(), "a run event")
            match event:
                case AgentCompletedEventResourceV3():
                    outputs.append(
                        AgentOutput(
                            node_id=event.node_id,
                            output=_decoded_output(event.output_base64),
                            output_hash=event.output_hash,
                            attempt_id=event.attempt_id,
                        )
                    )
                case WaitingInputEventResourceV3():
                    # The node's answer schema lives on the workflow revision, not
                    # on the event, so the refusal names the node and stops there.
                    raise RunNeedsAnotherActor(
                        f"node {event.node_id} is waiting for an answer; this "
                        "command carries no answer"
                    )
                case ActionReconciliationRequiredEventResourceV3():
                    raise RunNeedsAnotherActor(
                        f"node {event.node_id} reached an unknown outcome; only an "
                        "accountable operator determination resolves it, and this "
                        "command makes none"
                    )
                case _:
                    raise _why_the_run_stops(api, public_run_reference, event)
    except AtelierApiTransportFailure as failure:
        raise service_refusal(failure) from failure
    return RunHistory(tuple(outputs), last_cursor)


def _why_the_run_stops(
    api: AtelierApi,
    public_run_reference: str,
    event: AgentFailedEventResourceV3 | AgentExecutorBindingUnavailableEventResourceV3,
) -> RunNeedsAnotherActor:
    """The failure an operator is handed, with the reason read where it lives.

    A refusal before any attempt names its reason and the refusing boundary's
    sentence on the event; an attempt's failure carries its stored receipt words
    the same way, or asks the node resource the console panel reads for them.
    """

    if isinstance(event, AgentExecutorBindingUnavailableEventResourceV3):
        said = "" if event.detail is None else f": {event.detail}"
        return RunNeedsAnotherActor(
            f"node {event.node_id} was refused before any attempt: "
            f"{event.reason}{said}{_RUN_ENDED}"
        )
    if (reason := event.reason) is None:
        node = quote(event.node_id, safe="")
        reason = decoded(
            _node_detail_resource,
            _get(api, f"{RUN_PATH}/{public_run_reference}/nodes/{node}"),
            "a node detail",
        ).refusal
    said = ", and no reason was recorded" if reason is None else f": {reason}"
    return RunNeedsAnotherActor(
        f"agent attempt {event.attempt_id} of node {event.node_id} failed with "
        f"{event.failure_code}{said}{_RUN_ENDED}"
    )


def _decoded_output(output_base64: str) -> bytes:
    try:
        return decode_canonical_base64(output_base64)
    except ValueError as error:
        raise UnreadableServiceAnswer(
            f"an agent output is not canonical base64: {error}"
        ) from error


def _carried_event(data: str) -> CarriedEvent:
    """Read which event this is, and where it sits, from the event itself.

    Every run event resource carries its own kind and its own cursor, and those
    fields are the published contract; the stream frame's optional name field is
    not, and the served API writes none, so the payload is what this command
    reads. The failure frame carries a kind but no cursor, which is why the
    cursor is optional here and nowhere else.
    """

    try:
        carried = json.loads(data)
    except json.JSONDecodeError as error:
        raise UnreadableServiceAnswer(
            f"the event stream carried something that is not JSON: {error}"
        ) from error
    if not isinstance(carried, dict):
        raise UnreadableServiceAnswer("the event stream carried no run event")
    cursor = carried.get("cursor")
    return CarriedEvent(
        kind=str(carried.get("event", "")),
        cursor=cursor if isinstance(cursor, str) else None,
    )


def _failed_stream(url: str, data: str) -> str:
    failure = decoded(_stream_failure_resource, data.encode(), "a stream failure")
    return _refusal_sentence(url, failure.problem)


def _server_sent_data(lines: Iterator[str]) -> Iterator[str]:
    data_lines: list[str] = []
    for line in lines:
        if not line:
            if data_lines:
                yield "\n".join(data_lines)
            data_lines = []
            continue
        field, _, value = line.partition(":")
        if field == "data":
            data_lines.append(value.removeprefix(" "))
    if data_lines:
        yield "\n".join(data_lines)


def _post(
    api: AtelierApi, path: str, payload: bytes, media_type: str = JSON_MEDIA_TYPE
) -> bytes:
    try:
        return api.post(
            path, payload, media_type=media_type, timeout=REQUEST_TIMEOUT_SECONDS
        )
    except AtelierApiTransportFailure as failure:
        raise service_refusal(failure) from failure


def _get(api: AtelierApi, path: str) -> bytes:
    try:
        return api.get(path, timeout=REQUEST_TIMEOUT_SECONDS)
    except AtelierApiTransportFailure as failure:
        raise service_refusal(failure) from failure


def service_refusal(failure: AtelierApiTransportFailure) -> RunCommandRefusal:
    """Hand the service's own typed refusal on, without inventing prose for it."""

    if failure.status is None:
        return ServiceUnreachable(
            f"no Atelier service answered at {failure.url}: {failure.reason}"
        )
    try:
        problem = ProblemResource.model_validate_json(failure.body)
    except ValidationError:
        return ServiceRefused(
            f"{failure.url} answered {failure.status} {failure.reason}"
        )
    return ServiceRefused(_refusal_sentence(failure.url, problem), problem)


def _refusal_sentence(url: str, problem: ProblemResource) -> str:
    """One shape for a typed problem, whichever channel the service wrote it on."""

    return (
        f"{url} refused this: {problem.status} {problem.title} "
        f"[{problem.type}] {problem.detail}"
    )


def decoded[Resource](
    adapter: TypeAdapter[Resource], answered: bytes, subject: str
) -> Resource:
    try:
        return adapter.validate_json(answered)
    except ValidationError as error:
        raise UnreadableServiceAnswer(
            f"the service answered something this command cannot read as "
            f"{subject}: {error}"
        ) from error
