from __future__ import annotations

import logging
import sys
from collections.abc import Iterator
from typing import TYPE_CHECKING, cast

import pytest

if TYPE_CHECKING:
    from atelier2.adapters.dbos.runtime import _DbosProcessOwner

PROOF_MARKER = "proves"
DBOS_RUNTIME_MODULE = "atelier2.adapters.dbos.runtime"
DBOS_LOGGER_NAME = "dbos"


@pytest.fixture(autouse=True)
def dbos_logger_handlers_stay_with_their_test() -> Iterator[None]:
    """Drop the handlers DBOS attaches to its own logger during a test.

    Nothing in the test tree configures the ``dbos`` logger, so the first DBOS
    construction in a process attaches a stream handler to whatever
    ``sys.stderr`` is at that moment, and ``DBOS.destroy`` leaves it there.
    Under ``capsys`` that stream is the test's own capture buffer, closed at
    its teardown; the next DBOS construction in the same worker flushes the
    closed buffer and refuses, so whichever DBOS test follows in the worker
    fails. Handing the logger back the handlers it had before the test keeps a
    binding's logging inside the test that opened it.
    """

    dbos_logger = logging.getLogger(DBOS_LOGGER_NAME)
    inherited = dbos_logger.handlers[:]
    yield
    for handler in dbos_logger.handlers[:]:
        if handler not in inherited:
            dbos_logger.removeHandler(handler)


@pytest.fixture(autouse=True)
def dbos_runtime_binding_is_left_free() -> Iterator[None]:
    """Fail the test that ends while the process-wide DBOS binding is still held.

    A process owns one DBOS runtime binding, so a test that opens a runtime and
    never closes it -- an assertion, a timeout, or a refusal that never came,
    between opening and closing -- makes every later runtime in that worker
    refuse. Releasing the binding here, and failing the test that kept it, is
    what keeps one broken test from being read as hundreds of them.

    A process that never imported the runtime cannot hold a binding, so the
    module is looked up rather than imported and this costs a test that has no
    runtime nothing.
    """

    yield
    module = sys.modules.get(DBOS_RUNTIME_MODULE)
    if module is None:
        return
    owner = cast("_DbosProcessOwner", module._PROCESS_OWNER)
    held = owner._bound
    if held is None:
        return
    database = held.settings.database_path
    while owner._bound is not None:
        owner.release(held)
    pytest.fail(
        "this test ended still holding the process-wide DBOS runtime binding "
        f"of {database}"
    )


@pytest.fixture(
    params=[
        pytest.param(b"not an open-pr request", id="utf8-body"),
        pytest.param(b"\xff", id="non-utf8-body"),
        pytest.param(b'{"body":"missing head"}', id="incomplete-object"),
    ]
)
def malformed_open_pr_payload(request: pytest.FixtureRequest) -> bytes:
    return request.param


@pytest.fixture
def dbos_logging_isolation() -> Iterator[None]:
    """Keep DBOS from flushing capture handlers another test has closed."""
    root = logging.getLogger()
    inherited = root.handlers[:]
    root.handlers = []
    try:
        yield
    finally:
        root.handlers = inherited


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    """Carry every ``proves`` claim into the run report pytest writes.

    Collection is the earliest place a claim can be attached, so it reaches the
    report even for a test that errors in setup. The ``record_property`` fixture
    would do the same at call time, but it warns under every junit family except
    the legacy ones.
    """
    for item in items:
        for marker in item.iter_markers(name=PROOF_MARKER):
            for identifier in marker.args:
                item.user_properties.append((PROOF_MARKER, identifier))
