from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import sqlalchemy as sa
from sqlalchemy.engine import Engine
from sqlalchemy.exc import DatabaseError, OperationalError
from sqlalchemy.exc import TimeoutError as PoolTimeoutError

from atelier2.adapters.dbos.host_configuration import model_configuration_snapshot
from atelier2.adapters.dbos.schema import (
    agent_configuration_revisions,
    auth_profile_revisions,
)
from atelier2.adapters.dbos.transactions import canonical_write_transaction
from atelier2.application.resolve_start_bindings import (
    AuthProfileMissingForConfiguration,
)
from atelier2.application.role_candidates import configuration_registered
from atelier2.contracts.agents import (
    AgentConfigurationRevision,
    AgentConfigurationRevisionFormatVersion,
    AgentConfigurationRevisionHash,
    AgentConfigurationRevisionListItem,
    AgentExecutionCapability,
    AgentExecutorRevision,
    AuthMode,
    AuthProfileRevision,
    AuthProfileRevisionHash,
    ProviderId,
    ProviderProbeFailure,
)
from atelier2.contracts.host_configuration import ModelRegistryBytesDisagree
from atelier2.contracts.pages import require_page_limit
from atelier2.contracts.provider_probe_receipts import ProviderProbeResult
from atelier2.ports.agent_configurations import (
    AgentConfigurationCatalog,
    AgentConfigurationRevisionCollision,
    AgentConfigurationRevisionCreated,
    AgentConfigurationRevisionExisting,
    AgentConfigurationRevisionPage,
    AgentExecutorBindingUnavailable,
    AuthProfileRevisionCollision,
    AuthProfileRevisionConflict,
    AuthProfileRevisionCreated,
    AuthProfileRevisionExisting,
    AuthProfileRevisionMissing,
    AuthProfileRevisionPage,
    CatalogReadUnavailable,
    ListAgentConfigurationRevisionsResult,
    ListAuthProfileRevisionsResult,
    PublishAgentConfigurationRevisionResult,
    PublishAuthProfileRevisionResult,
)
from atelier2.ports.agent_executions import AgentExecutorKey, AgentExecutorRegistry
from atelier2.ports.durable_runs import DurableStateCorrupt, DurableWriteUnavailable


def auth_profile_from_record(record: Mapping[Any, Any]) -> AuthProfileRevision:
    revision = AuthProfileRevision(
        str(record["profile_id"]),
        int(record["revision_number"]),
        ProviderId(str(record["provider_id"])),
        AuthMode(str(record["auth_mode"])),
    )
    if revision.revision_hash.value != record["revision_hash"]:
        raise ValueError("durable auth profile hash disagrees with its fields")
    return revision


def agent_configuration_from_record(
    record: Mapping[Any, Any],
) -> AgentConfigurationRevision:
    revision = AgentConfigurationRevision(
        str(record["model"]),
        AuthProfileRevisionHash(str(record["auth_profile_revision_hash"])),
        AgentExecutorRevision(str(record["executor_revision"])),
        AgentExecutionCapability(str(record["requested_capability"])),
        AgentConfigurationRevisionFormatVersion(int(record["revision_format_version"])),
    )
    if revision.revision_hash.value != record["revision_hash"]:
        raise ValueError("durable agent configuration hash disagrees with its fields")
    return revision


class DbosAgentConfigurationCatalog(AgentConfigurationCatalog):
    def __init__(self, engine: Engine, registry: AgentExecutorRegistry) -> None:
        self._engine = engine
        self._registry = registry

    def publish_auth_profile_revision(
        self, revision: AuthProfileRevision
    ) -> PublishAuthProfileRevisionResult:
        try:
            with canonical_write_transaction(self._engine) as connection:
                keyed = (
                    connection.execute(
                        sa.select(auth_profile_revisions).where(
                            auth_profile_revisions.c.profile_id == revision.profile_id,
                            auth_profile_revisions.c.revision_number
                            == revision.revision_number,
                        )
                    )
                    .mappings()
                    .one_or_none()
                )
                if keyed is not None:
                    durable = auth_profile_from_record(keyed)
                    if durable == revision:
                        return AuthProfileRevisionExisting(durable)
                    return AuthProfileRevisionConflict()
                collision = (
                    connection.execute(
                        sa.select(auth_profile_revisions).where(
                            auth_profile_revisions.c.revision_hash
                            == revision.revision_hash.value
                        )
                    )
                    .mappings()
                    .one_or_none()
                )
                if collision is not None:
                    durable = auth_profile_from_record(collision)
                    if durable == revision:
                        return AuthProfileRevisionExisting(durable)
                    return AuthProfileRevisionCollision()
                connection.execute(
                    auth_profile_revisions.insert().values(
                        revision_hash=revision.revision_hash.value,
                        profile_id=revision.profile_id,
                        revision_number=revision.revision_number,
                        provider_id=revision.provider_id.value,
                        auth_mode=revision.auth_mode.value,
                    )
                )
                return AuthProfileRevisionCreated(revision)
        except (OperationalError, PoolTimeoutError):
            return DurableWriteUnavailable()
        except (ValueError, RuntimeError, DatabaseError):
            return DurableStateCorrupt()

    def publish_agent_configuration_revision(
        self, revision: AgentConfigurationRevision
    ) -> PublishAgentConfigurationRevisionResult:
        try:
            with canonical_write_transaction(self._engine) as connection:
                auth_record = (
                    connection.execute(
                        sa.select(auth_profile_revisions).where(
                            auth_profile_revisions.c.revision_hash
                            == revision.auth_profile_revision_hash.value
                        )
                    )
                    .mappings()
                    .one_or_none()
                )
                if auth_record is None:
                    return AuthProfileRevisionMissing()
                auth = auth_profile_from_record(auth_record)
                if not self._registry.contains(
                    AgentExecutorKey(auth.provider_id, revision.executor_revision)
                ):
                    return AgentExecutorBindingUnavailable()
                existing = (
                    connection.execute(
                        sa.select(agent_configuration_revisions).where(
                            agent_configuration_revisions.c.revision_hash
                            == revision.revision_hash.value
                        )
                    )
                    .mappings()
                    .one_or_none()
                )
                if existing is not None:
                    durable = agent_configuration_from_record(existing)
                    if durable == revision:
                        return AgentConfigurationRevisionExisting(durable, auth)
                    return AgentConfigurationRevisionCollision()
                connection.execute(
                    agent_configuration_revisions.insert().values(
                        revision_hash=revision.revision_hash.value,
                        model=revision.model,
                        auth_profile_revision_hash=(
                            revision.auth_profile_revision_hash.value
                        ),
                        executor_revision=revision.executor_revision.value,
                        revision_format_version=int(revision.revision_format_version),
                        requested_capability=revision.requested_capability.value,
                    )
                )
                return AgentConfigurationRevisionCreated(revision, auth)
        except (OperationalError, PoolTimeoutError):
            return DurableWriteUnavailable()
        except (ValueError, RuntimeError, DatabaseError):
            return DurableStateCorrupt()

    def agent_configuration_revision(
        self, revision_hash: AgentConfigurationRevisionHash
    ) -> tuple[AgentConfigurationRevision, AuthProfileRevision] | None:
        with self._engine.connect() as connection:
            record = (
                connection.execute(
                    sa.select(agent_configuration_revisions).where(
                        agent_configuration_revisions.c.revision_hash
                        == revision_hash.value
                    )
                )
                .mappings()
                .one_or_none()
            )
            if record is None:
                return None
            configuration = agent_configuration_from_record(record)
            auth_record = (
                connection.execute(
                    sa.select(auth_profile_revisions).where(
                        auth_profile_revisions.c.revision_hash
                        == configuration.auth_profile_revision_hash.value
                    )
                )
                .mappings()
                .one_or_none()
            )
        if auth_record is None:
            raise AuthProfileMissingForConfiguration(
                configuration.auth_profile_revision_hash
            )
        return configuration, auth_profile_from_record(auth_record)

    def list_agent_configuration_revisions(
        self, after: AgentConfigurationRevisionHash | None, limit: int
    ) -> ListAgentConfigurationRevisionsResult:
        require_page_limit(limit, "revision")
        try:
            with self._engine.connect() as connection:
                statement = sa.select(agent_configuration_revisions)
                if after is not None:
                    statement = statement.where(
                        agent_configuration_revisions.c.revision_hash > after.value
                    )
                records = tuple(
                    connection.execute(
                        statement.order_by(
                            agent_configuration_revisions.c.revision_hash
                        ).limit(limit + 1)
                    ).mappings()
                )
                has_more = len(records) > limit
                page = records[:limit]
                # One snapshot for the whole page: the registry pointer a
                # start's cast would read does not vary per configuration,
                # so it is asked once through the same connection's
                # transaction rather than once per listed item.
                registries = model_configuration_snapshot(connection, None).registries
                items: list[AgentConfigurationRevisionListItem] = []
                for record in page:
                    configuration = agent_configuration_from_record(record)
                    auth_record = (
                        connection.execute(
                            sa.select(auth_profile_revisions).where(
                                auth_profile_revisions.c.revision_hash
                                == configuration.auth_profile_revision_hash.value
                            )
                        )
                        .mappings()
                        .one_or_none()
                    )
                    if auth_record is None:
                        raise ValueError(
                            "agent configuration references a missing auth profile"
                        )
                    auth = auth_profile_from_record(auth_record)
                    executor_key = AgentExecutorKey(
                        auth.provider_id, configuration.executor_revision
                    )
                    has_valid_receipt = self._registry.has_valid_receipt(
                        configuration.revision_hash
                    )
                    # A receipt worth naming as a failure only when it is
                    # itself the reason live evidence is missing -- a stale or
                    # foreign-commit success stays `provider-probe-receipt-
                    # missing`, honestly, rather than borrowing a failure that
                    # was never the cause.
                    probe_failure = None
                    if not has_valid_receipt:
                        receipt = self._registry.latest_receipt(
                            configuration.revision_hash
                        )
                        if receipt is not None and receipt.result is (
                            ProviderProbeResult.FAILED
                        ):
                            assert receipt.problem_code is not None
                            probe_failure = ProviderProbeFailure(
                                receipt.problem_code, receipt.observed_at
                            )
                    items.append(
                        AgentConfigurationRevisionListItem(
                            configuration,
                            auth,
                            self._registry.is_structurally_startable(
                                executor_key, configuration.requested_capability
                            ),
                            configuration_registered(
                                configuration.revision_hash,
                                auth.provider_id.value,
                                configuration.model,
                                registries,
                            ),
                            has_valid_receipt,
                            probe_failure,
                        )
                    )
                return AgentConfigurationRevisionPage(
                    tuple(items),
                    items[-1].revision.revision_hash if has_more and items else None,
                )
        except (OperationalError, PoolTimeoutError):
            return CatalogReadUnavailable()
        except (ValueError, RuntimeError, DatabaseError, ModelRegistryBytesDisagree):
            return DurableStateCorrupt()

    def list_auth_profile_revisions(
        self, after: AuthProfileRevisionHash | None, limit: int
    ) -> ListAuthProfileRevisionsResult:
        require_page_limit(limit, "revision")
        try:
            with self._engine.connect() as connection:
                statement = sa.select(auth_profile_revisions)
                if after is not None:
                    statement = statement.where(
                        auth_profile_revisions.c.revision_hash > after.value
                    )
                records = tuple(
                    connection.execute(
                        statement.order_by(
                            auth_profile_revisions.c.revision_hash
                        ).limit(limit + 1)
                    ).mappings()
                )
                has_more = len(records) > limit
                items = tuple(
                    auth_profile_from_record(record) for record in records[:limit]
                )
                return AuthProfileRevisionPage(
                    items,
                    items[-1].revision_hash if has_more and items else None,
                )
        except (OperationalError, PoolTimeoutError):
            return CatalogReadUnavailable()
        except (ValueError, RuntimeError, DatabaseError):
            return DurableStateCorrupt()
