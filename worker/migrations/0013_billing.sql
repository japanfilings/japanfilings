-- Paid plans through a merchant of record. No card data, names or addresses are stored here;
-- the provider holds them. Subscriptions are the source of truth for users.plan.
CREATE TABLE IF NOT EXISTS billing_subscriptions (
  provider TEXT NOT NULL,
  subscription_id TEXT NOT NULL,
  user_id TEXT NOT NULL,
  customer_id TEXT,
  plan TEXT NOT NULL,                 -- free | pro | business
  status TEXT NOT NULL,               -- as normalised: active | trialing | past_due | paused | canceled
  current_period_end TEXT,
  cancel_at TEXT,                     -- set while a cancellation is scheduled; access continues until then
  last_event_at TEXT NOT NULL,        -- providers do not guarantee order; older events are not applied
  updated_at TEXT NOT NULL,
  PRIMARY KEY (provider, subscription_id)
);
CREATE INDEX IF NOT EXISTS billing_subscriptions_user ON billing_subscriptions(user_id);

-- Every processed event ID, so retries and replays are applied once.
CREATE TABLE IF NOT EXISTS billing_events (
  provider TEXT NOT NULL,
  event_id TEXT NOT NULL,
  event_type TEXT NOT NULL,
  occurred_at TEXT NOT NULL,
  received_at TEXT NOT NULL,
  PRIMARY KEY (provider, event_id)
);

-- Append-only record of what each event did. Provider IDs and plans only, no personal data.
CREATE TABLE IF NOT EXISTS billing_audit (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  at TEXT NOT NULL,
  provider TEXT NOT NULL,
  event_id TEXT NOT NULL,
  event_type TEXT NOT NULL,
  subscription_id TEXT,
  user_id TEXT,
  action TEXT NOT NULL,               -- applied | stale_ignored | unmatched_user | unknown_price
  old_plan TEXT,
  new_plan TEXT
);
CREATE INDEX IF NOT EXISTS billing_audit_user ON billing_audit(user_id, at);
