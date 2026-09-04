-- Data model of 13장. SQLite dialect; the same DDL maps to PostgreSQL when a
-- single PC stops being enough (20.1).
--
-- TB_EPISODE deliberately has no start_sec/end_sec: an episode is an ordered
-- set of cuts (TB_EDIT_TIMELINE), not a range over the source (13.1).

PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS tb_project (
    project_id      TEXT PRIMARY KEY,
    file_path       TEXT NOT NULL,
    duration_sec    REAL NOT NULL DEFAULT 0,
    status          TEXT NOT NULL DEFAULT 'QUEUED',
    profile_name    TEXT NOT NULL DEFAULT 'default',
    channel_ref     TEXT NOT NULL DEFAULT '',
    length_hint_sec REAL,
    created_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS tb_state_log (
    log_id      INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id  TEXT NOT NULL REFERENCES tb_project(project_id) ON DELETE CASCADE,
    state       TEXT NOT NULL,
    detail      TEXT NOT NULL DEFAULT '',
    at          TEXT NOT NULL
);

-- 5장: the raw understanding layer -----------------------------------------
CREATE TABLE IF NOT EXISTS tb_utterance (
    utterance_id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id   TEXT NOT NULL REFERENCES tb_project(project_id) ON DELETE CASCADE,
    start_sec    REAL NOT NULL,
    end_sec      REAL NOT NULL,
    speaker      TEXT NOT NULL DEFAULT 'UNKNOWN',
    track        TEXT NOT NULL DEFAULT 'mic',
    text         TEXT NOT NULL,
    words_json   TEXT NOT NULL DEFAULT '[]'
);
CREATE INDEX IF NOT EXISTS ix_utterance_time ON tb_utterance(project_id, start_sec);

CREATE TABLE IF NOT EXISTS tb_situation_span (
    span_id     INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id  TEXT NOT NULL REFERENCES tb_project(project_id) ON DELETE CASCADE,
    start_sec   REAL NOT NULL,
    end_sec     REAL NOT NULL,
    label       TEXT NOT NULL,
    speakers    TEXT NOT NULL DEFAULT '[]',
    evidence    TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS tb_window_summary (
    window_id      INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id     TEXT NOT NULL REFERENCES tb_project(project_id) ON DELETE CASCADE,
    start_sec      REAL NOT NULL,
    end_sec        REAL NOT NULL,
    summary        TEXT NOT NULL DEFAULT '',
    people         TEXT NOT NULL DEFAULT '[]',
    topics         TEXT NOT NULL DEFAULT '[]',
    screen         TEXT NOT NULL DEFAULT '',
    notable        INTEGER NOT NULL DEFAULT 0,
    notable_reason TEXT NOT NULL DEFAULT '',
    tension_peak   REAL NOT NULL DEFAULT 0,
    markers        TEXT NOT NULL DEFAULT '[]'
);
CREATE INDEX IF NOT EXISTS ix_window_time ON tb_window_summary(project_id, start_sec);

CREATE TABLE IF NOT EXISTS tb_detail_span (
    detail_id       INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id      TEXT NOT NULL REFERENCES tb_project(project_id) ON DELETE CASCADE,
    start_sec       REAL NOT NULL,
    end_sec         REAL NOT NULL,
    exact_start_sec REAL,
    exact_end_sec   REAL,
    beats           TEXT NOT NULL DEFAULT '[]',
    notes           TEXT NOT NULL DEFAULT ''
);

-- 5.4: long-term memory -----------------------------------------------------
CREATE TABLE IF NOT EXISTS tb_event (
    event_id   TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES tb_project(project_id) ON DELETE CASCADE,
    summary    TEXT NOT NULL DEFAULT '',
    people     TEXT NOT NULL DEFAULT '[]',
    relations  TEXT NOT NULL DEFAULT '[]'
);

CREATE TABLE IF NOT EXISTS tb_event_mention (
    mention_id       INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id         TEXT NOT NULL REFERENCES tb_event(event_id) ON DELETE CASCADE,
    source_start_sec REAL NOT NULL,
    source_end_sec   REAL NOT NULL,
    role             TEXT NOT NULL DEFAULT '',
    quote            TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS ix_mention_event ON tb_event_mention(event_id);

-- 6장: discovery ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS tb_content_candidate (
    candidate_id         TEXT PRIMARY KEY,
    project_id           TEXT NOT NULL REFERENCES tb_project(project_id) ON DELETE CASCADE,
    core_summary         TEXT NOT NULL DEFAULT '',
    related_event_ids    TEXT NOT NULL DEFAULT '[]',
    required_context     TEXT NOT NULL DEFAULT '',
    required_context_sec REAL NOT NULL DEFAULT 0,
    independence_score   REAL NOT NULL DEFAULT 0,
    density_score        REAL NOT NULL DEFAULT 0,
    has_resolution       INTEGER NOT NULL DEFAULT 1,
    decision             TEXT NOT NULL DEFAULT 'hold',
    decision_reason      TEXT NOT NULL DEFAULT '',
    combine_with         TEXT NOT NULL DEFAULT '[]',
    human_verdict        TEXT
);

-- 7-10장: episodes and their timelines --------------------------------------
CREATE TABLE IF NOT EXISTS tb_episode (
    episode_id           TEXT PRIMARY KEY,
    project_id           TEXT NOT NULL REFERENCES tb_project(project_id) ON DELETE CASCADE,
    candidate_ids        TEXT NOT NULL DEFAULT '[]',
    title_candidates     TEXT NOT NULL DEFAULT '[]',
    planned_structure    TEXT NOT NULL DEFAULT '{}',
    target_type          TEXT NOT NULL DEFAULT '',
    planned_duration_sec REAL NOT NULL DEFAULT 0,
    output_mp4_path      TEXT,
    thumbnail_path       TEXT,
    thumbnail_candidates TEXT NOT NULL DEFAULT '[]',
    metadata             TEXT NOT NULL DEFAULT '{}',
    render_status        TEXT NOT NULL DEFAULT 'pending',
    review_status        TEXT NOT NULL DEFAULT 'not_submitted',
    notes                TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS tb_edit_timeline (
    cut_id           INTEGER PRIMARY KEY AUTOINCREMENT,
    episode_id       TEXT NOT NULL REFERENCES tb_episode(episode_id) ON DELETE CASCADE,
    sequence_order   INTEGER NOT NULL,
    source_start_sec REAL NOT NULL,
    source_end_sec   REAL NOT NULL,
    speaker_tag      TEXT NOT NULL DEFAULT 'UNKNOWN',
    scene_role       TEXT NOT NULL DEFAULT '',
    pacing_mode      TEXT NOT NULL DEFAULT 'TRIM',
    pacing_reason    TEXT NOT NULL DEFAULT '',
    visual_effect    TEXT NOT NULL DEFAULT '{}',
    audio_effect     TEXT NOT NULL DEFAULT '{}',
    subtitle_ref     TEXT,
    silences         TEXT NOT NULL DEFAULT '[]',
    remove_spans     TEXT NOT NULL DEFAULT '[]'
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_timeline_order ON tb_edit_timeline(episode_id, sequence_order);

CREATE TABLE IF NOT EXISTS tb_subtitle (
    subtitle_id INTEGER PRIMARY KEY AUTOINCREMENT,
    episode_id  TEXT NOT NULL REFERENCES tb_episode(episode_id) ON DELETE CASCADE,
    start_sec   REAL NOT NULL,
    end_sec     REAL NOT NULL,
    text        TEXT NOT NULL,
    speaker     TEXT NOT NULL DEFAULT 'UNKNOWN',
    emphasis    INTEGER NOT NULL DEFAULT 0,
    style       TEXT
);

-- 12.3: the three learning loops --------------------------------------------
CREATE TABLE IF NOT EXISTS tb_yt_reference (           -- loop A
    ref_id             TEXT PRIMARY KEY,
    video_id           TEXT NOT NULL,
    channel_id         TEXT NOT NULL DEFAULT '',
    public_metrics     TEXT NOT NULL DEFAULT '{}',     -- public metrics only (4.2)
    extracted_patterns TEXT NOT NULL DEFAULT '{}',
    analyzed_at        TEXT NOT NULL
    -- no media column: reference media is discarded after analysis (4.6)
);

CREATE TABLE IF NOT EXISTS tb_source_output_pair (     -- loop B
    pair_id            TEXT PRIMARY KEY,
    source_ref         TEXT NOT NULL,
    output_ref         TEXT NOT NULL,
    selection_analysis TEXT NOT NULL DEFAULT '{}',
    created_at         TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS tb_performance (            -- loop C
    perf_id      TEXT PRIMARY KEY,
    episode_id   TEXT NOT NULL REFERENCES tb_episode(episode_id) ON DELETE CASCADE,
    metrics      TEXT NOT NULL DEFAULT '{}',
    collected_at TEXT NOT NULL
);

-- 17장 -----------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS tb_calibration_profile (
    profile_id  TEXT PRIMARY KEY,
    channel_ref TEXT NOT NULL DEFAULT '',
    name        TEXT NOT NULL DEFAULT '',
    params      TEXT NOT NULL DEFAULT '{}',
    measured_at TEXT,
    eval_score  TEXT NOT NULL DEFAULT '{}'
);

-- 11.4: quota ledger so the retry queue can target the PT reset --------------
CREATE TABLE IF NOT EXISTS tb_quota_usage (
    usage_id  INTEGER PRIMARY KEY AUTOINCREMENT,
    pt_date   TEXT NOT NULL,
    units     INTEGER NOT NULL,
    reason    TEXT NOT NULL DEFAULT '',
    at        TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_quota_date ON tb_quota_usage(pt_date);

CREATE TABLE IF NOT EXISTS tb_upload_queue (
    queue_id     INTEGER PRIMARY KEY AUTOINCREMENT,
    episode_id   TEXT NOT NULL REFERENCES tb_episode(episode_id) ON DELETE CASCADE,
    state        TEXT NOT NULL DEFAULT 'RETRY_QUEUED',
    retry_after  TEXT,
    last_error   TEXT NOT NULL DEFAULT '',
    updated_at   TEXT NOT NULL
);
-- One pending upload per episode: a retry that hits the quota again must update
-- its own row, not stack a second one on every drain attempt.
CREATE UNIQUE INDEX IF NOT EXISTS ux_upload_queue_episode ON tb_upload_queue(episode_id);
