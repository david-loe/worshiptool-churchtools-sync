from __future__ import annotations

import asyncio

import httpx
import pytest
from pydantic import ValidationError

from app.schemas import (
    ConnectionCreate,
    ConnectionUpdate,
    InvitationAccept,
    InvitationCreate,
    LoginRequest,
    MemberRoleUpdate,
    NotificationPreferenceOut,
    NotificationPreferenceUpdate,
    ProviderConnectionSettings,
    ProviderMetadata,
    ProfileCreate,
    ProfileNotificationConfig,
    ProfileUpdate,
    PushSubscriptionCreate,
    RecoveryConfirmRequest,
    RecoveryRequest,
    RegisterRequest,
    RunCreate,
    TotpConfirmRequest,
    TotpDisableRequest,
    TotpSetupRequest,
    VerificationResendRequest,
    VerifyEmailRequest,
    WorkspaceCreate,
    WorkspaceQuotaUpdate,
    WorkspaceUpdate,
)
from app.main import create_app


@pytest.mark.parametrize(
    "request_model",
    [
        RegisterRequest,
        VerifyEmailRequest,
        LoginRequest,
        RecoveryRequest,
        RecoveryConfirmRequest,
        TotpConfirmRequest,
        TotpDisableRequest,
        TotpSetupRequest,
        VerificationResendRequest,
        WorkspaceCreate,
        WorkspaceUpdate,
        MemberRoleUpdate,
        InvitationCreate,
        InvitationAccept,
        ConnectionCreate,
        ConnectionUpdate,
        ProfileCreate,
        ProfileUpdate,
        RunCreate,
        NotificationPreferenceUpdate,
        PushSubscriptionCreate,
        WorkspaceQuotaUpdate,
    ],
)
def test_every_http_request_model_rejects_unknown_fields(request_model):
    assert request_model.model_config.get("extra") == "forbid"


def test_connection_patch_rejects_the_immutable_provider_field():
    with pytest.raises(ValidationError):
        ConnectionUpdate.model_validate({"name": "Neu", "provider": "churchtools"})


def test_provider_settings_are_typed_timezone_only_and_provider_specific():
    parsed = ConnectionCreate.model_validate(
        {
            "provider": "worshiptools",
            "name": "WT",
            "settings": {"timezone": "Europe/Berlin"},
        }
    )
    assert parsed.settings == ProviderConnectionSettings(timezone="Europe/Berlin")

    with pytest.raises(ValidationError):
        ConnectionCreate.model_validate(
            {
                "provider": "worshiptools",
                "name": "WT",
                "settings": {"secret": "must-not-be-accepted"},
            }
        )
    with pytest.raises(ValidationError):
        ConnectionCreate.model_validate(
            {
                "provider": "worshiptools",
                "name": "WT",
                "settings": {"timezone": "Mars/Olympus_Mons"},
            }
        )
    with pytest.raises(ValidationError):
        ConnectionCreate.model_validate(
            {
                "provider": "churchtools",
                "name": "CT",
                "settings": {"timezone": "Europe/Berlin"},
            }
        )


@pytest.mark.parametrize(
    "field",
    [
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
    ],
)
def test_profile_patch_rejects_explicit_null_for_non_nullable_fields(field):
    with pytest.raises(ValidationError):
        ProfileUpdate.model_validate({field: None})


def test_profile_patch_allows_clearing_nullable_schedule_and_song_fields():
    patch = ProfileUpdate.model_validate(
        {
            "interval_minutes": None,
            "cron_expression": None,
            "song_category_id": None,
        }
    )
    assert patch.model_fields_set == {
        "interval_minutes",
        "cron_expression",
        "song_category_id",
    }


def test_auth_tokens_and_profile_collections_are_resource_bounded():
    with pytest.raises(ValidationError):
        LoginRequest(email="person@example.org", password="x" * 1025)
    with pytest.raises(ValidationError):
        VerifyEmailRequest(token="x" * 513)
    with pytest.raises(ValidationError):
        ProfileCreate.model_validate(
            {
                "source_connection_id": "00000000-0000-0000-0000-000000000001",
                "target_connection_id": "00000000-0000-0000-0000-000000000002",
                "name": "Zu viele Regeln",
                "song_category_id": 1,
                "event_rules": [{} for _ in range(101)],
            }
        )


@pytest.mark.parametrize(
    ("model", "payload"),
    [
        (ProfileNotificationConfig, {"in_app": False}),
        (NotificationPreferenceUpdate, {"in_app_enabled": False}),
    ],
)
def test_in_app_notifications_cannot_be_disabled(model, payload):
    with pytest.raises(ValidationError):
        model.model_validate(payload)


def test_legacy_disabled_in_app_preference_is_normalized_on_output():
    preference = NotificationPreferenceOut.model_validate(
        {
            "in_app_enabled": False,
            "push_enabled": False,
            "email_enabled": True,
            "success_notifications": False,
            "telegram_enabled": False,
        }
    )

    assert preference.in_app_enabled is True


def test_provider_metadata_has_a_closed_typed_contract():
    metadata = ProviderMetadata.model_validate(
        {
            "data": {
                "calendars": [{"id": "2", "name": "Gottesdienst"}],
                "campuses": [],
                "song_categories": [{"id": "7", "name": "Lobpreis"}],
            },
            "retrieved_at": "2026-08-22T12:00:00Z",
        }
    )

    assert metadata.data.song_categories[0].id == "7"
    with pytest.raises(ValidationError):
        ProviderMetadata.model_validate(
            {
                "data": {
                    "calendars": [],
                    "campuses": [],
                    "song_categories": [],
                    "unexpected": [],
                },
                "retrieved_at": "2026-08-22T12:00:00Z",
            }
        )


def test_openapi_documents_problem_media_type_without_conditional_etags(
    settings, database
):
    schema = create_app(settings, database=database).openapi()
    path = "/api/v1/workspaces/{workspace_id}/connections/{connection_id}"
    patch = schema["paths"][path]["patch"]
    assert not any(
        item.get("name", "").casefold() == "if-match"
        for item in patch.get("parameters", [])
    )
    assert "412" not in patch["responses"]
    assert "428" not in patch["responses"]
    assert "application/problem+json" in patch["responses"]["default"]["content"]
    assert "ETag" not in patch["responses"]["200"].get("headers", {})
    assert (
        schema["components"]["schemas"]["ProblemDetails"]["properties"]["errors"]
        ["anyOf"][0]["type"]
        == "array"
    )


def test_openapi_documents_cookie_and_csrf_from_actual_dependencies(
    settings, database
):
    schema = create_app(settings, database=database).openapi()
    schemes = schema["components"]["securitySchemes"]
    assert schemes["SessionCookie"] == {
        "type": "apiKey",
        "in": "cookie",
        "name": settings.session_cookie_name,
        "description": "Sichere HttpOnly-Sitzung",
    }
    assert schemes["CsrfHeader"]["name"] == settings.csrf_header_name
    assert "security" not in schema["paths"]["/api/v1/auth/register"]["post"]
    assert "security" not in schema["paths"]["/api/v1/auth/verification/request"]["post"]
    assert schema["paths"]["/api/v1/workspaces"]["get"]["security"] == [
        {"SessionCookie": []}
    ]
    assert schema["paths"]["/api/v1/workspaces"]["post"]["security"] == [
        {"SessionCookie": [], "CsrfHeader": []}
    ]


def test_openapi_and_locations_follow_custom_api_prefix(settings, database):
    custom = settings.model_copy(update={"api_prefix": "/api/custom"})
    schema = create_app(custom, database=database).openapi()
    assert "/api/custom/auth/register" in schema["paths"]
    assert "/api/v1/auth/register" not in schema["paths"]


def test_runtime_validation_error_matches_documented_problem_contract(
    settings, database
):
    app = create_app(settings, database=database)

    async def request() -> httpx.Response:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="https://testserver"
        ) as client:
            return await client.post(
                "/api/v1/auth/register", json={"unexpected": True}
            )

    response = asyncio.run(request())

    assert response.status_code == 422
    assert response.headers["content-type"].startswith("application/problem+json")
    body = response.json()
    assert body["code"] == "validation_error"
    assert isinstance(body["errors"], list)


def test_unhandled_runtime_error_is_redacted_problem_with_trace_header(
    settings, database, caplog
):
    app = create_app(settings, database=database)

    @app.get("/api/v1/test-unhandled-error")
    def fail() -> None:
        raise RuntimeError("sensitive-internal-detail")

    async def request() -> httpx.Response:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="https://testserver"
        ) as client:
            return await client.get("/api/v1/test-unhandled-error")

    response = asyncio.run(request())

    assert response.status_code == 500
    assert response.headers["content-type"].startswith("application/problem+json")
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-request-id"]
    assert response.json()["code"] == "internal_error"
    assert "sensitive-internal-detail" not in response.text
    assert "sensitive-internal-detail" not in caplog.text
