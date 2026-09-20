"""Authenticated v1 resources and RFC 9457-style problem responses."""
# FastAPI dependency injection intentionally uses Depends(...) in defaults.
# ruff: noqa: B008
import asyncio
import hmac
import json
import sqlite3
from typing import Annotated

from fastapi import APIRouter, Depends, Header, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.security import APIKeyHeader
from starlette.exceptions import HTTPException

from .models import (
    AnnouncementRequest,
    LinkRequest,
    Node,
    NodeState,
    Operation,
    Problem,
    ProblemError,
)

api_key = APIKeyHeader(name="X-API-Key", auto_error=False)
RESULT_CONTRACT_VERSION = "1.0"


def credentials(config):
    entries = config.get("api.credentials", [])
    if entries:
        return entries
    return [
        {
            "name": "legacy",
            "key": config.api_key,
            "authority": ["observe", "control"],
        }
    ]


def authenticate(config, key, authority):
    if key:
        for credential in credentials(config):
            candidate = credential.get("key", "")
            if candidate and hmac.compare_digest(
                key.encode(),
                candidate.encode(),
            ):
                allowed = credential["authority"]
                if authority not in allowed and not (
                    authority == "observe"
                    and "control" in allowed
                ):
                    raise ProblemError(
                        403,
                        "AUTHORITY_DENIED",
                        "Credential lacks the required authority.",
                    )
                return credential["name"]
    raise ProblemError(
        401,
        "AUTHENTICATION_REQUIRED",
        "A valid X-API-Key header is required.",
    )


def problem_response(
    request,
    status,
    code,
    detail,
):
    return JSONResponse(
        status_code=status,
        media_type="application/problem+json",
        content={
            "type": f"urn:asl3:problem:{code.lower()}",
            "title": code.replace("_", " ").title(),
            "status": status,
            "detail": detail,
            "code": code,
            "instance": request.url.path,
        },
    )


def install_api(
    app,
    config,
    node_cache,
    limiter,
):
    async def problem_handler(request, exc):
        return problem_response(
            request,
            exc.status,
            exc.code,
            exc.detail,
        )

    async def validation_handler(
        request,
        exc,
    ):
        if request.url.path.startswith("/v1/"):
            return problem_response(
                request,
                422,
                "INVALID_REQUEST",
                "Request does not match the v1 schema.",
            )
        from fastapi.exception_handlers import (
            request_validation_exception_handler,
        )
        return await request_validation_exception_handler(
            request,
            exc,
        )
    async def http_handler(
        request,
        exc,
    ):
        if request.url.path.startswith("/v1/"):
            code = {
                404: "NOT_FOUND",
                405: "METHOD_NOT_ALLOWED",
            }.get(
                exc.status_code,
                "HTTP_ERROR",
            )
            return problem_response(
                request,
                exc.status_code,
                code,
                str(exc.detail),
            )
        from fastapi.exception_handlers import (
            http_exception_handler,
        )
        return await http_exception_handler(
            request,
            exc,
        )

    async def sqlite_handler(
        request,
        exc,
    ):
        return problem_response(
            request,
            503,
            "LEDGER_UNAVAILABLE",
            "Operation persistence is unavailable.",
        )

    app.add_exception_handler(
        ProblemError,
        problem_handler,
    )
    app.add_exception_handler(
        RequestValidationError,
        validation_handler,
    )
    app.add_exception_handler(
        HTTPException,
        http_handler,
    )
    app.add_exception_handler(
        sqlite3.Error,
        sqlite_handler,
    )

    async def observe(
        key: str | None = Depends(api_key),
    ):
        return authenticate(
            config,
            key,
            "observe",
        )

    async def control(
        key: str | None = Depends(api_key),
    ):
        return authenticate(
            config,
            key,
            "control",
        )

    def platform(request: Request):
        runtime = getattr(
            request.app.state,
            "platform",
            None,
        )
        if runtime is None:
            raise ProblemError(
                503,
                "CONTROL_UNAVAILABLE",
                "The local runtime has not started.",
            )
        return runtime

    problem_responses = {
        code: {
            "content": {
                "application/problem+json": {
                    "schema": Problem.model_json_schema(),
                }
            },
            "description": "ASL problem with stable code",
        }
        for code in (
            401,
            403,
            404,
            409,
            422,
            429,
            503,
        )
    }

    router = APIRouter(
        prefix="/v1",
        tags=["v1"],
        responses=problem_responses,
    )
    key_type = Annotated[
        str | None,
        Header(
            alias="Idempotency-Key",
            min_length=1,
            max_length=128,
            pattern=r"^[!-~]+$",
        ),
    ]

    def accepted(
        runtime,
        kind,
        body,
        credential,
        key,
    ):
        operation = runtime.admit(
            kind,
            body,
            credential,
            key,
        )
        return JSONResponse(
            status_code=202,
            content=operation.model_dump(
                mode="json"
            ),
            headers={
                "Location": (
                    f"/v1/operations/{operation.id}"
                )
            },
        )

    @router.get(
        "/node/state",
        response_model=NodeState,
        dependencies=[Depends(observe)],
    )
    async def state(
        runtime=Depends(platform),
    ):
        return await runtime.observer.snapshot()

    @router.get(
        "/capabilities",
        dependencies=[Depends(observe)],
    )
    async def capabilities(
        runtime=Depends(platform),
    ):
        control_enabled = (
            runtime.healthy
            and runtime.owner is not None
            and not runtime.closing
        )
        operations = [
            "link_node",
            "unlink_node",
            "unlink_all",
            "announce",
        ]
        # Read-only version evidence. Unavailable or malformed evidence stays
        # explicitly undetected; it is never inferred from a contract version.
        software_evidence = await runtime.software_evidence()
        app_rpt = software_evidence["app_rpt"]
        return {
            "node": config.node_number,
            "api_version": "1",
            "result_contract_version": (
                RESULT_CONTRACT_VERSION
            ),
            "backend": "app_rpt-native-ami",
            "backend_contract_version": (
                runtime.transport.contract_version
            ),
            # Mirror of backend_software.app_rpt: the installed app_rpt this
            # API controls. null/false when it could not be read.
            "backend_software_version": app_rpt["version"],
            "backend_software_version_detected": (
                app_rpt["detected"]
            ),
            "backend_software": software_evidence,
            "observation": {
                "source": "RptStatus/XStat+SawStat",
                "fresh_per_request": True,
                "unknown_is_fail_closed_for_correctness": True,
            },
            "features": {
                "fresh_state": True,
                "directory": True,
                "operations": True,
                "sse_snapshots": config.events_enabled,
                "control_enabled": control_enabled,
                "supported_operations": operations,
                "announcements": [
                    "identify",
                    "time",
                    "status",
                    "version",
                ],
                "dtmf": False,
                "macros": False,
                "idempotency": True,
                "single_control_owner": True,
                "automatic_control_replay": False,
            },
            "idempotency": {
                "header": "Idempotency-Key",
                "scope": (
                    "configured node and credential name"
                ),
                "retention": "indefinite in v1",
            },
            "traffic_policy": (
                runtime.policy.characteristics()
            ),
            "events": {
                "delivery": (
                    "periodic fresh snapshots"
                ),
                "authentication": (
                    "X-API-Key header"
                ),
                "replay": False,
            },
        }
    @router.get(
        "/directory/{node}",
        dependencies=[Depends(observe)],
    )
    async def directory(node: Node):
        return {
            **node_cache.lookup(node),
            "source": "public-directory-cache",
            "last_updated": (
                node_cache.last_updated
            ),
            "authoritative_for_target_validity": False,
        }

    @router.get(
        "/operations",
        response_model=list[Operation],
        dependencies=[Depends(observe)],
    )
    async def operations(
        limit: int = Query(
            100,
            ge=1,
            le=500,
        ),
        offset: int = Query(
            0,
            ge=0,
        ),
        runtime=Depends(platform),
    ):
        return runtime.require_ledger().list(
            limit,
            offset,
        )

    @router.get(
        "/operations/{operation_id}",
        response_model=Operation,
        dependencies=[Depends(observe)],
    )
    async def operation(
        operation_id: str,
        runtime=Depends(platform),
    ):
        result = runtime.require_ledger().get(
            operation_id
        )
        if result is None:
            raise ProblemError(
                404,
                "OPERATION_NOT_FOUND",
                "No operation has this identity.",
            )
        return result

    @router.post(
        "/links",
        status_code=202,
        response_model=Operation,
    )
    @limiter.limit(
        lambda: f"{config.rate_limit}/minute"
    )
    async def link(
        request: Request,
        body: LinkRequest,
        idempotency_key: key_type = None,
        credential=Depends(control),
        runtime=Depends(platform),
    ):
        return accepted(
            runtime,
            "link_node",
            body.model_dump(),
            credential,
            idempotency_key,
        )

    @router.delete(
        "/links/{node}",
        status_code=202,
        response_model=Operation,
    )
    @limiter.limit(
        lambda: f"{config.rate_limit}/minute"
    )
    async def unlink(
        request: Request,
        node: Node,
        idempotency_key: key_type = None,
        credential=Depends(control),
        runtime=Depends(platform),
    ):
        return accepted(
            runtime,
            "unlink_node",
            {"node": node},
            credential,
            idempotency_key,
        )

    @router.delete(
        "/links",
        status_code=202,
        response_model=Operation,
    )
    @limiter.limit(
        lambda: f"{config.rate_limit}/minute"
    )
    async def unlink_all(
        request: Request,
        idempotency_key: key_type = None,
        credential=Depends(control),
        runtime=Depends(platform),
    ):
        return accepted(
            runtime,
            "unlink_all",
            {},
            credential,
            idempotency_key,
        )
    @router.post(
        "/announcements",
        status_code=202,
        response_model=Operation,
    )
    @limiter.limit(
        lambda: f"{config.rate_limit}/minute"
    )
    async def announce(
        request: Request,
        body: AnnouncementRequest,
        idempotency_key: key_type = None,
        credential=Depends(control),
        runtime=Depends(platform),
    ):
        return accepted(
            runtime,
            "announce",
            body.model_dump(),
            credential,
            idempotency_key,
        )

    @router.get(
        "/events",
        dependencies=[Depends(observe)],
        response_class=StreamingResponse,
        responses={
            200: {
                "content": {
                    "text/event-stream": {}
                },
                "description": (
                    "Fresh node.state snapshots"
                ),
            }
        },
    )
    async def events(
        request: Request,
        runtime=Depends(platform),
    ):
        if not config.events_enabled:
            raise ProblemError(
                503,
                "EVENTS_DISABLED",
                "Snapshot events are disabled in configuration.",
            )
        async def stream():
            while not await request.is_disconnected():
                snapshot = (
                    await runtime.observer.snapshot()
                )
                yield (
                    "event: node.state\n"
                    f"data: {json.dumps(snapshot.model_dump(mode='json'))}\n\n"
                )
                await asyncio.sleep(
                    max(
                        0.1,
                        config.events_snapshot_interval,
                    )
                )

        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
            },
        )

    app.include_router(router)
