from __future__ import annotations

import uuid
from datetime import datetime, timezone
from urllib.parse import urlsplit

from fastapi import APIRouter, Query, Request, status
from sqlalchemy import func, or_, select

from ..dependencies import CsrfDep, DbDep, SettingsDep, WorkspaceAccessDep, WorkspaceAdminDep
from ..models import ProviderConnection, ProviderType, RemoteBinding, SyncProfile, Workspace
from ..problems import ProblemException
from ..probes import ProviderProbeError
from ..schemas import (
    ConnectionCreate,
    ConnectionList,
    ConnectionOut,
    ConnectionTestResult,
    ConnectionUpdate,
    ProviderConnectionSettings,
    ProviderMetadata,
)
from ..security import SecretCipher, normalize_email


router = APIRouter(
    prefix="/workspaces/{workspace_id}/connections", tags=["Verbindungen"]
)


def _validated_base_url(provider: ProviderType, raw: str | None) -> str | None:
    if raw is None:
        return None
    if provider == ProviderType.WORSHIPTOOLS:
        raise ProblemException(
            422,
            "WorshipTools-Adresse nicht konfigurierbar",
            "WorshipTools verwendet feste, vom Adapter verwaltete Endpunkte.",
            "worshiptools_base_url_not_configurable",
        )
    value = raw.strip().rstrip("/")
    parsed = urlsplit(value)
    try:
        port = parsed.port
    except ValueError as exc:
        raise ProblemException(
            422,
            "Ungültige URL",
            "Die Provider-URL enthält einen ungültigen Port.",
            "invalid_provider_url",
        ) from exc
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise ProblemException(
            422,
            "Ungültige URL",
            "Provider-URLs müssen vollständige HTTPS-URLs ohne Zugangsdaten sein.",
            "invalid_provider_url",
        )
    hostname = parsed.hostname.casefold().rstrip(".")
    if (
        not hostname.endswith(".church.tools")
        or port not in (None, 443)
        or parsed.path not in ("", "/")
        or parsed.query
        or parsed.fragment
    ):
        raise ProblemException(
            422,
            "ChurchTools-Domain nicht erlaubt",
            "Erlaubt ist nur der HTTPS-Ursprung einer *.church.tools-Subdomain ohne Pfad oder Sonderport.",
            "churchtools_host_not_allowed",
        )
    return f"https://{hostname}"


def _hint(provider: ProviderType, credentials: dict[str, str]) -> str | None:
    if not credentials:
        return None
    if provider == ProviderType.WORSHIPTOOLS and credentials.get("email"):
        return credentials["email"][:120]
    if provider == ProviderType.CHURCHTOOLS:
        return "Login-Token hinterlegt"
    return "Zugangsdaten hinterlegt"


def _immutable_connection_identity() -> ProblemException:
    return ProblemException(
        409,
        "Verbindungsidentität ist unveränderlich",
        "Lege für eine andere Provider-Instanz oder Account-Identität eine neue Verbindung an.",
        "connection_identity_immutable",
    )


def _invalid_credentials(
    detail: str, code: str = "invalid_connection_credentials"
) -> ProblemException:
    return ProblemException(422, "Ungültige Zugangsdaten", detail, code)


def _canonical_settings(
    provider: ProviderType, settings: ProviderConnectionSettings
) -> dict[str, str]:
    values = settings.model_dump(exclude_none=True)
    if provider == ProviderType.CHURCHTOOLS and values:
        raise ProblemException(
            422,
            "Ungültige Provider-Einstellungen",
            "ChurchTools-Verbindungen besitzen keine zusätzlichen Provider-Einstellungen.",
            "churchtools_settings_not_supported",
        )
    return {"timezone": str(values["timezone"])} if "timezone" in values else {}


def _bounded_credential(value: str, *, label: str, max_bytes: int) -> str:
    if "\x00" in value or len(value.encode("utf-8")) > max_bytes:
        raise _invalid_credentials(
            f"{label} überschreitet die erlaubte Länge oder enthält ungültige Zeichen."
        )
    return value


def _merged_credentials(
    provider: ProviderType,
    existing: dict[str, object],
    patch: dict[str, str],
    *,
    identity_locked: bool,
) -> dict[str, str]:
    """Apply a write-only credential patch without dropping undisclosed fields."""

    if not patch:
        raise _invalid_credentials(
            "Das Zugangsdaten-Update enthält keine Werte.",
            "empty_credentials_update",
        )
    if provider == ProviderType.WORSHIPTOOLS:
        expected = {"email", "account_id", "password"}
        if not set(patch).issubset(expected):
            raise _invalid_credentials(
                "WorshipTools akzeptiert nur E-Mail-Adresse, Passwort und Account-ID."
            )
        merged = {key: str(existing.get(key) or "") for key in expected}
        merged.update(patch)
        if any(not merged[key].strip() for key in expected):
            raise _invalid_credentials(
                "WorshipTools benötigt E-Mail-Adresse, Passwort und Account-ID."
            )
        if identity_locked:
            if normalize_email(merged["email"]) != normalize_email(
                str(existing.get("email") or "")
            ):
                raise _immutable_connection_identity()
            if merged["account_id"].strip() != str(
                existing.get("account_id") or ""
            ).strip():
                raise _immutable_connection_identity()
        return {
            "email": _bounded_credential(
                merged["email"].strip(), label="E-Mail-Adresse", max_bytes=320
            ),
            # Password whitespace is significant and must reach WorshipTools
            # byte-for-byte unchanged.
            "password": _bounded_credential(
                merged["password"], label="Passwort", max_bytes=1024
            ),
            "account_id": _bounded_credential(
                merged["account_id"].strip(), label="Account-ID", max_bytes=200
            ),
        }
    if provider == ProviderType.CHURCHTOOLS:
        expected = {"token", "login_token"}
        if not set(patch).issubset(expected):
            raise _invalid_credentials("ChurchTools akzeptiert nur ein Login-Token.")
        if (
            patch.get("token")
            and patch.get("login_token")
            and patch["token"].strip() != patch["login_token"].strip()
        ):
            raise _invalid_credentials(
                "Die beiden Login-Token-Werte widersprechen sich."
            )
        token = str(
            patch.get("token")
            or patch.get("login_token")
            or existing.get("token")
            or existing.get("login_token")
            or ""
        ).strip()
        if not token:
            raise _invalid_credentials("ChurchTools benötigt ein Login-Token.")
        return {
            "token": _bounded_credential(
                token, label="ChurchTools-Login-Token", max_bytes=4096
            )
        }
    raise _invalid_credentials("Der Provider wird nicht unterstützt.")


def _updated_credentials(
    connection: ProviderConnection,
    settings: SettingsDep,
    patch: dict[str, str],
    *,
    identity_locked: bool,
) -> dict[str, str]:
    existing: dict[str, object] = {}
    if connection.credentials_encrypted:
        existing = SecretCipher(settings).decrypt_json(
            connection.credentials_encrypted, context=f"connection:{connection.id}"
        )
    return _merged_credentials(
        connection.provider,
        existing,
        patch,
        identity_locked=identity_locked,
    )


def _connection(
    db: DbDep, workspace_id: uuid.UUID, connection_id: uuid.UUID
) -> ProviderConnection:
    connection = db.scalar(
        select(ProviderConnection).where(
            ProviderConnection.id == connection_id,
            ProviderConnection.workspace_id == workspace_id,
        )
    )
    if connection is None:
        raise ProblemException(404, "Nicht gefunden", "Verbindung nicht gefunden.", "not_found")
    return connection


def _annotate_delete_blockers(
    db: DbDep,
    workspace_id: uuid.UUID,
    connections: list[ProviderConnection],
) -> None:
    """Attach batched, read-only DELETE guard information for API serialization."""

    connection_ids = {connection.id for connection in connections}
    blockers: dict[uuid.UUID, set[str]] = {connection_id: set() for connection_id in connection_ids}
    if connection_ids:
        for source_id, target_id in db.execute(
            select(SyncProfile.source_connection_id, SyncProfile.target_connection_id).where(
                SyncProfile.workspace_id == workspace_id,
                or_(
                    SyncProfile.source_connection_id.in_(connection_ids),
                    SyncProfile.target_connection_id.in_(connection_ids),
                ),
            )
        ):
            if source_id in blockers:
                blockers[source_id].add("profile_reference")
            if target_id in blockers:
                blockers[target_id].add("profile_reference")
        for connection_id in db.scalars(
            select(RemoteBinding.target_connection_id)
            .where(
                RemoteBinding.workspace_id == workspace_id,
                RemoteBinding.target_connection_id.in_(connection_ids),
            )
            .distinct()
        ):
            blockers[connection_id].add("remote_binding")
    order = {"profile_reference": 0, "remote_binding": 1}
    for connection in connections:
        connection.delete_blockers = sorted(blockers[connection.id], key=order.__getitem__)


@router.get("", response_model=ConnectionList)
def list_connections(
    access: WorkspaceAccessDep,
    db: DbDep,
    provider: ProviderType | None = None,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> ConnectionList:
    predicates = [ProviderConnection.workspace_id == access.workspace.id]
    if provider is not None:
        predicates.append(ProviderConnection.provider == provider)
    total = db.scalar(
        select(func.count()).select_from(ProviderConnection).where(*predicates)
    ) or 0
    items = list(db.scalars(
        select(ProviderConnection)
        .where(*predicates)
        .order_by(ProviderConnection.name, ProviderConnection.id)
        .limit(limit)
        .offset(offset)
    ).all())
    _annotate_delete_blockers(db, access.workspace.id, items)
    return ConnectionList(items=items, total=total, limit=limit, offset=offset)


@router.post("", response_model=ConnectionOut, status_code=status.HTTP_201_CREATED)
def create_connection(
    payload: ConnectionCreate,
    access: WorkspaceAdminDep,
    settings: SettingsDep,
    db: DbDep,
    csrf: CsrfDep,
) -> ProviderConnection:
    workspace = db.scalar(
        select(Workspace)
        .where(Workspace.id == access.workspace.id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if workspace is None:
        raise ProblemException(
            404, "Nicht gefunden", "Workspace nicht gefunden.", "not_found"
        )
    connection_count = db.scalar(
        select(func.count())
        .select_from(ProviderConnection)
        .where(ProviderConnection.workspace_id == workspace.id)
    ) or 0
    if connection_count >= settings.max_connections_per_workspace:
        raise ProblemException(
            409,
            "Verbindungs-Limit erreicht",
            "Entferne zuerst eine nicht mehr benötigte Provider-Verbindung.",
            "connection_quota_exceeded",
        )
    credentials = (
        _merged_credentials(
            payload.provider, {}, payload.credentials, identity_locked=False
        )
        if payload.credentials
        else {}
    )
    connection = ProviderConnection(
        workspace_id=workspace.id,
        provider=payload.provider,
        name=payload.name.strip(),
        base_url=_validated_base_url(payload.provider, payload.base_url),
        settings_json=_canonical_settings(payload.provider, payload.settings),
        credentials_configured=bool(credentials),
        credential_hint=_hint(payload.provider, credentials),
        encryption_key_version=settings.encryption_key_version,
    )
    db.add(connection)
    db.flush()
    if credentials:
        connection.credentials_encrypted = SecretCipher(settings).encrypt_json(
            credentials, context=f"connection:{connection.id}"
        )
    db.commit()
    connection.delete_blockers = []
    return connection


@router.get("/{connection_id}", response_model=ConnectionOut)
def get_connection(
    connection_id: uuid.UUID,
    access: WorkspaceAccessDep,
    db: DbDep,
) -> ProviderConnection:
    connection = _connection(db, access.workspace.id, connection_id)
    _annotate_delete_blockers(db, access.workspace.id, [connection])
    return connection


@router.patch("/{connection_id}", response_model=ConnectionOut)
def update_connection(
    connection_id: uuid.UUID,
    payload: ConnectionUpdate,
    access: WorkspaceAdminDep,
    settings: SettingsDep,
    db: DbDep,
    csrf: CsrfDep,
) -> ProviderConnection:
    workspace = db.scalar(
        select(Workspace)
        .where(Workspace.id == access.workspace.id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if workspace is None:
        raise ProblemException(
            404, "Nicht gefunden", "Workspace nicht gefunden.", "not_found"
        )
    connection = db.scalar(
        select(ProviderConnection)
        .where(
            ProviderConnection.id == connection_id,
            ProviderConnection.workspace_id == workspace.id,
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if connection is None:
        raise ProblemException(
            404, "Nicht gefunden", "Verbindung nicht gefunden.", "not_found"
        )
    referenced = db.scalar(
        select(SyncProfile.id)
        .where(
            SyncProfile.workspace_id == workspace.id,
            or_(
                SyncProfile.source_connection_id == connection.id,
                SyncProfile.target_connection_id == connection.id,
            ),
        )
        .limit(1)
    )
    if payload.name is not None:
        connection.name = payload.name.strip()
    if payload.base_url is not None:
        validated_base_url = _validated_base_url(connection.provider, payload.base_url)
        if referenced is not None and validated_base_url != connection.base_url:
            raise _immutable_connection_identity()
        connection.base_url = validated_base_url
    if payload.settings is not None:
        connection.settings_json = _canonical_settings(
            connection.provider, payload.settings
        )
    if payload.credentials is not None:
        credentials = _updated_credentials(
            connection,
            settings,
            payload.credentials,
            identity_locked=referenced is not None,
        )
        connection.credentials_encrypted = (
            SecretCipher(settings).encrypt_json(
                credentials, context=f"connection:{connection.id}"
            )
            if credentials
            else None
        )
        connection.credentials_configured = bool(credentials)
        connection.credential_hint = _hint(connection.provider, credentials)
        connection.encryption_key_version = settings.encryption_key_version
    connection.revision += 1
    db.commit()
    _annotate_delete_blockers(db, workspace.id, [connection])
    return connection


@router.delete("/{connection_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_connection(
    connection_id: uuid.UUID,
    access: WorkspaceAdminDep,
    db: DbDep,
    csrf: CsrfDep,
) -> None:
    workspace = db.scalar(
        select(Workspace)
        .where(Workspace.id == access.workspace.id)
        .with_for_update()
    )
    if workspace is None:
        raise ProblemException(
            404, "Nicht gefunden", "Workspace nicht gefunden.", "not_found"
        )
    connection = db.scalar(
        select(ProviderConnection).where(
            ProviderConnection.id == connection_id,
            ProviderConnection.workspace_id == workspace.id,
        ).with_for_update()
    )
    if connection is None:
        raise ProblemException(
            404, "Nicht gefunden", "Verbindung nicht gefunden.", "not_found"
        )
    profile_reference = db.scalar(
        select(SyncProfile.id)
        .where(
            SyncProfile.workspace_id == workspace.id,
            or_(
                SyncProfile.source_connection_id == connection.id,
                SyncProfile.target_connection_id == connection.id,
            ),
        )
        .limit(1)
    )
    binding_reference = db.scalar(
        select(RemoteBinding.id)
        .where(
            RemoteBinding.workspace_id == workspace.id,
            RemoteBinding.target_connection_id == connection.id,
        )
        .limit(1)
    )
    if profile_reference is not None or binding_reference is not None:
        raise ProblemException(
            409,
            "Verbindung wird verwendet",
            "Entferne zuerst alle verknüpften Sync-Profile und Besitzmarkierungen.",
            "connection_in_use",
        )
    db.delete(connection)
    db.commit()


@router.post("/{connection_id}/test", response_model=ConnectionTestResult)
def test_connection(
    connection_id: uuid.UUID,
    request: Request,
    access: WorkspaceAdminDep,
    settings: SettingsDep,
    db: DbDep,
    csrf: CsrfDep,
) -> ConnectionTestResult:
    connection = _connection(db, access.workspace.id, connection_id)
    probe_client = getattr(request.app.state, "connection_probe_client", None)
    tester = getattr(request.app.state, "connection_tester", None)
    if probe_client is None and tester is None:
        raise ProblemException(
            501,
            "Verbindungstest nicht verfügbar",
            "Für diesen Provider ist noch kein Testadapter registriert.",
            "connection_tester_unavailable",
        )
    try:
        if probe_client is not None:
            result = probe_client.test(access.workspace.id, connection.id)
        else:
            credentials = (
                SecretCipher(settings).decrypt_json(
                    connection.credentials_encrypted,
                    context=f"connection:{connection.id}",
                )
                if connection.credentials_encrypted
                else {}
            )
            result = tester.test(connection, credentials)
    except ProviderProbeError as exc:
        raise _probe_problem(exc, metadata=False) from None
    except Exception:
        # Provider exceptions may contain response bodies or credentials.
        result = {"succeeded": False, "message": "Verbindungstest fehlgeschlagen."}
    tested_at = datetime.now(timezone.utc)
    connection.last_tested_at = tested_at
    connection.last_test_succeeded = bool(result.get("succeeded"))
    connection.last_test_message = str(result.get("message", ""))[:500]
    db.commit()
    return ConnectionTestResult(
        succeeded=connection.last_test_succeeded,
        message=connection.last_test_message or "",
        identity=result.get("identity", {}),
        capabilities=result.get("capabilities", []),
        tested_at=tested_at,
    )


@router.get("/{connection_id}/metadata", response_model=ProviderMetadata)
def get_connection_metadata(
    connection_id: uuid.UUID,
    request: Request,
    access: WorkspaceAdminDep,
    settings: SettingsDep,
    db: DbDep,
) -> ProviderMetadata:
    connection = _connection(db, access.workspace.id, connection_id)
    probe_client = getattr(request.app.state, "connection_probe_client", None)
    tester = getattr(request.app.state, "connection_tester", None)
    if probe_client is None and (tester is None or not hasattr(tester, "metadata")):
        raise ProblemException(
            501,
            "Metadaten nicht verfügbar",
            "Für diesen Provider ist noch kein Metadatenadapter registriert.",
            "provider_metadata_unavailable",
        )
    try:
        if probe_client is not None:
            data = probe_client.metadata(access.workspace.id, connection.id)
        else:
            credentials = (
                SecretCipher(settings).decrypt_json(
                    connection.credentials_encrypted,
                    context=f"connection:{connection.id}",
                )
                if connection.credentials_encrypted
                else {}
            )
            data = tester.metadata(connection, credentials)
    except ProviderProbeError as exc:
        raise _probe_problem(exc, metadata=True) from None
    except Exception as exc:
        raise ProblemException(
            502,
            "Provider nicht erreichbar",
            "Die Provider-Metadaten konnten nicht geladen werden.",
            "provider_metadata_failed",
        ) from exc
    return ProviderMetadata(data=data, retrieved_at=datetime.now(timezone.utc))


def _probe_problem(
    error: ProviderProbeError, *, metadata: bool
) -> ProblemException:
    headers = (
        {"Retry-After": str(error.retry_after)}
        if error.retry_after is not None
        else None
    )
    if error.code == "provider_probe_in_progress":
        return ProblemException(
            409,
            "Provider-Prüfung läuft bereits",
            "Eine identische Provider-Prüfung wird bereits verarbeitet.",
            error.code,
            headers=headers,
        )
    if error.code == "provider_probe_timeout":
        return ProblemException(
            504,
            "Provider-Prüfung dauert zu lange",
            "Die Provider-Prüfung wurde eingereiht, lieferte aber nicht rechtzeitig ein Ergebnis.",
            error.code,
            headers=headers,
        )
    if error.code in {
        "provider_probe_provider_failed",
        "provider_probe_invalid_result",
        "provider_probe_result_too_large",
        "provider_probe_invalid_response",
    }:
        return ProblemException(
            502,
            "Provider-Antwort nicht verfügbar",
            (
                "Die Provider-Metadaten konnten nicht sicher geladen werden."
                if metadata
                else "Die Provider-Verbindung konnte nicht sicher geprüft werden."
            ),
            error.code,
            headers=headers,
        )
    return ProblemException(
        503,
        "Provider-Prüfung nicht verfügbar",
        "Der Hintergrunddienst für Provider-Prüfungen ist vorübergehend nicht verfügbar.",
        error.code,
        headers=headers,
    )
