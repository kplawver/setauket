"""SQLite persistence. Content tables and search indexes live in the same database."""

import hashlib
import re
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path

import sqlite_vec

from setauket import identity
from setauket.identity import uid
from setauket.models import EMBED_MODEL, EMBED_REVISION
from setauket.session_import import ImportedSession, ImportedTurn

MEMORY_SCHEMA = """
CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS sessions (
  id TEXT PRIMARY KEY, harness_id TEXT NOT NULL REFERENCES harnesses(id),
  agent_id TEXT NOT NULL REFERENCES agents(id), project_id TEXT REFERENCES projects(id),
  started_at REAL NOT NULL, last_turn_at REAL NOT NULL, ended_at REAL,
  summary_id TEXT, archived_at REAL
);
CREATE INDEX IF NOT EXISTS sessions_age ON sessions(last_turn_at) WHERE archived_at IS NULL;
CREATE TABLE IF NOT EXISTS turns (
  id TEXT PRIMARY KEY, session_id TEXT NOT NULL REFERENCES sessions(id),
  harness_id TEXT NOT NULL REFERENCES harnesses(id), agent_id TEXT NOT NULL REFERENCES agents(id),
  source_id TEXT NOT NULL, role TEXT NOT NULL, content TEXT NOT NULL, created_at REAL NOT NULL,
  UNIQUE(session_id, source_id)
);
CREATE INDEX IF NOT EXISTS turns_session ON turns(session_id, created_at);
CREATE TABLE IF NOT EXISTS summaries (
  id TEXT PRIMARY KEY, session_id TEXT UNIQUE NOT NULL REFERENCES sessions(id),
  content TEXT NOT NULL, model TEXT NOT NULL, created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS memories (
  id TEXT PRIMARY KEY, kind TEXT NOT NULL CHECK(kind IN ('decision', 'preference')),
  content TEXT NOT NULL, project_id TEXT REFERENCES projects(id),
  harness_id TEXT NOT NULL REFERENCES harnesses(id), agent_id TEXT NOT NULL REFERENCES agents(id),
  supersedes_id TEXT UNIQUE REFERENCES memories(id), superseded_by TEXT UNIQUE REFERENCES memories(id),
  created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS chunks (
  id INTEGER PRIMARY KEY, category TEXT NOT NULL CHECK(category IN ('hot','cold','memory')),
  source_id TEXT NOT NULL, content TEXT NOT NULL, created_at REAL NOT NULL,
  project_id TEXT REFERENCES projects(id), harness_id TEXT NOT NULL REFERENCES harnesses(id),
  agent_id TEXT NOT NULL REFERENCES agents(id), embedded INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS chunks_source ON chunks(category, source_id);
CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(content, chunk_id UNINDEXED, tokenize='porter unicode61');
CREATE TABLE IF NOT EXISTS jobs (
  id INTEGER PRIMARY KEY, kind TEXT NOT NULL, source_id TEXT NOT NULL,
  attempts INTEGER NOT NULL DEFAULT 0, error TEXT, next_at REAL NOT NULL,
  UNIQUE(kind, source_id)
);
"""

IMPORT_SCHEMA = """
CREATE TABLE IF NOT EXISTS import_sources (
  source_path TEXT PRIMARY KEY, source_session_id TEXT NOT NULL,
  session_id TEXT REFERENCES sessions(id), last_event_id TEXT, segment INTEGER NOT NULL DEFAULT 0,
  history_hash TEXT
);
"""

CAPTURE_SCHEMA = """
CREATE TABLE IF NOT EXISTS capture_sources (
  source_key TEXT PRIMARY KEY, session_id TEXT NOT NULL REFERENCES sessions(id),
  project_key TEXT NOT NULL, agent_id TEXT NOT NULL REFERENCES agents(id),
  current_prompt_key TEXT, current_prompt_hash TEXT,
  prompt_answered INTEGER NOT NULL DEFAULT 0, next_prompt INTEGER NOT NULL DEFAULT 0,
  segment INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS capture_events (
  source_key TEXT NOT NULL REFERENCES capture_sources(source_key),
  source_id TEXT NOT NULL, content_hash TEXT NOT NULL,
  PRIMARY KEY(source_key, source_id)
);
"""


class Store:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path.parent.chmod(0o700)
        if self.path.exists():
            self.path.chmod(0o600)
        with self.connect() as db:
            version = db.execute("PRAGMA user_version").fetchone()[0]
            if version not in (0, 1):
                raise ValueError("Unsupported database version; upgrade Setauket before opening it")
            db.executescript(identity.IDENTITY_SCHEMA)
            db.executescript(MEMORY_SCHEMA)
            db.executescript(IMPORT_SCHEMA)
            db.executescript(CAPTURE_SCHEMA)
            db.execute("CREATE VIRTUAL TABLE IF NOT EXISTS vec_history USING vec0(embedding float[384])")
            db.execute("CREATE VIRTUAL TABLE IF NOT EXISTS vec_memory USING vec0(embedding float[384])")
            db.execute("INSERT OR IGNORE INTO metadata VALUES ('embedding_model',?)", (EMBED_MODEL,))
            stored = db.execute("SELECT value FROM metadata WHERE key='embedding_model'").fetchone()[0]
            if stored != EMBED_MODEL:
                raise ValueError("Embedding model changed; indexes must be rebuilt before continuing")
            db.execute("INSERT OR IGNORE INTO metadata VALUES ('embedding_revision',?)", (EMBED_REVISION,))
            revision = db.execute("SELECT value FROM metadata WHERE key='embedding_revision'").fetchone()[0]
            if revision != EMBED_REVISION:
                raise ValueError("Embedding revision changed; indexes must be rebuilt before continuing")
            db.execute("PRAGMA user_version=1")
        self.path.chmod(0o600)

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("PRAGMA busy_timeout=10000")
        db.execute("PRAGMA journal_mode=WAL")
        db.enable_load_extension(True)
        sqlite_vec.load(db)
        db.enable_load_extension(False)
        try:
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def register_harness(self, installation_key: str, name: str) -> str:
        with self.connect() as db:
            return identity.register_harness(db, installation_key, name)

    def register_agent(self, harness_id: str, external_id: str, parent_id: str | None = None) -> str:
        with self.connect() as db:
            return identity.register_agent(db, harness_id, external_id, parent_id)

    def start_session(self, harness_id: str, agent_id: str, project_key: str | None = None) -> str:
        with self.connect() as db:
            identity.actor(db, harness_id, agent_id)
            project_id = identity.project(db, project_key)
            session_id = uid()
            now = time.time()
            db.execute("INSERT INTO sessions (id,harness_id,agent_id,project_id,started_at,last_turn_at) VALUES (?,?,?,?,?,?)",
                       (session_id, harness_id, agent_id, project_id, now, now))
            return session_id

    def end_session(self, session_id: str, harness_id: str) -> None:
        with self.connect() as db:
            row = db.execute("SELECT harness_id FROM sessions WHERE id=?", (session_id,)).fetchone()
            if not row or row[0] != harness_id:
                raise ValueError("Unknown session for harness")
            db.execute("UPDATE sessions SET ended_at=? WHERE id=?", (time.time(), session_id))

    def add_turn(self, session_id: str, harness_id: str, agent_id: str, source_id: str,
                 role: str, content: str) -> str:
        if role not in {"user", "assistant", "tool", "system"} or not source_id or not content.strip():
            raise ValueError("A source ID, valid role and nonempty content are required")
        with self.connect() as db:
            identity.actor(db, harness_id, agent_id)
            session = db.execute("SELECT harness_id, project_id, archived_at FROM sessions WHERE id=?", (session_id,)).fetchone()
            if not session or session[0] != harness_id or session[2] is not None:
                raise ValueError("Unknown or archived session for harness")
            existing = db.execute("SELECT id FROM turns WHERE session_id=? AND source_id=?", (session_id, source_id)).fetchone()
            if existing:
                return existing[0]
            return self._write_turn(db, session_id, session["project_id"], harness_id,
                                    agent_id, source_id, role, content, time.time())

    def _write_turn(self, db, session_id: str, project_id: str | None,
                    harness_id: str, agent_id: str, source_id: str, role: str,
                    content: str, created_at: float) -> str:
        turn_id = uid()
        db.execute("INSERT INTO turns VALUES (?,?,?,?,?,?,?,?)",
                   (turn_id, session_id, harness_id, agent_id, source_id, role, content, created_at))
        db.execute("UPDATE sessions SET last_turn_at=MAX(last_turn_at,?), ended_at=NULL WHERE id=?",
                   (created_at, session_id))
        self._add_chunks(db, "hot", turn_id, content, created_at, project_id, harness_id, agent_id)
        return turn_id

    def remember(self, kind: str, content: str, harness_id: str, agent_id: str,
                 project_key: str | None = None, supersedes_id: str | None = None) -> str:
        if kind not in {"decision", "preference"} or not content.strip():
            raise ValueError("A decision or preference needs content")
        with self.connect() as db:
            identity.actor(db, harness_id, agent_id)
            project_id = identity.project(db, project_key)
            if supersedes_id:
                previous = db.execute("SELECT kind,project_id,superseded_by FROM memories WHERE id=?", (supersedes_id,)).fetchone()
                if not previous or previous[0] != kind or previous[1] != project_id or previous[2]:
                    raise ValueError("Memory to supersede must be current and have the same kind and scope")
            memory_id, now = uid(), time.time()
            db.execute("INSERT INTO memories VALUES (?,?,?,?,?,?,?,?,?)",
                       (memory_id, kind, content, project_id, harness_id, agent_id, supersedes_id, None, now))
            if supersedes_id:
                db.execute("UPDATE memories SET superseded_by=? WHERE id=? AND superseded_by IS NULL", (memory_id, supersedes_id))
                self._delete_chunks(db, "memory", supersedes_id)
            self._add_chunks(db, "memory", memory_id, content, now, project_id, harness_id, agent_id)
            return memory_id

    @staticmethod
    def _parts(content: str):
        # Split long content without dropping its tail; indexing is independent of raw storage.
        for offset in range(0, len(content), 600):
            yield content[offset:offset + 600]

    def _add_chunks(self, db, category, source_id, content, created_at, project_id, harness_id, agent_id):
        for part in self._parts(content):
            cursor = db.execute("INSERT INTO chunks(category,source_id,content,created_at,project_id,harness_id,agent_id) VALUES (?,?,?,?,?,?,?)",
                                (category, source_id, part, created_at, project_id, harness_id, agent_id))
            chunk_id = cursor.lastrowid
            db.execute("INSERT INTO chunks_fts(content,chunk_id) VALUES (?,?)", (part, chunk_id))
            db.execute("INSERT OR IGNORE INTO jobs(kind,source_id,next_at) VALUES ('embed',?,?)", (str(chunk_id), time.time()))

    def _delete_chunks(self, db, category: str, source_id: str):
        ids = [r[0] for r in db.execute("SELECT id FROM chunks WHERE category=? AND source_id=?", (category, source_id))]
        for chunk_id in ids:
            db.execute("DELETE FROM chunks_fts WHERE chunk_id=?", (chunk_id,))
            db.execute("DELETE FROM vec_memory WHERE rowid=?", (chunk_id,)) if category == "memory" else db.execute("DELETE FROM vec_history WHERE rowid=?", (chunk_id,))
            db.execute("DELETE FROM jobs WHERE kind='embed' AND source_id=?", (str(chunk_id),))
            db.execute("DELETE FROM chunks WHERE id=?", (chunk_id,))

    def get_session(self, session_id: str) -> dict | None:
        with self.connect() as db:
            row = db.execute("SELECT s.*,p.project_key FROM sessions s LEFT JOIN projects p ON p.id=s.project_id WHERE s.id=?", (session_id,)).fetchone()
            if not row:
                return None
            result = dict(row)
            result["turns"] = [dict(r) for r in db.execute("SELECT * FROM turns WHERE session_id=? ORDER BY created_at,rowid", (session_id,))]
            summary = db.execute("SELECT * FROM summaries WHERE session_id=?", (session_id,)).fetchone()
            result["summary"] = dict(summary) if summary else None
            return result

    def get_memory(self, memory_id: str) -> list[dict]:
        with self.connect() as db:
            row = db.execute("SELECT * FROM memories WHERE id=?", (memory_id,)).fetchone()
            if not row:
                return []
            while row["supersedes_id"]:
                row = db.execute("SELECT * FROM memories WHERE id=?", (row["supersedes_id"],)).fetchone()
            history = []
            while row:
                history.append(dict(row))
                row = db.execute("SELECT * FROM memories WHERE id=?", (row["superseded_by"],)).fetchone() if row["superseded_by"] else None
            return history

    def archive(self, session_id: str, last_turn_at: float, summary: str, model: str) -> bool:
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            session = db.execute("SELECT * FROM sessions WHERE id=?", (session_id,)).fetchone()
            if not session or session["archived_at"] is not None or session["last_turn_at"] != last_turn_at:
                return False
            summary_id, now = uid(), time.time()
            db.execute("INSERT INTO summaries VALUES (?,?,?,?,?)", (summary_id, session_id, summary, model, now))
            self._add_chunks(db, "cold", summary_id, summary, now, session["project_id"], session["harness_id"], session["agent_id"])
            for row in db.execute("SELECT id FROM turns WHERE session_id=?", (session_id,)).fetchall():
                self._delete_chunks(db, "hot", row[0])
            db.execute("DELETE FROM turns WHERE session_id=?", (session_id,))
            db.execute("UPDATE sessions SET summary_id=?, archived_at=? WHERE id=?", (summary_id, now, session_id))
            return True

    def was_captured_session(self, harness: str, project_key: str, external_session_id: str) -> bool:
        with self.connect() as db:
            return db.execute("SELECT 1 FROM capture_sources WHERE source_key=?",
                              (f"{harness}-hook:{project_key}:{external_session_id}",)).fetchone() is not None

    def was_captured_claude(self, project_key: str, claude_session_id: str) -> bool:
        return self.was_captured_session("claude", project_key, claude_session_id)

    def capture_turn(self, harness: str, project_key: str, external_session_id: str,
                     prompt_id: str | None, role: str, content: str,
                     transcript_path: str | None = None) -> bool:
        """Record a visible harness event using the same turn indexes and retention as MCP."""
        if harness not in {"claude", "omp"}:
            raise ValueError("Unsupported capture harness")
        if role not in {"user", "assistant"} or not content.strip() or len(content) > 250_000:
            raise ValueError("Capture requires visible user/assistant text of at most 250,000 characters")
        if not external_session_id or len(external_session_id) > 128 or not project_key:
            raise ValueError("Capture requires a session ID and project")
        if prompt_id and len(prompt_id) > 128:
            raise ValueError("Prompt ID is too long")
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            harness_id = identity.register_harness(db, f"setauket:{harness}:hook",
                                                   "Claude Code" if harness == "claude" else "Oh My Pi")
            agent_id = identity.register_agent(db, harness_id, f"{harness}-session:{external_session_id}")
            source_key = f"{harness}-hook:{project_key}:{external_session_id}"
            digest = hashlib.sha256(content.encode()).hexdigest()
            if transcript_path and db.execute("SELECT 1 FROM import_sources WHERE source_path=?",
                                              (transcript_path,)).fetchone():
                raise ValueError("Session was already imported manually")
            row = db.execute("SELECT * FROM capture_sources WHERE source_key=?", (source_key,)).fetchone()
            if row and (row["project_key"] != project_key or row["agent_id"] != agent_id):
                raise ValueError("Capture source identity changed")
            prompt_key = prompt_id or (row["current_prompt_key"] if row else None)
            if role == "user" and not prompt_key:
                prompt_key = "local:1"
            elif role == "user" and not prompt_id and row and (row["prompt_answered"] or row["current_prompt_hash"] != digest):
                prompt_key = f"local:{row['next_prompt'] + 1}"
            if not prompt_key:
                raise ValueError("Cannot attribute assistant message without a prompt")
            source_id = f"{role}:{prompt_key}" if role == "user" else f"assistant:{prompt_key}:{digest}"
            if row:
                previous = db.execute("SELECT content_hash FROM capture_events WHERE source_key=? AND source_id=?",
                                      (source_key, source_id)).fetchone()
                if previous:
                    if previous[0] != digest:
                        raise ValueError("Prompt ID was reused with different text")
                    return False
            session_id = row["session_id"] if row else None
            session = db.execute("SELECT archived_at,project_id FROM sessions WHERE id=?", (session_id,)).fetchone() if session_id else None
            segment = row["segment"] if row else 0
            if session is None or session["archived_at"] is not None:
                if session is not None:
                    segment += 1
                session_id, now = uid(), time.time()
                project_id = identity.project(db, project_key)
                db.execute("INSERT INTO sessions(id,harness_id,agent_id,project_id,started_at,last_turn_at) "
                           "VALUES (?,?,?,?,?,?)", (session_id, harness_id, agent_id, project_id, now, now))
            elif session["project_id"] != identity.project(db, project_key):
                raise ValueError("Captured project changed")
            if row:
                db.execute("UPDATE capture_sources SET session_id=?,segment=? WHERE source_key=?",
                           (session_id, segment, source_key))
            else:
                db.execute("INSERT INTO capture_sources(source_key,session_id,project_key,agent_id) VALUES (?,?,?,?)",
                           (source_key, session_id, project_key, agent_id))
            if role == "user":
                next_prompt = row["next_prompt"] if row else 0
                if prompt_key.startswith("local:"):
                    next_prompt = int(prompt_key.split(":", 1)[1])
                db.execute("UPDATE capture_sources SET current_prompt_key=?,current_prompt_hash=?,prompt_answered=0,next_prompt=? "
                           "WHERE source_key=?", (prompt_key, digest, next_prompt, source_key))
            else:
                db.execute("UPDATE capture_sources SET prompt_answered=1,current_prompt_key=? WHERE source_key=?",
                           (prompt_key, source_key))
            db.execute("INSERT INTO capture_events VALUES (?,?,?)", (source_key, source_id, digest))
            self._write_turn(db, session_id, identity.project(db, project_key), harness_id,
                             agent_id, source_id, role, content.strip(), time.time())
            return True

    def capture_claude(self, project_key: str, claude_session_id: str, prompt_id: str | None,
                       role: str, content: str, transcript_path: str | None = None) -> bool:
        return self.capture_turn("claude", project_key, claude_session_id, prompt_id,
                                 role, content, transcript_path)

    @staticmethod
    def _import_hash(turns: list[ImportedTurn]) -> str:
        digest = hashlib.sha256()
        for turn in turns:
            digest.update(repr((turn.event_id, turn.role, turn.content)).encode())
            digest.update(b"\0")
        return digest.hexdigest()

    def import_session(self, source_path: str, parsed: ImportedSession,
                       harness_id: str, agent_id: str) -> dict:
        """Import visible turns atomically, resuming by stable source entry ID."""
        if not parsed.turns:
            return {"session_id": None, "imported": 0, "segment": 0}
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            identity.actor(db, harness_id, agent_id)
            existing = db.execute("SELECT * FROM import_sources WHERE source_path=?", (source_path,)).fetchone()
            if existing and existing["source_session_id"] != parsed.source_id:
                raise ValueError("This source now contains a different session")
            event_ids = [turn.event_id for turn in parsed.turns]
            if existing and existing["last_event_id"] not in event_ids:
                raise ValueError("The source active branch changed; importing it as the same history would be misleading")
            first_new = event_ids.index(existing["last_event_id"]) + 1 if existing else 0
            if existing and existing["history_hash"] and self._import_hash(parsed.turns[:first_new]) != existing["history_hash"]:
                raise ValueError("Previously imported messages changed; no data imported")
            pending = [turn for turn in parsed.turns[first_new:] if turn.content]
            segment = existing["segment"] if existing else 0
            session_id = existing["session_id"] if existing else None
            current = db.execute("SELECT harness_id,agent_id,archived_at FROM sessions WHERE id=?", (session_id,)).fetchone() if session_id else None
            if session_id and (not current or current["harness_id"] != harness_id or current["agent_id"] != agent_id):
                raise ValueError("Source belongs to a different harness or agent")
            if pending:
                if current and current["archived_at"] is not None:
                    segment += 1
                    session_id = None
                if session_id is None:
                    session_id = uid()
                    project_id = identity.project(db, parsed.project_key)
                    started_at = parsed.started_at if not existing else pending[0].created_at
                    db.execute("INSERT INTO sessions(id,harness_id,agent_id,project_id,started_at,last_turn_at) "
                               "VALUES (?,?,?,?,?,?)",
                               (session_id, harness_id, agent_id, project_id, started_at,
                                max(turn.created_at for turn in pending)))
                session = db.execute("SELECT project_id,last_turn_at FROM sessions WHERE id=?", (session_id,)).fetchone()
                expected_project = identity.project(db, parsed.project_key)
                if session["project_id"] != expected_project:
                    raise ValueError("Project changed for an already imported session")
                for turn in pending:
                    self._write_turn(db, session_id, session["project_id"], harness_id, agent_id,
                                     turn.event_id, turn.role, turn.content, turn.created_at)
            if existing:
                db.execute("UPDATE import_sources SET session_id=?, last_event_id=?,segment=?,history_hash=? WHERE source_path=?",
                           (session_id, event_ids[-1], segment, self._import_hash(parsed.turns), source_path))
            else:
                db.execute("INSERT INTO import_sources(source_path,source_session_id,session_id,last_event_id,segment,history_hash) VALUES (?,?,?,?,?,?)",
                           (source_path, parsed.source_id, session_id, event_ids[-1], segment,
                            self._import_hash(parsed.turns)))
            return {"session_id": session_id, "imported": len(pending), "segment": segment}

    def import_omp(self, source_path: str, parsed: ImportedSession,
                   harness_id: str, agent_id: str) -> dict:
        return self.import_session(source_path, parsed, harness_id, agent_id)

    def rebuild_indexes(self) -> int:
        """Restore derived FTS data and requeue vector embeddings without touching source content."""
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("DELETE FROM chunks_fts")
            db.execute("DELETE FROM vec_history")
            db.execute("DELETE FROM vec_memory")
            db.execute("UPDATE chunks SET embedded=0")
            db.execute("DELETE FROM jobs WHERE kind='embed'")
            rows = db.execute("SELECT id,content FROM chunks").fetchall()
            db.executemany("INSERT INTO chunks_fts(content,chunk_id) VALUES (?,?)",
                           ((row["content"], row["id"]) for row in rows))
            db.executemany("INSERT INTO jobs(kind,source_id,next_at) VALUES ('embed',?,?)",
                           ((str(row["id"]), time.time()) for row in rows))
            return len(rows)

    def search_rows(self, query: str, project_key: str | None = None, category: str | None = None, limit: int = 10, vector: list[float] | None = None) -> list[dict]:
        if not 1 <= limit <= 50:
            raise ValueError("Limit must be between 1 and 50")
        if category not in {None, "hot", "cold", "memory"}:
            raise ValueError("Invalid category")
        with self.connect() as db:
            filters, params = [], []
            if project_key:
                filters.append("p.project_key=?")
                params.append(project_key)
            if category:
                filters.append("c.category=?")
                params.append(category)
            where = (" AND " + " AND ".join(filters)) if filters else ""
            scores = {}
            # Match through FTS's tokenizer instead of interpolating arbitrary user syntax.
            terms = [re.sub(r'[^\w]', '', word) for word in query.split() if any(char.isalnum() for char in word)]
            terms = [term for term in terms][:12]
            terms = [term for term in terms if term]
            if terms:
                expression = " OR ".join('"' + term + '"' for term in terms)
                sql = ("SELECT c.id FROM chunks_fts f JOIN chunks c ON c.id=CAST(f.chunk_id AS INTEGER) "
                       "LEFT JOIN projects p ON p.id=c.project_id WHERE chunks_fts MATCH ?" + where +
                       " ORDER BY bm25(chunks_fts) LIMIT 100")
                for rank, row in enumerate(db.execute(sql, [expression, *params])):
                    scores[row[0]] = scores.get(row[0], 0) + 1 / (60 + rank + 1)
            if vector is not None:
                for table, allowed in [("vec_history", {"hot", "cold"}), ("vec_memory", {"memory"})]:
                    if category and category not in allowed:
                        continue
                    candidates = db.execute(f"SELECT rowid,distance FROM {table} WHERE embedding MATCH ? AND k=? ORDER BY distance",
                                            (sqlite_vec.serialize_float32(vector), 100)).fetchall()
                    for rank, (chunk_id, _) in enumerate(candidates):
                        row = db.execute("SELECT c.category,p.project_key FROM chunks c LEFT JOIN projects p ON c.project_id=p.id WHERE c.id=?", (chunk_id,)).fetchone()
                        if row and (not project_key or row[1] == project_key) and (not category or row[0] == category):
                            scores[chunk_id] = scores.get(chunk_id, 0) + 1 / (60 + rank + 1)
            results = []
            for chunk_id in sorted(scores, key=scores.get, reverse=True)[:limit]:
                row = db.execute("SELECT c.*,p.project_key FROM chunks c LEFT JOIN projects p ON c.project_id=p.id WHERE c.id=?", (chunk_id,)).fetchone()
                if row["category"] == "hot":
                    source = db.execute("SELECT session_id FROM turns WHERE id=?", (row["source_id"],)).fetchone()
                elif row["category"] == "cold":
                    source = db.execute("SELECT session_id FROM summaries WHERE id=?", (row["source_id"],)).fetchone()
                else:
                    source = None
                results.append({**dict(row), "session_id": source[0] if source else None, "score": scores[chunk_id]})
            return results