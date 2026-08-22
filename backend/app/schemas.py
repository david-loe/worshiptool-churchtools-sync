from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import regex
from croniter import CroniterBadCronError, CroniterBadDateError, croniter
from pydantic import (
    BaseModel,
    ConfigDict,
    EmailStr,
    Field,
    SecretStr,
    field_validator,
    model_validator,
)

from .event_filters import canonicalize_persisted_event_rules
from .models import (
    NotificationSeverity,
    ProviderType,
    SyncActionKind,
    SyncActionStatus,
    SyncRunStatus,
    SyncTrigger,
    WorkspaceRole,
)


class ORMModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class APIRequest(BaseModel):
    """Reject misspelled or stale fields at every write boundary."""

    model_config = ConfigDict(extra="forbid")


class ProblemDetails(BaseModel):
    """RFC 9457-style error envelope emitted by every exception handler."""

    type: str = "about:blank"
    title: str
    status: int = Field(ge=400, le=599)
    detail: str
    instance: str
    code: str
    trace_id: str
    errors: list[dict[str, str]] | None = None


class UserOut(ORMModel):
    id: uuid.UUID
    email: EmailStr
    email_verified_at: datetime | None
    is_platform_admin: bool
    email_verified: bool = False
    totp_enabled: bool = False


class RegisterRequest(APIRequest):
    email: EmailStr
    password: SecretStr = Field(min_length=12, max_length=1024)
    workspace_name: str = Field(default="Mein Workspace", min_length=1, max_length=120)


class RegisterResponse(BaseModel):
    user: UserOut
    workspace_id: uuid.UUID
    verification_required: bool
    development_verification_token: str | None = None


class VerifyEmailRequest(APIRequest):
    token: SecretStr = Field(min_length=1, max_length=512)


class VerificationResendRequest(APIRequest):
    email: EmailStr


class VerificationRequestedResponse(BaseModel):
    accepted: bool = True
    development_verification_token: str | None = None


class LoginRequest(APIRequest):
    email: EmailStr
    password: SecretStr = Field(max_length=1024)
    totp_code: SecretStr | None = Field(default=None, max_length=16)
    recovery_code: SecretStr | None = Field(default=None, max_length=64)


class SessionResponse(BaseModel):
    user: UserOut
    csrf_token: str


class RecoveryRequest(APIRequest):
    email: EmailStr


class RecoveryRequestedResponse(BaseModel):
    accepted: bool = True
    development_recovery_token: str | None = None


class RecoveryConfirmRequest(APIRequest):
    token: SecretStr = Field(min_length=1, max_length=512)
    new_password: SecretStr = Field(min_length=12, max_length=1024)


class TotpSetupResponse(BaseModel):
    secret: str
    provisioning_uri: str


class TotpSetupRequest(APIRequest):
    password: SecretStr = Field(max_length=1024)
    code: SecretStr | None = Field(default=None, max_length=16)
    recovery_code: SecretStr | None = Field(default=None, max_length=64)

    @model_validator(mode="after")
    def one_current_factor_at_most(self) -> "TotpSetupRequest":
        if self.code is not None and self.recovery_code is not None:
            raise ValueError("Sende TOTP-Code oder Wiederherstellungscode, nicht beide")
        return self


class TotpConfirmRequest(APIRequest):
    code: SecretStr = Field(min_length=1, max_length=16)


class TotpConfirmResponse(BaseModel):
    enabled: bool
    recovery_codes: list[str]


class TotpDisableRequest(APIRequest):
    password: SecretStr = Field(max_length=1024)
    code: SecretStr | None = Field(default=None, max_length=16)
    recovery_code: SecretStr | None = Field(default=None, max_length=64)

    @model_validator(mode="after")
    def exactly_one_current_factor(self) -> "TotpDisableRequest":
        if (self.code is None) == (self.recovery_code is None):
            raise ValueError("Genau ein TOTP- oder Wiederherstellungscode ist erforderlich")
        return self


class WorkspaceCreate(APIRequest):
    name: str = Field(min_length=1, max_length=120)


class WorkspaceUpdate(APIRequest):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    archived: bool | None = None


class WorkspaceOut(ORMModel):
    id: uuid.UUID
    name: str
    slug: str
    archived_at: datetime | None
    profile_quota: int
    member_quota: int
    role: WorkspaceRole
    created_at: datetime
    updated_at: datetime


class WorkspaceList(BaseModel):
    items: list[WorkspaceOut]
    total: int
    limit: int
    offset: int


class MemberOut(BaseModel):
    id: uuid.UUID
    user_id: uuid.UUID
    email: EmailStr
    role: WorkspaceRole
    created_at: datetime


class MemberRoleUpdate(APIRequest):
    role: WorkspaceRole


class InvitationCreate(APIRequest):
    email: EmailStr
    role: WorkspaceRole = WorkspaceRole.VIEWER

    @field_validator("role")
    @classmethod
    def owner_cannot_be_invited(cls, value: WorkspaceRole) -> WorkspaceRole:
        if value == WorkspaceRole.OWNER:
            raise ValueError("Die Eigentümerrolle wird nach dem Beitritt übertragen.")
        return value


class InvitationOut(ORMModel):
    id: uuid.UUID
    workspace_id: uuid.UUID
    email: EmailStr
    role: WorkspaceRole
    created_at: datetime
    expires_at: datetime
    accepted_at: datetime | None
    development_invitation_token: str | None = None


class InvitationAccept(APIRequest):
    token: SecretStr = Field(min_length=1, max_length=512)


class ProviderConnectionSettings(APIRequest):
    """Non-secret provider options; credentials always use the write-only field."""

    timezone: str | None = Field(default=None, min_length=1, max_length=64)

    @field_validator("timezone")
    @classmethod
    def timezone_must_exist(cls, value: str | None) -> str | None:
        if value is None:
            return None
        try:
            ZoneInfo(value)
        except ZoneInfoNotFoundError as exc:
            raise ValueError("Die Provider-Zeitzone ist unbekannt") from exc
        return value


class ConnectionCreate(APIRequest):
    provider: ProviderType
    name: str = Field(min_length=1, max_length=120)
    base_url: str | None = Field(
        default=None,
        max_length=512,
        description="ChurchTools-HTTPS-Ursprung; für WorshipTools nicht konfigurierbar.",
    )
    settings: ProviderConnectionSettings = Field(
        default_factory=ProviderConnectionSettings
    )
    credentials: dict[str, str] = Field(
        default_factory=dict,
        max_length=3,
        json_schema_extra={"writeOnly": True},
    )

    @model_validator(mode="after")
    def settings_match_provider(self) -> "ConnectionCreate":
        if self.provider == ProviderType.CHURCHTOOLS and self.settings.timezone is not None:
            raise ValueError("ChurchTools-Verbindungen akzeptieren keine Provider-Settings")
        return self


class ConnectionUpdate(APIRequest):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    base_url: str | None = Field(
        default=None,
        max_length=512,
        description="ChurchTools-HTTPS-Ursprung; für WorshipTools nicht konfigurierbar.",
    )
    settings: ProviderConnectionSettings | None = None
    credentials: dict[str, str] | None = Field(
        default=None,
        max_length=3,
        json_schema_extra={"writeOnly": True},
    )


class ConnectionOut(ORMModel):
    id: uuid.UUID
    workspace_id: uuid.UUID
    provider: ProviderType
    name: str
    base_url: str | None = Field(
        default=None, exclude_if=lambda value: value is None
    )
    settings: ProviderConnectionSettings = Field(validation_alias="settings_json")
    credentials_configured: bool
    credential_hint: str | None
    revision: int
    last_tested_at: datetime | None
    last_test_succeeded: bool | None
    last_test_message: str | None
    delete_blockers: list[Literal["profile_reference", "remote_binding"]] = Field(
        default_factory=list,
        description="Read-only reasons why DELETE would currently be rejected.",
    )
    created_at: datetime
    updated_at: datetime

    @model_validator(mode="after")
    def hide_fixed_worshiptools_origins(self) -> "ConnectionOut":
        if self.provider == ProviderType.WORSHIPTOOLS:
            self.base_url = None
        else:
            self.settings = ProviderConnectionSettings()
        return self

    @field_validator("settings", mode="before")
    @classmethod
    def expose_only_canonical_non_secret_settings(cls, value: Any) -> dict[str, Any]:
        if not isinstance(value, dict):
            return {}
        timezone = value.get("timezone")
        return {"timezone": timezone} if timezone is not None else {}


class ConnectionList(BaseModel):
    items: list[ConnectionOut]
    total: int
    limit: int
    offset: int


class ConnectionTestResult(BaseModel):
    succeeded: bool
    message: str
    identity: dict[str, Any] = Field(default_factory=dict)
    capabilities: list[str] = Field(default_factory=list)
    tested_at: datetime


class ProviderOption(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1, max_length=200)
    name: str = Field(min_length=1, max_length=300)


class ProviderMetadataData(BaseModel):
    model_config = ConfigDict(extra="forbid")

    calendars: list[ProviderOption] = Field(default_factory=list, max_length=10_000)
    campuses: list[ProviderOption] = Field(default_factory=list, max_length=10_000)
    song_categories: list[ProviderOption] = Field(default_factory=list, max_length=10_000)


class ProviderMetadata(BaseModel):
    data: ProviderMetadataData
    retrieved_at: datetime


def validate_cron_schedule(expression: str, timezone_name: str) -> None:
    """Validate syntax and prove the schedule never fires below 30 minutes.

    A long sample in the configured local timezone also crosses common DST
    boundaries. Intervals are compared as real UTC elapsed time.
    """

    try:
        zone = ZoneInfo(timezone_name)
    except ZoneInfoNotFoundError as exc:
        raise ValueError(f"Die Zeitzone {timezone_name!r} ist unbekannt.") from exc
    try:
        iterator = croniter(expression, datetime(2026, 1, 1, tzinfo=zone))
        previous = iterator.get_next(datetime)
        for _ in range(128):
            current = iterator.get_next(datetime)
            elapsed = current.astimezone(timezone.utc) - previous.astimezone(timezone.utc)
            if elapsed < timedelta(minutes=30):
                raise ValueError(
                    "Cron-Ausführungen müssen immer mindestens 30 Minuten auseinanderliegen."
                )
            previous = current
    except (CroniterBadCronError, CroniterBadDateError, ValueError) as exc:
        if isinstance(exc, ValueError) and str(exc).startswith("Cron-Ausführungen"):
            raise
        raise ValueError("Der Cron-Ausdruck ist ungültig.") from exc


class EventSelectorConfig(APIRequest):

    name_contains: str | None = Field(default=None, min_length=1, max_length=200)
    name_regex: str | None = Field(default=None, min_length=1, max_length=256)
    campus_ids: list[str] = Field(default_factory=list, max_length=100)
    calendar_ids: list[str] = Field(default_factory=list, max_length=100)

    @field_validator("campus_ids", "calendar_ids")
    @classmethod
    def normalize_ids(cls, values: list[str]) -> list[str]:
        normalized: list[str] = []
        seen: set[str] = set()
        for value in values:
            item = value.strip()
            if not item:
                raise ValueError("IDs dürfen nicht leer sein")
            if len(item) > 100:
                raise ValueError("IDs dürfen höchstens 100 Zeichen lang sein")
            if item not in seen:
                seen.add(item)
                normalized.append(item)
        return normalized

    @field_validator("name_regex")
    @classmethod
    def validate_regex(cls, value: str | None) -> str | None:
        if value is not None:
            try:
                regex.compile(value)
            except regex.error as exc:
                raise ValueError("Ungültiger regulärer Ausdruck") from exc
        return value


class AgendaAnchorConfig(APIRequest):
    item_id: str | None = Field(default=None, min_length=1, max_length=200)
    item_type: str | None = Field(default=None, min_length=1, max_length=80)
    title: str | None = Field(default=None, min_length=1, max_length=300)

    @model_validator(mode="after")
    def contains_matcher(self) -> "AgendaAnchorConfig":
        if not any((self.item_id, self.item_type, self.title)):
            raise ValueError("Ein Anker benötigt item_id, item_type oder title")
        return self


class PlacementConfig(APIRequest):
    id: str = Field(min_length=1, max_length=100, pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]*$")
    anchor: AgendaAnchorConfig
    relation: Literal["before", "at", "after"] = "after"
    song_start: int = Field(default=0, ge=0)
    song_end: int | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def validate_slice(self) -> "PlacementConfig":
        if self.song_end is not None and self.song_end < self.song_start:
            raise ValueError("song_end darf nicht kleiner als song_start sein")
        return self


class ProfileNotificationConfig(APIRequest):
    """Workspace-profile policy, combined with each user's channel opt-ins."""

    in_app: Literal[True] = True
    web_push: bool = True
    email: bool = True
    telegram: bool = False
    notify_success: bool = False
    notify_new_songs: bool = True


class AgendaItemDefaults(APIRequest):
    """Optional overrides sent for every managed ChurchTools song item."""

    model_config = ConfigDict(extra="forbid", strict=True)

    title: str | None = Field(default=None, max_length=100)
    note: str | None = Field(default=None, max_length=4000)
    responsible: str | None = Field(default=None, max_length=1000)
    duration: int | None = Field(default=None, ge=0, le=86_400)


class ProfileCreate(APIRequest):
    source_connection_id: uuid.UUID
    target_connection_id: uuid.UUID
    name: str = Field(min_length=1, max_length=120)
    enabled: bool = False
    match_mode: Literal["exact_time", "date_only"] = "exact_time"
    source_timezone: str = "UTC"
    target_timezone: str = "UTC"
    lookahead_days: int = Field(default=28, ge=1, le=365)
    schedule_type: Literal["interval", "cron"] = "interval"
    interval_minutes: int | None = Field(default=60, ge=30, le=10080)
    cron_expression: str | None = Field(default=None, max_length=120)
    event_rules: list[EventSelectorConfig] = Field(default_factory=list, max_length=100)
    placements: list[PlacementConfig] = Field(default_factory=list, max_length=100)
    notification_preferences: ProfileNotificationConfig = Field(
        default_factory=ProfileNotificationConfig
    )
    create_missing_songs: bool = True
    song_category_id: int | None = Field(default=None, ge=1)
    arrangement_name: str = Field(
        default="Standard-Arrangement", min_length=2, max_length=50
    )
    agenda_item_defaults: AgendaItemDefaults = Field(
        default_factory=AgendaItemDefaults
    )

    @model_validator(mode="after")
    def validate_schedule(self) -> "ProfileCreate":
        if self.schedule_type == "interval" and self.interval_minutes is None:
            raise ValueError("interval_minutes ist für Intervall-Zeitpläne erforderlich")
        if self.schedule_type == "cron" and not self.cron_expression:
            raise ValueError("cron_expression ist für Cron-Zeitpläne erforderlich")
        if self.schedule_type == "cron" and self.cron_expression:
            validate_cron_schedule(self.cron_expression, self.target_timezone)
        if self.create_missing_songs and self.song_category_id is None:
            raise ValueError(
                "song_category_id ist bei automatischer Song-Erstellung erforderlich"
            )
        return self

    @field_validator("placements")
    @classmethod
    def unique_placement_ids(
        cls, values: list[PlacementConfig]
    ) -> list[PlacementConfig]:
        ids = [item.id for item in values]
        if len(ids) != len(set(ids)):
            raise ValueError("Platzierungs-IDs müssen eindeutig sein")
        return values


class ProfileUpdate(APIRequest):
    source_connection_id: uuid.UUID | None = None
    target_connection_id: uuid.UUID | None = None
    name: str | None = Field(default=None, min_length=1, max_length=120)
    enabled: bool | None = None
    match_mode: Literal["exact_time", "date_only"] | None = None
    source_timezone: str | None = None
    target_timezone: str | None = None
    lookahead_days: int | None = Field(default=None, ge=1, le=365)
    schedule_type: Literal["interval", "cron"] | None = None
    interval_minutes: int | None = Field(default=None, ge=30, le=10080)
    cron_expression: str | None = Field(default=None, max_length=120)
    event_rules: list[EventSelectorConfig] | None = Field(default=None, max_length=100)
    placements: list[PlacementConfig] | None = Field(default=None, max_length=100)
    notification_preferences: ProfileNotificationConfig | None = None
    create_missing_songs: bool | None = None
    song_category_id: int | None = Field(default=None, ge=1)
    arrangement_name: str | None = Field(default=None, min_length=2, max_length=50)
    agenda_item_defaults: AgendaItemDefaults | None = None

    @field_validator("placements")
    @classmethod
    def unique_placement_ids(
        cls, values: list[PlacementConfig] | None
    ) -> list[PlacementConfig] | None:
        if values is not None:
            ids = [item.id for item in values]
            if len(ids) != len(set(ids)):
                raise ValueError("Platzierungs-IDs müssen eindeutig sein")
        return values

    @model_validator(mode="after")
    def reject_explicit_null_for_required_fields(self) -> "ProfileUpdate":
        required_fields = {
            "source_connection_id",
            "target_connection_id",
            "name",
            "enabled",
            "match_mode",
            "source_timezone",
            "target_timezone",
            "lookahead_days",
            "schedule_type",
            "event_rules",
            "placements",
            "notification_preferences",
            "create_missing_songs",
            "arrangement_name",
        }
        invalid = sorted(
            field
            for field in required_fields & self.model_fields_set
            if getattr(self, field) is None
        )
        if invalid:
            raise ValueError(
                "Diese Profilfelder dürfen nicht null sein: " + ", ".join(invalid)
            )
        return self


class ProfileOut(ORMModel):
    id: uuid.UUID
    workspace_id: uuid.UUID
    source_connection_id: uuid.UUID
    target_connection_id: uuid.UUID
    name: str
    enabled: bool
    revision: int
    match_mode: str
    source_timezone: str
    target_timezone: str
    lookahead_days: int
    schedule_type: str
    interval_minutes: int | None
    cron_expression: str | None
    next_scheduled_at: datetime | None
    event_rules: list[EventSelectorConfig]
    placements: list[PlacementConfig]
    notification_preferences: ProfileNotificationConfig
    create_missing_songs: bool
    song_category_id: int | None
    arrangement_name: str
    agenda_item_defaults: AgendaItemDefaults
    delete_blockers: list[Literal["run_history", "remote_binding"]] = Field(
        default_factory=list,
        description="Read-only reasons why DELETE would currently be rejected.",
    )
    created_at: datetime
    updated_at: datetime

    @field_validator("event_rules", mode="before")
    @classmethod
    def canonicalize_legacy_event_rules(cls, value: Any) -> list[dict[str, Any]]:
        return canonicalize_persisted_event_rules(value)

    @field_validator("notification_preferences", mode="before")
    @classmethod
    def enforce_canonical_in_app_notifications(cls, value: Any) -> Any:
        if isinstance(value, dict):
            return {**value, "in_app": True}
        return value


class ProfileList(BaseModel):
    items: list[ProfileOut]
    total: int
    limit: int
    offset: int


class RunCreate(APIRequest):
    dry_run: bool = False


class SyncActionOut(ORMModel):
    id: uuid.UUID
    event_id: str | None
    source_id: str | None
    target_id: str | None
    kind: SyncActionKind
    status: SyncActionStatus
    ordinal: int
    payload: dict[str, Any] = Field(validation_alias="payload_json")
    fingerprint: dict[str, Any] | None = Field(validation_alias="fingerprint_json")
    error: dict[str, Any] | None = Field(validation_alias="error_json")
    planned_at: datetime
    applied_at: datetime | None
    verified_at: datetime | None


class SyncRunSummary(ORMModel):
    id: uuid.UUID
    workspace_id: uuid.UUID
    profile_id: uuid.UUID
    config_revision: int
    status: SyncRunStatus
    trigger: SyncTrigger
    dry_run: bool
    created_at: datetime
    planned_at: datetime | None
    started_at: datetime | None
    finished_at: datetime | None
    error: dict[str, Any] | None = Field(validation_alias="error_json")


class SyncRunOut(SyncRunSummary):
    plan: dict[str, Any] | None = Field(validation_alias="plan_json")


class SyncRunDetail(SyncRunOut):
    pass


class SyncActionStatusCounts(BaseModel):
    planned: int = Field(default=0, ge=0)
    applied: int = Field(default=0, ge=0)
    verified: int = Field(default=0, ge=0)
    failed: int = Field(default=0, ge=0)
    skipped: int = Field(default=0, ge=0)


class SyncActionList(BaseModel):
    items: list[SyncActionOut]
    total: int = Field(ge=0)
    limit: int = Field(ge=1, le=200)
    offset: int = Field(ge=0)
    status_counts: SyncActionStatusCounts


class SyncRunList(BaseModel):
    items: list[SyncRunSummary]
    total: int
    limit: int
    offset: int


class NotificationOut(ORMModel):
    id: uuid.UUID
    workspace_id: uuid.UUID
    user_id: uuid.UUID | None
    run_id: uuid.UUID | None
    severity: NotificationSeverity
    category: str
    title: str
    body: str
    data: dict[str, Any] = Field(validation_alias="data_json")
    created_at: datetime
    read_at: datetime | None


class NotificationList(BaseModel):
    items: list[NotificationOut]
    total: int
    unread: int
    limit: int
    offset: int


class NotificationMarkAllReadResponse(BaseModel):
    updated: int = Field(ge=0)
    read_at: datetime


class NotificationPreferenceUpdate(APIRequest):
    in_app_enabled: Literal[True] = True
    push_enabled: bool = False
    email_enabled: bool = True
    success_notifications: bool = False
    telegram_enabled: bool = Field(
        default=False,
        description="Veralteter Kanal; standardmäßig deaktiviert.",
        deprecated=True,
    )


class NotificationPreferenceOut(ORMModel):
    in_app_enabled: Literal[True]
    push_enabled: bool
    email_enabled: bool
    success_notifications: bool
    telegram_enabled: bool

    @field_validator("in_app_enabled", mode="before")
    @classmethod
    def enforce_canonical_in_app_notifications(cls, value: Any) -> bool:
        return True


class PushSubscriptionCreate(APIRequest):
    endpoint: str = Field(min_length=10, max_length=2048, json_schema_extra={"writeOnly": True})
    p256dh: str = Field(min_length=10, max_length=512, json_schema_extra={"writeOnly": True})
    auth: str = Field(min_length=8, max_length=512, json_schema_extra={"writeOnly": True})
    device_name: str = Field(default="Browser", min_length=1, max_length=120)


class PushSubscriptionOut(ORMModel):
    id: uuid.UUID
    device_name: str
    created_at: datetime
    last_used_at: datetime | None
    revoked_at: datetime | None


class HealthOut(BaseModel):
    status: Literal["ok", "degraded"]
    database: Literal["ok", "error"]
    redis: Literal["ok", "error"]
    version: str


class VapidKeyOut(BaseModel):
    public_key: str


class AdminWorkspaceOut(BaseModel):
    id: uuid.UUID
    name: str
    slug: str
    archived_at: datetime | None
    profile_quota: int
    member_quota: int
    profile_count: int
    member_count: int
    created_at: datetime


class AdminWorkspaceList(BaseModel):
    items: list[AdminWorkspaceOut]
    total: int
    limit: int
    offset: int


class WorkspaceQuotaUpdate(APIRequest):
    profile_quota: int = Field(ge=1, le=1000)
    member_quota: int = Field(ge=1, le=10000)
