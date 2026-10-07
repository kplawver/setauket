"""Harness, agent and project identity, shared in shape with Clothesline.

Each service keeps its own copy of these tables so neither depends on the other.
Registering the same (installation_key, external_id) pair resolves to the same
logical agent within a service; row IDs are not comparable across services.
"""

import time
import uuid

IDENTITY_SCHEMA = """
CREATE TABLE IF NOT EXISTS harnesses (
  id TEXT PRIMARY KEY, installation_key TEXT UNIQUE NOT NULL, name TEXT NOT NULL, created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS agents (
  id TEXT PRIMARY KEY, harness_id TEXT NOT NULL REFERENCES harnesses(id),
  external_id TEXT NOT NULL, parent_id TEXT REFERENCES agents(id), created_at REAL NOT NULL,
  UNIQUE(harness_id, external_id)
);
CREATE TABLE IF NOT EXISTS projects (
  id TEXT PRIMARY KEY, project_key TEXT UNIQUE NOT NULL, name TEXT NOT NULL
);
"""


def uid() -> str:
    return uuid.uuid4().hex


# Harnesses with hook-based capture. Each entry needs a per-harness consent file,
# a capture-<harness> CLI command, event normalization in setauket.capture, and a
# hook file or extension under plugins/ or integrations/.
CAPTURE_HARNESSES = frozenset({"claude", "omp", "codex", "devin", "copilot", "opencode", "cline"})
CAPTURE_HARNESS_NAMES = {
    "claude": "Claude Code",
    "omp": "Oh My Pi",
    "codex": "OpenAI Codex",
    "devin": "Devin CLI",
    "copilot": "GitHub Copilot CLI",
    "opencode": "OpenCode",
    "cline": "Cline",
}


def register_harness(db, installation_key: str, name: str) -> str:
    if not installation_key.strip() or not name.strip():
        raise ValueError("An installation key and harness name are required")
    db.execute("INSERT OR IGNORE INTO harnesses VALUES (?,?,?,?)", (uid(), installation_key, name, time.time()))
    return db.execute("SELECT id FROM harnesses WHERE installation_key=?", (installation_key,)).fetchone()[0]


def register_agent(db, harness_id: str, external_id: str, parent_id: str | None = None) -> str:
    if parent_id and not db.execute("SELECT 1 FROM agents WHERE id=? AND harness_id=?", (parent_id, harness_id)).fetchone():
        raise ValueError("Parent agent does not belong to the harness")
    db.execute("INSERT OR IGNORE INTO agents VALUES (?,?,?,?,?)", (uid(), harness_id, external_id, parent_id, time.time()))
    return db.execute("SELECT id FROM agents WHERE harness_id=? AND external_id=?", (harness_id, external_id)).fetchone()[0]


def actor(db, harness_id: str, agent_id: str) -> None:
    if not db.execute("SELECT 1 FROM agents WHERE id=? AND harness_id=?", (agent_id, harness_id)).fetchone():
        raise ValueError("Agent does not belong to the harness")


def project(db, key: str | None) -> str | None:
    if not key:
        return None
    db.execute("INSERT OR IGNORE INTO projects VALUES (?,?,?)", (uid(), key, key.rsplit("/", 1)[-1]))
    return db.execute("SELECT id FROM projects WHERE project_key=?", (key,)).fetchone()[0]