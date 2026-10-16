-- CRM core, M0 foundations (apps/sales_crm/README.md §2.1, §5). Idempotent.

-- Team hierarchy: a sales manager sees their reports' accounts (auth.get_accessible_account_ids)
ALTER TABLE users ADD COLUMN IF NOT EXISTS manager_id int REFERENCES users(id) ON DELETE SET NULL;
CREATE INDEX IF NOT EXISTS users_manager ON users (manager_id) WHERE manager_id IS NOT NULL;

-- StradIT business lines (not the customer's LOBs — those are `lobs`)
CREATE TABLE IF NOT EXISTS business_lines (
  id     serial PRIMARY KEY,
  key    text UNIQUE NOT NULL CHECK (key ~ '^[a-z0-9_]{2,40}$'),
  name   text NOT NULL CHECK (length(name) BETWEEN 1 AND 80),
  active boolean NOT NULL DEFAULT true,
  sort   int NOT NULL DEFAULT 0
);
INSERT INTO business_lines (key, name, sort) VALUES
  ('fs', 'Financial Services', 1), ('federal', 'Federal', 2), ('training', 'Training', 3)
ON CONFLICT (key) DO NOTHING;

CREATE TABLE IF NOT EXISTS user_business_lines (
  user_id          int NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  business_line_id int NOT NULL REFERENCES business_lines(id) ON DELETE CASCADE,
  PRIMARY KEY (user_id, business_line_id)
);

ALTER TABLE deals    ADD COLUMN IF NOT EXISTS business_line_id int REFERENCES business_lines(id) ON DELETE SET NULL;
ALTER TABLE accounts ADD COLUMN IF NOT EXISTS primary_business_line_id int REFERENCES business_lines(id) ON DELETE SET NULL;
CREATE INDEX IF NOT EXISTS deals_business_line ON deals (business_line_id);

-- CRM settings (fiscal year, attribution default …) as simple key/value rows
CREATE TABLE IF NOT EXISTS crm_settings (
  key        text PRIMARY KEY,
  value      jsonb NOT NULL,
  updated_by int REFERENCES users(id) ON DELETE SET NULL,
  updated_at timestamptz NOT NULL DEFAULT now()
);
INSERT INTO crm_settings (key, value) VALUES
  ('fiscal_year_start_month', '1'),
  ('default_attribution_pct', '50'),
  ('stage_probability', '{"intro": 10, "discovery": 20, "proposal": 40, "pilot": 60, "contract": 80, "won": 100, "lost": 0}'),
  ('capture_provider', '"microsoft"')
ON CONFLICT (key) DO NOTHING;

-- ── M1 Introductions (README §2.2) ───────────────────────────────────────────
CREATE TABLE IF NOT EXISTS connectors (
  id           bigserial PRIMARY KEY,
  kind         text NOT NULL CHECK (kind IN ('partner','advisor','employee','customer','other')),
  name         text NOT NULL CHECK (length(name) BETWEEN 1 AND 200),
  organisation text,
  email        text,
  user_id      int UNIQUE REFERENCES users(id) ON DELETE SET NULL,     -- the partner's login
  default_attribution_pct numeric(5,2) CHECK (default_attribution_pct BETWEEN 0 AND 100),
  notes        text,
  active       boolean NOT NULL DEFAULT true,
  created_by   int REFERENCES users(id) ON DELETE SET NULL,
  created_at   timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS introductions (
  id               bigserial PRIMARY KEY,
  connector_id     bigint NOT NULL REFERENCES connectors(id) ON DELETE RESTRICT,
  account_id       int REFERENCES accounts(id) ON DELETE CASCADE,       -- NULL until a partner submission is triaged
  persona_id       int REFERENCES personas(id) ON DELETE SET NULL,
  submitted_account_name text,                                          -- what a partner typed
  submitted_contact_name text,
  submitted_contact_email text,
  submitted_contact_title text,
  business_line_id int REFERENCES business_lines(id) ON DELETE SET NULL,
  owner_user_id    int REFERENCES users(id) ON DELETE SET NULL,
  status           text NOT NULL DEFAULT 'proposed' CHECK (status IN
                   ('proposed','requested','accepted','intro_made','meeting_held','converted','declined','stale')),
  context          text,
  next_step        text,
  requested_at     timestamptz, accepted_at timestamptz, intro_made_at timestamptz,
  meeting_at       timestamptz, converted_at timestamptz, closed_reason text,
  deal_id          bigint REFERENCES deals(id) ON DELETE SET NULL,
  attribution_pct  numeric(5,2) CHECK (attribution_pct BETWEEN 0 AND 100),
  created_by       int REFERENCES users(id) ON DELETE SET NULL,
  created_at       timestamptz NOT NULL DEFAULT now(),
  updated_at       timestamptz NOT NULL DEFAULT now(),
  CHECK (account_id IS NOT NULL OR submitted_account_name IS NOT NULL)
);
CREATE UNIQUE INDEX IF NOT EXISTS introductions_path ON introductions (connector_id, account_id, persona_id)
  WHERE account_id IS NOT NULL AND persona_id IS NOT NULL AND status NOT IN ('declined','stale');
CREATE INDEX IF NOT EXISTS introductions_account ON introductions (account_id);
CREATE INDEX IF NOT EXISTS introductions_connector ON introductions (connector_id);
CREATE INDEX IF NOT EXISTS introductions_deal ON introductions (deal_id) WHERE deal_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS introduction_events (
  id              bigserial PRIMARY KEY,
  introduction_id bigint NOT NULL REFERENCES introductions(id) ON DELETE CASCADE,
  kind            text NOT NULL DEFAULT 'status' CHECK (kind IN ('status','note','field','created','converted')),
  from_status     text,
  to_status       text,
  note            text,
  partner_visible boolean NOT NULL DEFAULT true,     -- internal-only notes are hidden from the partner
  by_user         int REFERENCES users(id) ON DELETE SET NULL,
  at              timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS introduction_events_intro ON introduction_events (introduction_id, at);

ALTER TABLE deals ADD COLUMN IF NOT EXISTS source text CHECK (source IN ('introduction','outbound','inbound','existing','other'));
ALTER TABLE deals ADD COLUMN IF NOT EXISTS introduction_id bigint REFERENCES introductions(id) ON DELETE SET NULL;

-- ── M2 Forecasting (README §2.3, §3) ──────────────────────────────────────────
ALTER TABLE deals ADD COLUMN IF NOT EXISTS probability int CHECK (probability BETWEEN 0 AND 100);   -- NULL = stage default
ALTER TABLE deals ADD COLUMN IF NOT EXISTS forecast_category text NOT NULL DEFAULT 'pipeline'
      CHECK (forecast_category IN ('pipeline','best_case','commit','closed','omitted'));
ALTER TABLE deals ADD COLUMN IF NOT EXISTS amount_usd numeric(14,2);
ALTER TABLE deals ADD COLUMN IF NOT EXISTS closed_at timestamptz;

CREATE TABLE IF NOT EXISTS fx_rates (
  currency   text PRIMARY KEY CHECK (currency ~ '^[A-Z]{3}$'),
  usd_rate   numeric(14,6) NOT NULL CHECK (usd_rate > 0),      -- 1 unit of currency = usd_rate USD
  source     text NOT NULL DEFAULT 'manual',
  updated_by int REFERENCES users(id) ON DELETE SET NULL,
  updated_at timestamptz NOT NULL DEFAULT now()
);
-- Starting values only (source='default'): an admin should set real rates on the Forecast page.
INSERT INTO fx_rates (currency, usd_rate, source) VALUES
  ('USD', 1, 'fixed'), ('EUR', 1.08, 'default'), ('GBP', 1.27, 'default'), ('INR', 0.012, 'default')
ON CONFLICT (currency) DO NOTHING;

CREATE TABLE IF NOT EXISTS sales_targets (
  id               serial PRIMARY KEY,
  period           text NOT NULL CHECK (period ~ '^FY[0-9]{4}-Q[1-4]$'),
  user_id          int REFERENCES users(id) ON DELETE CASCADE,
  business_line_id int REFERENCES business_lines(id) ON DELETE CASCADE,
  amount_usd       numeric(14,2) NOT NULL CHECK (amount_usd >= 0),
  updated_by       int REFERENCES users(id) ON DELETE SET NULL,
  updated_at       timestamptz NOT NULL DEFAULT now(),
  CHECK ((user_id IS NULL) <> (business_line_id IS NULL))            -- a rep target OR a business-line target
);
CREATE UNIQUE INDEX IF NOT EXISTS sales_targets_user ON sales_targets (period, user_id) WHERE user_id IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS sales_targets_bl ON sales_targets (period, business_line_id) WHERE business_line_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS forecast_snapshots (
  snapshot_date     date   NOT NULL,
  deal_id           bigint NOT NULL,              -- no FK: snapshots outlive deleted deals
  name              text,
  account_id        int,
  owner_user_id     int,
  business_line_id  int,
  stage             text,
  forecast_category text,
  amount_usd        numeric(14,2),
  probability       int,
  expected_close    date,
  PRIMARY KEY (snapshot_date, deal_id)
);

-- Keep USD amount, close date and category consistent however a deal is written.
CREATE OR REPLACE FUNCTION crm_deal_forecast_fields() RETURNS trigger LANGUAGE plpgsql AS $fn$
BEGIN
  NEW.amount_usd := CASE WHEN NEW.value_amount IS NULL THEN NULL
    ELSE round(NEW.value_amount * (SELECT usd_rate FROM fx_rates WHERE currency = upper(NEW.currency)), 2) END;
  IF NEW.stage = 'won' THEN
    NEW.forecast_category := 'closed';
    NEW.closed_at := coalesce(NEW.closed_at, now());
  ELSIF NEW.stage = 'lost' THEN
    NEW.forecast_category := 'omitted';
    NEW.closed_at := coalesce(NEW.closed_at, now());
  ELSE
    NEW.closed_at := NULL;
    IF NEW.forecast_category = 'closed'
       OR (TG_OP = 'UPDATE' AND OLD.stage IN ('won','lost') AND NEW.forecast_category = 'omitted') THEN
      NEW.forecast_category := 'pipeline';
    END IF;
  END IF;
  RETURN NEW;
END $fn$;
DO $$ BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname = 'crm_deal_forecast_fields') THEN
    CREATE TRIGGER crm_deal_forecast_fields BEFORE INSERT OR UPDATE ON deals
      FOR EACH ROW EXECUTE FUNCTION crm_deal_forecast_fields();
  END IF;
END $$;
-- Backfill rows written before the trigger existed (cheap no-op afterwards)
UPDATE deals SET currency = currency
WHERE (value_amount IS NOT NULL AND amount_usd IS NULL) OR (stage IN ('won','lost') AND closed_at IS NULL);

-- ── M3 Activities (README §2.4, §4) ───────────────────────────────────────────
CREATE TABLE IF NOT EXISTS activities (
  id            bigserial PRIMARY KEY,
  type          text NOT NULL CHECK (type IN ('email','meeting','call','note','transcript','linkedin','task_done')),
  direction     text CHECK (direction IN ('inbound','outbound','internal')),
  subject       text CHECK (length(subject) <= 300),
  summary       text,                          -- what happened / extractive summary
  body          text,                          -- full text (transcripts; captured email bodies in M4)
  occurred_at   timestamptz NOT NULL DEFAULT now(),
  duration_min  int CHECK (duration_min BETWEEN 0 AND 1440),
  owner_user_id int REFERENCES users(id) ON DELETE SET NULL,
  source        text NOT NULL DEFAULT 'manual' CHECK (source IN ('manual','outlook','outlook_calendar','gmail','google_calendar','upload','copilot')),
  external_id   text,
  thread_id     text,
  visibility    text NOT NULL DEFAULT 'team' CHECK (visibility IN ('private','team')),
  participants  jsonb NOT NULL DEFAULT '[]',   -- [{name, email, persona_id, is_internal}]
  metadata      jsonb NOT NULL DEFAULT '{}',   -- transcript: speakers, action_items, file name …
  created_at    timestamptz NOT NULL DEFAULT now(),
  updated_at    timestamptz NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX IF NOT EXISTS activities_external ON activities (source, owner_user_id, external_id) WHERE external_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS activities_when ON activities (occurred_at DESC);

CREATE TABLE IF NOT EXISTS activity_links (
  activity_id bigint NOT NULL REFERENCES activities(id) ON DELETE CASCADE,
  object_type text NOT NULL CHECK (object_type IN ('account','persona','deal','introduction')),
  object_id   bigint NOT NULL,
  matched_by  text NOT NULL DEFAULT 'manual' CHECK (matched_by IN ('manual','email_exact','domain','calendar','rule','speaker','derived')),
  PRIMARY KEY (activity_id, object_type, object_id)
);
CREATE INDEX IF NOT EXISTS activity_links_object ON activity_links (object_type, object_id);

ALTER TABLE personas ADD COLUMN IF NOT EXISTS last_activity_at timestamptz;
ALTER TABLE deals    ADD COLUMN IF NOT EXISTS last_activity_at timestamptz;

-- Copilot indexing (apps/sales_copilot): team-visible activities become searchable.
DO $$ BEGIN
  IF EXISTS (SELECT 1 FROM pg_proc WHERE proname = 'rag_enqueue')
     AND NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname = 'rag_activities_ins') THEN
    CREATE TRIGGER rag_activities_ins AFTER INSERT ON activities FOR EACH ROW EXECUTE FUNCTION rag_enqueue('id');
    CREATE TRIGGER rag_activities_del AFTER DELETE ON activities FOR EACH ROW EXECUTE FUNCTION rag_enqueue('id');
    CREATE TRIGGER rag_activities_upd AFTER UPDATE ON activities FOR EACH ROW
      WHEN (OLD.subject IS DISTINCT FROM NEW.subject OR OLD.summary IS DISTINCT FROM NEW.summary
            OR OLD.body IS DISTINCT FROM NEW.body OR OLD.visibility IS DISTINCT FROM NEW.visibility)
      EXECUTE FUNCTION rag_enqueue('id');
  END IF;
END $$;
DO $$ BEGIN
  IF EXISTS (SELECT 1 FROM pg_proc WHERE proname = 'rag_enqueue')
     AND NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname = 'rag_activity_links_ins') THEN
    CREATE TRIGGER rag_activity_links_ins AFTER INSERT ON activity_links FOR EACH ROW EXECUTE FUNCTION rag_enqueue('activity_id');
    CREATE TRIGGER rag_activity_links_del AFTER DELETE ON activity_links FOR EACH ROW EXECUTE FUNCTION rag_enqueue('activity_id');
  END IF;
END $$;

-- Deleting a deal / introduction / contact / account removes its activity links (object_id has no FK).
-- The interaction itself stays on the other records it is linked to (e.g. the account).
CREATE OR REPLACE FUNCTION crm_unlink_activities() RETURNS trigger LANGUAGE plpgsql AS $fn$
BEGIN
  DELETE FROM activity_links WHERE object_type = TG_ARGV[0] AND object_id = OLD.id;
  RETURN OLD;
END $fn$;
DO $$
DECLARE t record;
BEGIN
  FOR t IN SELECT * FROM (VALUES ('deals','deal'), ('introductions','introduction'), ('personas','persona'), ('accounts','account')) AS v(tbl, otype)
  LOOP
    IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname = 'crm_unlink_' || t.tbl) THEN
      EXECUTE format('CREATE TRIGGER %I AFTER DELETE ON %I FOR EACH ROW EXECUTE FUNCTION crm_unlink_activities(%L)',
                     'crm_unlink_' || t.tbl, t.tbl, t.otype);
    END IF;
  END LOOP;
END $$;
-- Activities left with no links at all are removed
DELETE FROM activities a WHERE NOT EXISTS (SELECT 1 FROM activity_links l WHERE l.activity_id = a.id);

-- ── M4 Activity capture: Microsoft 365 (README §4) ────────────────────────────
CREATE TABLE IF NOT EXISTS capture_connections (
  id            serial PRIMARY KEY,
  user_id       int NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  provider      text NOT NULL CHECK (provider IN ('microsoft','google')),
  account_email text NOT NULL,
  scopes        text[] NOT NULL DEFAULT '{}',
  token_encrypted bytea NOT NULL,                 -- Fernet(JSON{access_token, refresh_token, expires_at}); never returned by the API
  cursors       jsonb NOT NULL DEFAULT '{}',      -- Graph delta links per stream
  status        text NOT NULL DEFAULT 'active' CHECK (status IN ('active','paused','needs_reauth','error')),
  error         text,
  settings      jsonb NOT NULL DEFAULT '{"capture_email": true, "capture_calendar": true, "store_bodies": false,
                                             "exclude_internal_only": true, "exclude_domains": []}',
  last_sync_at  timestamptz,
  last_stats    jsonb NOT NULL DEFAULT '{}',
  next_sync_at  timestamptz NOT NULL DEFAULT now(),
  created_at    timestamptz NOT NULL DEFAULT now(),
  UNIQUE (user_id, provider)
);
INSERT INTO crm_settings (key, value) VALUES ('capture_allow_bodies', 'false') ON CONFLICT (key) DO NOTHING;

-- ── Phase 1.1: account owner, manual contacts, email notifications ──────────
ALTER TABLE accounts ADD COLUMN IF NOT EXISTS owner_user_id int REFERENCES users(id) ON DELETE SET NULL;
ALTER TABLE personas ADD COLUMN IF NOT EXISTS created_by_user_id int REFERENCES users(id) ON DELETE SET NULL;

CREATE TABLE IF NOT EXISTS crm_notifications (
  id          bigserial PRIMARY KEY,
  user_id     int REFERENCES users(id) ON DELETE CASCADE,
  to_email    text NOT NULL,
  kind        text NOT NULL,
  subject     text NOT NULL,
  body        text NOT NULL,
  html        text,
  link        text,
  dedupe_key  text UNIQUE,                         -- the same event never emails twice
  status      text NOT NULL DEFAULT 'queued' CHECK (status IN ('queued','sent','logged','held','skipped','failed')),
  attempts    int NOT NULL DEFAULT 0,
  error       text,
  created_at  timestamptz NOT NULL DEFAULT now(),
  sent_at     timestamptz
);
CREATE INDEX IF NOT EXISTS crm_notifications_queue ON crm_notifications (id) WHERE status = 'queued';
CREATE INDEX IF NOT EXISTS crm_notifications_user ON crm_notifications (user_id, created_at DESC);

CREATE TABLE IF NOT EXISTS crm_notification_prefs (
  user_id    int PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
  settings   jsonb NOT NULL DEFAULT '{}',          -- {kind: true|false}; missing = default on
  updated_at timestamptz NOT NULL DEFAULT now()
);
-- Org-wide switch: nothing is emailed until an admin turns this on (notifications are kept as 'held').
INSERT INTO crm_settings (key, value) VALUES ('email_notifications_enabled', 'false') ON CONFLICT (key) DO NOTHING;
