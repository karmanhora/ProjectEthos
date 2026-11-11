"""Static checks over the grants migration.

These cannot prove the SQL runs, but they do prove the guarantees are still
written down: a later edit that deletes the anon revokes, or reintroduces a
table-wide UPDATE, fails here without needing a database.
"""
from pathlib import Path

import pytest

MIGRATION = (
    Path(__file__).resolve().parents[2]
    / "supabase"
    / "migrations"
    / "20261111000100_app_role_rls.sql"
)
SQL = MIGRATION.read_text(encoding="utf-8")
LOWER = SQL.lower()

TABLES = (
    "environments",
    "organizations",
    "environment_users",
    "teams",
    "team_memberships",
    "projects",
    "project_memberships",
    "tasks",
    "task_assignments",
    "task_dependencies",
    "environment_events",
    "environment_snapshots",
)


def test_migration_file_exists():
    assert MIGRATION.is_file()
    assert MIGRATION.stat().st_size > 0


def test_dollar_quotes_are_balanced():
    # A single missing $$ would swallow every later statement into one block.
    assert SQL.count("$$") % 2 == 0


@pytest.mark.parametrize("table", TABLES)
def test_every_table_is_named(table):
    assert f"'{table}'" in SQL


def test_anon_and_authenticated_are_revoked():
    assert "array['anon', 'authenticated']" in SQL
    assert "revoke all privileges on table public.%I from %I" in SQL


def test_public_is_revoked_from_tables():
    assert "revoke all privileges on table public.%I from public" in SQL


def test_default_privileges_are_revoked():
    assert "alter default privileges in schema public revoke all on tables" in LOWER
    assert "alter default privileges in schema public revoke all on sequences" in LOWER


def test_membership_inheritance_is_checked():
    assert "pg_auth_members" in SQL
    assert "inherits privileges through" in LOWER


def test_default_acl_is_checked():
    assert "pg_default_acl" in SQL
    assert "aclexplode" in LOWER
    assert "can still reach future ethos tables" in LOWER


def test_privileges_are_asserted_absent():
    assert "has_table_privilege" in LOWER
    assert "still holds a privilege" in LOWER


def test_roles_are_guarded_on_existence():
    # A bare Postgres has no anon/authenticated; naming them unguarded would
    # abort the migration instead of skipping the revoke.
    assert "from pg_roles where rolname = role_name" in LOWER


def test_ethos_app_keeps_least_privilege_grants():
    assert "grant select on all tables in schema public to ethos_app" in LOWER
    assert "grant update (" in LOWER
    # No table-wide UPDATE or DELETE grant anywhere.
    assert "grant update on" not in LOWER
    assert "grant delete" not in LOWER
    assert "grant all" not in LOWER


def test_ethos_app_append_only_tables_are_locked_down():
    assert "revoke update, delete, truncate on environment_events" in LOWER
    assert "revoke update, delete, truncate on environment_snapshots" in LOWER


def test_rls_policies_are_preserved():
    assert "enable row level security" in LOWER
    assert "force row level security" in LOWER
    assert "create policy ethos_app_access" in LOWER


def test_ethos_app_positive_control_is_asserted():
    assert "ethos_app lost select" in LOWER
    assert "ethos_app lost insert" in LOWER


def test_global_default_privileges_are_revoked():
    # Without FOR ROLE / IN SCHEMA this is the global default scope, the only
    # one a schema-level REVOKE cannot reach.
    assert "alter default privileges revoke all on tables" in LOWER
    assert "alter default privileges revoke all on sequences" in LOWER


def test_schema_scoped_defaults_are_still_revoked():
    assert "alter default privileges in schema public revoke all on tables" in LOWER


def test_default_privilege_validation_covers_global_scope():
    assert "defaults.defaclnamespace = 0 or namespace.nspname = 'public'" in SQL


def test_global_default_failure_names_owner_and_remedy():
    assert "defaclrole" in SQL
    assert "alter default privileges for role %i" in LOWER
    assert "cannot cancel a global default" in LOWER


def test_maintain_is_version_gated():
    # MAINTAIN is unknown before PostgreSQL 17, so naming it unconditionally
    # would make the migration fail on older servers.
    assert "server_version_num" in LOWER
    assert "170000" in SQL
    assert "privileges := privileges || ',MAINTAIN'" in SQL


def test_maintain_check_covers_or_omits_nothing():
    assert "'SELECT,INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER'" in SQL
    assert "ethos_app must not have maintain" in LOWER
