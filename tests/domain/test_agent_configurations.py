from __future__ import annotations

from collections.abc import Callable
from dataclasses import fields, replace

import pytest

from atelier2.application.compose_node_job import node_job
from atelier2.contracts import agents as agent_contracts
from atelier2.contracts.agents import (
    MAXIMUM_AGENT_OUTPUT_BYTES_V2,
    MAXIMUM_AGENT_PROCESS_INPUT_BYTES,
    AgentBinding,
    AgentBindingSet,
    AgentConfigurationNotStartableReason,
    AgentConfigurationRevision,
    AgentConfigurationRevisionListItem,
    AgentExecutionRequestV2,
    AgentExecutionResult,
    AgentExecutorOperationalIdentity,
    AgentExecutorRevision,
    AgentOutputLimitExceeded,
    AgentReceiptHash,
    AgentReceiptV2,
    AgentRole,
    AuthMode,
    AuthProfileRevision,
    ProviderId,
    ProviderProbeFailure,
    ResolvedAgentBinding,
)
from atelier2.contracts.executions import NodeExecutionId
from atelier2.contracts.node_records_v3 import DeliveredOutput, RunInput
from atelier2.contracts.provider_probe_receipts import ProviderProbeProblemCode
from atelier2.contracts.revisions_v3 import PublishedRevisionHash
from atelier2.contracts.runs import FIRST_ROUND_ORDINAL, RunId, WorkflowRevisionHash
from atelier2.contracts.when import RecordedAt


def _auth(
    *,
    profile_id: str = "max-primary",
    revision_number: int = 7,
    provider_id: str = "anthropic.claude",
    auth_mode: AuthMode = AuthMode.SUBSCRIPTION,
) -> AuthProfileRevision:
    return AuthProfileRevision(
        profile_id,
        revision_number,
        ProviderId(provider_id),
        auth_mode,
    )


def _configuration(
    auth: AuthProfileRevision | None = None,
    *,
    model: str = "claude-opus-5",
    executor_revision: str = "claude-cli/v1",
    requested_capability: agent_contracts.AgentExecutionCapability = (
        agent_contracts.AgentExecutionCapability.HEADLESS
    ),
    revision_format_version: agent_contracts.AgentConfigurationRevisionFormatVersion = (
        agent_contracts.AgentConfigurationRevisionFormatVersion.V1
    ),
) -> AgentConfigurationRevision:
    selected = _auth() if auth is None else auth
    return AgentConfigurationRevision(
        model,
        selected.revision_hash,
        AgentExecutorRevision(executor_revision),
        requested_capability,
        revision_format_version,
    )


def _resolved() -> ResolvedAgentBinding:
    auth = _auth()
    return ResolvedAgentBinding(AgentRole("builder"), _configuration(auth), auth)


def test_profile_configuration_and_binding_hashes_are_fixed_vectors() -> None:
    auth = _auth()
    configuration = _configuration(auth)
    bindings = AgentBindingSet(
        (
            AgentBinding(AgentRole("reviewer"), configuration.revision_hash),
            AgentBinding(AgentRole("builder"), configuration.revision_hash),
        )
    )

    assert (
        auth.revision_hash.value
        == "7ae178b50959a06bb720ebd05d8c98fdea6c18fe0f7952a1b4c886fb3003a670"
    )
    assert (
        configuration.revision_hash.value
        == "6ac1476eeafb0ca27e14ee34af18adc585a5d5cab6a714a4c143c8403e0d70ab"
    )
    assert (
        bindings.binding_set_hash.value
        == "460b2a01f7bdc6060a5b499f8558a929df17f802cde63c511ca47173a9dff745"
    )
    assert tuple(binding.role.value for binding in bindings.bindings) == (
        "builder",
        "reviewer",
    )


def test_legacy_and_capability_configuration_hashes_are_fixed_vectors() -> None:
    auth = _auth()
    capability = agent_contracts.AgentExecutionCapability
    format_version = agent_contracts.AgentConfigurationRevisionFormatVersion

    legacy = AgentConfigurationRevision(
        "claude-opus-5",
        auth.revision_hash,
        AgentExecutorRevision("claude-cli/v1"),
        capability.HEADLESS,
        format_version.V1,
    )
    headless = AgentConfigurationRevision(
        "claude-opus-5",
        auth.revision_hash,
        AgentExecutorRevision("claude-cli/v1"),
        capability.HEADLESS,
        format_version.V2,
    )
    interactive = AgentConfigurationRevision(
        "claude-opus-5",
        auth.revision_hash,
        AgentExecutorRevision("claude-cli/v1"),
        capability.INTERACTIVE,
        format_version.V2,
    )

    assert legacy.revision_hash.value == (
        "6ac1476eeafb0ca27e14ee34af18adc585a5d5cab6a714a4c143c8403e0d70ab"
    )
    assert headless.revision_hash.value == (
        "2b12dec1d7a461dd08e7c913be274b1d4a22c2a632c12f53c15dc1538b25b8a4"
    )
    assert interactive.revision_hash.value == (
        "b4fa512a291b4239b4766e767b9ab0a69bace0231a9bf9d4da9da92d377e1ac2"
    )
    with pytest.raises(ValueError, match="legacy.*headless"):
        AgentConfigurationRevision(
            "claude-opus-5",
            auth.revision_hash,
            AgentExecutorRevision("claude-cli/v1"),
            capability.INTERACTIVE,
            format_version.V1,
        )


def test_capability_v2_binding_request_and_receipt_hashes_are_fixed_vectors() -> None:
    auth = _auth()
    configuration = _configuration(
        auth,
        requested_capability=agent_contracts.AgentExecutionCapability.HEADLESS,
        revision_format_version=(
            agent_contracts.AgentConfigurationRevisionFormatVersion.V2
        ),
    )
    resolved = ResolvedAgentBinding(AgentRole("builder"), configuration, auth)
    bindings = AgentBindingSet(
        (AgentBinding(resolved.role, configuration.revision_hash),)
    )
    run_id = RunId("run/v2")
    revision_hash = WorkflowRevisionHash("1" * 64)
    request = AgentExecutionRequestV2(
        NodeExecutionId.for_node(run_id, revision_hash, "agent"),
        run_id,
        revision_hash,
        "agent",
        resolved,
        AgentExecutorOperationalIdentity("claude-process-17"),
        b"implement the story",
    )
    receipt = AgentReceiptV2.for_execution(
        request,
        bindings.binding_set_hash,
        AgentExecutionResult(b"\xff\x00done"),
    )

    assert (
        bindings.binding_set_hash.value
        == "42f73effc55713c876c195f00d11c49ec36008134874b5fec50c7ddcf4bf29b0"
    )
    assert (
        request.request_hash.value
        == "52b5804335c67689bdd9edc74a7a1404390893211c5d74e0582292eb238a86ef"
    )
    assert (
        receipt.receipt_hash.value
        == "a20f510d292e9a30af3414db23cc5168c5d539bba77847ce6161b00266dc2314"
    )
    with_schema = AgentExecutionRequestV2(
        NodeExecutionId.for_node(run_id, revision_hash, "agent"),
        run_id,
        revision_hash,
        "agent",
        resolved,
        AgentExecutorOperationalIdentity("claude-process-17"),
        b"implement the story",
        b'{"type": "string"}',
    )
    assert with_schema.request_hash == request.request_hash
    with_turns = AgentExecutionRequestV2(
        NodeExecutionId.for_node(run_id, revision_hash, "agent"),
        run_id,
        revision_hash,
        "agent",
        resolved,
        AgentExecutorOperationalIdentity("claude-process-17"),
        b"implement the story",
        maximum_assistant_turns=8,
    )
    assert with_turns.request_hash == request.request_hash


def test_public_auth_and_configuration_contracts_have_only_exact_safe_fields() -> None:
    assert tuple(field.name for field in fields(AuthProfileRevision)) == (
        "profile_id",
        "revision_number",
        "provider_id",
        "auth_mode",
        "revision_hash",
    )
    assert tuple(field.name for field in fields(AgentConfigurationRevision)) == (
        "model",
        "auth_profile_revision_hash",
        "executor_revision",
        "requested_capability",
        "revision_format_version",
        "revision_hash",
    )


@pytest.mark.parametrize(
    ("mutation", "different"),
    [
        (lambda value: replace(value, profile_id="max-secondary"), "profile"),
        (lambda value: replace(value, revision_number=8), "number"),
        (lambda value: replace(value, provider_id=ProviderId("openai")), "provider"),
        (lambda value: replace(value, auth_mode=AuthMode.API_KEY), "mode"),
    ],
)
def test_every_auth_field_changes_its_revision_hash(
    mutation: Callable[[AuthProfileRevision], AuthProfileRevision], different: str
) -> None:
    original = _auth()
    changed = mutation(original)

    assert changed.revision_hash != original.revision_hash, different


@pytest.mark.parametrize(
    "mutation",
    (
        lambda value: replace(value, model="claude-sonnet-5"),
        lambda value: replace(
            value, auth_profile_revision_hash=_auth(profile_id="other").revision_hash
        ),
        lambda value: replace(
            value, executor_revision=AgentExecutorRevision("claude-cli/v2")
        ),
    ),
)
def test_every_configuration_field_changes_its_revision_hash(
    mutation: Callable[[AgentConfigurationRevision], AgentConfigurationRevision],
) -> None:
    original = _configuration()

    assert mutation(original).revision_hash != original.revision_hash


@pytest.mark.parametrize(
    "mutation",
    (
        lambda value: replace(
            value,
            requested_capability=agent_contracts.AgentExecutionCapability.INTERACTIVE,
        ),
        lambda value: replace(
            value,
            revision_format_version=(
                agent_contracts.AgentConfigurationRevisionFormatVersion.V1
            ),
        ),
    ),
)
def test_capability_configuration_fields_change_the_v2_revision_hash(
    mutation: Callable[[AgentConfigurationRevision], AgentConfigurationRevision],
) -> None:
    original = _configuration(
        requested_capability=agent_contracts.AgentExecutionCapability.HEADLESS,
        revision_format_version=(
            agent_contracts.AgentConfigurationRevisionFormatVersion.V2
        ),
    )

    assert mutation(original).revision_hash != original.revision_hash


def test_every_binding_entry_field_changes_the_binding_set_hash() -> None:
    configuration = _configuration()
    original = AgentBindingSet(
        (AgentBinding(AgentRole("builder"), configuration.revision_hash),)
    )
    changed_role = AgentBindingSet(
        (AgentBinding(AgentRole("reviewer"), configuration.revision_hash),)
    )
    changed_configuration = AgentBindingSet(
        (
            AgentBinding(
                AgentRole("builder"),
                _configuration(model="claude-sonnet-5").revision_hash,
            ),
        )
    )

    assert changed_role.binding_set_hash != original.binding_set_hash
    assert changed_configuration.binding_set_hash != original.binding_set_hash


@pytest.mark.parametrize(
    "value",
    ("", "A", "-openai", "open ai", "openai/cli", "a" * 65),
)
def test_provider_id_is_an_extensible_lowercase_ascii_slug(value: str) -> None:
    with pytest.raises(ValueError):
        ProviderId(value)


@pytest.mark.parametrize("value", ("", "x" * 1025))
def test_exact_human_fields_share_the_closed_character_bound(value: str) -> None:
    with pytest.raises(ValueError):
        AgentRole(value)
    with pytest.raises(ValueError):
        AuthProfileRevision(value, 1, ProviderId("openai"), AuthMode.API_KEY)


def test_roles_keep_exact_unicode_without_normalization() -> None:
    assert AgentRole("Reviewér") != AgentRole("Reviewér")
    assert AgentRole(" Builder ").value == " Builder "


def test_binding_set_rejects_duplicate_exact_roles() -> None:
    configuration = _configuration()

    with pytest.raises(ValueError, match="unique"):
        AgentBindingSet(
            (
                AgentBinding(AgentRole("builder"), configuration.revision_hash),
                AgentBinding(AgentRole("builder"), configuration.revision_hash),
            )
        )


def test_v2_request_and_receipt_are_fixed_and_tamper_evident() -> None:
    run_id = RunId("run/v2")
    revision_hash = WorkflowRevisionHash("1" * 64)
    execution_id = NodeExecutionId.for_node(run_id, revision_hash, "agent")
    resolved = _resolved()
    operational_identity = AgentExecutorOperationalIdentity("claude-process-17")
    request = AgentExecutionRequestV2(
        execution_id,
        run_id,
        revision_hash,
        "agent",
        resolved,
        operational_identity,
        b"implement the story",
    )
    binding_set = AgentBindingSet(
        (AgentBinding(resolved.role, resolved.configuration.revision_hash),)
    )
    receipt = AgentReceiptV2.for_execution(
        request,
        binding_set.binding_set_hash,
        AgentExecutionResult(b"\xff\x00done"),
    )

    assert (
        request.request_hash.value
        == "1e39a4df0133a4b518eb0534232039e02f14fa422cc45602d1025c56b05ad869"
    )
    assert (
        receipt.output_hash.value
        == "34eeebf86b55024d6a7c56aa5ffc14c5da8b425f007aeeaacdee8e8fe6b6e9bc"
    )
    assert (
        receipt.receipt_hash.value
        == "3f77b20c9f6b17522f495bda351b7d1c0af199941d506eda871bb955dede59d5"
    )

    with pytest.raises(ValueError, match="binding"):
        replace(receipt, role=AgentRole("reviewer"))
    with pytest.raises(ValueError, match="hash"):
        replace(receipt, output_bytes=b"changed")


def _request_variant(field: str) -> AgentExecutionRequestV2:
    run_id = RunId("run/v2" if field != "run_id" else "run/changed")
    revision_hash = WorkflowRevisionHash("1" * 64 if field != "revision" else "2" * 64)
    node_id = "agent" if field != "node" else "other-agent"
    auth = _auth(
        profile_id="other-profile" if field == "profile_id" else "max-primary",
        revision_number=8 if field == "revision_number" else 7,
        provider_id="openai" if field == "provider_id" else "anthropic.claude",
        auth_mode=AuthMode.API_KEY if field == "auth_mode" else AuthMode.SUBSCRIPTION,
    )
    configuration = _configuration(
        auth,
        model="claude-sonnet-5" if field == "model" else "claude-opus-5",
        executor_revision="claude-cli/v2"
        if field == "executor_revision"
        else "claude-cli/v1",
    )
    resolved = ResolvedAgentBinding(
        AgentRole("reviewer" if field == "role" else "builder"),
        configuration,
        auth,
    )
    return AgentExecutionRequestV2(
        NodeExecutionId.for_node(run_id, revision_hash, node_id),
        run_id,
        revision_hash,
        node_id,
        resolved,
        AgentExecutorOperationalIdentity(
            "other-operation" if field == "operational_identity" else "operation"
        ),
        b"changed job" if field == "job" else b"job",
    )


@pytest.mark.parametrize(
    "field",
    (
        "run_id",
        "revision",
        "node",
        "role",
        "profile_id",
        "revision_number",
        "provider_id",
        "auth_mode",
        "model",
        "executor_revision",
        "operational_identity",
        "job",
    ),
)
def test_every_v2_request_input_changes_the_request_hash(field: str) -> None:
    assert (
        _request_variant(field).request_hash
        != _request_variant("unchanged").request_hash
    )


def _tamper_receipt(receipt: AgentReceiptV2, field: str) -> AgentReceiptV2:
    changed: dict[str, object] = {
        "request_hash": type(receipt.request_hash)("0" * 64),
        "node_execution_id": NodeExecutionId("0" * 64),
        "run_id": RunId("other-run"),
        "workflow_revision_hash": WorkflowRevisionHash("2" * 64),
        "node_id": "other-node",
        "role": AgentRole("reviewer"),
        "binding_set_hash": type(receipt.binding_set_hash)("0" * 64),
        "agent_configuration_revision_hash": type(
            receipt.agent_configuration_revision_hash
        )("0" * 64),
        "auth_profile_revision_hash": type(receipt.auth_profile_revision_hash)(
            "0" * 64
        ),
        "profile_id": "other-profile",
        "revision_number": 8,
        "provider_id": ProviderId("openai"),
        "auth_mode": AuthMode.API_KEY,
        "model": "other-model",
        "executor_revision": AgentExecutorRevision("other-executor"),
        "executor_operational_identity": AgentExecutorOperationalIdentity(
            "other-operation"
        ),
        "output_bytes": b"other-output",
        "output_hash": type(receipt.output_hash)("0" * 64),
        "receipt_hash": type(receipt.receipt_hash)("0" * 64),
    }
    return replace(receipt, **{field: changed[field]})


@pytest.mark.parametrize(
    "field",
    (
        "request_hash",
        "node_execution_id",
        "run_id",
        "workflow_revision_hash",
        "node_id",
        "role",
        "binding_set_hash",
        "agent_configuration_revision_hash",
        "auth_profile_revision_hash",
        "profile_id",
        "revision_number",
        "provider_id",
        "auth_mode",
        "model",
        "executor_revision",
        "executor_operational_identity",
        "output_bytes",
        "output_hash",
        "receipt_hash",
    ),
)
def test_every_v2_receipt_field_is_tamper_evident(field: str) -> None:
    request = _request_variant("unchanged")
    binding_set = AgentBindingSet(
        (
            AgentBinding(
                request.resolved_binding.role,
                request.resolved_binding.configuration.revision_hash,
            ),
        )
    )
    receipt = AgentReceiptV2.for_execution(
        request, binding_set.binding_set_hash, AgentExecutionResult(b"output")
    )

    with pytest.raises(ValueError):
        _tamper_receipt(receipt, field)


def _stored_receipt(
    *,
    resolved: ResolvedAgentBinding | None = None,
    run_id: str = "run/v2",
    node_id: str = "agent",
    round_ordinal: int = FIRST_ROUND_ORDINAL,
    operational_identity: str = "operation",
    output: bytes = b"output",
    also_bound_roles: tuple[str, ...] = (),
    declared_output_schema: bytes | None = None,
    maximum_assistant_turns: int | None = None,
) -> AgentReceiptV2:
    binding = _resolved() if resolved is None else resolved
    run = RunId(run_id)
    revision_hash = WorkflowRevisionHash("1" * 64)
    request = AgentExecutionRequestV2(
        NodeExecutionId.for_node(run, revision_hash, node_id, round_ordinal),
        run,
        revision_hash,
        node_id,
        binding,
        AgentExecutorOperationalIdentity(operational_identity),
        b"job",
        declared_output_schema,
        round_ordinal,
        maximum_assistant_turns,
    )
    binding_set = AgentBindingSet(
        (
            AgentBinding(binding.role, binding.configuration.revision_hash),
            *(
                AgentBinding(AgentRole(role), binding.configuration.revision_hash)
                for role in also_bound_roles
            ),
        )
    )
    return AgentReceiptV2.for_execution(
        request, binding_set.binding_set_hash, AgentExecutionResult(output)
    )


def _unicode_binding() -> ResolvedAgentBinding:
    auth = _auth(profile_id="max-primär")
    return ResolvedAgentBinding(
        AgentRole("Reviewér"),
        _configuration(auth, model="modèle-ü", executor_revision="cli/ünï"),
        auth,
    )


def _largest_api_key_binding() -> ResolvedAgentBinding:
    auth = _auth(
        revision_number=agent_contracts.MAXIMUM_SIGNED_INT64,
        provider_id="openai",
        auth_mode=AuthMode.API_KEY,
    )
    return ResolvedAgentBinding(AgentRole("builder"), _configuration(auth), auth)


def _tool_capability_binding() -> ResolvedAgentBinding:
    auth = _auth()
    return ResolvedAgentBinding(
        AgentRole("builder"),
        _configuration(
            auth,
            requested_capability=(
                agent_contracts.AgentExecutionCapability.HEADLESS_WITH_TOOLS
            ),
            revision_format_version=(
                agent_contracts.AgentConfigurationRevisionFormatVersion.V2
            ),
        ),
        auth,
    )


@pytest.mark.parametrize(
    ("receipt", "stored_hash"),
    (
        pytest.param(
            _stored_receipt(),
            "9fda9e136e3e0531d06d91f66a85188e56490260367780b7d0e8ba484afbee88",
            id="ascii-first-round",
        ),
        pytest.param(
            _stored_receipt(
                resolved=_unicode_binding(),
                run_id="lauf/größe",
                node_id="prüfer-knoten",
                operational_identity="prozeß-17",
            ),
            "66d95d5cdb667aba2edd51c12c7af7407901d581dd477cf175afc732ae58dc6e",
            id="unicode-text",
        ),
        pytest.param(
            _stored_receipt(output=b""),
            "401de598ece0ec1e7bc54474969d458a0faf15e402321bad9d4799d1f83b8122",
            id="empty-output",
        ),
        pytest.param(
            _stored_receipt(output=b"\xff" * MAXIMUM_AGENT_OUTPUT_BYTES_V2),
            "22c05f0e5760178160f3be14f2a0dfa1ea1298173d9a71378097861ba75e9bfd",
            id="largest-output",
        ),
        pytest.param(
            _stored_receipt(resolved=_largest_api_key_binding()),
            "88fcde353fb06fda2a77286169ca004560bff52dd3e63f4dad31c7369e1d6e50",
            id="largest-revision-api-key",
        ),
        pytest.param(
            _stored_receipt(round_ordinal=3),
            "5bd659ee11962c277d5874e77f4271505752af614a192191e9e9855fbcfefcc7",
            id="later-round",
        ),
        pytest.param(
            _stored_receipt(resolved=_tool_capability_binding()),
            "4cabf48be4777148598cd1d93a6971deed25f863a3b91a67373e6755d951e541",
            id="tool-capability-configuration",
        ),
        pytest.param(
            _stored_receipt(also_bound_roles=("reviewer",)),
            "7791361a1548a7d9eb837a255d2f254e6be71f1b44e779e09997737fe57a5a2c",
            id="two-role-binding-set",
        ),
        pytest.param(
            _stored_receipt(
                declared_output_schema=b'{"type": "string"}',
                maximum_assistant_turns=8,
            ),
            "9fda9e136e3e0531d06d91f66a85188e56490260367780b7d0e8ba484afbee88",
            id="optional-request-fields-stay-outside",
        ),
    ),
)
def test_every_stored_receipt_shape_keeps_its_exact_hash(
    receipt: AgentReceiptV2, stored_hash: str
) -> None:
    """A receipt hash is durable identity, so every stored value must still verify.

    Each literal was computed by the production derivation, never rebuilt here:
    a different value would rename a receipt the store already holds. Both doors
    must reach it -- the one an execution seals a receipt through, and the one a
    stored row is read back through, which recomputes the hash from its fields.
    """
    assert receipt.receipt_hash.value == stored_hash
    assert replace(receipt, receipt_hash=AgentReceiptHash(stored_hash)) == receipt


def test_v2_output_bound_accepts_49152_and_rejects_49153_before_receipt() -> None:
    run_id = RunId("run/v2")
    revision_hash = WorkflowRevisionHash("1" * 64)
    resolved = _resolved()
    request = AgentExecutionRequestV2(
        NodeExecutionId.for_node(run_id, revision_hash, "agent"),
        run_id,
        revision_hash,
        "agent",
        resolved,
        AgentExecutorOperationalIdentity("process"),
        b"job",
    )
    binding_set = AgentBindingSet(
        (AgentBinding(resolved.role, resolved.configuration.revision_hash),)
    )

    accepted = AgentReceiptV2.for_execution(
        request,
        binding_set.binding_set_hash,
        AgentExecutionResult(b"x" * MAXIMUM_AGENT_OUTPUT_BYTES_V2),
    )
    assert len(accepted.output_bytes) == MAXIMUM_AGENT_OUTPUT_BYTES_V2
    with pytest.raises(AgentOutputLimitExceeded):
        AgentReceiptV2.for_execution(
            request,
            binding_set.binding_set_hash,
            AgentExecutionResult(b"x" * (MAXIMUM_AGENT_OUTPUT_BYTES_V2 + 1)),
        )


def test_v2_request_holds_job_bytes_to_the_process_input_bound() -> None:
    accepted = replace(
        _request_variant("unchanged"),
        job_bytes=b"x" * MAXIMUM_AGENT_PROCESS_INPUT_BYTES,
    )
    assert len(accepted.job_bytes) == MAXIMUM_AGENT_PROCESS_INPUT_BYTES
    with pytest.raises(ValueError, match=str(MAXIMUM_AGENT_PROCESS_INPUT_BYTES)):
        replace(
            _request_variant("unchanged"),
            job_bytes=b"x" * (MAXIMUM_AGENT_PROCESS_INPUT_BYTES + 1),
        )


def test_a_composed_chain_job_is_held_to_the_process_input_bound() -> None:
    instruction = "Plate the dish."
    orders = (RunInput("portions", PublishedRevisionHash("a" * 64), b"4"),)
    empty_result = DeliveredOutput("cook", "plate", b"")
    overhead = len(node_job(instruction, orders, (empty_result,)).encode("utf-8"))
    fitting = b"s" * (MAXIMUM_AGENT_PROCESS_INPUT_BYTES - overhead)
    overflowing = b"s" * (MAXIMUM_AGENT_PROCESS_INPUT_BYTES - overhead + 1)

    accepted = replace(
        _request_variant("unchanged"),
        job_bytes=node_job(
            instruction, orders, (DeliveredOutput("cook", "plate", fitting),)
        ).encode("utf-8"),
    )
    assert len(accepted.job_bytes) == MAXIMUM_AGENT_PROCESS_INPUT_BYTES
    with pytest.raises(ValueError, match=str(MAXIMUM_AGENT_PROCESS_INPUT_BYTES)):
        replace(
            _request_variant("unchanged"),
            job_bytes=node_job(
                instruction, orders, (DeliveredOutput("cook", "plate", overflowing),)
            ).encode("utf-8"),
        )


def test_probe_failure_evidence_is_carried_only_when_it_names_the_reason() -> None:
    """#1128: a superseded model outranks a failed receipt in the precedence.

    A configuration the model registry no longer points at is refused with
    `model-not-registered` before its receipt is ever asked about, even when
    that receipt itself failed -- so `probe_failure_evidence` must stay
    `None` there, not echo the raw `probe_failure` the projection used to
    read unconditionally and crash the wire validator with.
    """
    auth = _auth()
    configuration = _configuration(auth)
    failure = ProviderProbeFailure(
        ProviderProbeProblemCode("provider-overloaded"),
        RecordedAt("2026-09-03T11:22:00Z"),
    )

    superseded_with_failed_receipt = AgentConfigurationRevisionListItem(
        configuration, auth, True, False, False, failure
    )
    assert (
        superseded_with_failed_receipt.not_startable_reason
        is AgentConfigurationNotStartableReason.MODEL_NOT_REGISTERED
    )
    assert superseded_with_failed_receipt.probe_failure_evidence is None

    registered_with_failed_receipt = AgentConfigurationRevisionListItem(
        configuration, auth, True, True, False, failure
    )
    assert (
        registered_with_failed_receipt.not_startable_reason
        is AgentConfigurationNotStartableReason.PROVIDER_PROBE_FAILED
    )
    assert registered_with_failed_receipt.probe_failure_evidence is failure
