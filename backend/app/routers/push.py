from fastapi import APIRouter

from ..dependencies import SettingsDep
from ..problems import ProblemException
from ..schemas import VapidKeyOut


router = APIRouter(prefix="/push", tags=["Web Push"])


@router.get("/vapid-key", response_model=VapidKeyOut)
def vapid_key(settings: SettingsDep) -> VapidKeyOut:
    if not settings.vapid_public_key:
        raise ProblemException(
            503,
            "Web Push nicht konfiguriert",
            "Für diese Instanz ist kein öffentlicher VAPID-Schlüssel konfiguriert.",
            "web_push_not_configured",
        )
    return VapidKeyOut(public_key=settings.vapid_public_key)
