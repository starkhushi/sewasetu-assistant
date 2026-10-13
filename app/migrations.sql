-- Additions for the agent channel. Every statement is idempotent; app.py runs this
-- file at start-up under an advisory lock, so a fresh database and a restored one
-- both end up in the same shape. schema.sql and seed.sql are left untouched.

-- Which channel an application came from: WEB (the form), AGENT (an AI assistant
-- through the MCP server), or COUNTER for paper. Seeded rows default to WEB.
ALTER TABLE applications ADD COLUMN IF NOT EXISTS channel VARCHAR(20) DEFAULT 'WEB';

-- Pension is payable from the month the application was made, not the month it was
-- decided. Set when an application is approved (by an officer or deemed).
ALTER TABLE applications ADD COLUMN IF NOT EXISTS effective_from DATE;

-- Annual life certificate: due a year after approval, then a year after each
-- confirmation. Informational for the citizen and the officer; nothing is paused by it.
ALTER TABLE applications ADD COLUMN IF NOT EXISTS life_certificate_due DATE;
ALTER TABLE applications ADD COLUMN IF NOT EXISTS life_certified_at TIMESTAMP;

-- An application being filled in by an assistant, keyed by the verified mobile.
-- Same fields as the web form's session draft, same two-hour spirit (stale drafts
-- are simply overwritten).
CREATE TABLE IF NOT EXISTS drafts (
    mobile     VARCHAR(15) PRIMARY KEY,
    data       JSONB NOT NULL DEFAULT '{}'::jsonb,
    doc_path   VARCHAR(200),
    channel    VARCHAR(20),
    updated_at TIMESTAMP
);

-- One-time links so a document can be chosen on the phone's browser and attached
-- to the draft, when the assistant itself cannot carry the file.
CREATE TABLE IF NOT EXISTS upload_tokens (
    token      VARCHAR(64) PRIMARY KEY,
    mobile     VARCHAR(15),
    created_at TIMESTAMP,
    used_at    TIMESTAMP
);

-- "Approved but the money never came" and similar, raised by the citizen against an
-- application and closed by an officer with a recorded resolution.
CREATE TABLE IF NOT EXISTS grievances (
    id             SERIAL PRIMARY KEY,
    application_id INTEGER,
    mobile         VARCHAR(15),
    kind           VARCHAR(30),
    detail         VARCHAR(1000),
    status         VARCHAR(20) DEFAULT 'OPEN',
    channel        VARCHAR(20),
    raised_at      TIMESTAMP,
    resolved_at    TIMESTAMP,
    resolved_by    VARCHAR(50),
    resolution     VARCHAR(1000)
);
-- Copies of uploaded documents, keyed by the doc_path stored on the application. The
-- upload folder stays the primary copy; this one survives hosts with throwaway disks.
CREATE TABLE IF NOT EXISTS documents (
    path       VARCHAR(200) PRIMARY KEY,
    content    BYTEA,
    created_at TIMESTAMP
);

-- The website's own chat assistant (assistant.py). One row per signed-in conversation:
-- the model's history, what the person sees, and the sign-in it acts under.
CREATE TABLE IF NOT EXISTS assistant_chats (
    id            VARCHAR(48) PRIMARY KEY,
    mobile        VARCHAR(15),
    contents      JSONB NOT NULL DEFAULT '[]'::jsonb,
    display       JSONB NOT NULL DEFAULT '[]'::jsonb,
    access_token  TEXT,
    refresh_token TEXT,
    token_exp     TIMESTAMP,
    steps         INTEGER DEFAULT 0,
    created_at    TIMESTAMP,
    updated_at    TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_grievance_app ON grievances(application_id);
CREATE INDEX IF NOT EXISTS idx_grievance_status ON grievances(status);
CREATE INDEX IF NOT EXISTS idx_app_bank ON applications(bank_account);

-- Application numbers are drawn at random; officers and citizens look cases up by them,
-- so two applications must never share one.
CREATE UNIQUE INDEX IF NOT EXISTS uq_app_no ON applications(application_no);
