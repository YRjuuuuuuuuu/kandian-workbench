import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS accounts(id TEXT PRIMARY KEY, name TEXT NOT NULL, mode_id TEXT NOT NULL, is_demo INTEGER NOT NULL DEFAULT 0, paused INTEGER NOT NULL DEFAULT 0, daily_limit INTEGER NOT NULL DEFAULT 2);
CREATE TABLE IF NOT EXISTS sources(id TEXT PRIMARY KEY, name TEXT NOT NULL, mode_id TEXT NOT NULL, url TEXT NOT NULL, rights_note TEXT NOT NULL, enabled INTEGER NOT NULL DEFAULT 0, interval_minutes INTEGER NOT NULL DEFAULT 30, last_check TEXT, last_error TEXT, etag TEXT, modified TEXT);
CREATE TABLE IF NOT EXISTS topics(id TEXT PRIMARY KEY, account_id TEXT NOT NULL REFERENCES accounts(id), mode_id TEXT NOT NULL, event_key TEXT NOT NULL, data TEXT NOT NULL, version INTEGER NOT NULL DEFAULT 1, is_demo INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL, UNIQUE(account_id,event_key));
CREATE TABLE IF NOT EXISTS articles(id TEXT PRIMARY KEY, topic_id TEXT NOT NULL REFERENCES topics(id), account_id TEXT NOT NULL REFERENCES accounts(id), template_id TEXT NOT NULL, revision INTEGER NOT NULL DEFAULT 1, data TEXT NOT NULL, material_hash TEXT NOT NULL, revision_hash TEXT NOT NULL, approval_hash TEXT, status TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS revisions(id INTEGER PRIMARY KEY, article_id TEXT NOT NULL, revision INTEGER NOT NULL, data TEXT NOT NULL, revision_hash TEXT NOT NULL, created_at TEXT NOT NULL, UNIQUE(article_id,revision));
CREATE TABLE IF NOT EXISTS jobs(id TEXT PRIMARY KEY, topic_id TEXT NOT NULL REFERENCES topics(id), template_id TEXT NOT NULL, provider TEXT NOT NULL, state TEXT NOT NULL, input_hash TEXT NOT NULL, payload TEXT NOT NULL, result TEXT, error TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS schedules(id TEXT PRIMARY KEY, article_id TEXT NOT NULL REFERENCES articles(id), account_id TEXT NOT NULL, revision_hash TEXT NOT NULL, due_at TEXT NOT NULL, state TEXT NOT NULL, remote_url TEXT, proof_note TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL, UNIQUE(article_id,revision_hash));
CREATE TABLE IF NOT EXISTS metrics(id TEXT PRIMARY KEY, article_id TEXT NOT NULL REFERENCES articles(id), observed_at TEXT NOT NULL, horizon TEXT NOT NULL, data TEXT NOT NULL, is_demo INTEGER NOT NULL DEFAULT 0, UNIQUE(article_id,observed_at));
CREATE TABLE IF NOT EXISTS policies(id TEXT PRIMARY KEY, account_id TEXT NOT NULL REFERENCES accounts(id), version INTEGER NOT NULL, data TEXT NOT NULL, active INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL, UNIQUE(account_id,version));
CREATE TABLE IF NOT EXISTS audit(id INTEGER PRIMARY KEY, created_at TEXT NOT NULL, action TEXT NOT NULL, object_id TEXT, detail TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS ix_jobs_state ON jobs(state);
CREATE INDEX IF NOT EXISTS ix_schedule_due ON schedules(state,due_at);
CREATE TABLE IF NOT EXISTS agent_runs(id TEXT PRIMARY KEY, account_id TEXT NOT NULL REFERENCES accounts(id), kind TEXT NOT NULL, state TEXT NOT NULL, stage TEXT NOT NULL, payload TEXT NOT NULL, input_hash TEXT NOT NULL, result TEXT, error TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL, schedule_key TEXT UNIQUE);
DROP INDEX IF EXISTS ix_agent_account_active;
CREATE INDEX IF NOT EXISTS ix_agent_account_queue ON agent_runs(account_id,state,created_at);
CREATE TABLE IF NOT EXISTS agent_events(id INTEGER PRIMARY KEY, run_id TEXT NOT NULL REFERENCES agent_runs(id), created_at TEXT NOT NULL, kind TEXT NOT NULL, message TEXT NOT NULL, data TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS agent_checkpoints(run_id TEXT PRIMARY KEY REFERENCES agent_runs(id), data TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS agent_candidates(id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES agent_runs(id), account_id TEXT NOT NULL REFERENCES accounts(id), event_key TEXT NOT NULL, data TEXT NOT NULL, decision TEXT NOT NULL DEFAULT 'pending', topic_id TEXT, article_id TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS ix_candidates_account ON agent_candidates(account_id,decision);
CREATE TABLE IF NOT EXISTS agent_signals(id TEXT PRIMARY KEY, account_id TEXT NOT NULL REFERENCES accounts(id), platform TEXT NOT NULL, url TEXT NOT NULL, observed_at TEXT NOT NULL, data TEXT NOT NULL, UNIQUE(account_id,platform,url,observed_at));
CREATE TABLE IF NOT EXISTS photo_assets(id TEXT PRIMARY KEY, article_id TEXT NOT NULL REFERENCES articles(id), source_url TEXT NOT NULL, image_url TEXT NOT NULL, local_name TEXT NOT NULL, sha256 TEXT NOT NULL, mime TEXT NOT NULL, data TEXT NOT NULL, selected INTEGER NOT NULL DEFAULT 0, position INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL, UNIQUE(article_id,image_url));
CREATE TABLE IF NOT EXISTS article_suggestions(id TEXT PRIMARY KEY, article_id TEXT NOT NULL REFERENCES articles(id), run_id TEXT NOT NULL, base_revision INTEGER NOT NULL, data TEXT NOT NULL, state TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS article_exports(id TEXT PRIMARY KEY, article_id TEXT NOT NULL REFERENCES articles(id), revision INTEGER NOT NULL, revision_hash TEXT NOT NULL, document_hash TEXT NOT NULL, snapshot TEXT NOT NULL, format TEXT NOT NULL, created_at TEXT NOT NULL, delivered_at TEXT);
CREATE INDEX IF NOT EXISTS ix_article_exports ON article_exports(article_id,delivered_at DESC);
CREATE TABLE IF NOT EXISTS article_publications(id TEXT PRIMARY KEY, article_id TEXT NOT NULL REFERENCES articles(id), export_id TEXT REFERENCES article_exports(id), revision INTEGER, revision_hash TEXT NOT NULL, document_hash TEXT NOT NULL, snapshot TEXT NOT NULL, title TEXT NOT NULL, confirmed_at TEXT NOT NULL, published_at TEXT, remote_url TEXT NOT NULL DEFAULT '', source TEXT NOT NULL, state TEXT NOT NULL, version INTEGER NOT NULL DEFAULT 1, updated_at TEXT NOT NULL);
CREATE UNIQUE INDEX IF NOT EXISTS ix_publication_active ON article_publications(article_id) WHERE state='active';
CREATE TABLE IF NOT EXISTS writer_profiles(id TEXT PRIMARY KEY, mode_id TEXT NOT NULL, version INTEGER NOT NULL, state TEXT NOT NULL, data TEXT NOT NULL, created_at TEXT NOT NULL, UNIQUE(mode_id,version));
"""


def dumps(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


class Store:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.db() as db:
            db.executescript(SCHEMA)

    @contextmanager
    def db(self):
        db = sqlite3.connect(self.path, timeout=20)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("PRAGMA journal_mode=WAL")
        try:
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def all(self, sql, params=()):
        with self.db() as db:
            return [dict(row) for row in db.execute(sql, params)]

    def one(self, sql, params=()):
        with self.db() as db:
            row = db.execute(sql, params).fetchone()
            return dict(row) if row else None
