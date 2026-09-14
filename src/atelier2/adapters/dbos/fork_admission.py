"""Whether a fork may begin the origin's document -- the question a start answers.

A fork enqueues a successor run of the very document its origin ran, so the
rules that admit a declared start are the rules that admit a fork. The origin
ran under the rules of the build that started it; this build asks its own, and
refuses a document whose form or references it would not start today -- a
second publisher naming no `starts_from`, a grant no runtime redeems, a
reference nothing resolves. The refusal comes before the successor row, its
bindings, and the enqueue, so no claim and no provider ever sees a document
this build would not admit.
"""

from __future__ import annotations

from typing import assert_never

from atelier2.application.evaluate_executability import (
    DocumentNotExecutable,
    ExecutableDocument,
    evaluate_executability,
)
from atelier2.application.refusals import DurableStateCorrupt, ReadUnavailable
from atelier2.contracts.workflows_v3 import WorkflowGraphV3
from atelier2.ports.durable_run_forks import (
    DurableRunForkDocumentNotExecutable,
    DurableRunForkStateCorrupt,
    DurableRunForkWriteUnavailable,
)
from atelier2.ports.published_revisions import PublishedRevisionResolver

FORK_OF_AN_UNADMITTED_DOCUMENT = (
    "a fork begins where a start begins, and this build refuses to start the "
    "document this run was started under: {reason}"
)
"""What an operator is told when the document their origin ran is no longer admitted."""


def unadmitted_fork_document(
    graph: WorkflowGraphV3, revisions: PublishedRevisionResolver
) -> (
    DurableRunForkDocumentNotExecutable
    | DurableRunForkWriteUnavailable
    | DurableRunForkStateCorrupt
    | None
):
    """What refuses a fork of this document, or nothing where a start would admit it."""
    match evaluate_executability(graph, revisions):
        case ExecutableDocument():
            return None
        case DocumentNotExecutable(reason):
            return DurableRunForkDocumentNotExecutable(
                FORK_OF_AN_UNADMITTED_DOCUMENT.format(reason=reason)
            )
        case ReadUnavailable(detail):
            return DurableRunForkWriteUnavailable(detail)
        case DurableStateCorrupt():
            return DurableRunForkStateCorrupt()
        case _ as unreachable:
            assert_never(unreachable)
