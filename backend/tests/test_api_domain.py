from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from pydantic import ValidationError
from sqlalchemy import func, select
from starlette.requests import Request

from app.dependencies import WorkspaceAccess, workspace_access
from app.main import create_app
from app.models import (
    Membership,
    NotificationOutbox,
    ProviderConnection,
    RemoteBinding,
    SyncAction,
    SyncActionKind,
    SyncActionStatus,
    SyncRun,
    SyncRunStatus,
    SyncTrigger,
    User,
    Workspace,
    WorkspaceRole,
)
from app.problems import ProblemException
from app.routers.auth import register
from app.routers.connections import (
    create_connection,
    delete_connection,
    list_connections,
    update_connection,
)
from app.routers.profiles import (
    create_profile,
    delete_profile,
    list_profiles,
    update_profile,
)
from app.routers.runs import (
    cancel_run,
    get_run,
    list_run_actions,
    list_runs,
    start_run,
)
from app.runtime import SqlDueRunRepository
from app.routers.workspaces import accept_invitation, invite_member, update_workspace
from app.schemas import (
    ConnectionCreate,
    ConnectionOut,
    ConnectionUpdate,
    EventSelectorConfig,
    InvitationAccept,
    InvitationCreate,
    ProfileCreate,
    ProfileUpdate,
    RegisterRequest,
    RunCreate,
    SyncRunDetail,
    WorkspaceUpdate,
)
from app.security import SecretCipher


def _register(db, settings, email: str, workspace: str):
    result = register(
        RegisterRequest(
            email=email,
            password="correct horse battery staple",
            workspace_name=workspace,
        ),
        settings,
        db,
    )
    user = db.scalar(select(User).where(User.id == result.user.id))
    tenant = db.get(Workspace, result.workspace_id)
    assert user is not None and tenant is not None
    return user, tenant


def _access(user: User, workspace: Workspace) -> WorkspaceAccess:
    return WorkspaceAccess(workspace=workspace, user=user, role=WorkspaceRole.OWNER)


def _connections(db, settings, access):
    source = create_connection(
        ConnectionCreate(
            provider="worshiptools",
            name="WorshipTools",
            credentials={
                "email": "sync@example.org",
                "password": "upstream-secret",
                "account_id": "tenant-one",
            },
        ),
        access,
        settings,
        db,
        None,
    )
    target = create_connection(
        ConnectionCreate(
            provider="churchtools",
            name="ChurchTools",
            base_url="https://example.church.tools",
            credentials={"token": "Login private-token"},
        ),
        access,
        settings,
        db,
        None,
    )
    return source, target


def test_tenant_boundary_hides_foreign_workspace(db, settings):
    first_user, first_workspace = _register(db, settings, "one@example.org", "One")
    second_user, _ = _register(db, settings, "two@example.org", "Two")

    assert workspace_access(db, first_user, first_workspace.id).workspace.id == first_workspace.id
    with pytest.raises(ProblemException) as error:
        workspace_access(db, second_user, first_workspace.id)
    assert error.value.status == 404

    second_user.is_platform_admin = True
    db.commit()
    with pytest.raises(ProblemException) as admin_error:
        workspace_access(db, second_user, first_workspace.id)
    assert admin_error.value.status == 404


def test_connection_response_never_contains_credentials(db, settings):
    user, workspace = _register(db, settings, "owner@example.org", "Safe")
    source, target = _connections(db, settings, _access(user, workspace))

    serialized = ConnectionOut.model_validate(source).model_dump_json()
    assert "upstream-secret" not in serialized
    assert "credentials_encrypted" not in serialized
    assert source.credentials_encrypted is not None
    assert "upstream-secret" not in source.credentials_encrypted
    assert target.base_url == "https://example.church.tools"

    with pytest.raises(ProblemException) as error:
        create_connection(
            ConnectionCreate(
                provider="churchtools",
                name="Evil",
                base_url="https://attacker.example",
                credentials={"token": "secret"},
            ),
            _access(user, workspace),
            settings,
            db,
            None,
        )
    assert error.value.code == "churchtools_host_not_allowed"


def test_worshiptools_base_url_is_rejected_and_hidden_from_output(db, settings):
    user, workspace = _register(db, settings, "wt-url@example.org", "WT URL")
    access = _access(user, workspace)

    with pytest.raises(ProblemException) as error:
        create_connection(
            ConnectionCreate(
                provider="worshiptools",
                name="WorshipTools",
                base_url="https://worshiptools.com",
                credentials={
                    "email": "sync@example.org",
                    "password": "upstream-secret",
                    "account_id": "tenant-one",
                },
            ),
            access,
            settings,
            db,
            None,
        )
    assert error.value.code == "worshiptools_base_url_not_configurable"
    db.rollback()

    source, _target = _connections(db, settings, access)
    source.base_url = "https://legacy-worshiptools.example"
    serialized = ConnectionOut.model_validate(source).model_dump()
    assert "base_url" not in serialized


def test_provider_settings_are_stored_canonically_and_output_drops_legacy_keys(
    db, settings
):
    user, workspace = _register(db, settings, "settings@example.org", "Settings")
    access = _access(user, workspace)
    source = create_connection(
        ConnectionCreate(
            provider="worshiptools",
            name="WorshipTools",
            settings={"timezone": "Europe/Berlin"},
            credentials={
                "email": "sync@example.org",
                "password": "upstream-secret",
                "account_id": "tenant-one",
            },
        ),
        access,
        settings,
        db,
        None,
    )
    assert source.settings_json == {"timezone": "Europe/Berlin"}

    source.settings_json = {
        "timezone": "Europe/Berlin",
        "legacy_secret": "must-never-leave-the-api",
    }
    db.commit()
    output = ConnectionOut.model_validate(source).model_dump()
    assert output["settings"] == {"timezone": "Europe/Berlin"}
    assert "legacy_secret" not in str(output)


def test_connection_quota_is_enforced_under_workspace_lock(db, settings):
    settings = settings.model_copy(update={"max_connections_per_workspace": 2})
    user, workspace = _register(db, settings, "connection-quota@example.org", "Quota")
    access = _access(user, workspace)
    _connections(db, settings, access)

    with pytest.raises(ProblemException) as error:
        create_connection(
            ConnectionCreate(
                provider="churchtools",
                name="Third connection",
                base_url="https://third.church.tools",
                credentials={"token": "third-token"},
            ),
            access,
            settings,
            db,
            None,
        )

    assert error.value.status == 409
    assert error.value.code == "connection_quota_exceeded"
    assert (
        db.scalar(
            select(func.count())
            .select_from(ProviderConnection)
            .where(ProviderConnection.workspace_id == workspace.id)
        )
        == 2
    )


def test_unreferenced_connection_credential_patch_merges_without_secret_loss(
    db, settings
):
    user, workspace = _register(db, settings, "merge@example.org", "Merge")
    access = _access(user, workspace)
    source, _target = _connections(db, settings, access)

    updated = update_connection(
        source.id,
        ConnectionUpdate(credentials={"password": "rotated-password"}),
        access,
        settings,
        db,
        None,
    )
    credentials = SecretCipher(settings).decrypt_json(
        updated.credentials_encrypted,
        context=f"connection:{updated.id}",
    )
    assert credentials == {
        "email": "sync@example.org",
        "password": "rotated-password",
        "account_id": "tenant-one",
    }

    updated = update_connection(
        source.id,
        ConnectionUpdate(credentials={"email": "new-sync@example.org"}),
        access,
        settings,
        db,
        None,
    )
    credentials = SecretCipher(settings).decrypt_json(
        updated.credentials_encrypted,
        context=f"connection:{updated.id}",
    )
    assert credentials == {
        "email": "new-sync@example.org",
        "password": "rotated-password",
        "account_id": "tenant-one",
    }


def test_empty_or_incomplete_credential_update_is_rejected(db, settings):
    user, workspace = _register(db, settings, "empty-secret@example.org", "Secrets")
    access = _access(user, workspace)
    source = create_connection(
        ConnectionCreate(provider="worshiptools", name="Unconfigured"),
        access,
        settings,
        db,
        None,
    )

    with pytest.raises(ProblemException) as empty:
        update_connection(
            source.id,
            ConnectionUpdate(credentials={}),
            access,
            settings,
            db,
            None,
        )
    assert empty.value.code == "empty_credentials_update"
    db.rollback()

    with pytest.raises(ProblemException) as incomplete:
        update_connection(
            source.id,
            ConnectionUpdate(credentials={"password": "new-password"}),
            access,
            settings,
            db,
            None,
        )
    assert incomplete.value.code == "invalid_connection_credentials"


def test_provider_credentials_have_bounded_values_and_preserve_password_whitespace(
    db, settings
):
    user, workspace = _register(db, settings, "bounded-secret@example.org", "Bounds")
    access = _access(user, workspace)

    source = create_connection(
        ConnectionCreate(
            provider="worshiptools",
            name="Whitespace",
            credentials={
                "email": "sync@example.org",
                "password": "  significant password  ",
                "account_id": "tenant-one",
            },
        ),
        access,
        settings,
        db,
        None,
    )
    decrypted = SecretCipher(settings).decrypt_json(
        source.credentials_encrypted,
        context=f"connection:{source.id}",
    )
    assert decrypted["password"] == "  significant password  "

    with pytest.raises(ProblemException) as oversized:
        create_connection(
            ConnectionCreate(
                provider="churchtools",
                name="Oversized",
                base_url="https://example.church.tools",
                credentials={"token": "x" * 4097},
            ),
            access,
            settings,
            db,
            None,
        )
    assert oversized.value.code == "invalid_connection_credentials"


def test_profile_updates_increment_revision_without_conditional_header(db, settings):
    user, workspace = _register(db, settings, "profile@example.org", "Profile")
    access = _access(user, workspace)
    source, target = _connections(db, settings, access)
    profile = create_profile(
        ProfileCreate(
            source_connection_id=source.id,
            target_connection_id=target.id,
            name="Main",
            match_mode="exact_time",
            song_category_id=4,
            agenda_item_defaults={"duration": 300},
        ),
        access,
        db,
        None,
    )
    assert profile.revision == 1

    updated = update_profile(
        profile.id,
        ProfileUpdate(name="Changed"),
        access,
        db,
        None,
    )
    assert updated.name == "Changed"
    assert updated.revision == 2

    updated = update_profile(
        profile.id,
        ProfileUpdate(name="Latest"),
        access,
        db,
        None,
    )
    assert updated.name == "Latest"
    assert updated.revision == 3


@pytest.mark.parametrize("legacy_field", ["calendar_id", "campus_name"])
def test_event_selector_rejects_deprecated_singular_fields(legacy_field):
    with pytest.raises(ValidationError):
        EventSelectorConfig.model_validate(
            {
                legacy_field: "hidden-filter",
                "calendar_ids": [],
                "campus_ids": [],
            }
        )

    selector = EventSelectorConfig.model_validate(
        {
            "calendar_ids": [" calendar-1 ", "calendar-1"],
            "campus_ids": [" campus-1 "],
        }
    )
    assert selector.calendar_ids == ["calendar-1"]
    assert selector.campus_ids == ["campus-1"]


def test_profile_connections_can_only_change_before_remote_ownership(db, settings):
    user, workspace = _register(
        db, settings, "profile-connections@example.org", "Connection mapping"
    )
    access = _access(user, workspace)
    source, target = _connections(db, settings, access)
    other_source = create_connection(
        ConnectionCreate(
            provider="worshiptools",
            name="WorshipTools 2",
            credentials={
                "email": "sync-two@example.org",
                "password": "another-upstream-secret",
                "account_id": "tenant-two",
            },
        ),
        access,
        settings,
        db,
        None,
    )
    other_target = create_connection(
        ConnectionCreate(
            provider="churchtools",
            name="ChurchTools 2",
            base_url="https://other.church.tools",
            credentials={"token": "Login another-private-token"},
        ),
        access,
        settings,
        db,
        None,
    )
    profile = create_profile(
        ProfileCreate(
            source_connection_id=source.id,
            target_connection_id=target.id,
            name="Main",
            song_category_id=4,
        ),
        access,
        db,
        None,
    )

    updated = update_profile(
        profile.id,
        ProfileUpdate(
            source_connection_id=other_source.id,
            target_connection_id=other_target.id,
        ),
        access,
        db,
        None,
    )
    assert updated.source_connection_id == other_source.id
    assert updated.target_connection_id == other_target.id
    assert updated.revision == 2

    db.add(
        RemoteBinding(
            workspace_id=workspace.id,
            profile_id=profile.id,
            target_connection_id=other_target.id,
            target_event_id="event-1",
            agenda_item_id="item-1",
            source_key="song-1",
            placement_id="songs",
            fingerprint_json={"song_id": "song-1"},
        )
    )
    db.commit()

    with pytest.raises(ProblemException) as error:
        update_profile(
            profile.id,
            ProfileUpdate(
                source_connection_id=source.id,
                target_connection_id=target.id,
            ),
            access,
            db,
            None,
        )
    assert error.value.code == "profile_has_remote_bindings"


def test_cron_profile_rejects_sub_30_minute_schedule(db, settings):
    user, workspace = _register(db, settings, "cron@example.org", "Cron")
    access = _access(user, workspace)
    source, target = _connections(db, settings, access)
    profile = create_profile(
        ProfileCreate(
            source_connection_id=source.id,
            target_connection_id=target.id,
            name="Cron profile",
            song_category_id=4,
        ),
        access,
        db,
        None,
    )

    with pytest.raises(ProblemException) as error:
        update_profile(
            profile.id,
            ProfileUpdate(schedule_type="cron", cron_expression="* * * * *"),
            access,
            db,
            None,
        )
    assert error.value.code == "invalid_schedule"
    db.rollback()
    db.refresh(profile)

    updated = update_profile(
        profile.id,
        ProfileUpdate(schedule_type="cron", cron_expression="*/30 * * * *"),
        access,
        db,
        None,
    )
    assert updated.cron_expression == "*/30 * * * *"


class _Dispatcher:
    def __init__(self):
        self.ids = []

    def enqueue(self, run_id):
        self.ids.append(run_id)


class _FailingDispatcher:
    def enqueue(self, run_id):
        raise RuntimeError("broker unavailable")


def test_manual_run_is_persisted_before_dispatch_and_deduplicated(db, settings):
    user, workspace = _register(db, settings, "run@example.org", "Runs")
    access = _access(user, workspace)
    source, target = _connections(db, settings, access)
    profile = create_profile(
        ProfileCreate(
            source_connection_id=source.id,
            target_connection_id=target.id,
            name="Main",
            song_category_id=4,
        ),
        access,
        db,
        None,
    )
    dispatcher = _Dispatcher()
    app = SimpleNamespace(state=SimpleNamespace(run_dispatcher=dispatcher))
    request = Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/",
            "headers": [],
            "query_string": b"",
            "scheme": "http",
            "server": ("test", 80),
            "client": ("127.0.0.1", 1234),
            "app": app,
        }
    )
    run = start_run(
        profile.id, RunCreate(dry_run=True), request, access, settings, db, None
    )
    assert run.status == SyncRunStatus.QUEUED
    assert dispatcher.ids == [run.id]
    stored = db.get(type(run), run.id)
    assert stored is not None
    assert stored.dispatch_attempted_at is not None

    with pytest.raises(ProblemException) as error:
        start_run(
            profile.id, RunCreate(dry_run=False), request, access, settings, db, None
        )
    assert error.value.code == "run_already_active"

    # A completed onboarding preview must not consume the cooldown of the
    # first real manual sync.
    stored.status = SyncRunStatus.SUCCEEDED
    stored.finished_at = datetime.now(timezone.utc)
    db.commit()
    actual = start_run(
        profile.id, RunCreate(dry_run=False), request, access, settings, db, None
    )
    assert actual.status == SyncRunStatus.QUEUED
    assert actual.dispatch_attempted_at is not None
    assert dispatcher.ids == [run.id, actual.id]


def test_run_summary_omits_plan_and_actions_are_bounded_and_paginated(db, settings):
    user, workspace = _register(db, settings, "run-page@example.org", "Run page")
    access = _access(user, workspace)
    source, target = _connections(db, settings, access)
    profile = create_profile(
        ProfileCreate(
            source_connection_id=source.id,
            target_connection_id=target.id,
            name="Paged",
            song_category_id=4,
        ),
        access,
        db,
        None,
    )
    run = SyncRun(
        workspace_id=workspace.id,
        profile_id=profile.id,
        config_revision=profile.revision,
        status=SyncRunStatus.SUCCEEDED,
        trigger=SyncTrigger.MANUAL,
        plan_json={"persisted": True},
    )
    db.add(run)
    db.flush()
    db.add_all(
        [
            SyncAction(
                run_id=run.id,
                event_id=f"event-{index}",
                kind=SyncActionKind.NOOP,
                status=(
                    SyncActionStatus.VERIFIED
                    if index % 2 == 0
                    else SyncActionStatus.SKIPPED
                ),
                ordinal=index,
                payload_json={"index": index},
            )
            for index in range(205)
        ]
    )
    db.commit()

    summaries = list_runs(access, db, None, None, 50, 0)
    assert summaries.total == 1
    assert "plan" not in summaries.items[0].model_dump()
    first = list_run_actions(run.id, access, db, 200, 0)
    second = list_run_actions(run.id, access, db, 200, 200)
    assert first.total == second.total == 205
    assert len(first.items) == 200
    assert [item.ordinal for item in second.items] == [200, 201, 202, 203, 204]
    assert first.status_counts.verified == 103
    assert first.status_counts.skipped == 102
    detail = SyncRunDetail.model_validate(get_run(run.id, access, db))
    assert detail.plan == {"persisted": True}


def test_failed_immediate_dispatch_stays_queued_and_obeys_redelivery_backoff(
    database, db, settings
):
    user, workspace = _register(db, settings, "queue@example.org", "Queue")
    access = _access(user, workspace)
    source, target = _connections(db, settings, access)
    profile = create_profile(
        ProfileCreate(
            source_connection_id=source.id,
            target_connection_id=target.id,
            name="Queue recovery",
            song_category_id=4,
        ),
        access,
        db,
        None,
    )
    app = SimpleNamespace(state=SimpleNamespace(run_dispatcher=_FailingDispatcher()))
    request = Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/",
            "headers": [],
            "query_string": b"",
            "scheme": "http",
            "server": ("test", 80),
            "client": ("127.0.0.1", 1234),
            "app": app,
        }
    )

    with pytest.raises(ProblemException) as error:
        start_run(
            profile.id, RunCreate(dry_run=False), request, access, settings, db, None
        )
    assert error.value.code == "queue_unavailable"

    stored = db.scalar(
        select(SyncRun).where(SyncRun.profile_id == profile.id)
    )
    assert stored is not None
    assert stored.status == SyncRunStatus.QUEUED
    assert stored.dispatch_attempted_at is not None
    attempted_at = stored.dispatch_attempted_at
    if attempted_at.tzinfo is None:
        attempted_at = attempted_at.replace(tzinfo=timezone.utc)
    stored_id = str(stored.id)
    # End the fixture session's read transaction before the repository opens
    # its worker-scoped transaction on SQLite's shared connection.
    db.commit()

    repository = SqlDueRunRepository(database, redelivery_seconds=300)
    immediate = set(
        asyncio.run(repository.create_due_runs(attempted_at + timedelta(seconds=15), 100))
    )
    assert stored_id not in immediate
    after_timeout = set(
        asyncio.run(repository.create_due_runs(attempted_at + timedelta(seconds=301), 100))
    )
    assert stored_id in after_timeout


def test_openapi_exposes_versioned_contract_without_secret_response_fields(settings):
    document = create_app(settings).openapi()
    paths = document["paths"]
    assert "/api/v1/auth/register" in paths
    assert "/api/v1/workspaces/{workspace_id}/profiles/{profile_id}/preview" in paths
    connection_schema = document["components"]["schemas"]["ConnectionOut"]
    assert "credentials" not in connection_schema["properties"]
    assert "credentials_encrypted" not in connection_schema["properties"]
    profile_schema = document["components"]["schemas"]["ProfileCreate"]
    placement_schema = document["components"]["schemas"]["PlacementConfig"]
    assert profile_schema["properties"]["placements"]["items"]["$ref"].endswith(
        "/PlacementConfig"
    )
    assert {"id", "anchor", "relation", "song_start", "song_end"}.issubset(
        placement_schema["properties"]
    )
    assert "minimum" not in placement_schema["properties"]["song_start"]
    assert "minimum" not in placement_schema["properties"]["song_end"]["anyOf"][0]
    assert "negative Werte" in placement_schema["properties"]["song_start"]["description"]
    assert "negative Werte" in placement_schema["properties"]["song_end"]["description"]
    selector_schema = document["components"]["schemas"]["EventSelectorConfig"]
    assert selector_schema["additionalProperties"] is False
    assert "calendar_id" not in selector_schema["properties"]
    assert "campus_name" not in selector_schema["properties"]
    assert {"calendar_ids", "campus_ids"}.issubset(selector_schema["properties"])


def test_invitation_is_tenant_scoped_and_delivered_through_encrypted_outbox(
    db, settings
):
    settings = settings.model_copy(update={"expose_development_tokens": True})
    owner, workspace = _register(db, settings, "invite-owner@example.org", "Invites")
    invited, _ = _register(db, settings, "invited@example.org", "Personal")

    invitation = invite_member(
        InvitationCreate(email=invited.email, role=WorkspaceRole.OPERATOR),
        _access(owner, workspace),
        settings,
        db,
        None,
    )
    assert invitation.development_invitation_token
    outbox = db.scalar(select(NotificationOutbox))
    assert outbox is not None
    assert invitation.development_invitation_token not in outbox.payload_encrypted
    payload = SecretCipher(settings).decrypt_json(
        outbox.payload_encrypted, context=f"outbox:{outbox.id}"
    )
    assert payload["subject"] == "Einladung zu Invites"
    assert invitation.development_invitation_token in payload["text"]
    assert "/invite?token=" in payload["text"]
    assert payload["html"]

    accepted = accept_invitation(
        InvitationAccept(token=invitation.development_invitation_token),
        invited,
        settings,
        db,
        None,
    )
    assert accepted.id == workspace.id
    membership = db.scalar(
        select(Membership).where(
            Membership.workspace_id == workspace.id,
            Membership.user_id == invited.id,
        )
    )
    assert membership is not None
    assert membership.role == WorkspaceRole.OPERATOR


def test_archiving_workspace_disables_profiles_and_cancels_active_runs(db, settings):
    owner, workspace = _register(db, settings, "archive@example.org", "Archive")
    access = _access(owner, workspace)
    source, target = _connections(db, settings, access)
    profile = create_profile(
        ProfileCreate(
            source_connection_id=source.id,
            target_connection_id=target.id,
            name="Enabled",
            enabled=True,
            song_category_id=4,
        ),
        access,
        db,
        None,
    )
    run = SyncRun(
        workspace_id=workspace.id,
        profile_id=profile.id,
        config_revision=profile.revision,
        status=SyncRunStatus.RUNNING,
        trigger=SyncTrigger.MANUAL,
    )
    db.add(run)
    db.commit()

    with pytest.raises(ProblemException) as running_error:
        update_workspace(
            WorkspaceUpdate(archived=True), access, settings, db, None
        )
    assert running_error.value.code == "workspace_has_running_sync"
    assert running_error.value.headers["Location"].endswith(f"/runs/{run.id}")
    db.rollback()
    db.expire_all()
    assert db.get(Workspace, workspace.id).archived_at is None
    assert db.get(type(profile), profile.id).enabled
    assert db.get(SyncRun, run.id).status == SyncRunStatus.RUNNING

    run = db.get(SyncRun, run.id)
    run.status = SyncRunStatus.SUCCEEDED
    run.finished_at = datetime.now(timezone.utc)
    queued = SyncRun(
        workspace_id=workspace.id,
        profile_id=profile.id,
        config_revision=profile.revision,
        status=SyncRunStatus.QUEUED,
        trigger=SyncTrigger.SCHEDULED,
    )
    db.add(queued)
    db.commit()

    archived = update_workspace(
        WorkspaceUpdate(archived=True), access, settings, db, None
    )

    db.expire_all()
    assert archived.archived_at is not None
    assert not db.get(type(profile), profile.id).enabled
    stored_run = db.get(SyncRun, queued.id)
    assert stored_run.status == SyncRunStatus.CANCELED
    assert stored_run.finished_at is not None
    assert stored_run.error_json["code"] == "workspace_archived"
    assert db.get(SyncRun, run.id).status == SyncRunStatus.SUCCEEDED

    with pytest.raises(ProblemException) as activation_error:
        update_profile(
            profile.id,
            ProfileUpdate(enabled=True),
            access,
            db,
            None,
        )
    assert activation_error.value.code == "workspace_archived"
    db.rollback()

    update_workspace(
        WorkspaceUpdate(archived=False), access, settings, db, None
    )
    db.expire_all()
    assert db.get(type(profile), profile.id).enabled is False


def test_connection_delete_returns_conflict_while_profile_references_it(db, settings):
    owner, workspace = _register(db, settings, "delete@example.org", "Delete")
    access = _access(owner, workspace)
    source, target = _connections(db, settings, access)
    create_profile(
        ProfileCreate(
            source_connection_id=source.id,
            target_connection_id=target.id,
            name="Reference",
            song_category_id=4,
        ),
        access,
        db,
        None,
    )

    listed = list_connections(access, db, None, 50, 0)
    assert next(item for item in listed.items if item.id == source.id).delete_blockers == [
        "profile_reference"
    ]

    with pytest.raises(ProblemException) as error:
        delete_connection(source.id, access, db, None)

    assert error.value.status == 409
    assert error.value.code == "connection_in_use"


def test_connection_delete_does_not_require_conditional_header(db, settings):
    owner, workspace = _register(db, settings, "delete-fence@example.org", "Fence")
    access = _access(owner, workspace)
    source, _target = _connections(db, settings, access)

    delete_connection(source.id, access, db, None)
    assert db.get(ProviderConnection, source.id) is None


def test_connection_patch_increments_revision_and_is_immutable_after_profile_use(db, settings):
    owner, workspace = _register(db, settings, "patch@example.org", "Patch")
    access = _access(owner, workspace)
    source, target = _connections(db, settings, access)

    updated = update_connection(
        source.id,
        ConnectionUpdate(name="Renamed"),
        access,
        settings,
        db,
        None,
    )
    assert updated.revision == 2

    create_profile(
        ProfileCreate(
            source_connection_id=source.id,
            target_connection_id=target.id,
            name="Uses connection",
            song_category_id=4,
        ),
        access,
        db,
        None,
    )
    rotated_source = update_connection(
        source.id,
        ConnectionUpdate(credentials={"password": "rotated-password"}),
        access,
        settings,
        db,
        None,
    )
    assert rotated_source.revision == 3
    source_credentials = SecretCipher(settings).decrypt_json(
        rotated_source.credentials_encrypted,
        context=f"connection:{rotated_source.id}",
    )
    assert source_credentials == {
        "email": "sync@example.org",
        "password": "rotated-password",
        "account_id": "tenant-one",
    }
    renamed_source = update_connection(
        source.id,
        ConnectionUpdate(name="Referenced rename"),
        access,
        settings,
        db,
        None,
    )
    assert renamed_source.revision == 4

    with pytest.raises(ProblemException) as account_change:
        update_connection(
            source.id,
            ConnectionUpdate(
                credentials={
                    "account_id": "another-tenant",
                    "password": "rotated-again",
                }
            ),
            access,
            settings,
            db,
            None,
        )
    assert account_change.value.code == "connection_identity_immutable"
    db.rollback()

    rotated_target = update_connection(
        target.id,
        ConnectionUpdate(credentials={"token": "Login rotated-token"}),
        access,
        settings,
        db,
        None,
    )
    assert rotated_target.revision == 2
    target_credentials = SecretCipher(settings).decrypt_json(
        rotated_target.credentials_encrypted,
        context=f"connection:{rotated_target.id}",
    )
    assert target_credentials == {"token": "Login rotated-token"}
    with pytest.raises(ProblemException) as base_url_change:
        update_connection(
            target.id,
            ConnectionUpdate(base_url="https://other.church.tools"),
            access,
            settings,
            db,
            None,
        )
    assert base_url_change.value.code == "connection_identity_immutable"


def test_profile_delete_preserves_terminal_run_history(db, settings):
    owner, workspace = _register(db, settings, "history@example.org", "History")
    access = _access(owner, workspace)
    source, target = _connections(db, settings, access)
    profile = create_profile(
        ProfileCreate(
            source_connection_id=source.id,
            target_connection_id=target.id,
            name="History profile",
            song_category_id=4,
        ),
        access,
        db,
        None,
    )
    run = SyncRun(
        workspace_id=workspace.id,
        profile_id=profile.id,
        config_revision=profile.revision,
        status=SyncRunStatus.SUCCEEDED,
        trigger=SyncTrigger.MANUAL,
    )
    db.add(run)
    db.commit()

    listed = list_profiles(access, db, 50, 0)
    assert next(item for item in listed.items if item.id == profile.id).delete_blockers == [
        "run_history"
    ]

    with pytest.raises(ProblemException) as error:
        delete_profile(profile.id, access, db, None)

    assert error.value.status == 409
    assert error.value.code == "profile_has_run_history"
    assert db.get(SyncRun, run.id) is not None


def test_cancel_run_only_transitions_a_locked_queued_run(db, settings):
    owner, workspace = _register(db, settings, "cancel@example.org", "Cancel")
    access = _access(owner, workspace)
    source, target = _connections(db, settings, access)
    profile = create_profile(
        ProfileCreate(
            source_connection_id=source.id,
            target_connection_id=target.id,
            name="Cancelable",
            song_category_id=4,
        ),
        access,
        db,
        None,
    )
    queued = SyncRun(
        workspace_id=workspace.id,
        profile_id=profile.id,
        config_revision=profile.revision,
        status=SyncRunStatus.QUEUED,
        trigger=SyncTrigger.MANUAL,
    )
    db.add(queued)
    db.commit()

    canceled = cancel_run(queued.id, access, db, None)
    assert canceled.status == SyncRunStatus.CANCELED
    assert canceled.finished_at is not None

    running = SyncRun(
        workspace_id=workspace.id,
        profile_id=profile.id,
        config_revision=profile.revision,
        status=SyncRunStatus.RUNNING,
        trigger=SyncTrigger.MANUAL,
    )
    db.add(running)
    db.commit()
    with pytest.raises(ProblemException) as error:
        cancel_run(running.id, access, db, None)
    assert error.value.code == "run_not_cancelable"


def test_agenda_item_defaults_patch_merges_and_explicit_null_resets(db, settings):
    owner, workspace = _register(
        db, settings, "agenda-defaults@example.org", "Agenda defaults"
    )
    access = _access(owner, workspace)
    source, target = _connections(db, settings, access)
    profile = create_profile(
        ProfileCreate(
            source_connection_id=source.id,
            target_connection_id=target.id,
            name="Agenda defaults",
            song_category_id=4,
            agenda_item_defaults={
                "title": "Lobpreis",
                "note": "Bleibt erhalten",
                "responsible": "Team",
                "duration": 300,
            },
        ),
        access,
        db,
        None,
    )

    updated = update_profile(
        profile.id,
        ProfileUpdate(
            agenda_item_defaults={"title": None, "duration": 600}
        ),
        access,
        db,
        None,
    )
    assert updated.agenda_item_defaults == {
        "note": "Bleibt erhalten",
        "responsible": "Team",
        "duration": 600,
    }

    reset = update_profile(
        profile.id,
        ProfileUpdate(agenda_item_defaults=None),
        access,
        db,
        None,
    )
    assert reset.agenda_item_defaults == {}
