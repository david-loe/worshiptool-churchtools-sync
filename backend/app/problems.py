from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException
from sqlalchemy.exc import IntegrityError


PROBLEM_MEDIA_TYPE = "application/problem+json"


@dataclass
class ProblemException(Exception):
    status: int
    title: str
    detail: str
    code: str
    errors: list[dict[str, Any]] | None = None
    headers: dict[str, str] | None = None
    type_uri: str = "about:blank"


def problem_response(
    request: Request,
    *,
    status: int,
    title: str,
    detail: str,
    code: str,
    errors: list[dict[str, Any]] | None = None,
    headers: dict[str, str] | None = None,
    type_uri: str = "about:blank",
) -> JSONResponse:
    trace_id = getattr(request.state, "trace_id", str(uuid.uuid4()))
    body: dict[str, Any] = {
        "type": type_uri,
        "title": title,
        "status": status,
        "detail": detail,
        "instance": request.url.path,
        "code": code,
        "trace_id": trace_id,
    }
    if errors:
        body["errors"] = errors
    return JSONResponse(
        status_code=status,
        content=body,
        media_type=PROBLEM_MEDIA_TYPE,
        headers=headers,
    )


def install_problem_handlers(app: FastAPI) -> None:
    @app.exception_handler(ProblemException)
    async def handle_problem(request: Request, exc: ProblemException) -> JSONResponse:
        return problem_response(
            request,
            status=exc.status,
            title=exc.title,
            detail=exc.detail,
            code=exc.code,
            errors=exc.errors,
            headers=exc.headers,
            type_uri=exc.type_uri,
        )

    @app.exception_handler(RequestValidationError)
    async def handle_validation(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        errors = []
        for item in exc.errors():
            errors.append(
                {
                    "field": ".".join(str(part) for part in item.get("loc", [])[1:]),
                    "message": item.get("msg", "Ungültiger Wert"),
                    "code": item.get("type", "validation_error"),
                }
            )
        return problem_response(
            request,
            status=422,
            title="Validierungsfehler",
            detail="Die Anfrage enthält ungültige Felder.",
            code="validation_error",
            errors=errors,
        )

    @app.exception_handler(StarletteHTTPException)
    async def handle_http_error(
        request: Request, exc: StarletteHTTPException
    ) -> JSONResponse:
        titles = {404: "Nicht gefunden", 405: "Methode nicht erlaubt"}
        return problem_response(
            request,
            status=exc.status_code,
            title=titles.get(exc.status_code, "HTTP-Fehler"),
            detail=str(exc.detail),
            code=f"http_{exc.status_code}",
            headers=exc.headers,
        )

    @app.exception_handler(IntegrityError)
    async def handle_integrity(request: Request, exc: IntegrityError) -> JSONResponse:
        # Never expose constraint details or values from the database.
        return problem_response(
            request,
            status=409,
            title="Konflikt",
            detail="Die Änderung kollidiert mit bereits vorhandenen Daten.",
            code="integrity_conflict",
        )
