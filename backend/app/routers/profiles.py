from __future__ import annotations

import re
import uuid
from datetime import datetime, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import APIRouter, Header, Query, Response, status
from sqlalchemy import func, select

from ..dependencies import CsrfDep, DbDep, WorkspaceAccessDep, WorkspaceAdminDep
from ..models import (
    ProviderConnection,
    ProviderType,
    RemoteBinding,
    SyncProfile,
    SyncRun,
    SyncRunStatus,
    Workspace,
)
from ..problems import ProblemException
from ..scheduling import next_schedule_after
from ..schemas import (
    AgendaItemDefaults,
    ProfileCreate,
    ProfileList,
    ProfileOut,
    ProfileUpdate,
    validate_cron_schedule,
)


router = APIRouter(prefix="/workspaces/{workspace_id}/profiles", tags=["Sync-Profile"])


def _etag(revision: int) -> str:
    return f'"{revision}"'


def _locked_workspace(db: DbDep, workspace_id: uuid.UUID) -> Workspace:
    workspace = db.scalar(
        select(Workspace)
        .where(Workspace.id == workspace_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if workspace is None:
        raise ProblemException(
            404, "Nicht gefunden", "Workspace nicht gefunden.", "not_found"
        )
    return workspace


def _annotate_delete_blockers(
    db: DbDep,
    workspace_id: uuid.UUID,
    profiles: list[SyncProfile],
) -> None:
    """Attach batched, read-only DELETE guard information for API serialization."""

    profile_ids = {profile.id for profile in profiles}
    blockers: dict[uuid.UUID, set[str]] = {profile_id: set() for profile_id in profile_ids}
    if profile_ids:
        for profile_id in db.scalars(
            select(SyncRun.profile_id)
            .where(
                SyncRun.workspace_id == workspace_id,
                SyncRun.profile_id.in_(profile_ids),
            )
            .distinct()
        ):
            blockers[profile_id].add("run_history")
        for profile_id in db.scalars(
            select(RemoteBinding.profile_id)
            .where(
                RemoteBinding.workspace_id == workspace_id,
                RemoteBinding.profile_id.in_(profile_ids),
            )
            .distinct()
        ):
            blockers[profile_id].add("remote_binding")
    order = {"run_history": 0, "remote_binding": 1}
    for profile in profiles:
        profile.delete_blockers = sorted(blockers[profile.id], key=order.__getitem__)


def _expected_revision(if_match: str | None) -> int:
    if if_match is None:
        raise ProblemException(
            428,
            "Vorbedingung erforderlich",
            "Sende den zuletzt gelesenen ETag im If-Match-Header.",
            "if_match_required",
        )
    value = if_match.removeprefix("W/").strip().strip('"')
    try:
        return int(value)
    except ValueError as exc:
        raise ProblemException(400, "Ungültiger ETag", "If-Match ist ungültig.", "invalid_etag") from exc


def _validate_timezone(value: str) -> str:
    try:
        ZoneInfo(value)
    except ZoneInfoNotFoundError as exc:
        raise ProblemException(
            422, "Ungültige Zeitzone", f"Die Zeitzone {value!r} ist unbekannt.", "invalid_timezone"
        ) from exc
    return value


def _validate_rules(rules: list) -> None:
    for index, rule in enumerate(rules):
        if hasattr(rule, "model_dump"):
            rule = rule.model_dump()
        regex = rule.get("name_regex")
        if regex is not None:
            if not isinstance(regex, str) or len(regex) > 256:
                raise ProblemException(422, "Ungültige Regel", f"Regex in Regel {index + 1} ist zu lang.", "invalid_rule")
            try:
                re.compile(regex)
            except re.error as exc:
                raise ProblemException(422, "Ungültige Regex", f"Regex in Regel {index + 1} ist ungültig.", "invalid_regex") from exc


def _agenda_defaults_for_storage(value: AgendaItemDefaults) -> dict[str, object]:
    """Persist only effective overrides; null clears an adapter override."""

    return {
        key: item
        for key, item in value.model_dump(exclude_unset=True).items()
        if item is not None
    }


def _merge_agenda_defaults(
    current: dict[str, object] | None,
    patch: AgendaItemDefaults | None,
) -> dict[str, object]:
    if patch is None:
        return {}
    allowed = AgendaItemDefaults.model_fields.keys()
    merged = {
        key: item
        for key, item in (current or {}).items()
        if key in allowed
    }
    for key, item in patch.model_dump(exclude_unset=True).items():
        if item is None:
            merged.pop(key, None)
        else:
            merged[key] = item
    return merged


def _profile(
    db: DbDep,
    workspace_id: uuid.UUID,
    profile_id: uuid.UUID,
    *,
    for_update: bool = False,
) -> SyncProfile:
    query = select(SyncProfile).where(
        SyncProfile.id == profile_id, SyncProfile.workspace_id == workspace_id
    )
    if for_update:
        query = query.with_for_update().execution_options(populate_existing=True)
    profile = db.scalar(query)
    if profile is None:
        raise ProblemException(404, "Nicht gefunden", "Sync-Profil nicht gefunden.", "not_found")
    return profile


def _validate_connection_pair(
    db: DbDep, workspace_id: uuid.UUID, source_id: uuid.UUID, target_id: uuid.UUID
) -> None:
    connections = db.scalars(
        select(ProviderConnection).where(
            ProviderConnection.workspace_id == workspace_id,
            ProviderConnection.id.in_([source_id, target_id]),
        )
    ).all()
    by_id = {item.id: item for item in connections}
    if source_id not in by_id or target_id not in by_id:
        raise ProblemException(422, "Ungültige Verbindung", "Beide Verbindungen müssen zum Workspace gehören.", "invalid_connection")
    if by_id[source_id].provider != ProviderType.WORSHIPTOOLS or by_id[target_id].provider != ProviderType.CHURCHTOOLS:
        raise ProblemException(
            422,
            "Ungültige Provider-Reihenfolge",
            "Quelle muss WorshipTools und Ziel muss ChurchTools sein.",
            "invalid_provider_pair",
        )


@router.get("", response_model=ProfileList)
def list_profiles(
    access: WorkspaceAccessDep,
    db: DbDep,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> ProfileList:
    predicate = SyncProfile.workspace_id == access.workspace.id
    total = db.scalar(select(func.count()).select_from(SyncProfile).where(predicate)) or 0
    items = list(db.scalars(
        select(SyncProfile).where(predicate).order_by(SyncProfile.name).limit(limit).offset(offset)
    ).all())
    _annotate_delete_blockers(db, access.workspace.id, items)
    return ProfileList(items=items, total=total, limit=limit, offset=offset)


@router.post("", response_model=ProfileOut, status_code=status.HTTP_201_CREATED)
def create_profile(
    payload: ProfileCreate,
    response: Response,
    access: WorkspaceAdminDep,
    db: DbDep,
    csrf: CsrfDep,
) -> SyncProfile:
    workspace = _locked_workspace(db, access.workspace.id)
    if workspace.archived_at is not None and payload.enabled:
        raise ProblemException(
            409,
            "Workspace archiviert",
            "In einem archivierten Workspace kann kein Profil aktiviert werden.",
            "workspace_archived",
        )
    count = db.scalar(
        select(func.count()).select_from(SyncProfile).where(
            SyncProfile.workspace_id == workspace.id
        )
    ) or 0
    if count >= workspace.profile_quota:
        raise ProblemException(409, "Profil-Limit erreicht", "Das Profil-Limit dieses Workspaces ist erreicht.", "profile_quota_exceeded")
    _validate_connection_pair(
        db, workspace.id, payload.source_connection_id, payload.target_connection_id
    )
    _validate_timezone(payload.source_timezone)
    _validate_timezone(payload.target_timezone)
    _validate_rules(payload.event_rules)
    values = payload.model_dump()
    values["agenda_item_defaults"] = _agenda_defaults_for_storage(
        payload.agenda_item_defaults
    )
    profile = SyncProfile(workspace_id=workspace.id, **values)
    if profile.enabled:
        profile.next_scheduled_at = _next_scheduled_at(profile)
    db.add(profile)
    db.commit()
    profile.delete_blockers = []
    response.headers["ETag"] = _etag(profile.revision)
    return profile


@router.get("/{profile_id}", response_model=ProfileOut)
def get_profile(
    profile_id: uuid.UUID,
    response: Response,
    access: WorkspaceAccessDep,
    db: DbDep,
) -> SyncProfile:
    profile = _profile(db, access.workspace.id, profile_id)
    _annotate_delete_blockers(db, access.workspace.id, [profile])
    response.headers["ETag"] = _etag(profile.revision)
    return profile


@router.patch("/{profile_id}", response_model=ProfileOut)
def update_profile(
    profile_id: uuid.UUID,
    payload: ProfileUpdate,
    response: Response,
    access: WorkspaceAdminDep,
    db: DbDep,
    csrf: CsrfDep,
    if_match: str | None = Header(default=None, alias="If-Match"),
) -> SyncProfile:
    workspace = _locked_workspace(db, access.workspace.id)
    profile = _profile(
        db, access.workspace.id, profile_id, for_update=True
    )
    expected = _expected_revision(if_match)
    if expected != profile.revision:
        raise ProblemException(
            412,
            "Konfiguration wurde geändert",
            "Lade das Profil neu und führe deine Änderung erneut aus.",
            "revision_conflict",
            headers={"ETag": _etag(profile.revision)},
        )
    values = payload.model_dump(exclude_unset=True)
    if "agenda_item_defaults" in payload.model_fields_set:
        values["agenda_item_defaults"] = _merge_agenda_defaults(
            profile.agenda_item_defaults,
            payload.agenda_item_defaults,
        )
    if workspace.archived_at is not None and values.get("enabled") is True:
        raise ProblemException(
            409,
            "Workspace archiviert",
            "In einem archivierten Workspace kann kein Profil aktiviert werden.",
            "workspace_archived",
        )
    effective_source_id = values.get(
        "source_connection_id", profile.source_connection_id
    )
    effective_target_id = values.get(
        "target_connection_id", profile.target_connection_id
    )
    connections_changed = (
        effective_source_id != profile.source_connection_id
        or effective_target_id != profile.target_connection_id
    )
    if connections_changed:
        active_run = db.scalar(
            select(SyncRun.id)
            .where(
                SyncRun.profile_id == profile.id,
                SyncRun.status.in_(
                    (SyncRunStatus.QUEUED, SyncRunStatus.RUNNING)
                ),
            )
            .limit(1)
        )
        if active_run is not None:
            raise ProblemException(
                409,
                "Sync läuft bereits",
                "Verbindungen können während eines aktiven Sync-Laufs nicht gewechselt werden.",
                "profile_has_active_run",
            )
        binding = db.scalar(
            select(RemoteBinding.id)
            .where(RemoteBinding.profile_id == profile.id)
            .limit(1)
        )
        if binding is not None:
            raise ProblemException(
                409,
                "Profil besitzt Remote-Zuordnungen",
                "Lege für andere Provider-Verbindungen ein neues Profil an, damit bestehende Ownership-Zuordnungen sicher bleiben.",
                "profile_has_remote_bindings",
            )
        _validate_connection_pair(
            db,
            access.workspace.id,
            effective_source_id,
            effective_target_id,
        )
    was_enabled = profile.enabled
    schedule_changed = bool(
        values.keys()
        & {
            "schedule_type",
            "interval_minutes",
            "cron_expression",
            "target_timezone",
        }
    )
    if "source_timezone" in values:
        _validate_timezone(values["source_timezone"])
    if "target_timezone" in values:
        _validate_timezone(values["target_timezone"])
    if "event_rules" in values:
        _validate_rules(values["event_rules"])
    for key, value in values.items():
        setattr(profile, key, value)
    effective_schedule = profile.schedule_type
    if effective_schedule == "interval" and profile.interval_minutes is None:
        raise ProblemException(422, "Ungültiger Zeitplan", "Intervall-Minuten fehlen.", "invalid_schedule")
    if effective_schedule == "cron" and not profile.cron_expression:
        raise ProblemException(422, "Ungültiger Zeitplan", "Cron-Ausdruck fehlt.", "invalid_schedule")
    if effective_schedule == "cron" and profile.cron_expression:
        try:
            validate_cron_schedule(profile.cron_expression, profile.target_timezone)
        except ValueError as exc:
            raise ProblemException(
                422,
                "Ungültiger Cron-Zeitplan",
                str(exc),
                "invalid_schedule",
            ) from exc
    if profile.create_missing_songs and profile.song_category_id is None:
        raise ProblemException(
            422,
            "Song-Kategorie fehlt",
            "Bei automatischer Song-Erstellung ist eine ChurchTools-Song-Kategorie erforderlich.",
            "song_category_required",
        )
    if not profile.enabled:
        profile.next_scheduled_at = None
    elif not was_enabled or schedule_changed or profile.next_scheduled_at is None:
        profile.next_scheduled_at = _next_scheduled_at(profile)
    profile.revision += 1
    db.commit()
    _annotate_delete_blockers(db, workspace.id, [profile])
    response.headers["ETag"] = _etag(profile.revision)
    return profile


@router.delete("/{profile_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_profile(
    profile_id: uuid.UUID,
    access: WorkspaceAdminDep,
    db: DbDep,
    csrf: CsrfDep,
    if_match: str | None = Header(default=None, alias="If-Match"),
) -> None:
    _locked_workspace(db, access.workspace.id)
    profile = _profile(
        db, access.workspace.id, profile_id, for_update=True
    )
    if _expected_revision(if_match) != profile.revision:
        raise ProblemException(412, "Konfiguration wurde geändert", "Das Profil wurde zwischenzeitlich geändert.", "revision_conflict", headers={"ETag": _etag(profile.revision)})
    run_history = db.scalar(
        select(SyncRun.id)
        .where(
            SyncRun.profile_id == profile.id,
        )
        .limit(1)
    )
    if run_history is not None:
        raise ProblemException(
            409,
            "Profil besitzt Laufhistorie",
            "Ein Profil mit Sync-Historie kann nicht gelöscht werden.",
            "profile_has_run_history",
        )
    binding = db.scalar(
        select(RemoteBinding.id)
        .where(RemoteBinding.profile_id == profile.id)
        .limit(1)
    )
    if binding is not None:
        raise ProblemException(
            409,
            "Profil besitzt Remote-Zuordnungen",
            "Entferne die verwalteten Agenda-Zuordnungen vor dem Löschen.",
            "profile_has_remote_bindings",
        )
    db.delete(profile)
    db.commit()


def _next_scheduled_at(profile: SyncProfile) -> datetime:
    try:
        return next_schedule_after(
            schedule_type=profile.schedule_type,
            interval_minutes=profile.interval_minutes,
            cron_expression=profile.cron_expression,
            timezone_name=profile.target_timezone,
            after=datetime.now(timezone.utc),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise ProblemException(
            422,
            "Ungültiger Zeitplan",
            "Der nächste Ausführungszeitpunkt konnte nicht berechnet werden.",
            "invalid_schedule",
        ) from exc
