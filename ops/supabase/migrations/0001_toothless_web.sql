-- Phase 09.0 initial private application schema.
-- Apply only after review, from the trusted Mac using an administrative connection.
-- Runtime application access is a separately provisioned, non-owner role.
CREATE SCHEMA IF NOT EXISTS toothless_private;
REVOKE ALL ON SCHEMA toothless_private FROM PUBLIC, anon, authenticated;
DO $$ BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='toothless_web') THEN
    CREATE ROLE toothless_web NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS;
  END IF;
END $$;

CREATE TABLE IF NOT EXISTS toothless_private.projects (
    id uuid PRIMARY KEY,
    owner_id uuid NOT NULL,
    name text NOT NULL CHECK (length(name) BETWEEN 1 AND 120),
    description text NOT NULL DEFAULT '' CHECK (length(description) <= 4000),
    workspace_ref text,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (owner_id, name),
    UNIQUE (owner_id, id)
);
CREATE TABLE IF NOT EXISTS toothless_private.conversations (
    id uuid PRIMARY KEY,
    owner_id uuid NOT NULL,
    project_id uuid,
    title text NOT NULL CHECK (length(title) BETWEEN 1 AND 200),
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    archived_at timestamptz,
    UNIQUE (owner_id, id),
    FOREIGN KEY (owner_id, project_id) REFERENCES toothless_private.projects(owner_id, id)
);
CREATE TABLE IF NOT EXISTS toothless_private.agent_runs (
    id uuid PRIMARY KEY,
    owner_id uuid NOT NULL,
    conversation_id uuid NOT NULL,
    project_id uuid,
    execution_id text,
    objective text NOT NULL CHECK (length(objective) <= 8000),
    state text NOT NULL CHECK (state IN ('queued','working','awaiting_approval','awaiting_input','paused','cancelling','completed','failed','interrupted','uncertain')),
    phase text NOT NULL DEFAULT 'queued',
    permission_scope text NOT NULL,
    error_category text,
    started_at timestamptz,
    updated_at timestamptz NOT NULL DEFAULT now(),
    finished_at timestamptz,
    UNIQUE (owner_id, id),
    FOREIGN KEY (owner_id, conversation_id) REFERENCES toothless_private.conversations(owner_id, id),
    FOREIGN KEY (owner_id, project_id) REFERENCES toothless_private.projects(owner_id, id)
);
CREATE TABLE IF NOT EXISTS toothless_private.messages (
    id uuid PRIMARY KEY,
    owner_id uuid NOT NULL,
    conversation_id uuid NOT NULL,
    role text NOT NULL CHECK (role IN ('user','assistant','system-visible-event')),
    content text NOT NULL CHECK (length(content) <= 48000),
    content_type text NOT NULL DEFAULT 'text',
    created_at timestamptz NOT NULL DEFAULT now(),
    client_request_id uuid NOT NULL,
    associated_run_id uuid,
    status text NOT NULL DEFAULT 'accepted',
    UNIQUE (owner_id, client_request_id),
    FOREIGN KEY (owner_id, conversation_id) REFERENCES toothless_private.conversations(owner_id, id),
    FOREIGN KEY (owner_id, associated_run_id) REFERENCES toothless_private.agent_runs(owner_id, id)
);
CREATE TABLE IF NOT EXISTS toothless_private.agent_events (
    id uuid PRIMARY KEY,
    owner_id uuid NOT NULL,
    run_id uuid NOT NULL,
    sequence bigint NOT NULL CHECK (sequence > 0),
    event_type text NOT NULL,
    safe_summary text NOT NULL CHECK (length(safe_summary) <= 2000),
    created_at timestamptz NOT NULL DEFAULT now(),
    tool_name text,
    safe_metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
    UNIQUE (run_id, sequence),
    FOREIGN KEY (owner_id, run_id) REFERENCES toothless_private.agent_runs(owner_id, id)
);
CREATE TABLE IF NOT EXISTS toothless_private.approval_requests (
    id uuid PRIMARY KEY,
    owner_id uuid NOT NULL,
    run_id uuid NOT NULL,
    exact_operation text NOT NULL CHECK (length(exact_operation) <= 4000),
    target_resource text NOT NULL CHECK (length(target_resource) <= 1000),
    authorization_scope text NOT NULL,
    expires_at timestamptz NOT NULL,
    status text NOT NULL CHECK (status IN ('pending','approved','rejected','expired')),
    request_hash text NOT NULL,
    approved_at timestamptz,
    rejected_at timestamptz,
    consumed_at timestamptz,
    FOREIGN KEY (owner_id, run_id) REFERENCES toothless_private.agent_runs(owner_id, id)
);
CREATE TABLE IF NOT EXISTS toothless_private.project_context (
    id uuid PRIMARY KEY,
    owner_id uuid NOT NULL,
    project_id uuid NOT NULL,
    kind text NOT NULL,
    content text NOT NULL CHECK (length(content) <= 8000),
    origin_type text NOT NULL,
    origin_id text NOT NULL,
    version integer NOT NULL CHECK (version > 0),
    created_at timestamptz NOT NULL DEFAULT now(),
    superseded_at timestamptz,
    UNIQUE (project_id, kind, origin_type, origin_id, version),
    FOREIGN KEY (owner_id, project_id) REFERENCES toothless_private.projects(owner_id, id)
);

CREATE INDEX IF NOT EXISTS conversations_owner_updated_idx ON toothless_private.conversations(owner_id, updated_at DESC);
CREATE INDEX IF NOT EXISTS messages_conversation_created_idx ON toothless_private.messages(conversation_id, created_at, id);
CREATE INDEX IF NOT EXISTS runs_conversation_updated_idx ON toothless_private.agent_runs(conversation_id, updated_at DESC);
CREATE INDEX IF NOT EXISTS events_run_sequence_idx ON toothless_private.agent_events(run_id, sequence);
CREATE INDEX IF NOT EXISTS approvals_owner_status_expiry_idx ON toothless_private.approval_requests(owner_id, status, expires_at);
CREATE INDEX IF NOT EXISTS context_project_current_idx ON toothless_private.project_context(project_id, created_at DESC) WHERE superseded_at IS NULL;

DO $$ DECLARE t text; BEGIN
  FOREACH t IN ARRAY ARRAY['projects','conversations','messages','agent_runs','agent_events','approval_requests','project_context'] LOOP
    EXECUTE format('ALTER TABLE toothless_private.%I ENABLE ROW LEVEL SECURITY', t);
    EXECUTE format('ALTER TABLE toothless_private.%I FORCE ROW LEVEL SECURITY', t);
    EXECUTE format('DROP POLICY IF EXISTS toothless_owner_isolation ON toothless_private.%I', t);
    EXECUTE format('CREATE POLICY toothless_owner_isolation ON toothless_private.%I USING (owner_id::text = current_setting(''app.owner_id'', true)) WITH CHECK (owner_id::text = current_setting(''app.owner_id'', true))', t);
    EXECUTE format('REVOKE ALL ON toothless_private.%I FROM PUBLIC, anon, authenticated', t);
  END LOOP;
END $$;
GRANT USAGE ON SCHEMA toothless_private TO toothless_web;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA toothless_private TO toothless_web;
ALTER DEFAULT PRIVILEGES IN SCHEMA toothless_private GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO toothless_web;
