# ADR 0005: CI enforces package boundaries

- Status: ACCEPTED 2026-08-13 — implemented: the gate landed with this record and
  runs in the quality lane on every pull request. The source-module count in the
  Decision, and the Consequences bullet that priced it, are amended 2026-09-09
  (issue #1471): the wrapper requires nameability, not a counted inventory.

## Context

Atelier 2 has distinct host, API, adapter, application, port, and contract
owners, but prose and review alone cannot prevent imports from reversing those
dependencies. A boundary gate must catch real import paths, unexpected new
top-level packages, and ownership of DBOS, SQLAlchemy, JSON Schema evaluation,
and PyYAML without replacing a maintained import-analysis tool with project code.
It must also fail loudly if configuration disappears or a source scan no longer
matches the landed inventory.

Import Linter 2.13 and Tach 0.35.0 were measured on the same source tree at the
first landing of this record. Import Linter rejected the tested inward,
external-owner, and empty-package violations at the smaller dependency cost.
Tach omitted import-free modules from its map and accepted the empty-package
violation. Import Linter therefore covers the required import analysis. Its own
scan still remains green after a deleted leaf, so a narrow repository preflight
owns the exact source-module count, the reviewed contract metadata, and the
AST checks that Import Linter cannot see.

## Decision

`pyproject.toml` is the executable owner of the Import Linter contracts the
quality lane runs. The wrapper requires that named set to match exactly, then
runs its own preflights from one registry, then delegates import analysis to
Import Linter. The layer contract is exhaustive, treats API and adapters as
peers, and permits dependencies only toward the right of this derived view:

<!-- architecture-contract-view:start -->
```text
contracts: layers, root-facade, dbos-owner, githubkit-owner, wire-projection-split, route-vocabulary, schema-owner, yaml-owner, httpx-owner, agent-providers-owner
preflights: port-sentence-problems, api-port-record-problems, use-case-record-problems, route-port-problems, duplicate-problems
layers: __main__ > host > api | adapters > application > ports > contracts
dbos-owner: atelier2.adapters.dbos
yaml-owner: atelier2.adapters.yaml_workflows, atelier2.adapters.markdown_agent_definitions
root-facade-forbids: __main__, host, api, adapters, application, ports
```
<!-- architecture-contract-view:end -->

The root facade cannot bypass ports through a package or descendant import.
Only `atelier2.adapters.dbos` may import DBOS or SQLAlchemy. Only
`atelier2.adapters.agent_provider_catalog` may import the shared provider
library, so nothing else can start a provider child from facts no deployment
stated. Only the YAML
document adapters named above may import PyYAML; application code or any other
adapter that reaches the library directly fails yaml-owner. JSON Schema
evaluation stays inside its profile owner. Wire schemas name no port type,
including a port re-exported through application. Routes name no port type.

The wrapper's source-module count is the landed production inventory counted
from `src/atelier2/**/*.py`. Growth remains allowed, but only together with
remeasurement of that count in the same change. An addition or a deletion that
is not remeasured fails the count before any later import of the tree.
`route-port-problems` walks nested packages, so the same reach under
`api/routes/<group>/` fails that named preflight.

**Amendment 2026-09-09 (Operator-Ruling 2026-09-09,
[#1471](https://github.com/overnightworks/atelier-2/issues/1471)): the wrapper
owns nameability, not a counted inventory.** The exact count and the duty to
remeasure it are gone. A number answered the wrong question: it stayed green when
one module replaced another and went red when nothing was broken, so four lanes
in one day raised the same line for no defect, and a file beside the package it
could never see at all. What it groped at is that a contract can only judge a
file the analysis turned into a module. The wrapper therefore requires every
`.py` file under `src/` to be nameable as a module of the `atelier2` package —
inside that package, through directories that carry `__init__.py`, under names
Python can spell — and refuses each failing file with its path and its reason.
Ownership *inside* the package is not the wrapper's: the exhaustive layer
contract above already refuses an undeclared sibling. Growth needs no
remeasurement, and the counted inventory survives only as a reported number in
the preflight line.

The quality lane runs `uv run --locked python scripts/check_architecture.py`
immediately after dependency installation. The wrapper then delegates to Import
Linter with caching disabled and timings visible. The wrapper and
`uv run --locked lint-imports --no-cache --show-timings` keep the same contract
set and must report the same kept and broken counts.

The derived view above is checked byte-for-byte against a deterministic
rendering of the executable configuration and the preflight registry; it is not
an independent policy owner. Contract identifiers, YAML document adapters, and
the source-module count live in the executable configuration and the wrapper,
not as independently maintained counts in this record.

## Consequences

- Forbidden inward imports, root-facade bypasses, DBOS/SQLAlchemy/PyYAML
  ownership violations, a wire or route that names a port, undeclared top-level
  packages, and the named preflights fail before broader checks.
- CI prints positive source, contract, layer-member, analyzed-file, dependency,
  kept-contract, broken-contract, and architecture-preflight counts without
  credentials or services.
- The repository owns no Python import parser or graph. The wrapper owns
  configuration integrity, the source-module count, the preflight registry, and
  invocation of the maintained tool.
- Adding or deleting source without remeasurement fails even when the change is
  intentional; this is the explicit cost of detecting a silently empty,
  unexpectedly shrunken, or unexpectedly grown scan.
- **Amendment 2026-09-09 (Operator-Ruling 2026-09-09,
  [#1471](https://github.com/overnightworks/atelier-2/issues/1471)): the bullet
  above no longer holds.** That cost is not paid and its three cases have their
  own owners: a silently empty scan fails at `use-case-record-problems`, because
  the module it resolves is not this tree's module; a shrunken scan fails the
  layer contract as a missing layer only where it loses a declared layer, and a
  deleted leaf that breaks no import is as little a defect as a grown scan. What
  fails instead is a source file the import analysis cannot see, named with its
  path.

## Supersedes

None.
