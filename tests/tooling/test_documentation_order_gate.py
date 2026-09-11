from __future__ import annotations

import hashlib
import importlib.util
import os
import shutil
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from types import ModuleType

import pytest

from scripts.documentation_freshness import DocumentSourceWatermark, UnboundDocument
from scripts.requirement_contract import approval_bytes, read_document_source_watermarks

PROJECT_ROOT = Path(__file__).parents[2]
GATE = Path("scripts/check_documentation_order.py")
CONTRACT = Path("scripts/requirement_contract.py")
FRESHNESS = Path("scripts/documentation_freshness.py")
REQUIREMENTS = Path("docs/requirements")
REGISTRY = REQUIREMENTS / "revisions.toml"
DOCUMENTATION = REQUIREMENTS / "README.md"
LEGACY_DOCUMENT = REQUIREMENTS / "0008-example.md"
BOUND_START = "<!-- documentation-order-gate-bound:start -->"
BOUND_END = "<!-- documentation-order-gate-bound:end -->"
GIT_IDENTITY = (
    "-c",
    "user.name=test-builder",
    "-c",
    "user.email=test-builder@invalid",
)


def copied_project(tmp_path: Path) -> Path:
    project = tmp_path / "project"
    for relative in (GATE, CONTRACT, FRESHNESS):
        destination = project / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(PROJECT_ROOT / relative, destination)
    shutil.copytree(PROJECT_ROOT / REQUIREMENTS, project / REQUIREMENTS)
    return project


def copied_legacy_project(tmp_path: Path) -> Path:
    project = copied_project(tmp_path)
    content = (
        "# Legacy requirement\n\n"
        "### REQ-LEGACY-01: Legacy bytes remain frozen.\n"
        "Quelle: DESK — test fixture\n"
    ).encode()
    (project / LEGACY_DOCUMENT).write_bytes(content)
    registry = project / REGISTRY
    with registry.open("a", encoding="utf-8") as stream:
        stream.write("\n" + legacy_table("0008", LEGACY_DOCUMENT, digest(content)))
    return project


def run_gate(
    project: Path,
    *,
    base_revision: str | None = None,
    github_actions: bool = False,
) -> subprocess.CompletedProcess[str]:
    base = [] if base_revision is None else ["--base-revision", base_revision]
    environment = os.environ.copy()
    environment["GITHUB_ACTIONS"] = "true" if github_actions else "false"
    return subprocess.run(
        [sys.executable, str(GATE), *base],
        cwd=project,
        check=False,
        capture_output=True,
        text=True,
        env=environment,
    )


def commit_project(project: Path) -> str:
    subprocess.run(["git", "init", "--quiet"], cwd=project, check=True)
    subprocess.run(["git", "add", "."], cwd=project, check=True)
    subprocess.run(
        ["git", *GIT_IDENTITY, "commit", "--quiet", "-m", "base requirement shelf"],
        cwd=project,
        check=True,
    )
    return subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=project,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def annotated_tag(project: Path, name: str) -> str:
    subprocess.run(
        ["git", *GIT_IDENTITY, "tag", "--annotate", name, "--message", name],
        cwd=project,
        check=True,
    )
    return name


def project_with_repinned_legacy_bytes(tmp_path: Path) -> tuple[Path, str]:
    project = copied_legacy_project(tmp_path)
    registry = project / REGISTRY
    registry_text = registry.read_text(encoding="utf-8")
    registry.unlink()
    base_revision = commit_project(project)
    document = project / LEGACY_DOCUMENT
    old_digest = digest(document.read_bytes())
    document.write_bytes(document.read_bytes() + b"\n")
    registry.write_text(
        registry_text.replace(old_digest, digest(document.read_bytes())),
        encoding="utf-8",
    )
    return project, base_revision


def digest(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def bound_0004_watermark() -> DocumentSourceWatermark:
    watermark = next(
        item
        for item in read_document_source_watermarks(PROJECT_ROOT)
        if item.document.name.startswith("0004-")
    )
    assert isinstance(watermark, DocumentSourceWatermark)
    return watermark


def source_binding() -> str:
    watermark = bound_0004_watermark()
    return (
        "[[source_binding]]\n"
        f'document = "{watermark.document.name[:4]}"\n'
        f'content_sha256 = "{watermark.document_digest}"\n'
        f'source_thread = "{watermark.source_thread.identifier}"\n'
        f'watermark_kind = "{watermark.last_observed_source_object.kind.value}"\n'
        f'watermark = "{watermark.last_observed_source_object.identifier}"\n'
    )


def source_binding_with_digest(content_digest: str) -> str:
    watermark = bound_0004_watermark()
    return source_binding().replace(
        f'content_sha256 = "{watermark.document_digest}"',
        f'content_sha256 = "{content_digest}"',
    )


def source_binding_with_watermark(source_watermark: str) -> str:
    watermark = bound_0004_watermark()
    return source_binding().replace(
        f'watermark = "{watermark.last_observed_source_object.identifier}"',
        f'watermark = "{source_watermark}"',
    )


def source_binding_with_changed_watermark() -> str:
    watermark = bound_0004_watermark()
    original = watermark.last_observed_source_object.identifier
    return source_binding_with_watermark("0" + original[1:])


def source_binding_for_unbound_document_with_watermark(source_watermark: str) -> str:
    document = next(
        item
        for item in read_document_source_watermarks(PROJECT_ROOT)
        if item.document.name.startswith("0002-")
    )
    assert isinstance(document, UnboundDocument)
    return (
        source_binding()
        .replace('document = "0004"', 'document = "0002"')
        .replace(
            f'content_sha256 = "{bound_0004_watermark().document_digest}"',
            f'content_sha256 = "{document.document_digest}"',
        )
        .replace(
            f'watermark = "{bound_0004_watermark().last_observed_source_object.identifier}"',
            f'watermark = "{source_watermark}"',
        )
    )


def legacy_table(document: str, path: Path, content_digest: str) -> str:
    return (
        f'[[legacy]]\ndocument = "{document}"\npath = "{path}"\n'
        f'content_sha256 = "{content_digest}"\n'
    )


def revision_table(
    content_digest: str,
    *,
    predecessor: str = "GENESIS",
    comment: int = 123456,
) -> str:
    approval = digest(approval_bytes("0008", content_digest))
    return (
        '[[revision]]\ndocument = "0008"\n'
        f'path = "{LEGACY_DOCUMENT}"\ncontent_sha256 = "{content_digest}"\n'
        f'approval_comment_id = {comment}\napproval_sha256 = "{approval}"\n'
        f'predecessor = "{predecessor}"\n'
    )


def strict_content(sentence: str = "Adoption remains controlled.") -> bytes:
    return (
        "# Controlled adoption\n\n## Intent\n\nThe operator controls adoption.\n\n"
        f"## Rules\n\n### REQ-MIGRATED-01: {sentence}\n"
        "Quelle: OPERATOR — issue 1 comment 2\n"
    ).encode()


def migrate_legacy(project: Path, content: bytes | None = None) -> str:
    document = project / LEGACY_DOCUMENT
    old_digest = digest(document.read_bytes())
    migrated = content or strict_content()
    document.write_bytes(migrated)
    current_digest = digest(migrated)
    registry = project / REGISTRY
    original = registry.read_text(encoding="utf-8")
    before = legacy_table("0008", LEGACY_DOCUMENT, old_digest)
    assert before in original
    registry.write_text(
        original.replace(before, revision_table(current_digest)), encoding="utf-8"
    )
    return current_digest


def load_gate() -> ModuleType:
    scripts = str(PROJECT_ROOT / "scripts")
    if scripts not in sys.path:
        sys.path.insert(0, scripts)
    specification = importlib.util.spec_from_file_location(
        "check_documentation_order", PROJECT_ROOT / GATE
    )
    assert specification is not None
    assert specification.loader is not None
    module = importlib.util.module_from_spec(specification)
    sys.modules[specification.name] = module
    specification.loader.exec_module(module)
    return module


def test_the_current_requirement_contract_passes_both_wrappers(tmp_path: Path) -> None:
    result = run_gate(copied_project(tmp_path))

    assert result.returncode == 0, result.stdout + result.stderr
    assert (
        "7 document(s), 79 rule(s), 2 frozen legacy, 5 approval-backed" in result.stdout
    )


def test_the_documentation_wrapper_names_a_contract_refusal(tmp_path: Path) -> None:
    project = copied_legacy_project(tmp_path)
    document = project / LEGACY_DOCUMENT
    document.write_bytes(document.read_bytes() + b"\n")

    result = run_gate(project)

    assert result.returncode != 0
    assert "Documentation-order gate refused:" in result.stderr
    assert str(LEGACY_DOCUMENT) in result.stderr
    assert "legacy content digest" in result.stderr


@pytest.mark.proves("legacy-requirement-bytes-are-frozen-until-migration")
def test_same_change_legacy_bytes_and_matching_registry_repin_are_refused(
    tmp_path: Path,
) -> None:
    project, base_revision = project_with_repinned_legacy_bytes(tmp_path)

    result = run_gate(project, base_revision=base_revision)

    assert result.returncode != 0
    assert "requirement 0008" in result.stderr
    assert str(LEGACY_DOCUMENT) in result.stderr
    assert "migrate" in result.stderr


@pytest.mark.parametrize(
    "spell_base",
    (
        lambda project, base_revision: "HEAD",
        lambda project, base_revision: base_revision[:10],
        lambda project, base_revision: annotated_tag(project, "lane-base"),
    ),
    ids=("branch tip", "abbreviated sha", "annotated tag"),
)
def test_a_base_revision_is_judged_as_the_commit_git_resolves_it_to(
    tmp_path: Path, spell_base: Callable[[Path, str], str]
) -> None:
    project, base_revision = project_with_repinned_legacy_bytes(tmp_path)

    spelled = run_gate(project, base_revision=spell_base(project, base_revision))
    exact = run_gate(project, base_revision=base_revision)

    assert spelled.returncode != 0
    assert (spelled.returncode, spelled.stdout, spelled.stderr) == (
        exact.returncode,
        exact.stdout,
        exact.stderr,
    )


def test_a_legacy_pin_may_be_removed_only_into_a_revision(tmp_path: Path) -> None:
    project = copied_legacy_project(tmp_path)
    base_revision = commit_project(project)
    migrate_legacy(project)

    result = run_gate(project, base_revision=base_revision)

    assert result.returncode == 0, result.stdout + result.stderr


def test_pre_registry_bootstrap_with_exact_legacy_pins_passes(tmp_path: Path) -> None:
    project = copied_project(tmp_path)
    registry = project / REGISTRY
    content = registry.read_bytes()
    registry.unlink()
    base_revision = commit_project(project)
    registry.write_bytes(content)

    result = run_gate(project, base_revision=base_revision)

    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.proves("legacy-requirement-bytes-are-frozen-until-migration")
def test_a_new_legacy_document_after_the_registry_is_refused(tmp_path: Path) -> None:
    project = copied_project(tmp_path)
    base_revision = commit_project(project)
    path = REQUIREMENTS / "0008-new-legacy.md"
    content = (
        "# New legacy\n\n### REQ-NEWLEGACY-01: New work is revisioned.\n"
        "Quelle: DESK — issue 460\n"
    ).encode()
    (project / path).write_bytes(content)
    with (project / REGISTRY).open("a", encoding="utf-8") as registry:
        registry.write("\n" + legacy_table("0008", path, digest(content)))

    result = run_gate(project, base_revision=base_revision)

    assert result.returncode != 0
    assert "new legacy requirement 0008" in result.stderr


@pytest.mark.proves("a-requirement-revision-registry-is-linear-and-bound")
def test_an_approval_backed_successor_extends_history(tmp_path: Path) -> None:
    project = copied_legacy_project(tmp_path)
    predecessor = migrate_legacy(project)
    base_revision = commit_project(project)
    content = strict_content("A successor remains controlled.")
    (project / LEGACY_DOCUMENT).write_bytes(content)
    with (project / REGISTRY).open("a", encoding="utf-8") as registry:
        registry.write("\n" + revision_table(digest(content), predecessor=predecessor))

    result = run_gate(project, base_revision=base_revision)

    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.proves("a-requirement-revision-registry-is-linear-and-bound")
def test_approval_backed_history_cannot_be_rewritten_in_place(tmp_path: Path) -> None:
    project = copied_legacy_project(tmp_path)
    migrate_legacy(project)
    base_revision = commit_project(project)
    registry = project / REGISTRY
    registry.write_text(
        registry.read_text(encoding="utf-8").replace(
            "approval_comment_id = 123456", "approval_comment_id = 123457"
        ),
        encoding="utf-8",
    )

    result = run_gate(project, base_revision=base_revision)

    assert result.returncode != 0
    assert "revision 0008" in result.stderr
    assert "changed or deleted" in result.stderr


@pytest.mark.proves("a-requirement-revision-registry-is-linear-and-bound")
def test_approval_backed_history_cannot_be_deleted_and_restarted(
    tmp_path: Path,
) -> None:
    project = copied_legacy_project(tmp_path)
    old_digest = migrate_legacy(project)
    base_revision = commit_project(project)
    content = strict_content("Replacement history is forbidden.")
    (project / LEGACY_DOCUMENT).write_bytes(content)
    registry = project / REGISTRY
    registry.write_text(
        registry.read_text(encoding="utf-8").replace(
            revision_table(old_digest), revision_table(digest(content))
        ),
        encoding="utf-8",
    )

    result = run_gate(project, base_revision=base_revision)

    assert result.returncode != 0
    assert "revision 0008" in result.stderr
    assert "changed or deleted" in result.stderr


@pytest.mark.proves("a-requirement-revision-registry-is-linear-and-bound")
@pytest.mark.parametrize("relative", (LEGACY_DOCUMENT, REGISTRY))
def test_base_snapshot_refuses_nonregular_git_tree_modes(
    tmp_path: Path, relative: Path
) -> None:
    project = copied_legacy_project(tmp_path)
    target = project / relative
    content = target.read_bytes()
    outside = tmp_path / f"base-{target.name}"
    outside.write_bytes(content)
    target.unlink()
    target.symlink_to(outside)
    base_revision = commit_project(project)
    target.unlink()
    target.write_bytes(content)

    result = run_gate(project, base_revision=base_revision)

    assert result.returncode != 0
    assert str(relative) in result.stderr
    assert "regular Git file" in result.stderr


def test_github_actions_refuses_an_unbound_base_revision(tmp_path: Path) -> None:
    result = run_gate(copied_project(tmp_path), github_actions=True)

    assert result.returncode != 0
    assert "supplied no base revision" in result.stderr


@pytest.mark.parametrize(
    "base_revision",
    ("0" * 40, "origin/main"),
    ids=("absent commit", "unknown ref"),
)
def test_a_base_revision_git_cannot_resolve_fails_closed(
    tmp_path: Path, base_revision: str
) -> None:
    project = copied_project(tmp_path)
    commit_project(project)

    result = run_gate(project, base_revision=base_revision)

    assert result.returncode != 0
    assert (
        f"base revision {base_revision!r} does not resolve to a commit" in result.stderr
    )


@pytest.mark.parametrize(
    ("change", "document"),
    [
        (
            lambda content: content.replace(
                source_binding(),
                source_binding_with_digest("0" * 64),
            ),
            "0004",
        ),
        (lambda content: content + "\n" + source_binding(), "0004"),
        (
            lambda content: (
                content
                + "\n"
                + source_binding_for_unbound_document_with_watermark(
                    "unregistered-revision"
                )
            ),
            "0002",
        ),
        (
            lambda content: content.replace(
                'watermark_kind = "issue_body_revision"',
                'watermark_kind = "unrecognized_kind"',
            ),
            "0004",
        ),
    ],
    ids=(
        "wrong-document-digest",
        "duplicate-binding",
        "unregistered-revision",
        "unsupported-watermark-kind",
    ),
)
def test_source_bindings_fail_by_document_when_they_are_not_exact(
    tmp_path: Path,
    change: Callable[[str], str],
    document: str,
) -> None:
    project = copied_project(tmp_path)
    registry = project / REGISTRY
    registry.write_text(change(registry.read_text(encoding="utf-8")), encoding="utf-8")

    result = run_gate(project)

    assert result.returncode != 0
    assert f"source binding {document}" in result.stderr


@pytest.mark.parametrize(
    "change",
    [
        lambda content: content.replace(
            source_binding(), source_binding_with_changed_watermark()
        ),
        lambda content: content.replace("\n" + source_binding(), "\n"),
    ],
    ids=("changed", "deleted"),
)
def test_existing_source_bindings_are_append_only_against_the_base(
    tmp_path: Path, change: Callable[[str], str]
) -> None:
    project = copied_project(tmp_path)
    base_revision = commit_project(project)
    registry = project / REGISTRY
    registry.write_text(change(registry.read_text(encoding="utf-8")), encoding="utf-8")

    result = run_gate(project, base_revision=base_revision)

    assert result.returncode != 0
    assert "source binding 0004" in result.stderr
    assert "changed or deleted" in result.stderr


@pytest.mark.proves("the-documentation-order-gate-states-the-bound-of-what-it-proves")
def test_the_gate_and_the_documentation_state_the_same_bound(tmp_path: Path) -> None:
    result = run_gate(copied_project(tmp_path))
    documentation = (PROJECT_ROOT / DOCUMENTATION).read_text(encoding="utf-8")
    documented = documentation.split(BOUND_START, 1)[1].split(BOUND_END, 1)[0]

    assert result.returncode == 0, result.stdout + result.stderr
    assert documented.strip() == load_gate().render_honesty_bound().strip()
    assert documented.strip() in result.stdout
