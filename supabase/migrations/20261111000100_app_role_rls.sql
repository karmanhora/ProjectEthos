-- Application role, least-privilege grants, row-level security, and validation.
--
-- Three audiences are separated here:
--   ethos_app       the runtime role; least-privilege grants only.
--   anon            PostgREST anonymous role; no table privileges at all.
--   authenticated   PostgREST signed-in role; no table privileges at all.
--
-- The anon/authenticated revokes are followed by checks that *raise*, not warn.
-- A silently over-privileged role is indistinguishable from a correct one until
-- something goes wrong, so each guarantee is asserted after it is established.
--
-- The role is created if absent and then verified, because a role that already
-- exists with the wrong attributes would undermine every guarantee below.
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

-- ---------------------------------------------------------------------------
-- anon and authenticated: no direct table privileges.
-- ---------------------------------------------------------------------------
-- Supabase's PostgREST roles must not reach the Ethos tables at all. Every read
-- and write goes through the application, which authenticates as ethos_app.
--
-- Guarded on existence: a bare Postgres has neither role, and an unguarded
-- REVOKE naming a missing role aborts the migration.

do $$
declare
    role_name  text;
    table_name text;
    ethos_tables text[] := array[
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
    ];
begin
    foreach role_name in array array['anon', 'authenticated'] loop
        if not exists (select 1 from pg_roles where rolname = role_name) then
            raise notice 'role % does not exist; nothing to revoke', role_name;
            continue;
        end if;
        foreach table_name in array ethos_tables loop
            execute format(
                'revoke all privileges on table public.%I from %I',
                table_name, role_name
            );
        end loop;
    end loop;

    -- PUBLIC is not a role and always exists. It must not hold table privileges
    -- either: PUBLIC grants are inherited by every role, including future ones.
    foreach table_name in array ethos_tables loop
        execute format(
            'revoke all privileges on table public.%I from public',
            table_name
        );
    end loop;
end
$$;

-- Default privileges decide what a *future* table grants. PostgreSQL keeps two
-- kinds: schema-specific (defaclnamespace = that schema) and global
-- (defaclnamespace = 0), which applies to every schema the owning role creates
-- in. They are applied additively at CREATE TABLE time and privileges have no
-- negative form, so a schema-level REVOKE cannot subtract a global GRANT. Both
-- scopes must be revoked, and both are validated below.
--
-- A REVOKE without FOR ROLE only touches the defaults of the role running the
-- migration, which is the role that creates these tables. Defaults owned by a
-- different role are deliberately not rewritten here: validation reports them
-- with the exact remedy rather than leaving the guarantee silently unmet.
--
-- Blast radius: revoking globally removes anon/authenticated from defaults for
-- tables this role creates in *any* schema, not just public. That is the only
-- scope that can guarantee the Ethos tables, and this database has no other
-- application relying on those grants.
do $$
declare
    role_name text;
begin
    foreach role_name in array array['anon', 'authenticated'] loop
        if exists (select 1 from pg_roles where rolname = role_name) then
            execute format(
                'alter default privileges revoke all on tables from %I', role_name
            );
            execute format(
                'alter default privileges revoke all on sequences from %I', role_name
            );
            execute format(
                'alter default privileges in schema public revoke all on tables from %I',
                role_name
            );
            execute format(
                'alter default privileges in schema public revoke all on sequences from %I',
                role_name
            );
        end if;
    end loop;
    execute 'alter default privileges revoke all on tables from public';
    execute 'alter default privileges revoke all on sequences from public';
    execute 'alter default privileges in schema public revoke all on tables from public';
    execute 'alter default privileges in schema public revoke all on sequences from public';
end
$$;

-- ---------------------------------------------------------------------------
-- Row-level security. Unchanged policy shape; the grants above are what keep
-- these tables out of reach of anyone but ethos_app.
-- ---------------------------------------------------------------------------

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

-- ---------------------------------------------------------------------------
-- Validation. Everything above is asserted, not assumed.
-- ---------------------------------------------------------------------------

-- 1. anon / authenticated hold no privilege on any Ethos table.
--    has_table_privilege resolves inheritance too, so this also catches access
--    that arrived through a role membership rather than a direct grant.
do $$
declare
    role_name  text;
    table_name text;
    privileges text := 'SELECT,INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER';
    ethos_tables text[] := array[
        'environments', 'organizations', 'environment_users', 'teams',
        'team_memberships', 'projects', 'project_memberships', 'tasks',
        'task_assignments', 'task_dependencies', 'environment_events',
        'environment_snapshots'
    ];
begin
    -- MAINTAIN was added in PostgreSQL 17 and is rejected as an unknown
    -- privilege name by older servers, so the list is built at runtime rather
    -- than hard-coded. On 17+ it must be included: it is the privilege that
    -- allows VACUUM, ANALYZE, CLUSTER, REINDEX, REFRESH MATERIALIZED VIEW and
    -- LOCK TABLE, so omitting it would leave a real hole in this check.
    if current_setting('server_version_num')::int >= 170000 then
        privileges := privileges || ',MAINTAIN';
    end if;

    foreach role_name in array array['anon', 'authenticated'] loop
        if not exists (select 1 from pg_roles where rolname = role_name) then
            continue;
        end if;
        foreach table_name in array ethos_tables loop
            if has_table_privilege(
                role_name,
                format('public.%I', table_name),
                privileges
            ) then
                raise exception
                    'role % still holds a privilege on public.%',
                    role_name, table_name;
            end if;
        end loop;
    end loop;
end
$$;

-- 2. Neither role inherits privileges through a membership. A role that is a
--    member of a privileged role would pass check 1 only by accident.
do $$
declare
    role_name text;
    inherited text;
begin
    foreach role_name in array array['anon', 'authenticated'] loop
        if not exists (select 1 from pg_roles where rolname = role_name) then
            continue;
        end if;
        select string_agg(parent.rolname, ', ')
          into inherited
          from pg_auth_members memberships
          join pg_roles parent on parent.oid = memberships.roleid
          join pg_roles member on member.oid = memberships.member
         where member.rolname = role_name;
        if inherited is not null then
            raise exception
                'role % inherits privileges through: %', role_name, inherited;
        end if;
    end loop;
end
$$;

-- 3. No default privilege, in this schema or globally, still names anon,
--    authenticated or PUBLIC. Global entries (defaclnamespace = 0) are the
--    dangerous ones: they apply to tables created in any schema by their owning
--    role, and no schema-level REVOKE can subtract them.
do $$
declare
    offending record;
    details   text := '';
    remedy    text := '';
begin
    for offending in
        select owner_role.rolname as owner_name,
               case
                   when defaults.defaclnamespace = 0 then 'GLOBAL'
                   else namespace.nspname
               end as scope,
               case
                   when acl.grantee = 0 then 'PUBLIC'
                   else pg_get_userbyid(acl.grantee)
               end as grantee_name,
               acl.privilege_type
          from pg_default_acl defaults
          cross join lateral aclexplode(defaults.defaclacl) acl
          left join pg_namespace namespace
                 on namespace.oid = defaults.defaclnamespace
          left join pg_roles owner_role
                 on owner_role.oid = defaults.defaclrole
         where (defaults.defaclnamespace = 0 or namespace.nspname = 'public')
           and (acl.grantee = 0
                or pg_get_userbyid(acl.grantee) in ('anon', 'authenticated'))
    loop
        details := details || format(
            E'\n  %s default privileges of role %s grant %s to %s',
            offending.scope,
            offending.owner_name,
            offending.privilege_type,
            offending.grantee_name
        );
        remedy := remedy || format(
            E'\n  ALTER DEFAULT PRIVILEGES FOR ROLE %I REVOKE ALL ON TABLES FROM %I;',
            offending.owner_name,
            offending.grantee_name
        );
    end loop;

    if details <> '' then
        raise exception
            'default privileges can still reach future Ethos tables:%',
            details
            || E'\nRun the following as a superuser; a schema-level REVOKE cannot cancel a global default:'
            || remedy;
    end if;
end
$$;

-- 4. ethos_app kept exactly what it was given, and nothing more. Without this,
--    an over-broad REVOKE above would only be discovered by a failing write.
do $$
declare
    table_name text;
    ethos_tables text[] := array[
        'environments', 'organizations', 'environment_users', 'teams',
        'team_memberships', 'projects', 'project_memberships', 'tasks',
        'task_assignments', 'task_dependencies', 'environment_events',
        'environment_snapshots'
    ];
begin
    foreach table_name in array ethos_tables loop
        if not has_table_privilege('ethos_app', format('public.%I', table_name), 'SELECT') then
            raise exception 'ethos_app lost SELECT on public.%', table_name;
        end if;
        if not has_table_privilege('ethos_app', format('public.%I', table_name), 'INSERT') then
            raise exception 'ethos_app lost INSERT on public.%', table_name;
        end if;
    end loop;

    if has_table_privilege('ethos_app', 'public.environment_events', 'UPDATE') then
        raise exception 'ethos_app must not UPDATE environment_events';
    end if;
    if has_table_privilege('ethos_app', 'public.environment_events', 'DELETE') then
        raise exception 'ethos_app must not DELETE environment_events';
    end if;
    if has_table_privilege('ethos_app', 'public.environment_snapshots', 'UPDATE') then
        raise exception 'ethos_app must not UPDATE environment_snapshots';
    end if;
    if has_table_privilege('ethos_app', 'public.environment_snapshots', 'DELETE') then
        raise exception 'ethos_app must not DELETE environment_snapshots';
    end if;
    if has_table_privilege('ethos_app', 'public.environments', 'DELETE') then
        raise exception 'ethos_app must not DELETE environments';
    end if;

    -- Nothing above grants MAINTAIN, and this keeps it that way. Nested rather
    -- than combined with AND: PL/pgSQL does not guarantee short-circuit
    -- evaluation, and on a pre-17 server the bare name would raise.
    if current_setting('server_version_num')::int >= 170000 then
        if has_table_privilege('ethos_app', 'public.environment_events', 'MAINTAIN') then
            raise exception 'ethos_app must not have MAINTAIN on environment_events';
        end if;
        if has_table_privilege('ethos_app', 'public.environments', 'MAINTAIN') then
            raise exception 'ethos_app must not have MAINTAIN on environments';
        end if;
    end if;
end
$$;

-- Integration-test cleanup must never run as ethos_app: it needs DELETE on the
-- entity tables, which this role does not have. Tests use a separate
-- administrative credential (ETHOS_ADMIN_DATABASE_URL) for setup and teardown,
-- and the runtime credential only to prove the restrictions hold.
