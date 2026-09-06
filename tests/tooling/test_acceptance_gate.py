from __future__ import annotations

import hashlib
import importlib.util
import json
import re
import shutil
import subprocess
import sys
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from types import ModuleType

import pytest

PROJECT_ROOT = Path(__file__).parents[2]
GATE = Path("scripts/check_acceptance.py")
ACCEPTANCE = Path("acceptance")
DECLARATION = ACCEPTANCE / "94-acceptance-trace-in-ci.toml"
SECOND_STORY_DECLARATION = ACCEPTANCE / "89-a-second-story.toml"
CONFTEST = Path("tests/conftest.py")
PULL_REQUEST_TEMPLATE = Path(".github/pull_request_template.md")
DOCUMENTATION = Path("docs/requirements/README.md")
REQUIREMENTS = Path("docs/requirements")
A_LEGACY_REQUIREMENT = REQUIREMENTS / "0008-test-legacy.md"
A_DECLARED_REQUIREMENT = "REQ-KATALOG-04"
AN_UNDECLARED_REQUIREMENT = "REQ-NOBODY-99"
PROOFS = Path("tests/tooling/test_acceptance_gate.py")
REQUIREMENT_CONTRACT = Path("scripts/requirement_contract.py")
DOCUMENTATION_FRESHNESS = Path("scripts/documentation_freshness.py")
COPIED_FILES = (GATE, REQUIREMENT_CONTRACT, DOCUMENTATION_FRESHNESS, PROOFS)

BOUND_START = "<!-- acceptance-gate-bound:start -->"
BOUND_END = "<!-- acceptance-gate-bound:end -->"
REPORTS_DIRECTORY = Path("reports")
QUALITY_REPORT = "quality.junit.xml"
CRASH_REPORT = "crash.junit.xml"
FRONTEND_REPORT = "frontend.vitest.json"
PLAYWRIGHT_REPORT = "frontend.playwright.json"
MOVABLE_SENTENCE = "an-unproven-sentence-fails-the-gate"
REPORT_ONLY_SENTENCE = "one-page-of-a-stream-is-decided-before-any-frame-is-written"
UNDECLARED_SENTENCE = "a-sentence-no-story-declares"
UNPROVEN_REFUSAL = "with no test that ran and passed in this pipeline"
UNCOLLECTED_PYTHON_CLAIM = Path("scripts/proof_helper.py")
UNCOLLECTED_TYPESCRIPT_CLAIM = Path("frontend/tools/proof.ts")
SECOND_STORY_SENTENCE = "a-sentence-a-second-story-declares"
BROWSER_SENTENCE_DECLARATION = ACCEPTANCE / "1022-a-browser-only-sentence.toml"
BROWSER_SENTENCE = "a-sentence-only-a-browser-may-prove"
PULL_REQUEST_BODY = Path("pull-request-body.md")
LANDING_FIELD_LINE = (
    "- Literal acceptance sentence(s), by their identifier in `acceptance/`, "
    "or `none` and why this change declares none:"
)
STATES_NEITHER = "states no acceptance sentence and claims no exemption"
SECOND_DECLARED_SENTENCE = "an-orphaned-proof-fails-the-gate"
TRACE_COUNTS = re.compile(
    r"Acceptance trace: (\d+) sentences, (\d+) claims, (\d+) passing proofs, "
    r"(\d+) run reports"
)


class Outcome(Enum):
    PASSED = "passed"
    FAILED = "failed"
    SKIPPED = "skipped"


@dataclass(frozen=True, slots=True)
class ReportedTest:
    name: str
    claims: str | None = None
    outcome: Outcome = Outcome.PASSED
    located_in: Path | None = None
    class_scope: tuple[str, ...] = ()
    ran: bool = True


@dataclass(frozen=True, slots=True)
class TracedCounts:
    sentences: int
    claims: int
    passing_proofs: int
    run_reports: int


def traced_counts(stdout: str) -> TracedCounts:
    counted = TRACE_COUNTS.search(stdout)
    assert counted is not None, stdout
    return TracedCounts(*(int(count) for count in counted.groups()))


def junit_report(reported: Iterable[ReportedTest]) -> str:
    cases = []
    for test in reported:
        body = ""
        if test.claims is not None:
            body += (
                f'<properties><property name="proves" value="{test.claims}" />'
                "</properties>"
            )
        if test.outcome is Outcome.FAILED:
            body += '<failure message="AssertionError: deliberate" />'
        elif test.outcome is Outcome.SKIPPED:
            body += '<skipped type="pytest.skip" message="deliberate" />'
        location = (
            ""
            if test.located_in is None
            else (
                ' classname="'
                f"{'.'.join((*test.located_in.with_suffix('').parts, *test.class_scope))}"
                '"'
            )
        )
        cases.append(
            f'<testcase{location} name="{test.name}" time="0.001">{body}</testcase>'
        )
    return (
        '<?xml version="1.0" encoding="utf-8"?><testsuites name="pytest tests">'
        f'<testsuite name="pytest">{"".join(cases)}</testsuite></testsuites>'
    )


def vitest_report(
    reported: Iterable[ReportedTest],
    located_in: Path = Path("frontend/tests/lib/proof.test.ts"),
) -> str:
    return json.dumps(
        {
            "testResults": [
                {
                    "name": f"/workspace/{located_in}",
                    "assertionResults": [
                        {
                            "title": test.name,
                            "fullName": test.name,
                            "status": test.outcome.value,
                        }
                        for test in reported
                    ],
                }
            ]
        }
    )


def playwright_report(
    reported: Iterable[ReportedTest],
    located_in: Path = Path("frontend/tests/e2e/proof.spec.ts"),
    *,
    root_dir: Path = Path("/workspace/frontend/tests/e2e"),
    spec_file: str | None = None,
) -> str:
    reported_file = spec_file or str(Path(*located_in.parts[3:]))
    return json.dumps(
        {
            "config": {"rootDir": str(root_dir)},
            "suites": [
                {
                    "title": Path(reported_file).name,
                    "file": reported_file,
                    "specs": [
                        {
                            "title": test.name,
                            "file": reported_file,
                            "tests": [
                                {
                                    "results": (
                                        [{"status": test.outcome.value}]
                                        if test.ran
                                        else []
                                    )
                                }
                            ],
                        }
                        for test in reported
                    ],
                }
            ],
        }
    )


def load_acceptance_script() -> ModuleType:
    scripts = str(PROJECT_ROOT / "scripts")
    if scripts not in sys.path:
        sys.path.insert(0, scripts)
    specification = importlib.util.spec_from_file_location(
        "check_acceptance", PROJECT_ROOT / GATE
    )
    assert specification is not None
    assert specification.loader is not None
    module = importlib.util.module_from_spec(specification)
    sys.modules[specification.name] = module
    specification.loader.exec_module(module)
    return module


def browser_stand_ins(
    project: Path, *, without: str | None = None
) -> tuple[ReportedTest, ...]:
    """A stand-in Playwright claim for every browser-only sentence but one.

    Every declared sentence with no Python claim in the copied project (the
    real ``frontend/`` sources never travel into the fixture) needs some
    passing proof or the gate calls it unproven, and a sentence bound to
    ``proof = "browser"`` accepts only a Playwright one. A caller that owns
    one such sentence itself names it in ``without`` and supplies its own
    evidence instead.
    """

    script = load_acceptance_script()
    return tuple(
        ReportedTest(f"proves({sentence.identifier}): a stand-in browser proof")
        for sentence in script.read_declared_sentences(project)
        if sentence.proof is script.ProofKind.BROWSER and sentence.identifier != without
    )


def playwright_report_with_stand_ins(
    project: Path,
    reported: Iterable[ReportedTest],
    located_in: Path = Path("frontend/tests/e2e/proof.spec.ts"),
    *,
    root_dir: Path = Path("/workspace/frontend/tests/e2e"),
    spec_file: str | None = None,
    without: str | None = None,
) -> str:
    """A caller's own Playwright evidence, plus the stand-ins a full override drops.

    Overriding the Playwright report file replaces the fixture's default
    content outright, so every browser-only sentence loses its stand-in proof
    unless this composes it back in.
    """

    return playwright_report(
        (*reported, *browser_stand_ins(project, without=without)),
        located_in,
        root_dir=root_dir,
        spec_file=spec_file,
    )


def default_reports_proving_every_sentence(
    project: Path, *, without: str | None = None
) -> dict[str, str]:
    """Build the quality and Playwright reports a fixture needs to stand as green.

    Every declared sentence with no Python claim in the copied project needs
    some passing proof or the gate calls it unproven; a sentence bound to
    ``proof = "browser"`` gets its stand-in from the Playwright report, and
    every other sentence keeps the JUnit stand-in this fixture always used.
    """

    script = load_acceptance_script()
    claims = script.read_source_claims(project)
    python_claims_by_sentence = {
        sentence.identifier: tuple(
            claim
            for claim in claims
            if claim.sentence_identifier == sentence.identifier
            and claim.located_in.suffix == ".py"
        )
        for sentence in script.read_declared_sentences(project)
    }
    junit_proofs: list[ReportedTest] = []
    for sentence in script.read_declared_sentences(project):
        if sentence.identifier == without or sentence.proof is script.ProofKind.BROWSER:
            continue
        sentence_claims = python_claims_by_sentence[sentence.identifier]
        if sentence_claims:
            junit_proofs.extend(
                ReportedTest(
                    claim.claiming_test,
                    sentence.identifier,
                    located_in=claim.located_in,
                )
                for claim in sentence_claims
            )
        else:
            junit_proofs.append(
                ReportedTest(
                    f"test_proves_{sentence.identifier.replace('-', '_')}",
                    sentence.identifier,
                )
            )
    return {
        QUALITY_REPORT: junit_report(junit_proofs),
        PLAYWRIGHT_REPORT: playwright_report(
            browser_stand_ins(project, without=without)
        ),
    }


def copied_project(
    tmp_path: Path,
    reports: Mapping[str, str] | None = None,
    *,
    also_declaring: Mapping[Path, str] | None = None,
    unproven: str | None = None,
) -> Path:
    project = tmp_path / "project"
    for relative in COPIED_FILES:
        destination = project / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(PROJECT_ROOT / relative, destination)
    shutil.copytree(PROJECT_ROOT / ACCEPTANCE, project / ACCEPTANCE)
    shutil.copytree(PROJECT_ROOT / REQUIREMENTS, project / REQUIREMENTS)
    for relative, declaration in (also_declaring or {}).items():
        (project / relative).write_text(declaration, encoding="utf-8")
    written = {
        **default_reports_proving_every_sentence(project, without=unproven),
        CRASH_REPORT: junit_report(()),
        FRONTEND_REPORT: vitest_report(()),
        **(reports or {}),
    }
    (project / REPORTS_DIRECTORY).mkdir(parents=True)
    for file_name, content in written.items():
        (project / REPORTS_DIRECTORY / file_name).write_text(content, encoding="utf-8")
    return project


def run_gate(
    project: Path, proposed_by: str | None = None
) -> subprocess.CompletedProcess[str]:
    """Run the gate over a project, optionally as the landing a pull request proposes."""

    landing: list[str] = []
    if proposed_by is not None:
        (project / PULL_REQUEST_BODY).write_text(proposed_by, encoding="utf-8")
        landing = ["--pull-request-body", str(PULL_REQUEST_BODY)]
    return subprocess.run(
        [sys.executable, str(GATE), "--reports", str(REPORTS_DIRECTORY), *landing],
        cwd=project,
        check=False,
        capture_output=True,
        text=True,
    )


def a_pull_request_stating(binding: str) -> str:
    return (
        "## Story binding\n\n"
        "- HumanRequirement Issue URL: https://github.com/FlexOr2/atelier-2/issues/94\n"
        f"{LANDING_FIELD_LINE} {binding}\n"
        "- Context sources consulted: the gate\n"
    )


def a_pull_request_without_the_field() -> str:
    return "## What this is\n\nProse a template field never reached.\n"


def rewrite(project: Path, relative: Path, before: str, after: str) -> None:
    edited = project / relative
    text = edited.read_text(encoding="utf-8")
    assert before in text, f"{relative} no longer contains {before!r}"
    edited.write_text(text.replace(before, after), encoding="utf-8")


def test_a_run_proving_every_declared_sentence_passes_the_gate(tmp_path: Path) -> None:
    result = run_gate(copied_project(tmp_path))

    assert result.returncode == 0, result.stdout + result.stderr
    counted = traced_counts(result.stdout)
    assert counted.sentences > 0
    assert counted.claims > 0
    assert counted.passing_proofs > 0
    assert counted.run_reports > 0


@pytest.mark.proves("acceptance-sentences-are-declared-in-the-repository")
def test_every_declared_sentence_is_readable_from_a_versioned_repository_file() -> None:
    script = load_acceptance_script()

    sentences = script.read_declared_sentences(PROJECT_ROOT)

    declared = {sentence.identifier: sentence for sentence in sentences}
    assert MOVABLE_SENTENCE in declared
    assert declared[MOVABLE_SENTENCE].text.endswith("names that sentence.")
    assert all(
        (PROJECT_ROOT / sentence.declared_in).is_file()
        and sentence.story.startswith("https://github.com/")
        and sentence.text.strip()
        for sentence in sentences
    )


@pytest.mark.proves("an-unproven-sentence-fails-the-gate")
def test_a_sentence_no_test_claims_is_named_and_fails_the_gate(tmp_path: Path) -> None:
    project = copied_project(tmp_path, unproven=MOVABLE_SENTENCE)

    result = run_gate(project)

    assert result.returncode != 0, result.stdout + result.stderr
    assert "Acceptance gate failed:" in result.stderr
    assert f"declares {MOVABLE_SENTENCE!r} {UNPROVEN_REFUSAL}" in result.stderr


@pytest.mark.proves("an-orphaned-proof-fails-the-gate")
def test_a_claim_no_story_declares_is_named_and_fails_the_gate(tmp_path: Path) -> None:
    project = copied_project(
        tmp_path,
        {
            CRASH_REPORT: junit_report(
                (ReportedTest("test_orphaned_claim", UNDECLARED_SENTENCE),)
            )
        },
    )

    result = run_gate(project)

    assert result.returncode != 0, result.stdout + result.stderr
    assert (
        f"reports test_orphaned_claim proving {UNDECLARED_SENTENCE!r}, "
        "which no story declares" in result.stderr
    )


def a_pytest_run_of_the_proving_test(outcome: Outcome) -> str:
    return junit_report(
        (ReportedTest("test_proves_the_sentence", MOVABLE_SENTENCE, outcome),)
    )


@pytest.mark.proves("a-proof-that-did-not-run-and-pass-fails-the-gate")
@pytest.mark.parametrize(
    "run",
    [
        {QUALITY_REPORT: junit_report(())},
        {QUALITY_REPORT: a_pytest_run_of_the_proving_test(Outcome.SKIPPED)},
        {QUALITY_REPORT: a_pytest_run_of_the_proving_test(Outcome.FAILED)},
        {
            QUALITY_REPORT: junit_report(()),
            FRONTEND_REPORT: vitest_report((ReportedTest("shows the failed stream"),)),
        },
    ],
    ids=[
        "collected-but-never-executed",
        "skipped-in-the-run",
        "failed-in-the-run",
        "claimed-only-where-the-cockpit-run-never-reported-it",
    ],
)
def test_a_proof_the_run_reports_did_not_pass_is_named_and_fails_the_gate(
    tmp_path: Path, run: Mapping[str, str]
) -> None:
    result = run_gate(copied_project(tmp_path, run))

    assert result.returncode != 0, result.stdout + result.stderr
    assert f"declares {MOVABLE_SENTENCE!r} {UNPROVEN_REFUSAL}" in result.stderr


@pytest.mark.proves("the-gate-states-the-bound-of-what-it-proves")
def test_the_gate_and_the_documentation_state_the_same_bound(tmp_path: Path) -> None:
    result = run_gate(copied_project(tmp_path))
    assert result.returncode == 0, result.stdout + result.stderr
    documentation = (PROJECT_ROOT / DOCUMENTATION).read_text(encoding="utf-8")
    documented_bound = documentation.split(BOUND_START, 1)[1].split(BOUND_END, 1)[0]

    stated_bound = load_acceptance_script().render_honesty_bound()

    assert documented_bound.strip() == stated_bound.strip()
    assert stated_bound.strip() in result.stdout


@pytest.mark.proves("the-gate-states-the-bound-of-what-it-proves")
def test_the_bound_names_that_the_body_is_read_as_the_run_received_it() -> None:
    """The landing binding is a snapshot, and the bound has to say so.

    The gate reads the pull-request body the event handed it, so a body edited
    after a green run is never re-checked. That is an accepted limit rather than
    a defect -- re-firing CI on every edit would cancel running work, and reading
    the tracker live would put back the derivation this gate exists without. What
    is not acceptable is leaving it unsaid in the one place that lists what this
    gate does not prove.
    """
    stated_bound = load_acceptance_script().render_honesty_bound()

    unproven = [line for line in stated_bound.splitlines() if "does not prove" in line]

    assert any("edited after this run" in line for line in unproven), (
        "the bound lists what the gate cannot judge, and the body snapshot is "
        f"one of those things; it names only: {unproven}"
    )


@pytest.mark.parametrize(
    "run",
    [
        {
            CRASH_REPORT: junit_report(
                (ReportedTest("test_recovers_after_a_crash", REPORT_ONLY_SENTENCE),)
            )
        },
        {
            FRONTEND_REPORT: vitest_report(
                (ReportedTest(f"proves({REPORT_ONLY_SENTENCE}): the title carries it"),)
            )
        },
    ],
    ids=["only-the-crash-run", "only-the-cockpit-run"],
)
def test_a_sentence_any_required_report_proves_counts(
    tmp_path: Path, run: Mapping[str, str]
) -> None:
    result = run_gate(copied_project(tmp_path, run, unproven=REPORT_ONLY_SENTENCE))

    assert result.returncode == 0, result.stdout + result.stderr


def a_second_story_declaration() -> str:
    return (
        "schema_version = 1\n"
        'story = "https://github.com/FlexOr2/atelier-2/issues/89"\n\n'
        "[[sentence]]\n"
        f'id = "{SECOND_STORY_SENTENCE}"\n'
        'text = "A second story declares an acceptance sentence of its own."\n'
    )


@pytest.mark.proves("a-second-story-declaration-verifies-like-the-first")
def test_a_second_story_declaration_verifies_like_the_first(tmp_path: Path) -> None:
    one_story = run_gate(copied_project(tmp_path / "one-story"))

    two_stories = run_gate(
        copied_project(
            tmp_path / "two-stories",
            also_declaring={SECOND_STORY_DECLARATION: a_second_story_declaration()},
        )
    )

    assert two_stories.returncode == 0, two_stories.stdout + two_stories.stderr
    assert (
        traced_counts(two_stories.stdout).sentences
        == traced_counts(one_story.stdout).sentences + 1
    )


@pytest.mark.proves("a-story-that-declares-no-sentence-is-named-by-verification")
def test_a_landing_that_declares_no_acceptance_sentence_is_named(
    tmp_path: Path,
) -> None:
    result = run_gate(copied_project(tmp_path), a_pull_request_stating(""))

    assert result.returncode != 0, result.stdout + result.stderr
    assert "Acceptance gate failed:" in result.stderr
    assert STATES_NEITHER in result.stderr


@pytest.mark.parametrize(
    ("proposed_by", "problem"),
    [
        (a_pull_request_without_the_field(), STATES_NEITHER),
        (a_pull_request_stating("none"), STATES_NEITHER),
        (a_pull_request_stating("none:"), STATES_NEITHER),
        (
            a_pull_request_stating(UNDECLARED_SENTENCE),
            (
                f"names {UNDECLARED_SENTENCE!r} as an acceptance sentence of "
                "this landing, which no story declares"
            ),
        ),
        (
            a_pull_request_stating(f"`{MOVABLE_SENTENCE}`, `{UNDECLARED_SENTENCE}`"),
            (
                f"names {UNDECLARED_SENTENCE!r} as an acceptance sentence of "
                "this landing, which no story declares"
            ),
        ),
    ],
    ids=[
        "no-acceptance-field-at-all",
        "the-word-none-without-a-reason",
        "an-exemption-with-an-empty-reason",
        "an-identifier-no-story-declares",
        "one-declared-identifier-beside-an-undeclared-one",
    ],
)
def test_a_landing_whose_binding_does_not_hold_is_named(
    tmp_path: Path, proposed_by: str, problem: str
) -> None:
    result = run_gate(copied_project(tmp_path), proposed_by)

    assert result.returncode != 0, result.stdout + result.stderr
    assert problem in result.stderr


@pytest.mark.parametrize(
    "binding",
    [
        MOVABLE_SENTENCE,
        f"`{MOVABLE_SENTENCE}`, `{SECOND_DECLARED_SENTENCE}`",
        "none: documentation only",
        "None - this head moves modules and proves no new sentence",
    ],
    ids=[
        "one-declared-identifier",
        "several-declared-identifiers-in-backticks",
        "a-reasoned-exemption",
        "a-reasoned-exemption-written-in-prose",
    ],
)
def test_a_landing_that_states_its_binding_passes(tmp_path: Path, binding: str) -> None:
    result = run_gate(copied_project(tmp_path), a_pull_request_stating(binding))

    assert result.returncode == 0, result.stdout + result.stderr


def test_a_run_no_pull_request_proposes_says_the_binding_was_not_checked(
    tmp_path: Path,
) -> None:
    result = run_gate(copied_project(tmp_path))

    assert result.returncode == 0, result.stdout + result.stderr
    assert "no proposed pull request" in result.stdout


def write_claim(project: Path, relative: Path, text: str) -> None:
    written = project / relative
    written.parent.mkdir(parents=True, exist_ok=True)
    written.write_text(text, encoding="utf-8")


def a_python_claim_no_runner_collects(identifier: str) -> str:
    return (
        "import pytest\n\n\n"
        f'@pytest.mark.proves("{identifier}")\n'
        "def test_helps() -> None:\n    pass\n"
    )


@pytest.mark.parametrize(
    ("claim", "located_in", "claiming", "problem"),
    [
        (
            a_python_claim_no_runner_collects(UNDECLARED_SENTENCE),
            UNCOLLECTED_PYTHON_CLAIM,
            "test_helps",
            f"claims {UNDECLARED_SENTENCE!r}, which no story declares",
        ),
        (
            a_python_claim_no_runner_collects(MOVABLE_SENTENCE),
            UNCOLLECTED_PYTHON_CLAIM,
            "test_helps",
            f"claims {MOVABLE_SENTENCE!r}, which no run report shows passing",
        ),
        (
            f'it("proves({MOVABLE_SENTENCE}): nothing runs this file", () => {{}});\n',
            UNCOLLECTED_TYPESCRIPT_CLAIM,
            f"proves({MOVABLE_SENTENCE}): nothing runs this file",
            f"claims {MOVABLE_SENTENCE!r}, which no run report shows passing",
        ),
    ],
    ids=["undeclared-sentence", "declared-sentence", "typescript-claim"],
)
def test_a_claim_no_run_report_carries_is_named_wherever_it_sits(
    tmp_path: Path, claim: str, located_in: Path, claiming: str, problem: str
) -> None:
    project = copied_project(tmp_path, unproven=MOVABLE_SENTENCE)
    write_claim(project, located_in, claim)

    result = run_gate(project)

    assert result.returncode != 0, result.stdout + result.stderr
    assert f"{located_in}:{claiming} {problem}" in result.stderr


def test_each_claiming_test_must_appear_in_a_required_report(tmp_path: Path) -> None:
    project = copied_project(tmp_path, unproven=MOVABLE_SENTENCE)
    missing_claim = Path("tests/test_missing_claim.py")
    reported_claim = Path("tests/test_reported_claim.py")
    write_claim(
        project,
        missing_claim,
        a_python_claim_no_runner_collects(MOVABLE_SENTENCE).replace(
            "test_helps", "test_missing_claim"
        ),
    )
    write_claim(
        project,
        reported_claim,
        a_python_claim_no_runner_collects(MOVABLE_SENTENCE).replace(
            "test_helps", "test_reported_claim"
        ),
    )
    (project / REPORTS_DIRECTORY / CRASH_REPORT).write_text(
        junit_report(
            (
                ReportedTest(
                    "test_reported_claim",
                    MOVABLE_SENTENCE,
                    located_in=reported_claim,
                ),
            )
        ),
        encoding="utf-8",
    )

    result = run_gate(project)

    assert result.returncode != 0, result.stdout + result.stderr
    assert (
        f"{missing_claim}:test_missing_claim claims {MOVABLE_SENTENCE!r}, "
        "which no run report shows passing"
    ) in result.stderr
    assert f"{reported_claim}:test_reported_claim claims" not in result.stderr


def test_each_typescript_title_must_appear_in_a_required_report(tmp_path: Path) -> None:
    project = copied_project(tmp_path, unproven=REPORT_ONLY_SENTENCE)
    missing_title = f"proves({REPORT_ONLY_SENTENCE}): the browser-only flow"
    reported_title = f"proves({REPORT_ONLY_SENTENCE}): the unit flow"
    missing_claim = Path("frontend/tests/e2e/missing.spec.ts")
    reported_claim = Path("frontend/tests/lib/reported.test.ts")
    write_claim(project, missing_claim, f'it("{missing_title}", () => {{}});\n')
    write_claim(project, reported_claim, f'it("{reported_title}", () => {{}});\n')
    (project / REPORTS_DIRECTORY / FRONTEND_REPORT).write_text(
        vitest_report((ReportedTest(reported_title),), located_in=reported_claim),
        encoding="utf-8",
    )

    result = run_gate(project)

    assert result.returncode != 0, result.stdout + result.stderr
    assert (
        f"{missing_claim}:{missing_title} claims {REPORT_ONLY_SENTENCE!r}, "
        "which no run report shows passing"
    ) in result.stderr
    assert f"{reported_claim}:{reported_title} claims" not in result.stderr


def test_a_browser_claim_needs_its_passing_playwright_result(tmp_path: Path) -> None:
    project = copied_project(tmp_path, unproven=REPORT_ONLY_SENTENCE)
    claim = Path("frontend/tests/e2e/proof.spec.ts")
    title = f"proves({REPORT_ONLY_SENTENCE}): the browser flow"
    write_claim(project, claim, f'test("{title}", async () => {{}});\n')
    (project / REPORTS_DIRECTORY / PLAYWRIGHT_REPORT).write_text(
        playwright_report_with_stand_ins(
            project,
            (ReportedTest(title),),
            located_in=claim,
            without=REPORT_ONLY_SENTENCE,
        ),
        encoding="utf-8",
    )

    result = run_gate(project)

    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize(
    "report",
    [
        playwright_report((ReportedTest("CLAIM", outcome=Outcome.FAILED),)),
        playwright_report((ReportedTest("CLAIM", outcome=Outcome.SKIPPED),)),
        playwright_report((ReportedTest("CLAIM", ran=False),)),
    ],
    ids=["failed", "skipped", "unrun"],
)
def test_a_browser_claim_that_did_not_pass_is_named_and_fails_the_gate(
    tmp_path: Path, report: str
) -> None:
    project = copied_project(tmp_path, unproven=REPORT_ONLY_SENTENCE)
    claim = Path("frontend/tests/e2e/proof.spec.ts")
    title = f"proves({REPORT_ONLY_SENTENCE}): the browser flow"
    write_claim(project, claim, f'test("{title}", async () => {{}});\n')
    (project / REPORTS_DIRECTORY / PLAYWRIGHT_REPORT).write_text(
        report.replace("CLAIM", title), encoding="utf-8"
    )

    result = run_gate(project)

    assert result.returncode != 0, result.stdout + result.stderr
    assert (
        f"{claim}:{title} claims {REPORT_ONLY_SENTENCE!r}, "
        "which no run report shows passing"
    ) in result.stderr


def test_a_browser_claim_cannot_borrow_a_passing_vitest_row(tmp_path: Path) -> None:
    project = copied_project(tmp_path, unproven=REPORT_ONLY_SENTENCE)
    claim = Path("frontend/tests/e2e/proof.spec.ts")
    title = f"proves({REPORT_ONLY_SENTENCE}): the browser flow"
    write_claim(project, claim, f'test("{title}", async () => {{}});\n')
    (project / REPORTS_DIRECTORY / FRONTEND_REPORT).write_text(
        vitest_report((ReportedTest(title),), located_in=claim), encoding="utf-8"
    )
    (project / REPORTS_DIRECTORY / PLAYWRIGHT_REPORT).write_text(
        playwright_report((ReportedTest(title, outcome=Outcome.FAILED),)),
        encoding="utf-8",
    )

    result = run_gate(project)

    assert result.returncode != 0, result.stdout + result.stderr
    assert f"{claim}:{title} claims" in result.stderr


def test_a_unit_claim_cannot_borrow_a_passing_playwright_row(tmp_path: Path) -> None:
    project = copied_project(tmp_path, unproven=REPORT_ONLY_SENTENCE)
    claim = Path("frontend/tests/lib/proof.test.ts")
    title = f"proves({REPORT_ONLY_SENTENCE}): the unit flow"
    write_claim(project, claim, f'it("{title}", () => {{}});\n')
    (project / REPORTS_DIRECTORY / FRONTEND_REPORT).write_text(
        vitest_report((ReportedTest(title, outcome=Outcome.FAILED),), located_in=claim),
        encoding="utf-8",
    )
    (project / REPORTS_DIRECTORY / PLAYWRIGHT_REPORT).write_text(
        playwright_report(
            (ReportedTest(title),),
            root_dir=Path("/workspace/frontend/tests"),
            spec_file="lib/proof.test.ts",
        ),
        encoding="utf-8",
    )

    result = run_gate(project)

    assert result.returncode != 0, result.stdout + result.stderr
    assert f"{claim}:{title} claims" in result.stderr


def a_browser_sentence_declaration(*, proof: str | None) -> str:
    proof_line = f'proof = "{proof}"\n' if proof is not None else ""
    return (
        "schema_version = 1\n"
        'story = "https://github.com/FlexOr2/atelier-2/issues/1022"\n\n'
        "[[sentence]]\n"
        f'id = "{BROWSER_SENTENCE}"\n'
        'text = "A sentence about a rendered surface is proven only by a real '
        'browser run."\n'
        f"{proof_line}"
    )


def a_vitest_claim_report(sentence: str) -> tuple[str, str, str]:
    title = f"proves({sentence}): a jsdom run claims it too"
    return FRONTEND_REPORT, vitest_report((ReportedTest(title),)), title


def a_junit_claim_report(sentence: str) -> tuple[str, str, str]:
    claiming_test = "test_claims_it_from_a_junit_run"
    return (
        CRASH_REPORT,
        junit_report((ReportedTest(claiming_test, sentence),)),
        claiming_test,
    )


NON_PLAYWRIGHT_CLAIM_BUILDERS = (a_vitest_claim_report, a_junit_claim_report)


def a_browser_sentence_project(
    tmp_path: Path,
    reports: Mapping[str, str] | None = None,
    *,
    proof: str | None = "browser",
    playwright_claims: Iterable[ReportedTest] = (),
) -> Path:
    project = copied_project(
        tmp_path,
        reports,
        also_declaring={
            BROWSER_SENTENCE_DECLARATION: a_browser_sentence_declaration(proof=proof)
        },
        unproven=BROWSER_SENTENCE,
    )
    if playwright_claims:
        # Composed after the fixture exists: the fixture's own stand-in proofs for
        # every other browser-only sentence (today, sentence 435) have to survive
        # beside this test's own Playwright evidence, not be replaced by it.
        (project / REPORTS_DIRECTORY / PLAYWRIGHT_REPORT).write_text(
            playwright_report_with_stand_ins(
                project, playwright_claims, without=BROWSER_SENTENCE
            ),
            encoding="utf-8",
        )
    return project


@pytest.mark.parametrize(
    "build_claim", NON_PLAYWRIGHT_CLAIM_BUILDERS, ids=["vitest", "junit"]
)
def test_a_browser_sentence_fails_when_a_non_playwright_report_claims_it(
    tmp_path: Path, build_claim: Callable[[str], tuple[str, str, str]]
) -> None:
    report_file, report, claiming_test = build_claim(BROWSER_SENTENCE)
    project = a_browser_sentence_project(tmp_path, {report_file: report})

    result = run_gate(project)

    assert result.returncode != 0, result.stdout + result.stderr
    assert BROWSER_SENTENCE in result.stderr
    assert report_file in result.stderr
    assert claiming_test in result.stderr


@pytest.mark.parametrize(
    "build_claim", NON_PLAYWRIGHT_CLAIM_BUILDERS, ids=["vitest", "junit"]
)
def test_a_browser_sentence_fails_even_beside_a_valid_playwright_proof(
    tmp_path: Path, build_claim: Callable[[str], tuple[str, str, str]]
) -> None:
    report_file, report, claiming_test = build_claim(BROWSER_SENTENCE)
    playwright_title = f"proves({BROWSER_SENTENCE}): the browser flow"
    project = a_browser_sentence_project(
        tmp_path,
        {report_file: report},
        playwright_claims=(ReportedTest(playwright_title),),
    )

    result = run_gate(project)

    assert result.returncode != 0, result.stdout + result.stderr
    assert BROWSER_SENTENCE in result.stderr
    assert report_file in result.stderr
    assert claiming_test in result.stderr


def test_a_browser_sentence_passes_when_only_the_playwright_report_claims_it(
    tmp_path: Path,
) -> None:
    playwright_title = f"proves({BROWSER_SENTENCE}): the browser flow"
    project = a_browser_sentence_project(
        tmp_path, playwright_claims=(ReportedTest(playwright_title),)
    )

    result = run_gate(project)

    assert result.returncode == 0, result.stdout + result.stderr


def test_a_sentence_without_a_proof_key_still_accepts_any_report(
    tmp_path: Path,
) -> None:
    report_file, report, _claiming_test = a_vitest_claim_report(BROWSER_SENTENCE)
    project = a_browser_sentence_project(tmp_path, {report_file: report}, proof=None)

    result = run_gate(project)

    assert result.returncode == 0, result.stdout + result.stderr


def test_an_unknown_proof_value_is_refused(tmp_path: Path) -> None:
    project = a_browser_sentence_project(tmp_path)
    rewrite(
        project, BROWSER_SENTENCE_DECLARATION, 'proof = "browser"', 'proof = "gesture"'
    )

    result = run_gate(project)

    assert result.returncode != 0, result.stdout + result.stderr
    assert "Acceptance gate refused:" in result.stderr
    assert BROWSER_SENTENCE in result.stderr
    assert "gesture" in result.stderr


@pytest.mark.parametrize(
    ("root_dir", "spec_file", "problem"),
    [
        (Path("frontend/tests/e2e"), "proof.spec.ts", "rootDir is not absolute"),
        (
            Path("/workspace/frontend/tests/e2e"),
            "../proof.spec.ts",
            "file is outside rootDir",
        ),
    ],
    ids=["relative-root", "escaping-file"],
)
def test_a_playwright_report_path_outside_its_absolute_root_is_refused(
    tmp_path: Path, root_dir: Path, spec_file: str, problem: str
) -> None:
    project = copied_project(tmp_path)
    (project / REPORTS_DIRECTORY / PLAYWRIGHT_REPORT).write_text(
        playwright_report(
            (ReportedTest("a test without a claim"),),
            root_dir=root_dir,
            spec_file=spec_file,
        ),
        encoding="utf-8",
    )

    result = run_gate(project)

    assert result.returncode != 0, result.stdout + result.stderr
    assert f"{PLAYWRIGHT_REPORT} is not readable as a run report" in result.stderr
    assert problem in result.stderr


def test_same_named_python_tests_in_different_files_are_distinct_claims(
    tmp_path: Path,
) -> None:
    project = copied_project(tmp_path, unproven=REPORT_ONLY_SENTENCE)
    missing_claim = Path("tests/test_missing_duplicate.py")
    reported_claim = Path("tests/test_reported_duplicate.py")
    claim = a_python_claim_no_runner_collects(REPORT_ONLY_SENTENCE).replace(
        "test_helps", "test_duplicate"
    )
    write_claim(project, missing_claim, claim)
    write_claim(project, reported_claim, claim)
    (project / REPORTS_DIRECTORY / CRASH_REPORT).write_text(
        junit_report(
            (
                ReportedTest(
                    "test_duplicate",
                    REPORT_ONLY_SENTENCE,
                    located_in=reported_claim,
                ),
            )
        ),
        encoding="utf-8",
    )

    result = run_gate(project)

    assert result.returncode != 0, result.stdout + result.stderr
    assert f"{missing_claim}:test_duplicate claims" in result.stderr
    assert f"{reported_claim}:test_duplicate claims" not in result.stderr


def test_same_titled_typescript_tests_in_different_files_are_distinct_claims(
    tmp_path: Path,
) -> None:
    project = copied_project(tmp_path, unproven=REPORT_ONLY_SENTENCE)
    title = f"proves({REPORT_ONLY_SENTENCE}): the duplicate title"
    missing_claim = Path("frontend/tests/e2e/missing.spec.ts")
    reported_claim = Path("frontend/tests/lib/reported.test.ts")
    claim = f'it("{title}", () => {{}});\n'
    write_claim(project, missing_claim, claim)
    write_claim(project, reported_claim, claim)
    (project / REPORTS_DIRECTORY / FRONTEND_REPORT).write_text(
        vitest_report((ReportedTest(title),), located_in=reported_claim),
        encoding="utf-8",
    )

    result = run_gate(project)

    assert result.returncode != 0, result.stdout + result.stderr
    assert f"{missing_claim}:{title} claims" in result.stderr
    assert f"{reported_claim}:{title} claims" not in result.stderr


@pytest.mark.parametrize(
    ("located_in", "source"),
    [
        (
            Path("tests/test_duplicate.py"),
            (
                "import pytest\n\n\n"
                f'@pytest.mark.proves("{REPORT_ONLY_SENTENCE}")\n'
                "def test_duplicate() -> None:\n    pass\n\n\n"
                "class TestNested:\n"
                f'    @pytest.mark.proves("{REPORT_ONLY_SENTENCE}")\n'
                "    def test_duplicate(self) -> None:\n        pass\n"
            ),
        ),
        (
            Path("frontend/tests/lib/duplicate.test.ts"),
            (
                f'describe("one", () => it("proves({REPORT_ONLY_SENTENCE}): duplicate", () => {{}}));\n'
                f'describe("two", () => it("proves({REPORT_ONLY_SENTENCE}): duplicate", () => {{}}));\n'
            ),
        ),
    ],
    ids=["python-class-scope", "vitest-suite-scope"],
)
def test_a_repeated_source_test_identity_is_refused_as_ambiguous(
    tmp_path: Path, located_in: Path, source: str
) -> None:
    project = copied_project(tmp_path, unproven=REPORT_ONLY_SENTENCE)
    write_claim(project, located_in, source)
    if located_in.suffix == ".py":
        report_name = "test_duplicate"
        report = junit_report(
            (
                ReportedTest(
                    report_name,
                    REPORT_ONLY_SENTENCE,
                    located_in=located_in,
                ),
            )
        )
        report_file = CRASH_REPORT
    else:
        report_name = f"proves({REPORT_ONLY_SENTENCE}): duplicate"
        report = vitest_report((ReportedTest(report_name),), located_in=located_in)
        report_file = FRONTEND_REPORT
    (project / REPORTS_DIRECTORY / report_file).write_text(report, encoding="utf-8")

    result = run_gate(project)

    assert result.returncode != 0, result.stdout + result.stderr
    assert (
        f"{located_in}:{report_name} declares the same proof identity more than once"
        in result.stderr
    )


@pytest.mark.parametrize(
    "reported_name",
    ["test_helps", "test_helps@direct-systemd-user-manager"],
    ids=["plain", "xdist-group"],
)
def test_a_unique_python_class_method_matches_its_junit_identity(
    tmp_path: Path, reported_name: str
) -> None:
    project = copied_project(tmp_path, unproven=REPORT_ONLY_SENTENCE)
    located_in = Path("tests/test_class_claim.py")
    write_claim(
        project,
        located_in,
        (
            "import pytest\n\n\n"
            "class TestClaim:\n"
            f'    @pytest.mark.proves("{REPORT_ONLY_SENTENCE}")\n'
            "    def test_helps(self) -> None:\n        pass\n"
        ),
    )
    (project / REPORTS_DIRECTORY / CRASH_REPORT).write_text(
        junit_report(
            (
                ReportedTest(
                    reported_name,
                    REPORT_ONLY_SENTENCE,
                    located_in=located_in,
                    class_scope=("TestClaim",),
                ),
            )
        ),
        encoding="utf-8",
    )

    result = run_gate(project)

    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize(
    ("placeholder", "rendered"),
    [("%s", "one"), ("%s", ""), ("%c", ""), ("%#", "0"), ("%$", "1")],
    ids=[
        "value",
        "empty-value",
        "empty-css-format",
        "zero-based-index",
        "one-based-index",
    ],
)
def test_a_parameterized_typescript_title_matches_its_reported_cases(
    tmp_path: Path, placeholder: str, rendered: str
) -> None:
    project = copied_project(tmp_path)
    claim = Path("frontend/tests/lib/parameterized.test.ts")
    title = f"proves({REPORT_ONLY_SENTENCE}): {placeholder} remains named"
    write_claim(project, claim, f'it.each(["one"])("{title}", () => {{}});\n')
    (project / REPORTS_DIRECTORY / FRONTEND_REPORT).write_text(
        vitest_report(
            (
                ReportedTest(
                    f"proves({REPORT_ONLY_SENTENCE}): {rendered} remains named"
                ),
            ),
            located_in=claim,
        ),
        encoding="utf-8",
    )

    result = run_gate(project)

    assert result.returncode == 0, result.stdout + result.stderr


def test_a_parameterized_typescript_claim_cannot_borrow_a_static_tests_proof(
    tmp_path: Path,
) -> None:
    project = copied_project(tmp_path, unproven=REPORT_ONLY_SENTENCE)
    claim = Path("frontend/tests/lib/parameterized.test.ts")
    parameterized_title = f"proves({REPORT_ONLY_SENTENCE}): %s remains named"
    static_title = f"proves({REPORT_ONLY_SENTENCE}): one remains named"
    write_claim(
        project,
        claim,
        (
            f'it.each(["one"])("{parameterized_title}", () => {{}});\n'
            f'it("{static_title}", () => {{}});\n'
        ),
    )
    (project / REPORTS_DIRECTORY / FRONTEND_REPORT).write_text(
        vitest_report((ReportedTest(static_title),), located_in=claim),
        encoding="utf-8",
    )

    result = run_gate(project)

    assert result.returncode != 0, result.stdout + result.stderr
    assert f"{claim}:{parameterized_title} claims" in result.stderr
    assert f"{claim}:{static_title} claims" not in result.stderr


def test_an_escaped_typescript_title_matches_its_decoded_reported_title(
    tmp_path: Path,
) -> None:
    project = copied_project(tmp_path)
    claim = Path("frontend/tests/lib/escaped.test.ts")
    reported_title = f'proves({REPORT_ONLY_SENTENCE}): "one" remains named'
    source_title = reported_title.replace('"', '\\"')
    write_claim(project, claim, f'it("{source_title}", () => {{}});\n')
    (project / REPORTS_DIRECTORY / FRONTEND_REPORT).write_text(
        vitest_report((ReportedTest(reported_title),), located_in=claim),
        encoding="utf-8",
    )

    result = run_gate(project)

    assert result.returncode == 0, result.stdout + result.stderr


def test_a_typescript_claim_is_its_whole_title_and_not_a_comment() -> None:
    script = load_acceptance_script()
    title = f"context proves({REPORT_ONLY_SENTENCE}): remains one test"
    source = (
        f'// "proves({MOVABLE_SENTENCE}): comments make no claim"\n'
        f'it("{title}", () => {{}});\n'
    )

    claims = tuple(script.read_typescript_claims(source, Path("a.test.ts")))

    assert [(claim.sentence_identifier, claim.claiming_test) for claim in claims] == [
        (REPORT_ONLY_SENTENCE, title)
    ]


def test_every_sentence_this_repository_declares_is_claimed_by_one_of_its_tests() -> (
    None
):
    script = load_acceptance_script()

    claimed = {
        claim.sentence_identifier for claim in script.read_source_claims(PROJECT_ROOT)
    }

    declared = {
        sentence.identifier for sentence in script.read_declared_sentences(PROJECT_ROOT)
    }
    assert declared <= claimed, f"unclaimed: {sorted(declared - claimed)}"
    assert claimed <= declared, f"undeclared: {sorted(claimed - declared)}"


def empty_the_declarations(project: Path) -> None:
    for declaration in (project / ACCEPTANCE).glob(f"*{DECLARATION.suffix}"):
        declaration.unlink()


def claim_without_naming_a_sentence(project: Path) -> None:
    write_claim(
        project,
        UNCOLLECTED_PYTHON_CLAIM,
        "import pytest\n\n\n@pytest.mark.proves\ndef test_helps() -> None:\n    pass\n",
    )


def declare_an_unknown_key(project: Path) -> None:
    rewrite(
        project, DECLARATION, "schema_version = 1\n", 'schema_version = 1\nowner = ""\n'
    )


def declare_a_future_schema_version(project: Path) -> None:
    rewrite(project, DECLARATION, "schema_version = 1", "schema_version = 2")


def declare_a_sentence_twice(project: Path) -> None:
    with (project / DECLARATION).open("a", encoding="utf-8") as declaration:
        declaration.write(
            f'\n[[sentence]]\nid = "{MOVABLE_SENTENCE}"\ntext = "the same wish again"\n'
        )


def lose_the_cockpit_report(project: Path) -> None:
    (project / REPORTS_DIRECTORY / FRONTEND_REPORT).unlink()


def lose_the_browser_report(project: Path) -> None:
    (project / REPORTS_DIRECTORY / PLAYWRIGHT_REPORT).unlink()


def truncate_the_quality_report(project: Path) -> None:
    (project / REPORTS_DIRECTORY / QUALITY_REPORT).write_text(
        "<testsuites>", encoding="utf-8"
    )


def truncate_the_cockpit_report(project: Path) -> None:
    (project / REPORTS_DIRECTORY / FRONTEND_REPORT).write_text("{}", encoding="utf-8")


def truncate_the_browser_report(project: Path) -> None:
    (project / REPORTS_DIRECTORY / PLAYWRIGHT_REPORT).write_text("{}", encoding="utf-8")


@pytest.mark.parametrize(
    ("distrusted", "refusal"),
    [
        (empty_the_declarations, "declares no acceptance sentence"),
        (declare_an_unknown_key, "declares unknown keys ['owner']"),
        (declare_a_future_schema_version, "this gate reads version 1"),
        (declare_a_sentence_twice, "is declared twice"),
        (claim_without_naming_a_sentence, "without naming one sentence"),
        (lose_the_cockpit_report, f"the run report {FRONTEND_REPORT} is absent"),
        (lose_the_browser_report, f"the run report {PLAYWRIGHT_REPORT} is absent"),
        (
            truncate_the_quality_report,
            f"{QUALITY_REPORT} is not readable as a run report",
        ),
        (
            truncate_the_cockpit_report,
            f"{FRONTEND_REPORT} is not readable as a run report",
        ),
        (
            truncate_the_browser_report,
            f"{PLAYWRIGHT_REPORT} is not readable as a run report",
        ),
    ],
    ids=[
        "no-declaration",
        "unknown-declaration-key",
        "unread-schema-version",
        "duplicate-sentence",
        "marker-without-a-sentence",
        "missing-run-report",
        "missing-browser-report",
        "unreadable-pytest-report",
        "unreadable-vitest-report",
        "unreadable-playwright-report",
    ],
)
def test_a_declaration_or_run_report_the_gate_cannot_read_is_refused(
    tmp_path: Path, distrusted: Callable[[Path], None], refusal: str
) -> None:
    project = copied_project(tmp_path)
    distrusted(project)

    result = run_gate(project)

    assert result.returncode != 0, result.stdout + result.stderr
    assert "Acceptance gate refused:" in result.stderr
    assert refusal in result.stderr


def test_the_proves_marker_reaches_the_run_report_under_parallel_execution(
    tmp_path: Path,
) -> None:
    project = tmp_path / "run"
    (project / CONFTEST).parent.mkdir(parents=True)
    shutil.copy2(PROJECT_ROOT / CONFTEST, project / CONFTEST)
    (project / "pyproject.toml").write_text(
        "[tool.pytest.ini_options]\n"
        'addopts = "--strict-config --strict-markers"\n'
        'filterwarnings = ["error"]\n'
        'markers = ["proves(sentence_id): name the sentence this test proves"]\n',
        encoding="utf-8",
    )
    (project / "tests/test_claim.py").write_text(
        "import pytest\n\n\n"
        f'@pytest.mark.proves("{MOVABLE_SENTENCE}")\n'
        "def test_claims_its_sentence() -> None:\n    pass\n",
        encoding="utf-8",
    )
    report = project / QUALITY_REPORT

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-n",
            "auto",
            "-q",
            "-p",
            "no:cacheprovider",
            f"--junitxml={report}",
            "tests",
        ],
        cwd=project,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    proofs = tuple(load_acceptance_script().read_junit_proofs(report))
    assert [(proof.sentence_identifier, proof.proving_test) for proof in proofs] == [
        (MOVABLE_SENTENCE, "test_claims_its_sentence")
    ]


def a_declaration_binding(requirement: str) -> str:
    """One story whose only sentence names the requirement it serves."""

    return (
        "schema_version = 1\n"
        'story = "https://github.com/FlexOr2/atelier-2/issues/89"\n\n'
        "[[sentence]]\n"
        f'id = "{SECOND_STORY_SENTENCE}"\n'
        'text = "A second story declares one sentence."\n'
        f'requirement = "{requirement}"\n'
    )


def test_a_sentence_naming_a_requirement_no_document_declares_is_refused(
    tmp_path: Path,
) -> None:
    """A link to a rule nobody wrote is worse than no link at all.

    The whole value of the field is that a reader can follow it. A dead
    identifier turns the filing cabinet into the thing it was meant to stop --
    a drawer that answers confidently and wrongly -- so the gate refuses it by
    the name it could not find.
    """

    project = copied_project(
        tmp_path,
        also_declaring={
            SECOND_STORY_DECLARATION: a_declaration_binding(AN_UNDECLARED_REQUIREMENT)
        },
    )

    result = run_gate(project)

    assert result.returncode == 1
    assert AN_UNDECLARED_REQUIREMENT in result.stderr
    assert "no active requirement declares" in result.stderr


def test_a_sentence_naming_a_declared_requirement_passes_the_gate(
    tmp_path: Path,
) -> None:
    project = copied_project(
        tmp_path,
        also_declaring={
            SECOND_STORY_DECLARATION: a_declaration_binding(A_DECLARED_REQUIREMENT)
        },
    )

    result = run_gate(project)

    assert result.returncode == 0, result.stderr


def test_requirement_contract_drift_is_refused_before_trace(tmp_path: Path) -> None:
    project = copied_project(tmp_path)
    content = (
        "# Legacy requirement\n\n"
        "### REQ-LEGACY-01: Legacy bytes remain frozen.\n"
        "Quelle: DESK — test fixture\n"
    ).encode()
    document = project / A_LEGACY_REQUIREMENT
    document.write_bytes(content)
    registry = project / REQUIREMENTS / "revisions.toml"
    with registry.open("a", encoding="utf-8") as stream:
        stream.write(
            "\n[[legacy]]\n"
            'document = "0008"\n'
            f'path = "{A_LEGACY_REQUIREMENT}"\n'
            f'content_sha256 = "{hashlib.sha256(content).hexdigest()}"\n'
        )
    document.write_bytes(document.read_bytes() + b"\n")

    result = run_gate(project)

    assert result.returncode != 0
    assert "requirement contract refused" in result.stderr
    assert str(A_LEGACY_REQUIREMENT) in result.stderr
    assert "legacy content digest" in result.stderr


def test_sentences_binding_no_requirement_are_listed_rather_than_refused(
    tmp_path: Path,
) -> None:
    """Naming the gap is the point; failing on it would be a different story.

    Almost nothing is filed yet, and a gate that went red over that would stop
    the workshop to file paperwork. It says how many sentences reach a rule and
    how many do not, so the number is visible instead of comfortable.
    """

    result = run_gate(copied_project(tmp_path))

    assert result.returncode == 0, result.stderr
    assert "bind no requirement" in result.stdout


# The two answers that broke the gate in one night, kept verbatim: both read
# perfectly to a person, and both were the field's two ways of being wrong.
PROSE_ANSWER = (
    "the nine sentences of `acceptance/58-every-attempt-runs-in-its-own-workspace"
    ".toml`, each bound by @pytest.mark.proves"
)
EMPHASISED_EXEMPTION = "`none`. This head adds a check and a count to the gate"


def test_a_prose_answer_is_named_once_with_the_form_the_field_expects(
    tmp_path: Path,
) -> None:
    """Ten true complaints about ten words say less than one about the answer.

    A field answered in prose produced one `names 'the' … which no story
    declares` per word. Every line was true and none of them said what was
    actually wrong, so the reader had to infer the rule the gate was applying.
    """

    result = run_gate(copied_project(tmp_path), a_pull_request_stating(PROSE_ANSWER))

    assert result.returncode != 0
    problems = [line for line in result.stderr.splitlines() if line.startswith("  - ")]
    assert len(problems) == 1, problems
    assert "identifier" in problems[0]
    assert "none" in problems[0]


def test_an_exemption_a_body_wrote_in_backticks_is_read_as_the_exemption_it_is(
    tmp_path: Path,
) -> None:
    """The template itself shows `none` in backticks; the gate must read it.

    Markdown emphasis around the word is not a second meaning. Refusing it would
    make the template teach the one spelling the gate rejects, and every author
    pays a red run and a forced push to learn that.
    """

    result = run_gate(
        copied_project(tmp_path), a_pull_request_stating(EMPHASISED_EXEMPTION)
    )

    assert result.returncode == 0, result.stderr
    assert "Landing binding: exempt" in result.stdout


def test_one_misspelt_identifier_still_gets_its_own_line(tmp_path: Path) -> None:
    """A typo is not a form error, and collapsing the two would lose the name.

    An answer shaped like identifiers is read as identifiers: the one that no
    story declares is named, because that is the thing the author has to fix.
    """

    result = run_gate(
        copied_project(tmp_path),
        a_pull_request_stating(f"{MOVABLE_SENTENCE}, a-sentence-nobody-declared"),
    )

    assert result.returncode != 0
    assert "'a-sentence-nobody-declared'" in result.stderr
    assert "which no story declares" in result.stderr
    assert "neither form" not in result.stderr


def test_the_template_asks_for_a_form_the_gate_can_read() -> None:
    """The template is where an author learns the form; it must teach a true one.

    Both of the night's failures began in a body written from this template. A
    template whose acceptance line the field reader cannot find, or which shows a
    spelling the gate refuses, teaches the error it then punishes.
    """

    gate = load_acceptance_script()
    template = (PROJECT_ROOT / PULL_REQUEST_TEMPLATE).read_text(encoding="utf-8")

    field = gate.LANDING_FIELD.search(template)

    assert field is not None, "the field reader cannot find the template's own line"
    assert field.group("stated") == "", "the template's field is filled in"
    assert "the word none" in template
