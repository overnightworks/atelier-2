from __future__ import annotations

import logging
import traceback
from collections.abc import Sequence
from http import HTTPStatus

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException

from atelier2.api.problem_vocabulary import (
    PROBLEM_DEFINITIONS,
    ROUTE_NOT_FOUND_ACTION,
)
from atelier2.api.references import (
    MAXIMUM_INVALID_FIELD_PATH_CHARACTERS,
    MAXIMUM_INVALID_FIELD_REASON_CHARACTERS,
)
from atelier2.api.wire.resources import (
    InvalidFieldResource,
    ProblemResource,
    UncastRoleResource,
)

PROBLEM_TYPE_PREFIX = "urn:atelier2:problem:v1:"

_LOG = logging.getLogger("atelier2")


def route_not_found_detail(openapi_document_path: str) -> str:
    """The refusal a guessed path earns, naming the document that lists them."""

    return f"{ROUTE_NOT_FOUND_ACTION} at {openapi_document_path}."


def durable_projection_unrepresentable_detail(
    field_name: str,
    bound: int,
    unit: str,
    node_detail_path: str | None = None,
) -> str:
    """Say which stored value this API cannot represent, and where to inspect it."""

    detail = (
        f"Durable projection field {field_name!r} exceeds this API's bound of "
        f"{bound} {unit}."
    )
    if node_detail_path is not None:
        return f"{detail} Open the node detail: GET {node_detail_path}."
    return detail


class ApiProblem(Exception):
    def __init__(
        self,
        code: str,
        detail: str | None = None,
        invalid_fields: tuple[InvalidFieldResource, ...] | None = None,
        uncast_roles: tuple[UncastRoleResource, ...] | None = None,
    ) -> None:
        super().__init__(code)
        self.code = code
        self.detail = detail
        self.invalid_fields = invalid_fields
        self.uncast_roles = uncast_roles


def problem_resource(
    code: str,
    detail: str | None = None,
    invalid_fields: tuple[InvalidFieldResource, ...] | None = None,
    uncast_roles: tuple[UncastRoleResource, ...] | None = None,
) -> ProblemResource:
    definition = PROBLEM_DEFINITIONS[code]
    return ProblemResource(
        type=PROBLEM_TYPE_PREFIX + code,
        title=definition.title,
        status=definition.status,
        detail=definition.detail if detail is None else detail,
        invalid_fields=invalid_fields,
        uncast_roles=uncast_roles,
    )


def problem_response(
    code: str,
    detail: str | None = None,
    invalid_fields: tuple[InvalidFieldResource, ...] | None = None,
    uncast_roles: tuple[UncastRoleResource, ...] | None = None,
) -> JSONResponse:
    resource = problem_resource(code, detail, invalid_fields, uncast_roles)
    return JSONResponse(
        resource.model_dump(mode="json", exclude_none=True),
        status_code=resource.status,
        media_type="application/problem+json",
    )


def _clipped(text: str, bound: int) -> str:
    return text if len(text) <= bound else text[:bound]


def bounded_invalid_field(path: str, reason: str) -> InvalidFieldResource:
    """One `invalid_fields` entry, clipped to the wire's own length bounds.

    A path or reason drawn from content this API does not author -- a
    published document, a value it carries -- can run longer than a request's
    own loc ever does, and `InvalidFieldResource` refuses past its bound
    rather than silently widening it. Every caller that builds one from such
    content clips through here instead of learning the bound itself.
    """
    return InvalidFieldResource(
        path=_clipped(path, MAXIMUM_INVALID_FIELD_PATH_CHARACTERS),
        reason=_clipped(reason, MAXIMUM_INVALID_FIELD_REASON_CHARACTERS),
    )


def _order_name_from_body(body: object, index: object) -> str | None:
    """The order's own declared name, when the raw body still carries it.

    `orders/0` alone tells a caller which position failed, not which order it
    wrote; folding the declared name back into the path lets the refusal
    speak the caller's own vocabulary instead of a position.
    """
    if not isinstance(body, dict) or not isinstance(index, int):
        return None
    orders = body.get("orders")
    if not isinstance(orders, list) or not (0 <= index < len(orders)):
        return None
    order = orders[index]
    name = order.get("name") if isinstance(order, dict) else None
    return name if isinstance(name, str) else None


def _named_validation_path(loc: Sequence[object], body: object) -> str:
    parts = [str(part) for part in loc]
    for position, part in enumerate(loc):
        if part == "orders" and position + 1 < len(loc):
            name = _order_name_from_body(body, loc[position + 1])
            if name is not None:
                parts[position + 1] = name
            break
    return "/".join(parts) or "request"


def invalid_fields_from_validation(
    error: RequestValidationError,
) -> tuple[InvalidFieldResource, ...]:
    """The loc and message the framework already named, as wire pointers."""
    fields: list[InvalidFieldResource] = []
    for item in error.errors():
        loc = item.get("loc", ())
        path = _named_validation_path(loc, error.body)
        reason = str(item.get("msg") or item.get("type") or "invalid")
        fields.append(bounded_invalid_field(path, reason))
    return tuple(fields)


def _run_start_body_rejects_agent_bindings_shape(
    error: RequestValidationError,
) -> bool:
    """True only when the body's own matching request shape rejects
    `agent_bindings` or `workflow_format_version` themselves.

    `AnyStartRunRequestResource` is a bare union of three request shapes, so
    pydantic reports every shape's mismatch: a v3 body with a malformed
    `orders` entry still carries `agent_bindings` complaints from the v1 and
    v2 shapes it was never written for. The shape whose own
    `workflow_format_version` field validated (or that has none, for the
    legacy shape) is the one the caller meant; only its complaints decide
    this refusal.
    """
    fields_by_shape: dict[object, set[str]] = {}
    for item in error.errors():
        loc = item.get("loc", ())
        if len(loc) < 3:
            continue
        fields_by_shape.setdefault(loc[1], set()).add(str(loc[2]))
    return any(
        "workflow_format_version" not in fields and "agent_bindings" in fields
        for fields in fields_by_shape.values()
    )


def install_problem_handlers(
    app: FastAPI, *, versioned_run_start_path: str, openapi_document_path: str
) -> None:
    @app.exception_handler(ApiProblem)
    async def typed_problem(_request: Request, error: ApiProblem) -> JSONResponse:
        return problem_response(
            error.code, error.detail, error.invalid_fields, error.uncast_roles
        )

    @app.exception_handler(RequestValidationError)
    async def invalid_request(
        request: Request, error: RequestValidationError
    ) -> JSONResponse:
        if (
            request.method == "POST"
            and request.url.path == versioned_run_start_path
            and _run_start_body_rejects_agent_bindings_shape(error)
        ):
            return problem_response("invalid-agent-bindings")
        fields = invalid_fields_from_validation(error)
        return problem_response(
            "invalid-request",
            invalid_fields=fields or None,
        )

    @app.exception_handler(HTTPException)
    async def http_error(_request: Request, error: HTTPException) -> JSONResponse:
        if error.status_code == HTTPStatus.NOT_FOUND:
            return problem_response(
                "route-not-found", route_not_found_detail(openapi_document_path)
            )
        if error.status_code == HTTPStatus.METHOD_NOT_ALLOWED:
            return problem_response("method-not-allowed")
        return problem_response("internal-error")

    @app.exception_handler(Exception)
    async def unexpected_error(_request: Request, error: Exception) -> JSONResponse:
        detail = str(error).strip()
        sentence = (
            f"The HTTP request failed with an unhandled {type(error).__name__}: "
            f"{detail}."
            if detail
            else f"The HTTP request failed with an unhandled {type(error).__name__}."
        )
        _LOG.error(
            sentence,
            extra={
                "event": "http_internal_error",
                "exception": "".join(traceback.format_exception(error)),
            },
        )
        return problem_response("internal-error")
