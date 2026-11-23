-- Single-owner application authentication for the private Toothless console.
-- Apply only with the authorized Supabase administrative connection.
CREATE TABLE IF NOT EXISTS toothless_private.web_admin (
    singleton boolean PRIMARY KEY DEFAULT true CHECK (singleton),
    owner_id uuid NOT NULL UNIQUE,
    username text NOT NULL CHECK (length(username) BETWEEN 3 AND 64),
    username_normalized text NOT NULL UNIQUE CHECK (username_normalized = lower(username)),
    password_hash text NOT NULL CHECK (length(password_hash) BETWEEN 40 AND 512),
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS toothless_private.web_sessions (
    token_hash text PRIMARY KEY CHECK (length(token_hash) = 64),
    owner_id uuid NOT NULL REFERENCES toothless_private.web_admin(owner_id),
    csrf_hash text NOT NULL CHECK (length(csrf_hash) = 64),
    created_at timestamptz NOT NULL DEFAULT now(),
    expires_at timestamptz NOT NULL,
    revoked_at timestamptz
);

CREATE TABLE IF NOT EXISTS toothless_private.web_login_limits (
    username_key text PRIMARY KEY CHECK (length(username_key) = 64),
    window_started_at timestamptz NOT NULL,
    failures integer NOT NULL CHECK (failures BETWEEN 0 AND 100),
    blocked_until timestamptz
);

CREATE TABLE IF NOT EXISTS toothless_private.web_auth_events (
    id uuid PRIMARY KEY,
    owner_id uuid,
    event_type text NOT NULL CHECK (event_type IN ('bootstrap','login_succeeded','login_failed','logout','logout_all','password_changed')),
    created_at timestamptz NOT NULL DEFAULT now(),
    username_key text CHECK (username_key IS NULL OR length(username_key) = 64)
);
CREATE INDEX IF NOT EXISTS web_sessions_owner_expiry_idx ON toothless_private.web_sessions(owner_id, expires_at);
CREATE INDEX IF NOT EXISTS web_auth_events_created_idx ON toothless_private.web_auth_events(created_at);

DO $$ DECLARE t text; BEGIN
  FOREACH t IN ARRAY ARRAY['web_admin','web_sessions','web_login_limits','web_auth_events'] LOOP
    EXECUTE format('ALTER TABLE toothless_private.%I ENABLE ROW LEVEL SECURITY', t);
    EXECUTE format('ALTER TABLE toothless_private.%I FORCE ROW LEVEL SECURITY', t);
    EXECUTE format('DROP POLICY IF EXISTS toothless_owner_isolation ON toothless_private.%I', t);
    IF t <> 'web_login_limits' THEN
      EXECUTE format('CREATE POLICY toothless_owner_isolation ON toothless_private.%I USING (owner_id::text = current_setting(''app.owner_id'', true)) WITH CHECK (owner_id::text = current_setting(''app.owner_id'', true))', t);
    ELSE
      EXECUTE format('CREATE POLICY toothless_owner_isolation ON toothless_private.%I USING (true) WITH CHECK (true)', t);
    END IF;
    EXECUTE format('REVOKE ALL ON toothless_private.%I FROM PUBLIC, anon, authenticated', t);
  END LOOP;
END $$;

GRANT SELECT, INSERT, UPDATE, DELETE ON toothless_private.web_admin, toothless_private.web_sessions,
    toothless_private.web_login_limits, toothless_private.web_auth_events TO toothless_web;
