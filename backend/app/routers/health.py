from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from .. import __version__
from ..healthcheck import check_database, check_redis
from ..schemas import HealthOut


router = APIRouter(tags=["System"])


@router.get("/health/live")
def live() -> dict[str, str]:
    return {"status": "ok", "version": __version__}


@router.get(
    "/health/ready",
    response_model=HealthOut,
    responses={503: {"model": HealthOut, "description": "Abhängigkeit nicht bereit"}},
)
def ready(request: Request):
    database_status = "ok"
    redis_status = "ok"
    try:
        check_database(request.app.state.database)
        admin_database = request.app.state.admin_database
        if admin_database is not request.app.state.database:
            check_database(admin_database)
    except Exception:
        database_status = "error"
    try:
        check_redis(request.app.state.settings)
    except Exception:
        redis_status = "error"
    payload = {
        "status": (
            "ok" if database_status == "ok" and redis_status == "ok" else "degraded"
        ),
        "database": database_status,
        "redis": redis_status,
        "version": __version__,
    }
    if payload["status"] == "degraded":
        return JSONResponse(status_code=503, content=payload)
    return HealthOut.model_validate(payload)
