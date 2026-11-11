-- Application role, least-privilege grants, and row-level security.
--
-- The role is created if absent and then *verified*, because a role that already
-- exists with the wrong attributes would silently undermine every guarantee
-- below. The verification raises rather than warning: a partially privileged
-- application role is worse than a failed migration.
--
-- REPLICATION and BYPASSRLS are intentionally not named in CREATE ROLE: only a
-- superuser may specify them, and a non-superuser naming them fails outright.
-- Their defaults are already NO, and the verification asserts that.

do $$
begin
    if not exists (select 1 from pg_roles where rolname = 'ethos_app') then
        create role ethos_app login noinherit nosuperuser nocreatedb nocreaterole;
    end if;
end
$$;

do $$
declare
    attrs record;
begin
    select rolcanlogin, rolinherit, rolsuper, rolcreatedb, rolcreaterole,
           rolreplication, rolbypassrls
      into attrs
      from pg_roles
     where rolname = 'ethos_app';

    if attrs is null then
        raise exception 'role ethos_app was not created';
    end if;
    if not attrs.rolcanlogin then
        raise exception 'ethos_app must have LOGIN';
    end if;
    if attrs.rolinherit then
        raise exception 'ethos_app must be NOINHERIT';
    end if;
    if attrs.rolsuper then
        raise exception 'ethos_app must not be SUPERUSER';
    end if;
    if attrs.rolcreatedb then
        raise exception 'ethos_app must not have CREATEDB';
    end if;
    if attrs.rolcreaterole then
        raise exception 'ethos_app must not have CREATEROLE';
    end if;
    if attrs.rolreplication then
        raise exception 'ethos_app must not have REPLICATION';
    end if;
    if attrs.rolbypassrls then
        raise exception 'ethos_app must not have BYPASSRLS';
    end if;
end
$$;

grant usage on schema public to ethos_app;

-- Start from nothing. A database that previously received table-wide
-- UPDATE/DELETE would otherwise keep them, and a partial revoke is the worst of
-- both worlds: it looks least-privilege without being it.
revoke all privileges on all tables in schema public from ethos_app;

-- Reads: needed everywhere, and harmless on the append-only tables.
grant select on all tables in schema public to ethos_app;

-- Inserts: the commit pipeline creates rows in every table.
grant insert on
    environments,
    organizations,
    environment_users,
    teams,
    team_memberships,
    projects,
    project_memberships,
    tasks,
    task_assignments,
    task_dependencies,
    environment_events,
    environment_snapshots
to ethos_app;

-- Updates are granted per column, never per table. `id`, `environment_id` and
-- `created_at` are deliberately absent from every list: a row must not be able
-- to move between environments or be back-dated.

-- Head columns are written by the commit pipeline inside the same transaction
-- as the event; the rest are environment settings.
grant update (name, description, config, status,
              version, last_event_sequence, last_event_digest, updated_at)
    on environments to ethos_app;

grant update (name, slug) on organizations to ethos_app;
grant update (display_name, org_role, is_active) on environment_users to ethos_app;
grant update (name) on teams to ethos_app;
grant update (role, removed_at, removed_by_id) on team_memberships to ethos_app;
grant update (name, description, status, lead_user_id,
              started_on, target_on, updated_at)
    on projects to ethos_app;
grant update (role, removed_at) on project_memberships to ethos_app;
grant update (title, description, status, priority, task_type,
              due_on, completed_at, updated_at)
    on tasks to ethos_app;
grant update (removed_at) on task_assignments to ethos_app;

-- task_dependencies rows are immutable once written, so it receives no UPDATE
-- grant at all.

-- Append-only tables. Stated explicitly even though the blanket revoke above
-- already removed them, because these are the guarantees that matter most and a
-- future edit should have to delete a line that says so.
revoke update, delete, truncate on environment_events from ethos_app;
revoke update, delete, truncate on environment_snapshots from ethos_app;

-- No deletes anywhere for the runtime role. There is no delete action in the
-- domain, and taking an environment out of service is a status change.
revoke delete, truncate on all tables in schema public from ethos_app;

do $$
declare
    table_name text;
begin
    foreach table_name in array array[
        'environments',
        'organizations',
        'environment_users',
        'teams',
        'team_memberships',
        'projects',
        'project_memberships',
        'tasks',
        'task_assignments',
        'task_dependencies',
        'environment_events',
        'environment_snapshots'
    ]
    loop
        execute format('alter table public.%I enable row level security', table_name);
        execute format('alter table public.%I force row level security', table_name);
        execute format(
            'create policy ethos_app_access on public.%I for all to ethos_app '
            'using (true) with check (true)',
            table_name
        );
    end loop;
end
$$;

-- Phase 1 is single-tenant and the policy above is deliberately permissive: the
-- application is the only client and there is no authenticated end user to scope
-- by yet. RLS is enabled now so that scoping arrives as a policy change rather
-- than as a schema change, and so that any future role is denied by default.

-- Integration-test cleanup must never run as ethos_app: it needs DELETE on the
-- entity tables, which this role does not have. Tests use a separate
-- administrative credential (ETHOS_ADMIN_DATABASE_URL) for setup and teardown,
-- and the runtime credential only to prove the restrictions hold.
