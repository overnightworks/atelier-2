"""Which models a provider names, answered by the shared provider layer.

The provider layer is a library (`agent_providers`), not a second copy of it
here: it owns the CLI vocabulary, the credential containment its catalog
probes need, and the parsing of each provider's answer. This module is the
only place in atelier-2 that speaks to it, and it does two things -- it states
this deployment's provider facts once per process, and it turns one library
answer into this host's own discovery result.

Claude is deliberately absent from `_CATALOG_PROVIDERS`. The library's Claude
route lists the aliases the CLI's own `/model` prints (`opus`, `sonnet`),
while a model registry here names full model ids (`claude-opus-5`). Membership
in an alias list is therefore no evidence about a registry id, and reporting
one as the other would mark every registered Claude model unknown at its
provider. `ProviderModelDiscoveryUnsupported` says the true thing instead:
nothing here answers "which Claude models exist", so first use must check.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Final

from agent_providers.catalog import ProviderRoute, list_provider_models
from agent_providers.config import ProviderRuntimeConfig, configure
from agent_providers.errors import ProviderError
from agent_providers.process import clear_agent_cli_caches

from atelier2.adapters.claude_subscription import (
    CREDENTIAL_RECORD_ENTRY,
    ClaudeSubscriptionSettings,
)
from atelier2.adapters.codex_subscription import (
    AUTHENTICATION_FILE_NAME as CODEX_AUTHENTICATION_FILE_NAME,
)
from atelier2.adapters.codex_subscription import CodexSubscriptionSettings
from atelier2.adapters.grok_subscription import (
    AUTHENTICATION_FILE_NAME as GROK_AUTHENTICATION_FILE_NAME,
)
from atelier2.adapters.grok_subscription import GrokSubscriptionSettings
from atelier2.contracts.agents import (
    AgentConfigurationRevision,
    AuthMode,
    AuthProfileRevision,
    ProviderId,
)
from atelier2.ports.host_configuration import (
    ProviderModelDiscovery,
    ProviderModelDiscoveryResult,
    ProviderModelDiscoveryUnsupported,
    ProviderModelInspectionUnavailable,
)

CLAUDE_PROVIDER_ID: Final = ProviderId("anthropic")
GROK_PROVIDER_ID: Final = ProviderId("xai")
CODEX_PROVIDER_ID: Final = ProviderId("openai")

# This host's provider ids beside the names the library knows them by. A
# provider missing here has no model-list operation this host can read.
_CATALOG_PROVIDERS: Final[dict[ProviderId, str]] = {
    GROK_PROVIDER_ID: "grok",
    CODEX_PROVIDER_ID: "codex",
}

_CATALOG_ROUTES: Final[dict[AuthMode, ProviderRoute]] = {
    AuthMode.SUBSCRIPTION: ProviderRoute.CLI,
    AuthMode.API_KEY: ProviderRoute.API,
}

# An undeployed provider still fills its mandatory configuration fields. The
# empty name resolves against no search path, and this directory is one this
# host never creates, so a field that is read at all says by its own value
# that this deployment serves no such provider.
_UNDEPLOYED_PROVIDER_BINARY: Final = ""
_UNDEPLOYED_PROVIDER_DIRECTORY: Final = "provider-not-deployed"


@dataclass(frozen=True)
class ProviderCatalogDeployment:
    """The provider facts this deployment hands the library, once per process.

    Every value is a fact this host already owns: the executables and
    credential directories an operator named on the serve command line, and
    the scratch root every provider child of this deployment works below. The
    three API keys stay unset because atelier-2 runs subscription CLIs, and
    no turn facts are configured because this host asks the library for
    catalogs alone.

    Both binary-search answers are empty on purpose rather than by default:
    every executable an operator names here is validated as an absolute file,
    so nothing this deployment starts is ever resolved from a search path or
    a glob, and a bare name stays unresolvable.
    """

    claude: ClaudeSubscriptionSettings | None
    grok: GrokSubscriptionSettings | None
    codex: CodexSubscriptionSettings | None
    working_directory_root: Path

    def runtime_configuration(self) -> ProviderRuntimeConfig:
        return ProviderRuntimeConfig(
            claude_cli_binary=_binary_of(self.claude),
            grok_cli_binary=_binary_of(self.grok),
            codex_cli_binary=_binary_of(self.codex),
            claude_cli_binary_search_globs=(),
            cli_binary_search_path=(),
            claude_cli_auth_file=self._credential_file(
                self.claude, CREDENTIAL_RECORD_ENTRY
            ),
            grok_cli_auth_file=self._credential_file(
                self.grok, GROK_AUTHENTICATION_FILE_NAME
            ),
            codex_cli_auth_file=self._credential_file(
                self.codex, CODEX_AUTHENTICATION_FILE_NAME
            ),
            cli_working_directory_root=self.working_directory_root,
        )

    def served_providers(self) -> frozenset[ProviderId]:
        served = (
            (CLAUDE_PROVIDER_ID, self.claude),
            (GROK_PROVIDER_ID, self.grok),
            (CODEX_PROVIDER_ID, self.codex),
        )
        return frozenset(
            provider for provider, settings in served if settings is not None
        )

    def _credential_file(
        self,
        settings: ClaudeSubscriptionSettings
        | GrokSubscriptionSettings
        | CodexSubscriptionSettings
        | None,
        file_name: str,
    ) -> Path:
        if settings is None:
            return (
                self.working_directory_root / _UNDEPLOYED_PROVIDER_DIRECTORY / file_name
            )
        return settings.credential_directory / file_name


def configure_provider_catalog(deployment: ProviderCatalogDeployment) -> None:
    """Install this deployment's provider facts for the whole serving process."""

    configure(deployment.runtime_configuration())


@dataclass(frozen=True)
class AgentProviderCatalog:
    """Discovery answered by the library, for the providers this host serves."""

    served_providers: frozenset[ProviderId]

    def discover_models(
        self,
        configuration: AgentConfigurationRevision,
        auth_profile: AuthProfileRevision,
    ) -> ProviderModelDiscoveryResult:
        """Name the models this provider offers on this profile's own route.

        The answer feeds a durable registry revision, whose `checked` entries
        decide what may start. The library keeps its CLI probes for half a
        minute, which is the right trade for a status panel and the wrong one
        for a revision that outlives it, so this pass asks for a fresh answer
        and pays one probe for it.
        """

        del configuration  # Every executor of one provider reads one catalog.
        provider = _CATALOG_PROVIDERS.get(auth_profile.provider_id)
        if provider is None:
            return ProviderModelDiscoveryUnsupported()
        if auth_profile.provider_id not in self.served_providers:
            return ProviderModelInspectionUnavailable(
                f"this deployment serves no {auth_profile.provider_id.value} provider"
            )
        clear_agent_cli_caches()
        try:
            models = list_provider_models(
                provider, _CATALOG_ROUTES[auth_profile.auth_mode]
            )
        except ProviderError as refusal:
            return ProviderModelInspectionUnavailable(str(refusal))
        return ProviderModelDiscovery(frozenset(models))


def _binary_of(
    settings: ClaudeSubscriptionSettings
    | GrokSubscriptionSettings
    | CodexSubscriptionSettings
    | None,
) -> str:
    return _UNDEPLOYED_PROVIDER_BINARY if settings is None else str(settings.executable)
