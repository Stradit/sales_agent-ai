-- Deals pipeline schema (apps/sales_copilot/README.md §21.3). Idempotent.

CREATE TABLE IF NOT EXISTS deals (
  id              bigserial PRIMARY KEY,
  account_id      int  NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
  lob_id          int  REFERENCES lobs(id) ON DELETE SET NULL,
  name            text NOT NULL CHECK (length(name) BETWEEN 1 AND 200),
  owner_user_id   int  REFERENCES users(id) ON DELETE SET NULL,
  stage           text NOT NULL DEFAULT 'intro'
                  CHECK (stage IN ('intro','discovery','proposal','pilot','contract','won','lost')),
  offerings       text[] NOT NULL DEFAULT '{}',
  value_amount    numeric(14,2) CHECK (value_amount IS NULL OR value_amount >= 0),
  currency        text NOT NULL DEFAULT 'USD',
  expected_close  date,
  next_step       text,
  next_step_due   date,
  lost_reason     text,
  stage_changed_at timestamptz NOT NULL DEFAULT now(),
  created_at      timestamptz NOT NULL DEFAULT now(),
  updated_at      timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS deals_account ON deals (account_id);
CREATE INDEX IF NOT EXISTS deals_stage ON deals (stage);

CREATE TABLE IF NOT EXISTS deal_stakeholders (
  deal_id    bigint NOT NULL REFERENCES deals(id) ON DELETE CASCADE,
  persona_id int    NOT NULL REFERENCES personas(id) ON DELETE CASCADE,
  role       text   NOT NULL CHECK (role IN ('champion','economic_buyer','technical_evaluator','influencer','blocker','user')),
  sentiment  text   CHECK (sentiment IN ('positive','neutral','negative')),
  added_at   timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (deal_id, persona_id)
);

CREATE TABLE IF NOT EXISTS deal_stage_history (
  id         bigserial PRIMARY KEY,
  deal_id    bigint NOT NULL REFERENCES deals(id) ON DELETE CASCADE,
  from_stage text,
  to_stage   text NOT NULL,
  changed_by int REFERENCES users(id) ON DELETE SET NULL,
  changed_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS deal_stage_history_deal ON deal_stage_history (deal_id, changed_at);

CREATE TABLE IF NOT EXISTS deal_checklist (
  deal_id  bigint NOT NULL REFERENCES deals(id) ON DELETE CASCADE,
  stage    text   NOT NULL,
  item_key text   NOT NULL,
  label    text   NOT NULL,
  ordinal  int    NOT NULL DEFAULT 0,
  done     boolean NOT NULL DEFAULT false,
  note     text,
  done_by  int REFERENCES users(id) ON DELETE SET NULL,
  done_at  timestamptz,
  PRIMARY KEY (deal_id, stage, item_key)
);

CREATE TABLE IF NOT EXISTS deal_activity (
  id         bigserial PRIMARY KEY,
  deal_id    bigint NOT NULL REFERENCES deals(id) ON DELETE CASCADE,
  user_id    int REFERENCES users(id) ON DELETE SET NULL,
  kind       text NOT NULL CHECK (kind IN ('note','stage','checklist','stakeholder','field','created')),
  text       text NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS deal_activity_deal ON deal_activity (deal_id, created_at DESC);

ALTER TABLE action_items ADD COLUMN IF NOT EXISTS deal_id bigint REFERENCES deals(id) ON DELETE SET NULL;
CREATE INDEX IF NOT EXISTS action_items_deal ON action_items (deal_id) WHERE deal_id IS NOT NULL;

-- D3: MEDDICC qualification notes (free text per key)
ALTER TABLE deals ADD COLUMN IF NOT EXISTS qualification jsonb NOT NULL DEFAULT '{}';
