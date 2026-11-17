"""FastAPI application factory for the LifePilot private backend API."""

from __future__ import annotations

import logging
import time
import uuid
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator

from fastapi import FastAPI, HTTPException, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from lifepilot.api.auth import AuthPrincipal
from lifepilot.api.rate_limit import answer_concurrency_gate, enforce_rate_limit
from lifepilot.api.schemas import (
    AnswerRequest,
    AnswerResponse,
    HealthResponse,
    ReadinessResponse,
)
from lifepilot.api.settings import (
    assert_production_secrets,
    get_api_settings,
    reset_api_settings_cache,
)
from lifepilot.agent.config import get_agent_config, reset_agent_config_cache
from lifepilot.agent.contracts import AgentRequest, AgentStatus
from lifepilot.agent.runner import run_agent
from lifepilot.ops.health import readiness_payload
from lifepilot.ops.secrets_redact import redact_secrets

logger = logging.getLogger("lifepilot.api")

# Numeric HTTP codes avoid Starlette alias deprecation churn (behavior unchanged).
_HTTP_413 = 413
_HTTP_422 = 422


class RequestContextMiddleware:
    """Pure ASGI middleware for size limits + request IDs (no body buffering)."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        cfg = get_api_settings()
        headers = {
            k.decode("latin-1").lower(): v.decode("latin-1")
            for k, v in scope.get("headers") or []
        }
        method = (scope.get("method") or "GET").upper()
        content_length = headers.get("content-length")
        max_bytes = cfg.max_request_bytes
        if content_length:
            try:
                if int(content_length) > max_bytes:
                    response = JSONResponse(
                        status_code=_HTTP_413,
                        content={"detail": "Request body too large"},
                    )
                    await response(scope, receive, send)
                    return
            except ValueError:
                pass
        elif method in {"POST", "PUT", "PATCH"}:
            # Fail closed for chunked/missing Content-Length on mutating routes:
            # require an explicit length so body limits cannot be bypassed.
            response = JSONResponse(
                status_code=status.HTTP_411_LENGTH_REQUIRED,
                content={"detail": "Content-Length required"},
            )
            await response(scope, receive, send)
            return

        request_id = headers.get("x-request-id") or f"http_{uuid.uuid4().hex}"
        started = time.perf_counter()
        scope.setdefault("state", {})
        scope["state"]["request_id"] = request_id

        status_code_box = {"code": 500}
        received = {"n": 0}
        oversized = {"hit": False}
        response_started = {"hit": False}

        async def receive_wrapper() -> dict:
            if oversized["hit"]:
                return {"type": "http.disconnect"}
            message = await receive()
            if message.get("type") == "http.request":
                body = message.get("body") or b""
                received["n"] += len(body)
                if received["n"] > max_bytes:
                    oversized["hit"] = True
                    while message.get("more_body"):
                        message = await receive()
                    return {"type": "http.disconnect"}
            return message

        async def send_wrapper(message: dict) -> None:
            if message["type"] == "http.response.start":
                response_started["hit"] = True
                if oversized["hit"]:
                    message = {
                        **message,
                        "status": _HTTP_413,
                        "headers": [
                            (b"content-type", b"application/json"),
                            (b"x-request-id", request_id.encode("latin-1")),
                        ],
                    }
                    status_code_box["code"] = _HTTP_413
                else:
                    status_code_box["code"] = int(message.get("status") or 500)
                    headers_list = list(message.get("headers") or [])
                    headers_list.append(
                        (b"x-request-id", request_id.encode("latin-1"))
                    )
                    message = {**message, "headers": headers_list}
            elif message["type"] == "http.response.body" and oversized["hit"]:
                message = {
                    "type": "http.response.body",
                    "body": b'{"detail":"Request body too large"}',
                    "more_body": False,
                }
            await send(message)

        try:
            await self.app(scope, receive_wrapper, send_wrapper)
            if oversized["hit"] and not response_started["hit"]:
                response = JSONResponse(
                    status_code=_HTTP_413,
                    content={
                        "detail": "Request body too large",
                        "request_id": request_id,
                    },
                )
                await response(scope, receive, send)
                return
        except Exception:  # noqa: BLE001
            if oversized["hit"] and not response_started["hit"]:
                response = JSONResponse(
                    status_code=_HTTP_413,
                    content={
                        "detail": "Request body too large",
                        "request_id": request_id,
                    },
                )
                await response(scope, receive, send)
                return
            logger.exception(
                "api.unhandled request_id=%s path=%s",
                request_id,
                scope.get("path"),
            )
            response = JSONResponse(
                status_code=500,
                content={"detail": "Internal server error", "request_id": request_id},
            )
            await response(scope, receive, send)
            return

        elapsed_ms = round((time.perf_counter() - started) * 1000, 2)
        logger.info(
            "api.request request_id=%s method=%s path=%s status=%s latency_ms=%s",
            request_id,
            scope.get("method"),
            scope.get("path"),
            status_code_box["code"],
            elapsed_ms,
        )


def _safe_errors(errors: list[str] | None) -> list[str]:
    return [redact_secrets(e) for e in (errors or [])]


def _to_answer_response(resp, *, include_evidence: bool) -> AnswerResponse:
    evidence_payload = None
    if include_evidence and resp.evidence:
        evidence_payload = [
            {
                "evidence_id": e.evidence_id,
                "source_type": e.source_type.value,
                "verification_state": e.verification_state.value,
                "source_url": e.source_url,
                "jurisdiction": e.jurisdiction,
            }
            for e in resp.evidence
        ]
    return AnswerResponse(
        request_id=resp.request_id,
        status=resp.status.value,
        answer=resp.answer,
        claims=[c.model_dump(mode="json") for c in (resp.claims or [])],
        missing_information=list(resp.missing_information or []),
        warnings=[redact_secrets(w) for w in (resp.warnings or [])],
        errors=_safe_errors(resp.errors),
        retrieval_decision=resp.retrieval_decision,
        verification=resp.verification,
        timing=resp.timing.model_dump(mode="json") if resp.timing else {},
        usage=resp.usage.model_dump(mode="json") if resp.usage else None,
        jurisdiction=resp.jurisdiction,
        pathway_id=resp.pathway_id,
        evidence_count=len(resp.evidence or []),
        evidence=evidence_payload,
    )


def create_app() -> FastAPI:
    reset_api_settings_cache()
    reset_agent_config_cache()
    settings = get_api_settings()

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        assert_production_secrets(settings)
        logging.basicConfig(
            level=logging.INFO,
            format="%(asctime)s %(levelname)s %(name)s %(message)s",
        )
        logger.info(
            "api.startup env=%s require_auth=%s rate_limit=%s concurrent=%s",
            settings.environment,
            settings.require_auth,
            settings.rate_limit_per_minute,
            settings.max_concurrent_answers,
        )
        yield
        logger.info("api.shutdown")

    app = FastAPI(
        title="LifePilot API",
        version="9d5",
        description=(
            "Private/internal immigration information backend. "
            "Not legal advice. Uses production agent graph with external_mode=auto "
            "and unified legal verification."
        ),
        lifespan=lifespan,
    )
    app.add_middleware(RequestContextMiddleware)

    @app.exception_handler(RequestValidationError)
    async def validation_handler(request: Request, exc: RequestValidationError):
        request_id = getattr(request.state, "request_id", None)
        safe_errors: list[dict[str, Any]] = []
        for err in exc.errors():
            item = {k: v for k, v in err.items() if k != "ctx"}
            ctx = err.get("ctx")
            if isinstance(ctx, dict):
                item["ctx"] = {
                    ck: (str(cv) if isinstance(cv, BaseException) else cv)
                    for ck, cv in ctx.items()
                }
            safe_errors.append(item)
        return JSONResponse(
            status_code=_HTTP_422,
            content={
                "detail": "Invalid request",
                "request_id": request_id,
                "errors": safe_errors,
            },
        )

    @app.get("/health", response_model=HealthResponse, tags=["ops"])
    def health() -> HealthResponse:
        return HealthResponse()

    @app.get("/ready", response_model=ReadinessResponse, tags=["ops"])
    def ready() -> ReadinessResponse:
        # Orchestrator-facing probe: boolean readiness only (no secret-bearing paths).
        details = readiness_payload()
        public = {
            "ready": bool(details.get("ready")),
            "reasons": list(details.get("reasons") or []),
            "database_ok": bool((details.get("database") or {}).get("ok")),
            "llm_configured": bool((details.get("llm") or {}).get("configured")),
            "checked_at": details.get("checked_at"),
        }
        if not public["ready"]:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=public,
            )
        return ReadinessResponse(ready=True, details=public)

    @app.get("/v1/ops/status", tags=["ops"])
    def ops_status(principal: AuthPrincipal) -> dict[str, Any]:
        del principal
        return readiness_payload()

    @app.post("/v1/answer", response_model=AnswerResponse, tags=["agent"])
    def answer(
        request: Request,
        principal: AuthPrincipal,
        payload: AnswerRequest,
    ) -> AnswerResponse:
        cfg = get_api_settings()
        enforce_rate_limit(request, principal)
        agent_cfg = get_agent_config()
        if not agent_cfg.verification_enabled:
            # Production API refuses to serve with verification disabled.
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Unified legal verification is disabled",
            )

        http_request_id = getattr(request.state, "request_id", None)
        logger.info(
            "api.answer_start request_id=%s jur=%s pathway=%s qlen=%s",
            http_request_id,
            payload.jurisdiction,
            payload.pathway_id,
            len(payload.question),
        )

        agent_request = AgentRequest(
            question=payload.question,
            jurisdiction=payload.jurisdiction,
            pathway_id=payload.pathway_id,
            conversation_id=payload.conversation_id,
            request_id=http_request_id,
            retrieval={
                "mode": "hybrid",
                "top_k": payload.top_k,
                "external_mode": "auto",
            },
        )

        with answer_concurrency_gate().acquire(timeout=0.05):
            resp = run_agent(agent_request, config=agent_cfg)

        # Fail closed if verification unexpectedly missing on a generated answer.
        if (
            resp.status == AgentStatus.SUCCESS
            and resp.answer
            and agent_cfg.verification_enabled
            and resp.verification is None
        ):
            logger.error(
                "api.verification_missing request_id=%s", resp.request_id
            )
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Verification did not run; refusing unchecked answer",
            )

        out = _to_answer_response(
            resp, include_evidence=cfg.include_evidence_in_response
        )
        logger.info(
            "api.answer_done request_id=%s status=%s decision=%s "
            "evidence=%s in_tokens=%s out_tokens=%s cost=%s ver_ms=%s",
            out.request_id,
            out.status,
            out.retrieval_decision,
            out.evidence_count,
            (out.usage or {}).get("input_tokens"),
            (out.usage or {}).get("output_tokens"),
            (out.usage or {}).get("estimated_cost_usd"),
            (out.timing or {}).get("verification_ms"),
        )
        return out

    return app


# ASGI entry for uvicorn: lifepilot.api.app:app
app = create_app()
