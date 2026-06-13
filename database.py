import sqlite3
import hashlib
import secrets
from pathlib import Path

DB_PATH = Path(__file__).parent / "data" / "linestep.db"


def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def hash_password(pw: str) -> str:
    return hashlib.sha256(pw.encode()).hexdigest()


def init_db():
    DB_PATH.parent.mkdir(exist_ok=True)
    conn = get_db()
    c = conn.cursor()

    c.executescript("""
    CREATE TABLE IF NOT EXISTS admins (
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        username     TEXT UNIQUE NOT NULL,
        password_hash TEXT NOT NULL,
        created_at   TEXT DEFAULT (datetime('now','localtime'))
    );

    CREATE TABLE IF NOT EXISTS companies (
        id                   INTEGER PRIMARY KEY AUTOINCREMENT,
        name                 TEXT NOT NULL,
        line_channel_token   TEXT NOT NULL,
        line_channel_secret  TEXT NOT NULL,
        default_scenario_id  INTEGER,
        created_at           TEXT DEFAULT (datetime('now','localtime'))
    );

    CREATE TABLE IF NOT EXISTS campaigns (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        company_id  INTEGER NOT NULL,
        name        TEXT NOT NULL,
        description TEXT DEFAULT '',
        click_count INTEGER DEFAULT 0,
        created_at  TEXT DEFAULT (datetime('now','localtime')),
        FOREIGN KEY (company_id) REFERENCES companies(id)
    );

    CREATE TABLE IF NOT EXISTS campaign_clicks (
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        campaign_id  INTEGER NOT NULL,
        clicked_at   TEXT DEFAULT (datetime('now','localtime')),
        line_user_id TEXT,
        matched_at   TEXT,
        FOREIGN KEY (campaign_id) REFERENCES campaigns(id)
    );

    CREATE TABLE IF NOT EXISTS followers (
        id               INTEGER PRIMARY KEY AUTOINCREMENT,
        company_id       INTEGER NOT NULL,
        line_user_id     TEXT NOT NULL,
        display_name     TEXT,
        picture_url      TEXT,
        status           TEXT DEFAULT 'active',
        follow_at        TEXT DEFAULT (datetime('now','localtime')),
        unfollow_at      TEXT,
        campaign_id      INTEGER,
        tags             TEXT DEFAULT '[]',
        memo             TEXT DEFAULT '',
        delivery_paused  INTEGER DEFAULT 0,
        created_at       TEXT DEFAULT (datetime('now','localtime')),
        UNIQUE(company_id, line_user_id),
        FOREIGN KEY (company_id) REFERENCES companies(id),
        FOREIGN KEY (campaign_id) REFERENCES campaigns(id)
    );

    CREATE TABLE IF NOT EXISTS broadcasts (
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        company_id   INTEGER NOT NULL,
        title        TEXT NOT NULL,
        tag_filter   TEXT DEFAULT '',
        message_type TEXT DEFAULT 'text',
        message_content TEXT NOT NULL,
        target_count INTEGER DEFAULT 0,
        sent_count   INTEGER DEFAULT 0,
        failed_count INTEGER DEFAULT 0,
        status       TEXT DEFAULT 'pending',
        created_at   TEXT DEFAULT (datetime('now','localtime')),
        sent_at      TEXT,
        FOREIGN KEY (company_id) REFERENCES companies(id)
    );

    CREATE TABLE IF NOT EXISTS scenarios (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        company_id  INTEGER NOT NULL,
        name        TEXT NOT NULL,
        description TEXT DEFAULT '',
        is_active   INTEGER DEFAULT 1,
        created_at  TEXT DEFAULT (datetime('now','localtime')),
        FOREIGN KEY (company_id) REFERENCES companies(id)
    );

    CREATE TABLE IF NOT EXISTS scenario_steps (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        scenario_id     INTEGER NOT NULL,
        step_order      INTEGER NOT NULL,
        delay_hours     INTEGER DEFAULT 0,
        message_type    TEXT DEFAULT 'text',
        message_content TEXT NOT NULL,
        created_at      TEXT DEFAULT (datetime('now','localtime')),
        FOREIGN KEY (scenario_id) REFERENCES scenarios(id)
    );

    CREATE TABLE IF NOT EXISTS follower_scenarios (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        follower_id INTEGER NOT NULL,
        scenario_id INTEGER NOT NULL,
        started_at  TEXT DEFAULT (datetime('now','localtime')),
        status      TEXT DEFAULT 'active',
        UNIQUE(follower_id, scenario_id),
        FOREIGN KEY (follower_id) REFERENCES followers(id),
        FOREIGN KEY (scenario_id) REFERENCES scenarios(id)
    );

    CREATE TABLE IF NOT EXISTS scheduled_messages (
        id                   INTEGER PRIMARY KEY AUTOINCREMENT,
        follower_id          INTEGER NOT NULL,
        follower_scenario_id INTEGER NOT NULL,
        scenario_step_id     INTEGER NOT NULL,
        scheduled_at         TEXT NOT NULL,
        sent_at              TEXT,
        status               TEXT DEFAULT 'pending',
        error_message        TEXT,
        created_at           TEXT DEFAULT (datetime('now','localtime')),
        FOREIGN KEY (follower_id) REFERENCES followers(id),
        FOREIGN KEY (scenario_step_id) REFERENCES scenario_steps(id)
    );
    """)

    # デフォルト管理者
    existing = c.execute("SELECT id FROM admins WHERE username='admin'").fetchone()
    if not existing:
        c.execute(
            "INSERT INTO admins (username, password_hash) VALUES (?, ?)",
            ("admin", hash_password("admin1234"))
        )

    conn.commit()
    conn.close()
