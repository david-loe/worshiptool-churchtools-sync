#!/usr/bin/env python3
"""Executable PostgreSQL 18 role/RLS boundary test.

The database must already be migrated to ``head``. CI provisions the roles
with ``deploy/postgres/001-create-app-role.sh`` and supplies four independent
DSNs through ``PGTEST_{OWNER,API,WORKER,ADMIN}_URL``.
"""

from __future__ import annotations

import os
import uuid
from dataclasses import dataclass

import psycopg
from psycopg.errors import InsufficientPrivilege
from sqlalchemy import create_engine, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from app.config import Settings
from app.dependencies import WorkspaceAccess, set_request_user_context
from app.models import Membership, NotificationOutbox, User, Workspace, WorkspaceRole
from app.outbox import enqueue_email
from app.problems import ProblemException
from app.routers.auth import register, request_recovery
from app.routers.workspaces import update_member_role
from app.schemas import MemberRoleUpdate, RecoveryRequest, RegisterRequest


@dataclass(frozen=True)
class FixtureIds:
    user_a: uuid.UUID
    user_b: uuid.UUID
    workspace_a: uuid.UUID
    workspace_b: uuid.UUID
    membership_a: uuid.UUID
    membership_b: uuid.UUID
    notification_a: uuid.UUID
    notification_b: uuid.UUID
    outbox_a: uuid.UUID
    outbox_b: uuid.UUID
    outbox_system: uuid.UUID
    outbox_system_api: uuid.UUID
    audit_a: uuid.UUID
    audit_b: uuid.UUID
    audit_system: uuid.UUID
    audit_admin: uuid.UUID
    audit_admin_system: uuid.UUID
    workspace_created: uuid.UUID
    membership_created: uuid.UUID
    membership_injected_cross: uuid.UUID
    membership_injected_local: uuid.UUID
    invitation_a_to_b: uuid.UUID
    membership_invited: uuid.UUID
    user_without_membership: uuid.UUID


def _ids() -> FixtureIds:
    return FixtureIds(*(uuid.uuid4() for _ in range(24)))


def _dsn(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"{name} must be set")
    return value


def _connect(dsn: str) -> psycopg.Connection:
    return psycopg.connect(dsn, autocommit=True)


def _test_settings(api_url: str) -> Settings:
    sqlalchemy_url = api_url.replace("postgresql://", "postgresql+psycopg://", 1)
    return Settings(
        environment="test",
        database_url=sqlalchemy_url,
        application_secret="role-test-application-secret-value-123456",
        encryption_secret="role-test-encryption-secret-value-1234567",
        expose_development_tokens=True,
        public_base_url="https://sync.example.org",
    )


def _assert_rejected_credentials() -> None:
    rejected_urls = [
        value.strip()
        for value in os.environ.get("PGTEST_REJECTED_URLS", "").split(",")
        if value.strip()
    ]
    for rejected_url in rejected_urls:
        try:
            connection = _connect(rejected_url)
        except psycopg.OperationalError:
            continue
        connection.close()
        raise AssertionError("A rotated database credential still authenticates")


def _seed(owner_url: str, ids: FixtureIds) -> None:
    suffix = ids.workspace_a.hex[:12]
    with _connect(owner_url) as connection, connection.transaction():
        connection.execute(
            """
            INSERT INTO users (
              id, email, normalized_email, password_hash, email_verified_at,
              is_active, is_platform_admin, totp_recovery_hashes,
              created_at, updated_at
            ) VALUES
              (%s, %s, %s, 'not-a-login-hash', CURRENT_TIMESTAMP,
               true, false, '[]'::json, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP),
              (%s, %s, %s, 'not-a-login-hash', CURRENT_TIMESTAMP,
               true, false, '[]'::json, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP),
              (%s, %s, %s, 'not-a-login-hash', CURRENT_TIMESTAMP,
               true, true, '[]'::json, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
            """,
            (
                ids.user_a,
                f"role-a-{suffix}@example.org",
                f"role-a-{suffix}@example.org",
                ids.user_b,
                f"role-b-{suffix}@example.org",
                f"role-b-{suffix}@example.org",
                ids.user_without_membership,
                f"role-admin-{suffix}@example.org",
                f"role-admin-{suffix}@example.org",
            ),
        )
        connection.execute(
            """
            INSERT INTO workspaces (
              id, name, slug, profile_quota, member_quota, created_at, updated_at
            ) VALUES
              (%s, 'RLS tenant A', %s, 3, 10, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP),
              (%s, 'RLS tenant B', %s, 3, 10, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
            """,
            (
                ids.workspace_a,
                f"rls-a-{suffix}",
                ids.workspace_b,
                f"rls-b-{suffix}",
            ),
        )
        connection.execute(
            """
            INSERT INTO memberships (
              id, workspace_id, user_id, role, created_at, updated_at
            ) VALUES
              (%s, %s, %s, 'viewer', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP),
              (%s, %s, %s, 'owner', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
            """,
            (
                ids.membership_a,
                ids.workspace_a,
                ids.user_a,
                ids.membership_b,
                ids.workspace_b,
                ids.user_b,
            ),
        )
        connection.execute(
            """
            INSERT INTO notifications (
              id, workspace_id, user_id, severity, category, title, body,
              data_json, created_at
            ) VALUES
              (%s, %s, %s, 'info', 'rls-test', 'tenant A', 'tenant A',
               '{}'::json, CURRENT_TIMESTAMP),
              (%s, %s, %s, 'info', 'rls-test', 'tenant B', 'tenant B',
               '{}'::json, CURRENT_TIMESTAMP)
            """,
            (
                ids.notification_a,
                ids.workspace_a,
                ids.user_a,
                ids.notification_b,
                ids.workspace_b,
                ids.user_b,
            ),
        )
        connection.execute(
            """
            INSERT INTO notification_outbox (
              id, workspace_id, channel, recipient, payload_encrypted,
              idempotency_key, status, attempts, created_at, next_attempt_at
            ) VALUES
              (%s, %s, 'email', 'a@example.invalid', 'opaque', %s,
               'pending', 0, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP),
              (%s, %s, 'email', 'b@example.invalid', 'opaque', %s,
               'pending', 0, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP),
              (%s, NULL, 'email', 'system@example.invalid', 'opaque', %s,
               'pending', 0, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
            """,
            (
                ids.outbox_a,
                ids.workspace_a,
                f"rls-a-{suffix}",
                ids.outbox_b,
                ids.workspace_b,
                f"rls-b-{suffix}",
                ids.outbox_system,
                f"rls-system-{suffix}",
            ),
        )
        connection.execute(
            """
            INSERT INTO audit_events (
              id, workspace_id, action, metadata_json, created_at
            ) VALUES
              (%s, %s, 'rls_test_a', '{}'::json, CURRENT_TIMESTAMP),
              (%s, %s, 'rls_test_b', '{}'::json, CURRENT_TIMESTAMP),
              (%s, NULL, 'rls_test_system', '{}'::json, CURRENT_TIMESTAMP)
            """,
            (
                ids.audit_a,
                ids.workspace_a,
                ids.audit_b,
                ids.workspace_b,
                ids.audit_system,
            ),
        )


def _assert_role_attributes(owner_url: str) -> None:
    with _connect(owner_url) as connection:
        rows = connection.execute(
            """
            SELECT rolname, rolsuper, rolcreaterole, rolcreatedb, rolbypassrls
            FROM pg_roles
            WHERE rolname IN (
              'worshipsync_api', 'worshipsync_worker', 'worshipsync_admin'
            )
            ORDER BY rolname
            """
        ).fetchall()
    assert len(rows) == 3, rows
    assert all(not any(attributes) for _name, *attributes in rows), rows


def _assert_api_boundary(api_url: str, ids: FixtureIds) -> None:
    with _connect(api_url) as connection, connection.transaction():
        assert connection.execute("SELECT current_user").fetchone()[0] == "worshipsync_api"
        connection.execute(
            "SELECT set_config('app.user_id', %s, true)", (str(ids.user_a),)
        )
        # These custom settings remain syntactically settable for compatibility,
        # but revision 0008 no longer consumes them as authorization input.
        connection.execute("SELECT set_config('app.worker', '1', true)")
        connection.execute("SELECT set_config('app.platform_admin', '1', true)")
        assert connection.execute(
            "SELECT app_workspace_access(%s)", (ids.workspace_b,)
        ).fetchone()[0] is False

        workspace_ids = {
            row[0]
            for row in connection.execute(
                "SELECT id FROM workspaces WHERE id IN (%s, %s)",
                (ids.workspace_a, ids.workspace_b),
            ).fetchall()
        }
        assert workspace_ids == {ids.workspace_a}, workspace_ids
        assert connection.execute(
            "UPDATE workspaces SET name = 'cross-tenant' WHERE id = %s",
            (ids.workspace_b,),
        ).rowcount == 0
        # A viewer cannot bypass application RBAC by updating its own workspace
        # through the shared API database credential.
        assert connection.execute(
            "UPDATE workspaces SET name = 'viewer-escalation' WHERE id = %s",
            (ids.workspace_a,),
        ).rowcount == 0

        membership_ids = {
            row[0]
            for row in connection.execute(
                "SELECT id FROM memberships WHERE id IN (%s, %s)",
                (ids.membership_a, ids.membership_b),
            ).fetchall()
        }
        assert membership_ids == {ids.membership_a}, membership_ids
        assert connection.execute(
            "UPDATE memberships SET role = 'owner' WHERE id = %s",
            (ids.membership_a,),
        ).rowcount == 0
        assert connection.execute(
            "UPDATE memberships SET role = 'viewer' WHERE id = %s",
            (ids.membership_b,),
        ).rowcount == 0
        assert connection.execute(
            "DELETE FROM memberships WHERE id = %s", (ids.membership_b,)
        ).rowcount == 0

        notification_ids = {
            row[0]
            for row in connection.execute(
                "SELECT id FROM notifications WHERE id IN (%s, %s)",
                (ids.notification_a, ids.notification_b),
            ).fetchall()
        }
        assert notification_ids == {ids.notification_a}, notification_ids
        update_result = connection.execute(
            "UPDATE notifications SET title = 'forbidden' WHERE id = %s",
            (ids.notification_b,),
        )
        assert update_result.rowcount == 0

        outbox_ids = {
            row[0]
            for row in connection.execute(
                "SELECT id FROM notification_outbox WHERE id IN (%s, %s, %s)",
                (ids.outbox_a, ids.outbox_b, ids.outbox_system),
            ).fetchall()
        }
        assert outbox_ids == {ids.outbox_a}, outbox_ids

    _assert_api_statement_rejected(
        api_url,
        ids.user_a,
        """
        INSERT INTO notification_outbox (
          id, workspace_id, channel, recipient, payload_encrypted,
          idempotency_key, status, attempts, created_at, next_attempt_at
        ) VALUES (
          %s, NULL, 'email', 'foreign-account@example.invalid', 'opaque', %s,
          'pending', 0, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP
        )
        """,
        (ids.outbox_system_api, f"rls-api-system-{ids.outbox_system_api.hex}"),
        "API inserted a system e-mail for another account",
    )

    _assert_api_statement_rejected(
        api_url,
        ids.user_a,
        """
        INSERT INTO memberships (
          id, workspace_id, user_id, role, created_at, updated_at
        ) VALUES (%s, %s, %s, 'owner', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
        """,
        (ids.membership_injected_cross, ids.workspace_b, ids.user_a),
        "API injected itself into another tenant",
    )
    _assert_api_statement_rejected(
        api_url,
        ids.user_a,
        """
        INSERT INTO memberships (
          id, workspace_id, user_id, role, created_at, updated_at
        ) VALUES (%s, %s, %s, 'owner', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
        """,
        (ids.membership_injected_local, ids.workspace_a, ids.user_b),
        "API injected an uninvited account into its tenant",
    )
    _assert_api_statement_rejected(
        api_url,
        ids.user_b,
        "UPDATE memberships SET user_id = %s WHERE id = %s",
        (ids.user_a, ids.membership_b),
        "API changed a membership identity to inject another account",
    )
    _assert_api_statement_rejected(
        api_url,
        ids.user_b,
        "UPDATE memberships SET workspace_id = %s WHERE id = %s",
        (ids.workspace_a, ids.membership_b),
        "API moved a membership into another tenant",
    )
    _assert_api_statement_rejected(
        api_url,
        ids.user_b,
        "UPDATE workspaces SET profile_quota = 999 WHERE id = %s",
        (ids.workspace_b,),
        "API owner changed a platform-managed workspace quota",
    )
    _assert_api_statement_rejected(
        api_url,
        ids.user_a,
        "UPDATE users SET is_platform_admin = true WHERE id = %s",
        (ids.user_a,),
        "API promoted an account to platform administrator",
    )
    _assert_api_statement_rejected(
        api_url,
        ids.user_a,
        "UPDATE users SET is_active = false WHERE id = %s",
        (ids.user_a,),
        "API changed an account activation flag",
    )
    _assert_api_statement_rejected(
        api_url,
        ids.user_a,
        "DELETE FROM workspaces WHERE id = %s",
        (ids.workspace_b,),
        "API deleted another tenant",
    )

    # The intentional create path remains possible: an authenticated user may
    # insert an unclaimed workspace and exactly its own initial owner row.
    with _connect(api_url) as connection, connection.transaction():
        connection.execute(
            "SELECT set_config('app.user_id', %s, true)", (str(ids.user_a),)
        )
        connection.execute(
            """
            INSERT INTO workspaces (
              id, name, slug, profile_quota, member_quota, created_at, updated_at
            ) VALUES (
              %s, 'API-created workspace', %s, 3, 10,
              CURRENT_TIMESTAMP, CURRENT_TIMESTAMP
            )
            """,
            (ids.workspace_created, f"rls-created-{ids.workspace_created.hex[:12]}"),
        )
        connection.execute(
            """
            INSERT INTO memberships (
              id, workspace_id, user_id, role, created_at, updated_at
            ) VALUES (
              %s, %s, %s, 'owner', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP
            )
            """,
            (ids.membership_created, ids.workspace_created, ids.user_a),
        )
        assert connection.execute(
            "UPDATE workspaces SET name = 'Owner-managed' WHERE id = %s",
            (ids.workspace_created,),
        ).rowcount == 1
        assert connection.execute(
            "DELETE FROM memberships WHERE id = %s", (ids.membership_created,)
        ).rowcount == 0

    try:
        with _connect(api_url) as connection, connection.transaction():
            connection.execute(
                "SELECT set_config('role', 'worshipsync_worker', true)"
            )
    except InsufficientPrivilege:
        pass
    else:
        raise AssertionError("API role could assume worshipsync_worker")

    try:
        with _connect(api_url) as connection, connection.transaction():
            connection.execute("SELECT count(*) FROM audit_events")
    except InsufficientPrivilege:
        pass
    else:
        raise AssertionError("API role unexpectedly read audit_events")

    # Required authentication updates remain available after the account flag
    # columns have been fenced at the privilege layer.
    with _connect(api_url) as connection, connection.transaction():
        connection.execute(
            "SELECT set_config('app.user_id', %s, true)", (str(ids.user_a),)
        )
        assert connection.execute(
            """
            UPDATE users
            SET password_hash = password_hash,
                email_verified_at = CURRENT_TIMESTAMP,
                totp_secret_encrypted = 'role-test-totp',
                totp_pending_secret_encrypted = 'role-test-pending-totp',
                totp_recovery_hashes = '[]'::json,
                updated_at = CURRENT_TIMESTAMP
            WHERE id = %s
            """,
            (ids.user_a,),
        ).rowcount == 1


def _assert_api_statement_rejected(
    api_url: str,
    user_id: uuid.UUID,
    statement: str,
    parameters: tuple[object, ...],
    message: str,
) -> None:
    try:
        with _connect(api_url) as connection, connection.transaction():
            connection.execute(
                "SELECT set_config('app.user_id', %s, true)", (str(user_id),)
            )
            connection.execute(statement, parameters)
    except InsufficientPrivilege:
        pass
    else:
        raise AssertionError(message)


def _assert_invitation_join(owner_url: str, api_url: str, ids: FixtureIds) -> None:
    with _connect(owner_url) as connection, connection.transaction():
        normalized_email = connection.execute(
            "SELECT normalized_email FROM users WHERE id = %s", (ids.user_a,)
        ).fetchone()[0]
        connection.execute(
            """
            INSERT INTO workspace_invitations (
              id, workspace_id, invited_by_user_id, email, normalized_email,
              role, token_hash, created_at, expires_at
            ) VALUES (
              %s, %s, %s, %s, %s, 'operator', %s,
              CURRENT_TIMESTAMP, CURRENT_TIMESTAMP + INTERVAL '1 day'
            )
            """,
            (
                ids.invitation_a_to_b,
                ids.workspace_b,
                ids.user_b,
                normalized_email,
                normalized_email,
                ids.invitation_a_to_b.hex * 2,
            ),
        )

    # Mirrors the API's lock order and flush boundary: recipient invitation,
    # workspace FOR UPDATE, membership insert, then accepted_at update.
    with _connect(api_url) as connection, connection.transaction():
        connection.execute(
            "SELECT set_config('app.user_id', %s, true)", (str(ids.user_a),)
        )
        assert connection.execute(
            "SELECT id FROM workspace_invitations WHERE id = %s FOR UPDATE",
            (ids.invitation_a_to_b,),
        ).fetchone()[0] == ids.invitation_a_to_b
        assert connection.execute(
            "SELECT id FROM workspaces WHERE id = %s FOR UPDATE",
            (ids.workspace_b,),
        ).fetchone()[0] == ids.workspace_b
        connection.execute(
            """
            INSERT INTO memberships (
              id, workspace_id, user_id, role, created_at, updated_at
            ) VALUES (
              %s, %s, %s, 'operator', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP
            )
            """,
            (ids.membership_invited, ids.workspace_b, ids.user_a),
        )
        assert connection.execute(
            "UPDATE workspace_invitations SET accepted_at = CURRENT_TIMESTAMP WHERE id = %s",
            (ids.invitation_a_to_b,),
        ).rowcount == 1

    with _connect(api_url) as connection, connection.transaction():
        connection.execute(
            "SELECT set_config('app.user_id', %s, true)", (str(ids.user_a),)
        )
        assert connection.execute(
            "SELECT count(*) FROM memberships WHERE id = %s",
            (ids.membership_invited,),
        ).fetchone()[0] == 1

    _assert_owner_promotion_quota(owner_url, api_url, ids)


def _assert_owner_promotion_quota(
    owner_url: str, api_url: str, ids: FixtureIds
) -> None:
    settings = _test_settings(api_url).model_copy(
        update={"workspace_quota_per_user": 1}
    )
    engine = create_engine(settings.database_url)
    try:
        # Hold the target account lock and give the API a short statement
        # timeout. A query cancellation proves the promotion path waits on the
        # shared per-user serialization lock before evaluating the global
        # ownership count.
        with _connect(owner_url) as lock_connection, lock_connection.transaction():
            lock_connection.execute(
                "SELECT id FROM users WHERE id = %s FOR UPDATE", (ids.user_a,)
            )
            with Session(engine, expire_on_commit=False, autoflush=False) as db:
                set_request_user_context(db, ids.user_b)
                db.execute(text("SET LOCAL statement_timeout = '250ms'"))
                actor = db.get(User, ids.user_b)
                workspace = db.get(Workspace, ids.workspace_b)
                assert actor is not None and workspace is not None
                try:
                    update_member_role(
                        ids.membership_invited,
                        MemberRoleUpdate(role=WorkspaceRole.OWNER),
                        WorkspaceAccess(workspace, actor, WorkspaceRole.OWNER),
                        settings,
                        db,
                        None,
                    )
                except DBAPIError as error:
                    assert getattr(error.orig, "sqlstate", None) == "57014", error
                    db.rollback()
                else:
                    raise AssertionError(
                        "Owner promotion did not serialize on the target user row"
                    )

        # The actor cannot see the target's already-owned foreign workspace,
        # but the SECURITY-DEFINER aggregate must still reject the promotion.
        with Session(engine, expire_on_commit=False, autoflush=False) as db:
            set_request_user_context(db, ids.user_b)
            assert db.get(Workspace, ids.workspace_created) is None
            actor = db.get(User, ids.user_b)
            workspace = db.get(Workspace, ids.workspace_b)
            assert actor is not None and workspace is not None
            try:
                update_member_role(
                    ids.membership_invited,
                    MemberRoleUpdate(role=WorkspaceRole.OWNER),
                    WorkspaceAccess(workspace, actor, WorkspaceRole.OWNER),
                    settings,
                    db,
                    None,
                )
            except ProblemException as error:
                assert error.code == "workspace_quota_exceeded", error
                db.rollback()
            else:
                raise AssertionError("RLS-hidden owned workspace bypassed owner quota")

        # The column-level membership fence still permits the intended role
        # edit when it does not violate the global owner quota.
        with Session(engine, expire_on_commit=False, autoflush=False) as db:
            set_request_user_context(db, ids.user_b)
            actor = db.get(User, ids.user_b)
            workspace = db.get(Workspace, ids.workspace_b)
            assert actor is not None and workspace is not None
            result = update_member_role(
                ids.membership_invited,
                MemberRoleUpdate(role=WorkspaceRole.VIEWER),
                WorkspaceAccess(workspace, actor, WorkspaceRole.OWNER),
                settings,
                db,
                None,
            )
            assert result.role == WorkspaceRole.VIEWER
    finally:
        engine.dispose()


def _assert_registration(api_url: str, ids: FixtureIds) -> None:
    settings = _test_settings(api_url)
    email = f"role-register-{ids.workspace_a.hex[:12]}@example.org"
    engine = create_engine(settings.database_url)
    try:
        with Session(engine, expire_on_commit=False, autoflush=False) as db:
            response = register(
                RegisterRequest(
                    email=email,
                    password="registration-role-test-password",
                    workspace_name="RLS registration",
                ),
                settings,
                db,
            )
            assert response.verification_required is True
            assert response.development_verification_token

            set_request_user_context(db, response.user.id)
            membership = db.scalar(
                select(Membership).where(
                    Membership.user_id == response.user.id,
                    Membership.workspace_id == response.workspace_id,
                )
            )
            assert membership is not None
            assert membership.role == WorkspaceRole.OWNER
            assert db.scalar(
                select(Workspace).where(Workspace.id == response.workspace_id)
            ) is not None
    finally:
        engine.dispose()


def _assert_membershipless_recovery(api_url: str, ids: FixtureIds) -> None:
    settings = _test_settings(api_url)
    email = f"role-admin-{ids.workspace_a.hex[:12]}@example.org"
    engine = create_engine(settings.database_url)
    try:
        with Session(engine, expire_on_commit=False, autoflush=False) as db:
            first = request_recovery(RecoveryRequest(email=email), settings, db)
            second = request_recovery(RecoveryRequest(email=email), settings, db)
            assert first.development_recovery_token
            assert second.development_recovery_token

            set_request_user_context(db, ids.user_without_membership)
            recovery_rows = db.scalars(
                select(NotificationOutbox).where(
                    NotificationOutbox.workspace_id.is_(None),
                    NotificationOutbox.recipient == email,
                    NotificationOutbox.idempotency_key.like("password-recovery:%"),
                )
            ).all()
            assert len(recovery_rows) == 2

            idempotency_key = f"password-recovery:{ids.user_without_membership}:retry"
            first_item = enqueue_email(
                db,
                settings,
                recipient=email,
                subject="Recovery retry",
                text="Recovery retry",
                workspace_id=None,
                idempotency_key=idempotency_key,
            )
            first_item_id = first_item.id
            db.commit()

            set_request_user_context(db, ids.user_without_membership)
            repeated_item = enqueue_email(
                db,
                settings,
                recipient=email,
                subject="Recovery retry",
                text="Recovery retry",
                workspace_id=None,
                idempotency_key=idempotency_key,
            )
            assert repeated_item.id == first_item_id
            db.commit()
    finally:
        engine.dispose()


def _assert_worker_boundary(worker_url: str, ids: FixtureIds) -> None:
    with _connect(worker_url) as connection, connection.transaction():
        assert connection.execute("SELECT current_user").fetchone()[0] == "worshipsync_worker"
        assert connection.execute(
            "SELECT count(*) FROM workspaces WHERE id IN (%s, %s, %s)",
            (ids.workspace_a, ids.workspace_b, ids.workspace_created),
        ).fetchone()[0] == 3
        assert connection.execute(
            "SELECT id FROM workspaces WHERE id = %s FOR UPDATE",
            (ids.workspace_a,),
        ).fetchone()[0] == ids.workspace_a
        assert connection.execute(
            "SELECT count(*) FROM memberships WHERE id IN (%s, %s, %s, %s)",
            (
                ids.membership_a,
                ids.membership_b,
                ids.membership_created,
                ids.membership_invited,
            ),
        ).fetchone()[0] == 4
        notification_ids = {
            row[0]
            for row in connection.execute(
                "SELECT id FROM notifications WHERE id IN (%s, %s)",
                (ids.notification_a, ids.notification_b),
            ).fetchall()
        }
        assert notification_ids == {ids.notification_a, ids.notification_b}
        update_result = connection.execute(
            "UPDATE notifications SET title = 'worker-updated' WHERE id = %s",
            (ids.notification_b,),
        )
        assert update_result.rowcount == 1

        outbox_ids = {
            row[0]
            for row in connection.execute(
                "SELECT id FROM notification_outbox WHERE id IN (%s, %s, %s)",
                (
                    ids.outbox_a,
                    ids.outbox_b,
                    ids.outbox_system,
                ),
            ).fetchall()
        }
        assert outbox_ids == {
            ids.outbox_a,
            ids.outbox_b,
            ids.outbox_system,
        }
        audit_ids = {
            row[0]
            for row in connection.execute(
                "SELECT id FROM audit_events WHERE id IN (%s, %s, %s)",
                (ids.audit_a, ids.audit_b, ids.audit_system),
            ).fetchall()
        }
        assert audit_ids == {ids.audit_a, ids.audit_b, ids.audit_system}

    try:
        with _connect(worker_url) as connection, connection.transaction():
            connection.execute(
                "UPDATE workspaces SET updated_at = CURRENT_TIMESTAMP WHERE id = %s",
                (ids.workspace_a,),
            )
    except InsufficientPrivilege:
        pass
    else:
        raise AssertionError("Worker role updated a workspace")

    try:
        with _connect(worker_url) as connection, connection.transaction():
            connection.execute(
                "UPDATE users SET is_platform_admin = true WHERE id = %s",
                (ids.user_a,),
            )
    except InsufficientPrivilege:
        pass
    else:
        raise AssertionError("Worker role promoted a platform administrator")

    with _connect(worker_url) as connection, connection.transaction():
        assert connection.execute(
            """
            UPDATE users
            SET totp_secret_encrypted = 'worker-rotated-totp',
                totp_pending_secret_encrypted = NULL,
                updated_at = CURRENT_TIMESTAMP
            WHERE id = %s
            """,
            (ids.user_a,),
        ).rowcount == 1


def _assert_admin_boundary(admin_url: str, ids: FixtureIds) -> None:
    with _connect(admin_url) as connection, connection.transaction():
        assert connection.execute("SELECT current_user").fetchone()[0] == "worshipsync_admin"
        workspace_ids = {
            row[0]
            for row in connection.execute(
                "SELECT id FROM workspaces WHERE id IN (%s, %s)",
                (ids.workspace_a, ids.workspace_b),
            ).fetchall()
        }
        assert workspace_ids == {ids.workspace_a, ids.workspace_b}
        membership_ids = {
            row[0]
            for row in connection.execute(
                "SELECT id FROM memberships WHERE id IN (%s, %s)",
                (ids.membership_a, ids.membership_b),
            ).fetchall()
        }
        assert membership_ids == {ids.membership_a, ids.membership_b}
        connection.execute(
            """
            INSERT INTO audit_events (
              id, workspace_id, action, metadata_json, created_at
            ) VALUES (%s, %s, 'admin_rls_test', '{}'::json, CURRENT_TIMESTAMP)
            """,
            (ids.audit_admin, ids.workspace_a),
        )
        assert connection.execute(
            """
            UPDATE workspaces
            SET profile_quota = 17, member_quota = 71,
                updated_at = CURRENT_TIMESTAMP
            WHERE id = %s
            """,
            (ids.workspace_a,),
        ).rowcount == 1

    try:
        with _connect(admin_url) as connection, connection.transaction():
            connection.execute(
                """
                INSERT INTO audit_events (
                  id, workspace_id, action, metadata_json, created_at
                ) VALUES (%s, NULL, 'admin_system_forbidden', '{}'::json,
                          CURRENT_TIMESTAMP)
                """,
                (ids.audit_admin_system,),
            )
    except InsufficientPrivilege:
        pass
    else:
        raise AssertionError("Admin role inserted a system-wide audit row")

    try:
        with _connect(admin_url) as connection, connection.transaction():
            connection.execute("SELECT count(*) FROM notifications")
    except InsufficientPrivilege:
        pass
    else:
        raise AssertionError("Admin role unexpectedly read tenant notifications")


def _cleanup(owner_url: str, ids: FixtureIds) -> None:
    with _connect(owner_url) as connection, connection.transaction():
        registration_email = f"role-register-{ids.workspace_a.hex[:12]}@example.org"
        connection.execute(
            """
            DELETE FROM workspaces
            WHERE id IN (
              SELECT membership.workspace_id
              FROM memberships AS membership
              JOIN users AS registration_user
                ON registration_user.id = membership.user_id
              WHERE registration_user.normalized_email = %s
            )
            """,
            (registration_email,),
        )
        connection.execute(
            "DELETE FROM notification_outbox WHERE recipient = %s",
            (registration_email,),
        )
        connection.execute(
            "DELETE FROM users WHERE normalized_email = %s", (registration_email,)
        )
        connection.execute(
            "DELETE FROM notification_outbox WHERE recipient = %s",
            (f"role-admin-{ids.workspace_a.hex[:12]}@example.org",),
        )
        connection.execute(
            "DELETE FROM audit_events WHERE id IN (%s, %s, %s, %s, %s)",
            (
                ids.audit_a,
                ids.audit_b,
                ids.audit_system,
                ids.audit_admin,
                ids.audit_admin_system,
            ),
        )
        connection.execute(
            "DELETE FROM notification_outbox WHERE id IN (%s, %s, %s, %s)",
            (
                ids.outbox_a,
                ids.outbox_b,
                ids.outbox_system,
                ids.outbox_system_api,
            ),
        )
        connection.execute(
            "DELETE FROM workspaces WHERE id IN (%s, %s, %s)",
            (ids.workspace_a, ids.workspace_b, ids.workspace_created),
        )
        connection.execute(
            "DELETE FROM users WHERE id IN (%s, %s, %s)",
            (ids.user_a, ids.user_b, ids.user_without_membership),
        )


def main() -> None:
    owner_url = _dsn("PGTEST_OWNER_URL")
    api_url = _dsn("PGTEST_API_URL")
    worker_url = _dsn("PGTEST_WORKER_URL")
    admin_url = _dsn("PGTEST_ADMIN_URL")
    ids = _ids()
    try:
        _assert_rejected_credentials()
        _seed(owner_url, ids)
        _assert_role_attributes(owner_url)
        _assert_api_boundary(api_url, ids)
        _assert_registration(api_url, ids)
        _assert_membershipless_recovery(api_url, ids)
        _assert_invitation_join(owner_url, api_url, ids)
        _assert_worker_boundary(worker_url, ids)
        _assert_admin_boundary(admin_url, ids)
    finally:
        _cleanup(owner_url, ids)
    print("PostgreSQL role/RLS integration test passed")


if __name__ == "__main__":
    main()
