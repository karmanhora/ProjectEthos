-- Project Ethos - Phase 1 schema.
--
-- Twelve tables, no more. Every timestamp is supplied by the application clock:
-- there is deliberately no DEFAULT now() anywhere, so a replay from the event
-- log reproduces byte-identical state and hashes.
--
-- Integrity notes:
--   * Entity tables carry environment_id and are linked with *composite* foreign
--     keys, so a row can never reference an entity from another environment.
--   * "Active" membership and assignment rows are made unique by partial
--     indexes, so removal is a tombstone (removed_at) rather than a delete.
--   * environment_events is append-only by intent; see the RLS migration.

create table environments (
    id                  uuid primary key,
    name                text        not null check (char_length(name) between 1 and 120),
    description         text,
    config              jsonb       not null default '{}'::jsonb,
    version             integer     not null default 1 check (version >= 1),
    last_event_sequence bigint      not null default 0 check (last_event_sequence >= 0),
    last_event_digest   text        not null check (char_length(last_event_digest) = 64),
    status              text        not null default 'active'
                                    check (status in ('active', 'archived')),
    created_at          timestamptz not null,
    updated_at          timestamptz not null,
    constraint environments_config_is_object check (jsonb_typeof(config) = 'object')
);

create table organizations (
    id             uuid        primary key,
    environment_id uuid        not null references environments (id) on delete restrict,
    name           text        not null check (char_length(name) between 1 and 120),
    slug           text        not null check (slug ~ '^[a-z0-9][a-z0-9-]*$'
                                              and char_length(slug) between 2 and 40),
    created_at     timestamptz not null,
    unique (environment_id, id),
    constraint organizations_slug_uk unique (environment_id, slug)
);

create table environment_users (
    id             uuid        primary key,
    environment_id uuid        not null references environments (id) on delete restrict,
    org_id         uuid        not null,
    handle         text        not null check (char_length(handle) between 3 and 40),
    display_name   text        not null check (char_length(display_name) between 1 and 120),
    org_role       text        not null default 'member'
                               check (org_role in ('owner', 'admin', 'member', 'viewer')),
    is_active      boolean     not null default true,
    is_synthetic   boolean     not null default true,
    created_at     timestamptz not null,
    unique (environment_id, id),
    foreign key (environment_id, org_id)
        references organizations (environment_id, id) on delete restrict
);

-- Handles are compared case-insensitively by the domain, so the database must
-- agree: a case-sensitive unique constraint here would permit "Ada" and "ada".
create unique index environment_users_handle_uk
    on environment_users (environment_id, lower(handle));

create table teams (
    id             uuid        primary key,
    environment_id uuid        not null references environments (id) on delete restrict,
    org_id         uuid        not null,
    key            text        not null check (char_length(key) between 2 and 10),
    name           text        not null check (char_length(name) between 1 and 120),
    created_at     timestamptz not null,
    unique (environment_id, id),
    constraint teams_key_uk unique (environment_id, key),
    foreign key (environment_id, org_id)
        references organizations (environment_id, id) on delete restrict
);

create table team_memberships (
    id             uuid        primary key,
    environment_id uuid        not null references environments (id) on delete restrict,
    team_id        uuid        not null,
    user_id        uuid        not null,
    role           text        not null default 'member' check (role in ('lead', 'member')),
    added_by_id    uuid,
    added_at       timestamptz not null,
    removed_at     timestamptz,
    removed_by_id  uuid,
    foreign key (environment_id, team_id)
        references teams (environment_id, id) on delete restrict,
    foreign key (environment_id, user_id)
        references environment_users (environment_id, id) on delete restrict,
    foreign key (environment_id, added_by_id)
        references environment_users (environment_id, id) on delete restrict,
    foreign key (environment_id, removed_by_id)
        references environment_users (environment_id, id) on delete restrict,
    constraint team_memberships_removal_is_consistent
        check ((removed_at is null) = (removed_by_id is null))
);

create unique index team_memberships_active_uk
    on team_memberships (team_id, user_id) where removed_at is null;

create table projects (
    id             uuid        primary key,
    environment_id uuid        not null references environments (id) on delete restrict,
    org_id         uuid        not null,
    key            text        not null check (char_length(key) between 2 and 10),
    name           text        not null check (char_length(name) between 1 and 120),
    description    text,
    status         text        not null default 'active'
                               check (status in ('planning', 'active', 'paused',
                                                 'completed', 'archived')),
    lead_user_id   uuid,
    created_by_id  uuid        not null,
    started_on     date,
    target_on      date,
    created_at     timestamptz not null,
    updated_at     timestamptz not null,
    unique (environment_id, id),
    constraint projects_key_uk unique (environment_id, key),
    constraint projects_dates_ordered
        check (started_on is null or target_on is null or started_on <= target_on),
    foreign key (environment_id, org_id)
        references organizations (environment_id, id) on delete restrict,
    foreign key (environment_id, lead_user_id)
        references environment_users (environment_id, id) on delete restrict,
    foreign key (environment_id, created_by_id)
        references environment_users (environment_id, id) on delete restrict
);

create table project_memberships (
    id             uuid        primary key,
    environment_id uuid        not null references environments (id) on delete restrict,
    project_id     uuid        not null,
    user_id        uuid        not null,
    role           text        not null default 'contributor'
                               check (role in ('manager', 'contributor', 'viewer')),
    added_by_id    uuid,
    added_at       timestamptz not null,
    removed_at     timestamptz,
    foreign key (environment_id, project_id)
        references projects (environment_id, id) on delete restrict,
    foreign key (environment_id, user_id)
        references environment_users (environment_id, id) on delete restrict,
    foreign key (environment_id, added_by_id)
        references environment_users (environment_id, id) on delete restrict
);

create unique index project_memberships_active_uk
    on project_memberships (project_id, user_id) where removed_at is null;

create table tasks (
    id             uuid        primary key,
    environment_id uuid        not null references environments (id) on delete restrict,
    project_id     uuid        not null,
    title          text        not null check (char_length(title) between 1 and 200),
    description    text,
    status         text        not null default 'todo'
                               check (status in ('todo', 'in_progress', 'blocked',
                                                 'in_review', 'done', 'cancelled')),
    priority       text        not null default 'medium'
                               check (priority in ('low', 'medium', 'high', 'critical')),
    task_type      text        not null default 'task'
                               check (task_type in ('task', 'bug', 'feature', 'chore')),
    created_by_id  uuid        not null,
    due_on         date,
    completed_at   timestamptz,
    created_at     timestamptz not null,
    updated_at     timestamptz not null,
    unique (environment_id, id),
    constraint tasks_completion_is_consistent
        check ((status = 'done') = (completed_at is not null)),
    foreign key (environment_id, project_id)
        references projects (environment_id, id) on delete restrict,
    foreign key (environment_id, created_by_id)
        references environment_users (environment_id, id) on delete restrict
);

create index tasks_project_idx on tasks (environment_id, project_id, status);

create table task_assignments (
    id             uuid        primary key,
    environment_id uuid        not null references environments (id) on delete restrict,
    task_id        uuid        not null,
    user_id        uuid        not null,
    assigned_by_id uuid        not null,
    assigned_at    timestamptz not null,
    removed_at     timestamptz,
    foreign key (environment_id, task_id)
        references tasks (environment_id, id) on delete restrict,
    foreign key (environment_id, user_id)
        references environment_users (environment_id, id) on delete restrict,
    foreign key (environment_id, assigned_by_id)
        references environment_users (environment_id, id) on delete restrict
);

create unique index task_assignments_active_uk
    on task_assignments (task_id, user_id) where removed_at is null;

create table task_dependencies (
    id                 uuid        primary key,
    environment_id     uuid        not null references environments (id) on delete restrict,
    task_id            uuid        not null,
    depends_on_task_id uuid        not null,
    created_by_id      uuid        not null,
    created_at         timestamptz not null,
    constraint task_dependencies_no_self check (task_id <> depends_on_task_id),
    constraint task_dependencies_uk unique (task_id, depends_on_task_id),
    foreign key (environment_id, task_id)
        references tasks (environment_id, id) on delete restrict,
    foreign key (environment_id, depends_on_task_id)
        references tasks (environment_id, id) on delete restrict,
    foreign key (environment_id, created_by_id)
        references environment_users (environment_id, id) on delete restrict
);

-- The log. `sequence` is per environment and gapless; `prev_event_digest` plus
-- `event_digest` form the chain. A failed event records no state transition.
create table environment_events (
    id                     uuid        primary key,
    environment_id         uuid        not null references environments (id) on delete restrict,
    sequence               bigint      not null check (sequence >= 1),
    action_type            text        not null,
    actor_user_id          uuid,
    origin                 text        not null default 'api'
                                       check (origin in ('bootstrap', 'api')),
    parameters             jsonb       not null default '{}'::jsonb,
    changes                jsonb       not null default '{"changes": []}'::jsonb,
    status                 text        not null check (status in ('succeeded', 'failed')),
    failure_code           text,
    failure_reason         text,
    failure_details        jsonb,
    prev_state_version     integer     not null check (prev_state_version >= 0),
    prev_state_hash        text        not null check (char_length(prev_state_hash) = 64),
    resulting_state_version integer,
    resulting_state_hash   text        check (resulting_state_hash is null
                                              or char_length(resulting_state_hash) = 64),
    idempotency_key        text        not null check (char_length(idempotency_key) between 1 and 200),
    request_fingerprint    text        not null check (char_length(request_fingerprint) = 64),
    prev_event_digest      text        not null check (char_length(prev_event_digest) = 64),
    event_digest           text        not null check (char_length(event_digest) = 64),
    created_at             timestamptz not null,
    constraint environment_events_sequence_uk unique (environment_id, sequence),
    -- The application recognises a concurrent duplicate by this exact name.
    constraint environment_events_idempotency_uk unique (environment_id, idempotency_key),
    constraint environment_events_parameters_is_object
        check (jsonb_typeof(parameters) = 'object'),
    constraint environment_events_changes_shape
        check (jsonb_typeof(changes -> 'changes') = 'array'),
    constraint environment_events_failure_code_known
        check (failure_code is null or failure_code in
               ('ACTOR_NOT_FOUND', 'ENTITY_NOT_FOUND', 'PERMISSION_DENIED',
                'RULE_VIOLATION', 'ACTION_FAILED')),
    constraint environment_events_succeeded_shape
        check (status <> 'succeeded' or (
            resulting_state_version is not null
            and resulting_state_hash is not null
            and failure_code is null
            and failure_reason is null
        )),
    constraint environment_events_failed_shape
        check (status <> 'failed' or (
            resulting_state_version is null
            and resulting_state_hash is null
            and failure_code is not null
            and jsonb_array_length(changes -> 'changes') = 0
        )),
    foreign key (environment_id, actor_user_id)
        references environment_users (environment_id, id) on delete restrict
);

create index environment_events_actor_idx
    on environment_events (environment_id, actor_user_id);

create table environment_snapshots (
    id                  uuid        primary key,
    environment_id      uuid        not null references environments (id) on delete restrict,
    version             integer     not null check (version >= 1),
    last_event_sequence bigint      not null check (last_event_sequence >= 1),
    state               jsonb       not null,
    state_hash          text        not null check (char_length(state_hash) = 64),
    kind                text        not null default 'auto'
                                    check (kind in ('bootstrap', 'auto', 'manual')),
    label               text,
    created_at          timestamptz not null,
    constraint environment_snapshots_version_uk unique (environment_id, version),
    constraint environment_snapshots_state_is_object check (jsonb_typeof(state) = 'object')
);
