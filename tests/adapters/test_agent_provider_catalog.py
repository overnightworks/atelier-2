"""What the host discovers about a provider's models through the shared library.

The library owns the CLI vocabulary and the credential containment its probes
need, and proves both in its own suite. What is proven here is this host's own
half: which providers it asks about at all, what it makes of the answer, and
that a provider it cannot ask about says so instead of reporting no models.

The served-provider cases drive the real path down to a spawned process, with
a stand-in program in place of the billed CLI, because a fake in front of the
library would prove only that this module can be mocked.
"""

from __future__ import annotations

import json
import os
import stat
import sys
from pathlib import Path

import pytest

from atelier2.adapters.agent_provider_catalog import (
    CLAUDE_PROVIDER_ID,
    CODEX_PROVIDER_ID,
    GROK_PROVIDER_ID,
    AgentProviderCatalog,
    ProviderCatalogDeployment,
    configure_provider_catalog,
)
from atelier2.adapters.claude_subscription import CREDENTIAL_RECORD_ENTRY
from atelier2.adapters.codex_subscription import (
    AUTHENTICATION_FILE_NAME,
    CodexSandboxMode,
    CodexSubscriptionSettings,
)
from atelier2.adapters.grok_subscription import GrokSubscriptionSettings
from atelier2.contracts.agents import (
    AgentConfigurationRevision,
    AgentConfigurationRevisionFormatVersion,
    AgentExecutionCapability,
    AgentExecutorRevision,
    AuthMode,
    AuthProfileRevision,
    ProviderId,
)
from atelier2.ports.host_configuration import (
    ProviderModelDiscovery,
    ProviderModelDiscoveryUnsupported,
    ProviderModelInspectionUnavailable,
)
from tests.scenarios.agents import claude_subscription_deployment, stand_in_bubblewrap

pytestmark = pytest.mark.usefixtures("provider_runtime_per_test")

# A `grok models` that answers from a file beside itself, so one deployment can
# report a changed catalog without being rewritten.
GROK_MODELS_ANSWERING_FROM_A_FILE = """
import sys
from pathlib import Path

print("You are logged in with someone.")
print("Available models:")
print(Path(sys.argv[0]).resolve().parent.joinpath("catalog.txt").read_text())
"""

GROK_MODELS_REFUSING = """
import sys

sys.stderr.write("synthetic Grok refusal\\n")
raise SystemExit(1)
"""


def _grok_deployment(directory: Path, program: str) -> GrokSubscriptionSettings:
    executable = directory / "grok"
    executable.write_text(f"#!{sys.executable}\n{program}", encoding="utf-8")
    executable.chmod(executable.stat().st_mode | stat.S_IEXEC)
    workspace = directory / "workspace"
    workspace.mkdir()
    credentials = directory / "grok-credentials"
    credentials.mkdir()
    authentication = credentials / AUTHENTICATION_FILE_NAME
    authentication.write_text(json.dumps({"realm": {}}), encoding="utf-8")
    authentication.chmod(0o600)
    return GrokSubscriptionSettings(
        executable,
        workspace,
        credentials,
        os.environ["PATH"],
        stand_in_bubblewrap(directory),
    )


def _codex_deployment(directory: Path) -> CodexSubscriptionSettings:
    executable = directory / "codex"
    executable.write_text(
        f"#!{sys.executable}\nraise SystemExit(0)\n", encoding="utf-8"
    )
    executable.chmod(executable.stat().st_mode | stat.S_IEXEC)
    credentials = directory / "codex-credentials"
    credentials.mkdir()
    (credentials / AUTHENTICATION_FILE_NAME).write_text("{}", encoding="utf-8")
    return CodexSubscriptionSettings(
        executable, credentials, os.environ["PATH"], CodexSandboxMode.READ_ONLY
    )


def _grok_catalog(directory: Path, models: str, program: str) -> AgentProviderCatalog:
    deployment = ProviderCatalogDeployment(
        None, _grok_deployment(directory, program), None, directory
    )
    (directory / "catalog.txt").write_text(models, encoding="utf-8")
    configure_provider_catalog(deployment)
    return AgentProviderCatalog(deployment.served_providers())


def _auth_profile(provider: ProviderId) -> AuthProfileRevision:
    return AuthProfileRevision("profile", 1, provider, AuthMode.SUBSCRIPTION)


def _configuration(model: str) -> AgentConfigurationRevision:
    return AgentConfigurationRevision(
        model,
        _auth_profile(GROK_PROVIDER_ID).revision_hash,
        AgentExecutorRevision("grok-subscription/v1"),
        AgentExecutionCapability.HEADLESS,
        AgentConfigurationRevisionFormatVersion.V2,
    )


@pytest.mark.parametrize(
    "provider",
    [
        pytest.param(ProviderId("qwen"), id="a provider the library does not know"),
        pytest.param(CLAUDE_PROVIDER_ID, id="claude, whose catalog lists aliases"),
    ],
)
def test_a_provider_with_no_model_list_says_so_rather_than_listing_none(
    provider: ProviderId,
) -> None:
    """An empty catalog and an absent catalog mean opposite things to a registry.

    A model that is missing from a list its provider really published is
    unknown at that provider; a model whose provider publishes no list this
    host can read is merely unchecked, and first use must check it.
    """

    catalog = AgentProviderCatalog(frozenset({provider}))

    discovered = catalog.discover_models(
        _configuration("any-model"), _auth_profile(provider)
    )

    assert discovered == ProviderModelDiscoveryUnsupported()


def test_a_served_provider_reports_the_models_its_own_cli_names(
    tmp_path: Path,
) -> None:
    catalog = _grok_catalog(
        tmp_path,
        "* grok-4.6 (default)\n* grok-4.5\n",
        GROK_MODELS_ANSWERING_FROM_A_FILE,
    )

    discovered = catalog.discover_models(
        _configuration("grok-4.6"), _auth_profile(GROK_PROVIDER_ID)
    )

    assert discovered == ProviderModelDiscovery(frozenset({"grok-4.6", "grok-4.5"}))


def test_a_second_discovery_reads_the_catalog_again_rather_than_a_cached_one(
    tmp_path: Path,
) -> None:
    """A registry revision outlives the half minute the library caches a probe.

    What it records decides which models may start, so a pass pays one fresh
    probe rather than freezing an answer that was already stale when it was
    written.
    """

    catalog = _grok_catalog(tmp_path, "* grok-4.5\n", GROK_MODELS_ANSWERING_FROM_A_FILE)
    catalog.discover_models(_configuration("grok-4.5"), _auth_profile(GROK_PROVIDER_ID))

    (tmp_path / "catalog.txt").write_text("* grok-4.6\n", encoding="utf-8")
    discovered = catalog.discover_models(
        _configuration("grok-4.6"), _auth_profile(GROK_PROVIDER_ID)
    )

    assert discovered == ProviderModelDiscovery(frozenset({"grok-4.6"}))


def test_a_refusing_cli_leaves_the_registry_unwritten_rather_than_empty(
    tmp_path: Path,
) -> None:
    """An unreadable provider must not publish a revision that names no model.

    Read as a catalog, an empty answer would mark every registered model
    unknown at its provider and stop it starting.
    """

    catalog = _grok_catalog(tmp_path, "", GROK_MODELS_REFUSING)

    discovered = catalog.discover_models(
        _configuration("grok-4.6"), _auth_profile(GROK_PROVIDER_ID)
    )

    assert isinstance(discovered, ProviderModelInspectionUnavailable)


def test_a_provider_this_deployment_does_not_serve_answers_nothing_about_models(
    tmp_path: Path,
) -> None:
    catalog = _grok_catalog(tmp_path, "* grok-4.6\n", GROK_MODELS_ANSWERING_FROM_A_FILE)

    discovered = catalog.discover_models(
        _configuration("gpt-5-codex"), _auth_profile(CODEX_PROVIDER_ID)
    )

    assert isinstance(discovered, ProviderModelInspectionUnavailable)


def test_the_deployment_hands_the_library_this_hosts_own_provider_files(
    tmp_path: Path,
) -> None:
    claude = claude_subscription_deployment(tmp_path, "raise SystemExit(0)")
    grok = _grok_deployment(tmp_path, GROK_MODELS_REFUSING)
    codex = _codex_deployment(tmp_path)

    configuration = ProviderCatalogDeployment(
        claude, grok, codex, tmp_path
    ).runtime_configuration()

    assert configuration.claude_cli_binary == str(claude.executable)
    assert configuration.grok_cli_binary == str(grok.executable)
    assert configuration.codex_cli_binary == str(codex.executable)
    assert configuration.claude_cli_auth_file == (
        claude.credential_directory / CREDENTIAL_RECORD_ENTRY
    )
    assert configuration.grok_cli_auth_file == (
        grok.credential_directory / AUTHENTICATION_FILE_NAME
    )
    assert configuration.codex_cli_auth_file == (
        codex.credential_directory / AUTHENTICATION_FILE_NAME
    )
    assert configuration.cli_working_directory_root == tmp_path


def test_an_undeployed_provider_names_no_binary_and_no_credential_this_host_holds(
    tmp_path: Path,
) -> None:
    """Three mandatory file answers, and this host serves two providers at most.

    The library has no way to be told "this deployment serves no Codex", so
    the fields still get values -- and those values must be ones that cannot
    reach a credential, whatever a later caller does with them.
    """

    configuration = ProviderCatalogDeployment(
        None, _grok_deployment(tmp_path, GROK_MODELS_REFUSING), None, tmp_path
    ).runtime_configuration()

    assert configuration.claude_cli_binary == ""
    assert configuration.codex_cli_binary == ""
    assert not configuration.claude_cli_auth_file.exists()
    assert not configuration.codex_cli_auth_file.exists()
    assert configuration.cli_binary_search_path == ()
    assert configuration.claude_cli_binary_search_globs == ()


def test_a_catalog_host_states_no_api_key_and_no_turn_facts(tmp_path: Path) -> None:
    """Subscription CLIs and catalogs only: anything else would be invented."""

    configuration = ProviderCatalogDeployment(
        None, _grok_deployment(tmp_path, GROK_MODELS_REFUSING), None, tmp_path
    ).runtime_configuration()

    assert configuration.anthropic_api_key is None
    assert configuration.xai_api_key is None
    assert configuration.openai_api_key is None
    assert configuration.turns is None
