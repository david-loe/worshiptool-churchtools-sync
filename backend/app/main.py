from __future__ import annotations

import logging
import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.routing import APIRoute

from . import __version__
from .config import Settings, get_settings
from .database import Database
from .dependencies import current_auth_session, require_csrf
from .problems import install_problem_handlers, problem_response
from .routers import (
    auth,
    admin,
    connections,
    health,
    notifications,
    profiles,
    push,
    runs,
    workspaces,
)
from .services import ConnectionProbeClient, ConnectionTester, RunDispatcher
from .schemas import ProblemDetails


_HTTP_METHODS = {"get", "post", "put", "patch", "delete", "options", "head"}
logger = logging.getLogger(__name__)


def _dependency_calls(dependant) -> set[object]:
    calls: set[object] = set()
    pending = [dependant]
    while pending:
        current = pending.pop()
        call = getattr(current, "call", None)
        if call is not None:
            calls.add(call)
        pending.extend(getattr(current, "dependencies", ()))
    return calls


def _iter_api_routes(routes, prefix: str = ""):
    """Yield effective paths across FastAPI's lazy included-router wrappers."""

    for route in routes:
        if isinstance(route, APIRoute):
            yield route, f"{prefix}{route.path}"
            continue
        original_router = getattr(route, "original_router", None)
        include_context = getattr(route, "include_context", None)
        if original_router is None or include_context is None:
            continue
        included_prefix = str(getattr(include_context, "prefix", ""))
        yield from _iter_api_routes(
            getattr(original_router, "routes", ()),
            f"{prefix}{included_prefix}",
        )


def _install_openapi_contract(app: FastAPI, settings: Settings) -> None:
    """Align generated documentation with the runtime problem contract."""

    original_openapi = app.openapi

    def documented_openapi():
        if app.openapi_schema is not None:
            return app.openapi_schema
        schema = original_openapi()
        component_root = schema.setdefault("components", {})
        components = component_root.setdefault("schemas", {})
        components["ProblemDetails"] = ProblemDetails.model_json_schema()
        component_root.setdefault("securitySchemes", {}).update(
            {
                "SessionCookie": {
                    "type": "apiKey",
                    "in": "cookie",
                    "name": settings.session_cookie_name,
                    "description": "Sichere HttpOnly-Sitzung",
                },
                "CsrfHeader": {
                    "type": "apiKey",
                    "in": "header",
                    "name": settings.csrf_header_name,
                    "description": "Sitzungsgebundenes Double-Submit-CSRF-Token",
                },
            }
        )
        route_dependencies: dict[tuple[str, str], set[object]] = {}
        for route, effective_path in _iter_api_routes(app.routes):
            calls = _dependency_calls(route.dependant)
            for route_method in route.methods or ():
                route_dependencies[(effective_path, route_method.casefold())] = calls
        problem_content = {
            "application/problem+json": {
                "schema": {"$ref": "#/components/schemas/ProblemDetails"}
            }
        }
        for path_item_path, path_item in schema.get("paths", {}).items():
            if not isinstance(path_item, dict):
                continue
            for method, operation in path_item.items():
                if method not in _HTTP_METHODS or not isinstance(operation, dict):
                    continue
                calls = route_dependencies.get((path_item_path, method), set())
                if current_auth_session in calls:
                    requirement = {"SessionCookie": []}
                    if require_csrf in calls:
                        requirement["CsrfHeader"] = []
                    operation["security"] = [requirement]
                else:
                    operation.pop("security", None)
                responses = operation.setdefault("responses", {})
                responses["default"] = {
                    "description": "Fehler im Problem-Details-Format",
                    "content": problem_content,
                }
                if "422" in responses:
                    responses["422"] = {
                        "description": "Validierungsfehler",
                        "content": problem_content,
                    }
        app.openapi_schema = schema
        return schema

    app.openapi = documented_openapi


def create_app(
    settings: Settings | None = None,
    *,
    database: Database | None = None,
    admin_database: Database | None = None,
    run_dispatcher: RunDispatcher | None = None,
    connection_tester: ConnectionTester | None = None,
    connection_probe_client: ConnectionProbeClient | None = None,
) -> FastAPI:
    runtime_settings = settings or get_settings()
    runtime_database = database or Database(runtime_settings)
    if admin_database is not None:
        runtime_admin_database = admin_database
    elif runtime_settings.database_admin_url is not None:
        runtime_admin_database = Database(
            runtime_settings, database_url=runtime_settings.database_admin_url
        )
    elif runtime_settings.environment == "production":
        raise RuntimeError(
            "WT_SYNC_DATABASE_ADMIN_URL is required by the production API"
        )
    else:
        # SQLite development/tests have no PostgreSQL role boundary.
        runtime_admin_database = runtime_database
    if run_dispatcher is None:
        from .runtime import ApiRunDispatcher

        run_dispatcher = ApiRunDispatcher(runtime_settings)
    if runtime_settings.environment == "production":
        # Production API containers intentionally have no provider egress and
        # must never instantiate or retain the credential-consuming tester.
        from .probes import RedisProviderProbeClient

        connection_tester = None
        connection_probe_client = connection_probe_client or RedisProviderProbeClient(
            runtime_settings
        )
    elif connection_tester is None and connection_probe_client is None:
        # Direct provider calls remain convenient for local development and
        # deterministic unit tests only.
        from .runtime import ProviderConnectionTester

        connection_tester = ProviderConnectionTester()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if runtime_settings.auto_create_schema:
            runtime_database.create_schema()
        yield
        if connection_probe_client is not None:
            connection_probe_client.close()
        runtime_database.dispose()
        if runtime_admin_database is not runtime_database:
            runtime_admin_database.dispose()

    app = FastAPI(
        title="WorshipTools ChurchTools Sync API",
        summary="Mandantenfähige Verwaltung und Ausführung von Sync-Profilen",
        version=__version__,
        lifespan=lifespan,
        openapi_url=f"{runtime_settings.api_prefix}/openapi.json",
        docs_url=(
            f"{runtime_settings.api_prefix}/docs"
            if runtime_settings.environment != "production"
            else None
        ),
        redoc_url=None,
    )
    app.state.settings = runtime_settings
    app.state.database = runtime_database
    app.state.admin_database = runtime_admin_database
    app.state.run_dispatcher = run_dispatcher
    app.state.connection_tester = connection_tester
    app.state.connection_probe_client = connection_probe_client

    if runtime_settings.cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=runtime_settings.cors_origins,
            allow_credentials=True,
            allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
            allow_headers=[
                "Accept",
                "Content-Type",
                runtime_settings.csrf_header_name,
                "X-Request-ID",
            ],
            expose_headers=["Location", "Retry-After", "X-Request-ID"],
        )

    @app.middleware("http")
    async def trace_and_security_headers(request: Request, call_next):
        incoming = request.headers.get("X-Request-ID", "")
        request.state.trace_id = (
            incoming[:100]
            if incoming and all(char.isalnum() or char in "-_." for char in incoming)
            else str(uuid.uuid4())
        )
        try:
            response = await call_next(request)
        except Exception as exc:
            # Keep the runtime contract identical to documented API failures
            # without reflecting exception strings, database details, or
            # provider responses to the caller.
            logger.error(
                "unhandled request failure",
                extra={
                    "trace_id": request.state.trace_id,
                    "method": request.method,
                    "request_path": request.url.path,
                    "error_type": type(exc).__name__,
                },
            )
            response = problem_response(
                request,
                status=500,
                title="Interner Fehler",
                detail="Die Anfrage konnte nicht verarbeitet werden.",
                code="internal_error",
            )
        response.headers["X-Request-ID"] = request.state.trace_id
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("Referrer-Policy", "same-origin")
        response.headers.setdefault(
            "Permissions-Policy", "camera=(), microphone=(), geolocation=()"
        )
        if request.url.path.startswith(runtime_settings.api_prefix):
            response.headers.setdefault("Cache-Control", "no-store")
        return response

    install_problem_handlers(app)
    app.include_router(health.router)
    for api_router in (
        auth.router,
        admin.router,
        workspaces.router,
        connections.router,
        profiles.router,
        runs.router,
        notifications.router,
        push.router,
    ):
        app.include_router(api_router, prefix=runtime_settings.api_prefix)
    _install_openapi_contract(app, runtime_settings)
    return app


app = create_app()
