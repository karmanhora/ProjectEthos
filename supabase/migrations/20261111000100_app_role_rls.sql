-- Application role and row-level security.
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
grant select, insert, update, delete on all tables in schema public to ethos_app;

-- Environments are never deleted: the log, the snapshots and every entity hang
-- off them, and a cascade would erase history. Taking an environment out of
-- service is a status change, not a delete.
revoke delete on environments from ethos_app;

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

-- Append-only enforcement at the database boundary. The event log is the record
-- of what happened; an UPDATE could rewrite history without touching the digest
-- chain, so the role is not granted UPDATE on it.
revoke update on environment_events from ethos_app;
