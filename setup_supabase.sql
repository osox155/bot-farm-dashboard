-- Paste this into Supabase SQL Editor (https://supabase.com/dashboard/project/stfrmlgckxnzlmvietcx/sql/new)
-- and click "Run" once.

CREATE TABLE IF NOT EXISTS accounts (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    name TEXT NOT NULL,
    bot_name TEXT NOT NULL DEFAULT '',
    created_at TIMESTAMPTZ DEFAULT NOW(),
    last_active TIMESTAMPTZ,
    status TEXT DEFAULT 'unknown',
    UNIQUE(name, bot_name)
);

CREATE TABLE IF NOT EXISTS sessions (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    session_id TEXT UNIQUE NOT NULL,
    bot_name TEXT DEFAULT '',
    started_at TIMESTAMPTZ,
    ended_at TIMESTAMPTZ,
    accounts_count INTEGER DEFAULT 0,
    total_replies INTEGER DEFAULT 0,
    total_messages INTEGER DEFAULT 0,
    total_failures INTEGER DEFAULT 0,
    status TEXT DEFAULT 'running'
);

CREATE TABLE IF NOT EXISTS events (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    session_id TEXT NOT NULL,
    bot_name TEXT DEFAULT '',
    account_name TEXT,
    event_type TEXT NOT NULL,
    option_type TEXT,
    details JSONB,
    created_at TIMESTAMPTZ NOT NULL
);

CREATE TABLE IF NOT EXISTS login_attempts (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    session_id TEXT,
    bot_name TEXT DEFAULT '',
    account_name TEXT NOT NULL,
    success INTEGER NOT NULL DEFAULT 0,
    reason TEXT,
    attempted_at TIMESTAMPTZ NOT NULL
);

CREATE TABLE IF NOT EXISTS daily_stats (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    date DATE NOT NULL,
    bot_name TEXT DEFAULT '',
    account_name TEXT,
    total_replies INTEGER DEFAULT 0,
    total_messages INTEGER DEFAULT 0,
    total_failures INTEGER DEFAULT 0,
    login_failures INTEGER DEFAULT 0,
    UNIQUE(date, bot_name, account_name)
);

-- Remote control command queue: the cloud dashboard enqueues a command here,
-- and the broker running on the operator's PC polls + executes it locally, then
-- marks it done. This is how the web dashboard controls the bots on the correct
-- machine (the dashboard itself runs in the cloud and cannot touch the PC).
CREATE TABLE IF NOT EXISTS bot_commands (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    action TEXT NOT NULL,          -- start | stop | restart | start-all | stop-all | restart-all
    bot_name TEXT,                 -- target bot for single-bot actions; NULL for *-all
    status TEXT NOT NULL DEFAULT 'pending',  -- pending | done | error
    result TEXT,                   -- broker's execution result/message
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    executed_at TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_bot_commands_status ON bot_commands(status);
CREATE INDEX IF NOT EXISTS idx_bot_commands_created ON bot_commands(created_at);

CREATE INDEX IF NOT EXISTS idx_events_session ON events(session_id);
CREATE INDEX IF NOT EXISTS idx_events_bot ON events(bot_name);
CREATE INDEX IF NOT EXISTS idx_events_account ON events(account_name);
CREATE INDEX IF NOT EXISTS idx_events_created ON events(created_at);
CREATE INDEX IF NOT EXISTS idx_daily_date ON daily_stats(date);

-- Allow anon key to access all tables (needed for bot writes)
ALTER TABLE accounts ENABLE ROW LEVEL SECURITY;
ALTER TABLE sessions ENABLE ROW LEVEL SECURITY;
ALTER TABLE events ENABLE ROW LEVEL SECURITY;
ALTER TABLE login_attempts ENABLE ROW LEVEL SECURITY;
ALTER TABLE daily_stats ENABLE ROW LEVEL SECURITY;
ALTER TABLE bot_commands ENABLE ROW LEVEL SECURITY;

CREATE POLICY "anon_all_accounts" ON accounts FOR ALL USING (true) WITH CHECK (true);
CREATE POLICY "anon_all_sessions" ON sessions FOR ALL USING (true) WITH CHECK (true);
CREATE POLICY "anon_all_events" ON events FOR ALL USING (true) WITH CHECK (true);
CREATE POLICY "anon_all_login_attempts" ON login_attempts FOR ALL USING (true) WITH CHECK (true);
CREATE POLICY "anon_all_daily_stats" ON daily_stats FOR ALL USING (true) WITH CHECK (true);
CREATE POLICY "anon_all_bot_commands" ON bot_commands FOR ALL USING (true) WITH CHECK (true);
