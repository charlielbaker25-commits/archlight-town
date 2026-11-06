"""The website pack's own tables. The engine creates actions, events and costs."""
import sqlite3

LEADS_SCHEMA = """
CREATE TABLE IF NOT EXISTS leads (
    id             INTEGER PRIMARY KEY,
    business_name  TEXT NOT NULL,
    business_type  TEXT NOT NULL,
    area           TEXT NOT NULL,
    address        TEXT,
    phone          TEXT,
    website        TEXT,
    public_email   TEXT NOT NULL DEFAULT '',   -- blank if none
    contact_route  TEXT NOT NULL CHECK (contact_route IN ('email', 'call')),
    source         TEXT NOT NULL CHECK (source = 'overture'),   -- only licensed data
    source_id      TEXT NOT NULL UNIQUE,       -- Overture's place ID, so a lead is never saved twice
    source_release TEXT NOT NULL,
    confidence     REAL,
    status         TEXT NOT NULL DEFAULT 'new',
    action_id      INTEGER REFERENCES actions(id),   -- the approval that added it (NULL when Scout saved it)
    created_at     TEXT NOT NULL,
    saved_by       TEXT NOT NULL DEFAULT 'charlie',  -- 'charlie' (an approved add_lead) or 'scout' (auto-saved, 2026-11-05)
    check_url      TEXT,                             -- the Google search link for the package card (built in plain code)
    why            TEXT                              -- Scout's one-line reason, for the package card
);
"""

# One row per `town testset` run, so runs can be compared over time (BLUEPRINT 7.1).
TESTSET_RUNS_SCHEMA = """
CREATE TABLE IF NOT EXISTS testset_runs (
    id          INTEGER PRIMARY KEY,
    ts          TEXT NOT NULL,                 -- UTC, like every timestamp
    instance    TEXT NOT NULL,                 -- 'real' or 'dev'
    git_commit  TEXT NOT NULL,                 -- the code that was scored ('+dirty' if uncommitted changes)
    cases       INTEGER NOT NULL,
    runs        INTEGER NOT NULL DEFAULT 1,    -- how many times the set was run (`--runs N`)
    earned      REAL NOT NULL,                 -- points earned across every scored agent (average over the runs)
    possible    REAL NOT NULL,
    cost_usd    REAL NOT NULL,
    scores      TEXT NOT NULL DEFAULT '{}'     -- JSON: per-agent scores, models and per-case results
);
"""

# One row per Inspector look at a saved lead's website. Builder reads the build-worthy ones,
# and Builder and Messenger use the screenshots as the "before" picture.
INSPECTIONS_SCHEMA = """
CREATE TABLE IF NOT EXISTS inspections (
    id                 INTEGER PRIMARY KEY,
    lead_id            INTEGER NOT NULL REFERENCES leads(id),
    website            TEXT NOT NULL DEFAULT '',   -- the website on the lead when it was inspected
    final_url          TEXT,                       -- where the homepage ended up after redirects
    status             TEXT NOT NULL CHECK (status IN ('done', 'failed')),   -- failed = couldn't load it
    problems           TEXT NOT NULL DEFAULT '[]', -- JSON list of the test-set categories, e.g. ["no_https"]
    need_score         INTEGER,                    -- 0 to 100
    build_worthy       INTEGER,                    -- 1 if need_score >= inspector.build_worthy_score
    note               TEXT,                       -- the "why they need help" note
    note_by            TEXT,                       -- the model that wrote it, or 'code'
    cost_usd           REAL NOT NULL DEFAULT 0,
    facts              TEXT NOT NULL DEFAULT '{}', -- JSON: everything measured, so a grade can be explained
    desktop_screenshot TEXT,                       -- file paths ("before" pictures)
    mobile_screenshot  TEXT,
    error              TEXT,
    created_at         TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_inspections_lead ON inspections(lead_id);
"""

# One row per Builder attempt: a sample site built in a private workspace folder (never in git).
# The measurements are how we judge whether 5 builds a day fit Charlie's Claude plan.
BUILDS_SCHEMA = """
CREATE TABLE IF NOT EXISTS builds (
    id                 INTEGER PRIMARY KEY,
    lead_id            INTEGER REFERENCES leads(id),   -- NULL for a test-set build
    testset_case       TEXT,                           -- the case folder, for a test-set build
    inspection_id      INTEGER REFERENCES inspections(id),
    workspace          TEXT NOT NULL,                  -- the private folder; the site is in <workspace>/dist
    status             TEXT NOT NULL CHECK (status IN ('running', 'built', 'failed')),
    failure            TEXT,                           -- why it failed, in plain words
    facts_check        TEXT NOT NULL DEFAULT '{}',     -- JSON: passed + every problem found
    quality            TEXT NOT NULL DEFAULT '{}',     -- JSON: 375px overflow, Lighthouse scores
    model              TEXT,
    rounds             INTEGER NOT NULL DEFAULT 0,     -- `claude -p` runs (1 + fix rounds)
    wall_seconds       REAL,                           -- the whole build, start to finish
    claude_seconds     REAL,                           -- time inside `claude -p` (all rounds)
    turns              INTEGER,
    input_tokens       INTEGER,
    output_tokens      INTEGER,
    cache_read_tokens  INTEGER,
    cache_write_tokens INTEGER,
    api_equivalent_usd REAL,                           -- the same tokens at API prices; NOT billed
    tool_uses          TEXT NOT NULL DEFAULT '{}',     -- JSON: tool name -> times used
    design_skill_used  INTEGER,                        -- 1 if Builder opened the frontend-design skill
    session_id         TEXT,
    dist_hash          TEXT,                           -- SHA-256 of every built file (binds a Critic verdict)
    send_back_of       INTEGER REFERENCES builds(id),  -- the first build, when this is a Critic send-back
    critic             TEXT,                           -- 'passed', 'sent_back' or 'critic_failed'; NULL = not reviewed
    kind               TEXT NOT NULL DEFAULT 'preview',  -- 'preview' (a lead's sample site) or 'homepage' (Example Web Studio's own)
    created_at         TEXT NOT NULL,
    finished_at        TEXT
);
CREATE INDEX IF NOT EXISTS idx_builds_lead ON builds(lead_id);
"""

# One row per Critic review: a fresh-eyes check of one build (or a frozen test-set site) before Charlie
# sees it. The verdict is bound to the exact files reviewed by dist_hash: any change needs a new review.
REVIEWS_SCHEMA = """
CREATE TABLE IF NOT EXISTS reviews (
    id                 INTEGER PRIMARY KEY,
    build_id           INTEGER REFERENCES builds(id),  -- NULL for a test-set fixture
    testset_case       TEXT,                           -- the critic fixture's folder, for a test-set review
    workspace          TEXT NOT NULL,                  -- the Critic's own folder (screenshots, reports, copies)
    dist_hash          TEXT,                           -- SHA-256 of every file in the reviewed dist folder
    verdict            TEXT NOT NULL CHECK (verdict IN ('pass', 'fail')),
    decided_by         TEXT NOT NULL,                  -- 'code' (a hard check failed, or no readable answer) or 'critic'
    hard_failures      TEXT NOT NULL DEFAULT '[]',     -- JSON: the plain-code checks that failed
    measurements       TEXT NOT NULL DEFAULT '{}',     -- JSON: what code measured (axe, Lighthouse, links...)
    scores             TEXT NOT NULL DEFAULT '{}',     -- JSON: honesty, design, ... each {score, reason}
    must_fix           TEXT NOT NULL DEFAULT '[]',     -- JSON: what has to change (sent back to Builder)
    nice_to_have       TEXT NOT NULL DEFAULT '[]',
    invented_claims    TEXT NOT NULL DEFAULT '[]',
    left_out_facts     TEXT NOT NULL DEFAULT '[]',
    summary            TEXT,
    previous_review_id INTEGER REFERENCES reviews(id), -- the review whose must-fix list this one confirms
    error              TEXT,                           -- why the review couldn't finish, if it didn't
    model              TEXT,
    wall_seconds       REAL,
    claude_seconds     REAL,
    turns              INTEGER,
    input_tokens       INTEGER,
    output_tokens      INTEGER,
    cache_read_tokens  INTEGER,
    cache_write_tokens INTEGER,
    api_equivalent_usd REAL,                           -- the same tokens at API prices; NOT billed
    session_id         TEXT,
    created_at         TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_reviews_build ON reviews(build_id);
"""

# Preview links and the homepage on Cloudflare Pages (Session F).
# preview_tokens: every random path ever handed out, so one is never reused (even from a rejected proposal).
# previews: what is (or was) live. The live site is always rebuilt from the rows with status 'live'.
# deployments: every upload to Cloudflare, and whether its old copy has been deleted there.
PREVIEWS_SCHEMA = """
CREATE TABLE IF NOT EXISTS preview_tokens (
    token       TEXT PRIMARY KEY,                    -- 64 hex characters (256 random bits)
    build_id    INTEGER REFERENCES builds(id),
    action_id   INTEGER REFERENCES actions(id),      -- the publish proposal that carries it
    created_at  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS previews (
    id           INTEGER PRIMARY KEY,
    kind         TEXT NOT NULL CHECK (kind IN ('preview', 'homepage')),
    build_id     INTEGER NOT NULL REFERENCES builds(id),
    lead_id      INTEGER REFERENCES leads(id),       -- NULL for the homepage
    token        TEXT UNIQUE,                        -- the private path /p/<token>/; NULL for the homepage
    dist_hash    TEXT NOT NULL,                      -- the Critic-passed files that went live
    action_id    INTEGER NOT NULL REFERENCES actions(id),   -- Charlie's approval
    status       TEXT NOT NULL CHECK (status IN ('live', 'expired', 'taken_down', 'replaced')),
    published_at TEXT NOT NULL,
    expires_at   TEXT,                               -- previews only (previews.expire_days)
    ended_at     TEXT,
    end_reason   TEXT
);
CREATE TABLE IF NOT EXISTS deployments (
    id            INTEGER PRIMARY KEY,
    deployment_id TEXT NOT NULL,                     -- Cloudflare's id (or the fake's)
    url           TEXT,                              -- its own *.pages.dev address
    fake          INTEGER NOT NULL,                  -- 1 = the fake client (dev instance, tests): nothing went online
    reason        TEXT NOT NULL,
    live_set      TEXT NOT NULL DEFAULT '{}',        -- JSON: the homepage build and the preview ids in it
    files         INTEGER,
    created_at    TEXT NOT NULL,
    deleted_at    TEXT,                              -- when its copy was deleted on Cloudflare
    delete_error  TEXT
);
"""

# The Guard's do-not-contact list (Session G): see guard.py.
DNC_SCHEMA = """
CREATE TABLE IF NOT EXISTS do_not_contact (
    id            INTEGER PRIMARY KEY,
    kind          TEXT NOT NULL CHECK (kind IN ('email', 'phone', 'business')),
    value         TEXT NOT NULL,          -- normalized: lowercase email, 10 phone digits, plain business words
    shown         TEXT NOT NULL,          -- as entered, for `town dnc list`
    reason        TEXT NOT NULL,
    added_by      TEXT NOT NULL,          -- 'charlie', 'claude-code' (dev only), or the agent that honored an opt-out
    linked_from   INTEGER REFERENCES do_not_contact(id),   -- a business blocked because its email/phone was
    created_at    TEXT NOT NULL,
    removed_at    TEXT,                   -- NULL = active
    removed_by    TEXT,
    remove_reason TEXT
);
CREATE INDEX IF NOT EXISTS idx_dnc_value ON do_not_contact(kind, value);
"""

# One row per Messenger draft (Session G): an email, a follow-up or a call script, with what it cost in
# Charlie's plan usage. A draft that broke a rule twice is kept as 'no_draft' with the reason.
DRAFTS_SCHEMA = """
CREATE TABLE IF NOT EXISTS drafts (
    id                 INTEGER PRIMARY KEY,
    lead_id            INTEGER REFERENCES leads(id),   -- NULL for a test-set draft
    testset_case       TEXT,
    kind               TEXT NOT NULL CHECK (kind IN ('email', 'follow_up', 'call_script')),
    status             TEXT NOT NULL CHECK (status IN ('proposed', 'no_draft', 'testset')),
    content            TEXT NOT NULL DEFAULT '{}',     -- JSON: subject/body, or the call script's parts
    problems           TEXT NOT NULL DEFAULT '[]',     -- JSON: rule problems found by code (no_draft = still there)
    build_id           INTEGER REFERENCES builds(id),
    preview_url        TEXT,
    action_id          INTEGER REFERENCES actions(id),
    error              TEXT,
    workspace          TEXT,
    model              TEXT,
    rounds             INTEGER NOT NULL DEFAULT 0,
    wall_seconds       REAL,
    claude_seconds     REAL,
    turns              INTEGER,
    input_tokens       INTEGER,
    output_tokens      INTEGER,
    cache_read_tokens  INTEGER,
    cache_write_tokens INTEGER,
    api_equivalent_usd REAL,                           -- the same tokens at API prices; NOT billed
    session_id         TEXT,
    created_at         TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_drafts_lead ON drafts(lead_id);
"""

# One row per approved email saved as a Gmail draft (session H). Nothing is sent: `send_email` stays a dry run
# until shadow week passes. The ids are kept so a later session can thread follow-ups and replies.
GMAIL_DRAFTS_SCHEMA = """
CREATE TABLE IF NOT EXISTS gmail_drafts (
    id          INTEGER PRIMARY KEY,
    action_id   INTEGER NOT NULL UNIQUE REFERENCES actions(id),   -- one draft per approved email, ever
    lead_id     INTEGER REFERENCES leads(id),
    draft_id    TEXT NOT NULL,              -- Gmail's draft id (or the fake's)
    message_id  TEXT NOT NULL DEFAULT '',   -- Gmail's message id inside the draft
    thread_id   TEXT NOT NULL DEFAULT '',
    to_address  TEXT NOT NULL,
    subject     TEXT NOT NULL,
    fake        INTEGER NOT NULL,           -- 1 = FakeGmail (dev, tests, Claude Code): nothing reached Google
    created_at  TEXT NOT NULL,
    content_hash    TEXT NOT NULL DEFAULT '',   -- SHA-256 of the approved To, subject and body (go-live week)
    sent_at         TEXT,                       -- set only when go-live sent this approved draft (drafts.send)
    sent_message_id TEXT,
    sent_thread_id  TEXT
);
"""

# The Clerk's inbox (go-live week): one row per message read from a Town thread (a reply, a bounce, an auto-reply).
# The reply text is DATA, never instructions: it's shown to Charlie and (only when code couldn't sort it) to Haiku
# inside a data wrapper. `kind` is what plain code (or Haiku, only toward the safe side) decided.
INBOX_SCHEMA = """
CREATE TABLE IF NOT EXISTS inbox_messages (
    id               INTEGER PRIMARY KEY,
    gmail_message_id TEXT NOT NULL UNIQUE,      -- Gmail's id (or the fake's): each message is handled once, ever
    thread_id        TEXT NOT NULL DEFAULT '',
    lead_id          INTEGER REFERENCES leads(id),
    action_id        INTEGER REFERENCES actions(id),   -- the email this answers (its send_email action), if known
    from_address     TEXT NOT NULL DEFAULT '',
    subject          TEXT NOT NULL DEFAULT '',
    received_at      TEXT,
    text             TEXT NOT NULL DEFAULT '',  -- the reply's own words (quoted history cut), at most 2000 characters
    kind             TEXT NOT NULL CHECK (kind IN ('opt_out', 'complaint', 'interested', 'call_request', 'question',
                                                   'needs_charlie', 'bounce', 'auto_reply')),
    classified_by    TEXT NOT NULL,             -- 'code' or the model that sorted it (Haiku, only toward the safe side)
    queue_action_id  INTEGER REFERENCES actions(id),   -- its "reply needs Charlie" item, if any
    created_at       TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_inbox_lead ON inbox_messages(lead_id);
"""

# The Librarian's lessons (BLUEPRINT 5.2, layer 3). Each one is proposed with its evidence and only counts once
# Charlie approves it (status follows its propose_lesson action). See packs/websites/librarian.py.
LESSONS_SCHEMA = """
CREATE TABLE IF NOT EXISTS lessons (
    id           INTEGER PRIMARY KEY,
    text         TEXT NOT NULL,             -- one short rule, e.g. "Charlie rejects previews that look thin"
    agent        TEXT NOT NULL,             -- the agent it applies to (its playbook)
    evidence     TEXT NOT NULL,             -- JSON: {"ids": ["A12", "R4", ...], "count": 3, "out_of": 3, "why": ...}
    status       TEXT NOT NULL DEFAULT 'proposed',   -- proposed, approved, rejected, retired
    action_id    INTEGER,                   -- its propose_lesson action
    retire_action_id INTEGER,               -- the approved retire_lesson action, if retired
    created_at   TEXT NOT NULL,
    decided_at   TEXT
);
"""

PACK_SCHEMAS = (LEADS_SCHEMA, TESTSET_RUNS_SCHEMA, INSPECTIONS_SCHEMA, BUILDS_SCHEMA, REVIEWS_SCHEMA,
                PREVIEWS_SCHEMA, DNC_SCHEMA, DRAFTS_SCHEMA, GMAIL_DRAFTS_SCHEMA, LESSONS_SCHEMA, INBOX_SCHEMA)

# Columns added after a table first shipped. CREATE TABLE IF NOT EXISTS never changes a table that
# already exists, so an older database gets each missing column added here: (table, column, definition).
ADDED_COLUMNS = (
    ("testset_runs", "runs", "INTEGER NOT NULL DEFAULT 1"),
    # Session E: the hash of the built files, the build a Critic send-back came from (send-backs don't
    # count toward sites_per_day), and the Critic's outcome ('passed', 'sent_back' or 'critic_failed').
    ("builds", "dist_hash", "TEXT"),
    ("builds", "send_back_of", "INTEGER REFERENCES builds(id)"),
    ("builds", "critic", "TEXT"),
    # Session F: Example Web Studio's own homepage is built like a preview but published at the root.
    ("builds", "kind", "TEXT NOT NULL DEFAULT 'preview'"),
    # Go-live week: the approved content's hash (a send must match it), and what happened when it was sent.
    ("gmail_drafts", "content_hash", "TEXT NOT NULL DEFAULT ''"),
    ("gmail_drafts", "sent_at", "TEXT"),
    ("gmail_drafts", "sent_message_id", "TEXT"),
    ("gmail_drafts", "sent_thread_id", "TEXT"),
    # One click (2026-11-05): Scout saves passing picks itself; the card needs the Check link and Scout's reason.
    ("leads", "saved_by", "TEXT NOT NULL DEFAULT 'charlie'"),
    ("leads", "check_url", "TEXT"),
    ("leads", "why", "TEXT"),
)


def upgrade(conn: sqlite3.Connection) -> None:
    """Bring an older database up to date. Safe to run every time: it only adds what's missing."""
    for table, column, definition in ADDED_COLUMNS:
        existing = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
        if existing and column not in existing:   # a missing table is created later with every column
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
