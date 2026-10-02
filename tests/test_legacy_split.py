"""The v5 -> two-database split must preserve every row and both integrity checks."""

import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

# The split produces both databases, so this test needs both packages installed. It skips
# rather than fails when Clothesline is absent, which is the normal state for this repo.
pytest.importorskip("clothesline", reason="the split script also produces the Clothesline database")

from clothesline.bus import MessageBus
from clothesline.storage import Store as ClotheslineStore
from split_legacy_db import LEGACY_VERSION, split

from setauket.models import EMBED_MODEL, EMBED_REVISION
from setauket.storage import Store as SetauketStore

LEGACY_SCHEMA = """
CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS harnesses (
  id TEXT PRIMARY KEY, installation_key TEXT UNIQUE NOT NULL, name TEXT NOT NULL, created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS agents (
  id TEXT PRIMARY KEY, harness_id TEXT NOT NULL REFERENCES harnesses(id),
  external_id TEXT NOT NULL, parent_id TEXT REFERENCES agents(id), created_at REAL NOT NULL,
  UNIQUE(harness_id, external_id)
);
CREATE TABLE IF NOT EXISTS projects (id TEXT PRIMARY KEY, project_key TEXT UNIQUE NOT NULL, name TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS sessions (
  id TEXT PRIMARY KEY, harness_id TEXT NOT NULL REFERENCES harnesses(id),
  agent_id TEXT NOT NULL REFERENCES agents(id), project_id TEXT REFERENCES projects(id),
  started_at REAL NOT NULL, last_turn_at REAL NOT NULL, ended_at REAL, summary_id TEXT, archived_at REAL
);
CREATE TABLE IF NOT EXISTS turns (
  id TEXT PRIMARY KEY, session_id TEXT NOT NULL REFERENCES sessions(id),
  harness_id TEXT NOT NULL REFERENCES harnesses(id), agent_id TEXT NOT NULL REFERENCES agents(id),
  source_id TEXT NOT NULL, role TEXT NOT NULL, content TEXT NOT NULL, created_at REAL NOT NULL,
  UNIQUE(session_id, source_id)
);
CREATE TABLE IF NOT EXISTS memories (
  id TEXT PRIMARY KEY, kind TEXT NOT NULL, content TEXT NOT NULL, project_id TEXT REFERENCES projects(id),
  harness_id TEXT NOT NULL REFERENCES harnesses(id), agent_id TEXT NOT NULL REFERENCES agents(id),
  supersedes_id TEXT REFERENCES memories(id), superseded_by TEXT REFERENCES memories(id), created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS chunks (
  id INTEGER PRIMARY KEY, category TEXT NOT NULL, source_id TEXT NOT NULL, content TEXT NOT NULL,
  created_at REAL NOT NULL, project_id TEXT REFERENCES projects(id), harness_id TEXT NOT NULL,
  agent_id TEXT NOT NULL, embedded INTEGER NOT NULL DEFAULT 0
);
CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(content, chunk_id UNINDEXED, tokenize='porter unicode61');
CREATE TABLE IF NOT EXISTS jobs (
  id INTEGER PRIMARY KEY, kind TEXT NOT NULL, source_id TEXT NOT NULL,
  attempts INTEGER NOT NULL DEFAULT 0, error TEXT, next_at REAL NOT NULL, UNIQUE(kind, source_id)
);
CREATE TABLE IF NOT EXISTS agent_presence (
  agent_id TEXT PRIMARY KEY REFERENCES agents(id), online_at REAL NOT NULL,
  last_seen_at REAL NOT NULL, offline_at REAL
);
CREATE TABLE IF NOT EXISTS conversations (
  id TEXT PRIMARY KEY, title TEXT NOT NULL, project_id TEXT REFERENCES projects(id),
  session_id TEXT REFERENCES sessions(id), created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS messages (
  id INTEGER PRIMARY KEY AUTOINCREMENT, conversation_id TEXT NOT NULL REFERENCES conversations(id),
  sender_harness_id TEXT NOT NULL, sender_agent_id TEXT NOT NULL, recipient_agent_id TEXT,
  client_message_id TEXT NOT NULL, body TEXT NOT NULL, created_at REAL NOT NULL,
  UNIQUE(sender_agent_id, client_message_id)
);
CREATE TABLE IF NOT EXISTS message_deliveries (
  message_id INTEGER NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
  recipient_agent_id TEXT NOT NULL, acknowledged_at REAL,
  PRIMARY KEY(message_id, recipient_agent_id)
);
CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5(body, message_id UNINDEXED, tokenize='porter unicode61');
CREATE TABLE IF NOT EXISTS import_sources (
  source_path TEXT PRIMARY KEY, source_session_id TEXT NOT NULL, session_id TEXT REFERENCES sessions(id),
  last_event_id TEXT, segment INTEGER NOT NULL DEFAULT 0, history_hash TEXT
);
CREATE TABLE IF NOT EXISTS capture_sources (
  source_key TEXT PRIMARY KEY, session_id TEXT NOT NULL, project_key TEXT NOT NULL,
  agent_id TEXT NOT NULL, current_prompt_key TEXT, current_prompt_hash TEXT,
  prompt_answered INTEGER NOT NULL DEFAULT 0, next_prompt INTEGER NOT NULL DEFAULT 0,
  segment INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS capture_events (
  source_key TEXT NOT NULL, source_id TEXT NOT NULL, content_hash TEXT NOT NULL,
  PRIMARY KEY(source_key, source_id)
);
"""

VECTOR = [0.1] * 384


@pytest.fixture
def legacy(tmp_path):
    """A minimal but structurally faithful schema-v5 database."""
    sqlite_vec = pytest.importorskip("sqlite_vec")
    path = tmp_path / "legacy.sqlite3"
    db = sqlite3.connect(path)
    db.enable_load_extension(True)
    sqlite_vec.load(db)
    db.enable_load_extension(False)
    db.executescript(LEGACY_SCHEMA)
    db.execute("CREATE VIRTUAL TABLE vec_history USING vec0(embedding float[384])")
    db.execute("CREATE VIRTUAL TABLE vec_memory USING vec0(embedding float[384])")
    db.executemany("INSERT INTO metadata VALUES (?,?)",
                   [("embedding_model", EMBED_MODEL), ("embedding_revision", EMBED_REVISION)])
    db.execute("INSERT INTO harnesses VALUES ('h1','zed-install','Zed',1.0)")
    db.execute("INSERT INTO agents VALUES ('a1','h1','zed-main',NULL,1.0)")
    db.execute("INSERT INTO agents VALUES ('a2','h1','zed-sub','a1',1.0)")
    db.execute("INSERT INTO projects VALUES ('p1','repo/drill','drill')")
    db.execute("INSERT INTO sessions VALUES ('s1','h1','a1','p1',1.0,2.0,NULL,NULL,NULL)")
    db.execute("INSERT INTO turns VALUES ('t1','s1','h1','a1','event-1','user',"
               "'The parser drops quoted commas',2.0)")
    db.execute("INSERT INTO memories VALUES ('m1','decision','Prefer table tests',"
               "'p1','h1','a1',NULL,NULL,3.0)")
    db.execute("INSERT INTO chunks VALUES (7,'hot','t1','The parser drops quoted commas',2.0,'p1','h1','a1',1)")
    db.execute("INSERT INTO chunks_fts(content,chunk_id) VALUES ('The parser drops quoted commas',7)")
    db.execute("INSERT INTO vec_history(rowid,embedding) VALUES (7,?)",
               (sqlite_vec.serialize_float32(VECTOR),))
    db.execute("INSERT INTO agent_presence VALUES ('a1',1.0,1.0,NULL)")
    db.execute("INSERT INTO conversations VALUES ('c1','Cross-service','p1','s1',1.0)")
    db.execute("INSERT INTO messages VALUES (1,'c1','h1','a1','a2','m1','Review this please',1.0)")
    db.execute("INSERT INTO messages_fts(body,message_id) VALUES ('Review this please',1)")
    db.execute("INSERT INTO message_deliveries VALUES (1,'a2',NULL)")
    db.execute("INSERT INTO import_sources VALUES ('/tmp/src.jsonl','ext-1','s1','event-1',0,'hash')")
    db.execute("INSERT INTO capture_sources VALUES ('claude-hook:repo/drill:ext-1','s1','repo/drill','a1',"
               "NULL,NULL,0,0,0)")
    db.execute("INSERT INTO capture_events VALUES ('claude-hook:repo/drill:ext-1','user:p1','digest')")
    db.execute(f"PRAGMA user_version={LEGACY_VERSION}")
    db.commit()
    db.close()
    return path


def counts(path: Path, tables) -> dict:
    db = sqlite3.connect(path)
    try:
        return {table: db.execute(f"SELECT count(*) FROM {table}").fetchone()[0] for table in tables}
    finally:
        db.close()


def test_split_preserves_every_row_on_both_sides(legacy, tmp_path):
    setauket_out = tmp_path / "Setauket/setauket.sqlite3"
    clothesline_out = tmp_path / "Clothesline/clothesline.sqlite3"
    report = split(legacy, setauket_out, clothesline_out)

    assert setauket_out.stat().st_mode & 0o777 == 0o600
    assert clothesline_out.stat().st_mode & 0o777 == 0o600

    # Nothing was lost or invented on either side.
    for table, row in report.items():
        for target, value in row.items():
            if target in {"main", "bus", "legacy"}:
                assert value == row["legacy"], f"{table} on {target}"

    # Setauket received the session history; Clothesline received the conversation.
    memory = SetauketStore(setauket_out)
    session = memory.get_session("s1")
    assert [turn["content"] for turn in session["turns"]] == ["The parser drops quoted commas"]
    assert memory.get_memory("m1")[0]["content"] == "Prefer table tests"
    assert memory.search_rows("quoted")[0]["session_id"] == "s1"
    # Chunks stayed embedded, so the copied vectors need no re-embedding pass.
    assert memory.search_rows("quoted", vector=VECTOR)[0]["source_id"] == "t1"
    with memory.connect() as db:
        assert db.execute("SELECT embedded FROM chunks WHERE id=7").fetchone()[0] == 1
        assert db.execute("PRAGMA user_version").fetchone()[0] == 1

    bus = ClotheslineStore(clothesline_out)
    with bus.connect() as db:
        row = dict(db.execute("SELECT * FROM conversations WHERE id='c1'").fetchone())
    # The session reference survives as an opaque string now that sessions live elsewhere.
    assert row["session_id"] == "s1"
    assert bus.rebuild_indexes() == 1
    with bus.connect() as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 1
        assert not db.execute("SELECT name FROM sqlite_master WHERE name='turns'").fetchone()
        assert not db.execute("SELECT name FROM sqlite_master WHERE name='chunks'").fetchone()

    # Identity survived in both databases with the same IDs, and the bus still works.
    assert memory.register_agent("h1", "zed-main") == "a1"
    assert bus.register_agent("h1", "zed-main") == "a1"
    presence = MessageBus(bus).agents(True)
    # Presence survived with its original timestamp, so the fixture agent reads as stale.
    assert [(agent["external_id"], agent["status"]) for agent in presence] == [("zed-main", "stale")]
    assert MessageBus(bus).search("Review this")[0]["conversation_id"] == "c1"


def test_split_refuses_existing_outputs_and_wrong_versions(legacy, tmp_path):
    taken = tmp_path / "taken.sqlite3"
    taken.write_bytes(b"")
    with pytest.raises(SystemExit, match="Refusing to overwrite"):
        split(legacy, taken, tmp_path / "fresh.sqlite3")

    with sqlite3.connect(legacy) as db:
        db.execute(f"PRAGMA user_version={LEGACY_VERSION - 1}")
    with pytest.raises(SystemExit, match="Expected a schema v5"):
        split(legacy, tmp_path / "a.sqlite3", tmp_path / "b.sqlite3")


def test_split_creates_missing_legacy_tables(legacy, tmp_path):
    """An untouched legacy database may have empty tables; the split must still succeed."""
    with sqlite3.connect(legacy) as db:
        db.execute("DELETE FROM capture_events")
        db.execute("DELETE FROM message_deliveries")
    setauket_out = tmp_path / "setauket.sqlite3"
    clothesline_out = tmp_path / "clothesline.sqlite3"
    report = split(legacy, setauket_out, clothesline_out)
    assert report["capture_events"]["legacy"] == 0
    assert SetauketStore(setauket_out).get_session("s1")["id"] == "s1"