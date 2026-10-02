"""Read-only, explicitly selected Zed and OpenCode conversations."""

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path

import zstandard

from setauket.session_import import (
    MAX_SESSION_BYTES,
    ImportedSession,
    ImportedTurn,
    _timestamp,
    _visible_content,
)


@contextmanager
def _database(path: Path):
    path = path.expanduser().resolve(strict=True)
    db = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    try:
        yield db
    finally:
        db.close()


def list_session_ids(path: Path, harness: str) -> list[str]:
    table = {"zed": "threads", "opencode": "session"}[harness]
    with _database(path) as db:
        return [row[0] for row in db.execute(f'SELECT id FROM "{table}" ORDER BY id')]


def parse_zed(path: Path, session_id: str) -> ImportedSession:
    with _database(path) as db:
        thread = db.execute("SELECT * FROM threads WHERE id=?", (session_id,)).fetchone()
    if not thread:
        raise ValueError("Unknown Zed thread ID")
    if thread["data_type"] != "zstd" or not isinstance(thread["data"], bytes):
        raise ValueError("Unsupported Zed thread encoding")
    if len(thread["data"]) > MAX_SESSION_BYTES:
        raise ValueError("Zed compressed thread exceeds 50 MiB")
    try:
        payload = zstandard.ZstdDecompressor().decompress(thread["data"], max_output_size=MAX_SESSION_BYTES)
        data = json.loads(payload)
    except (zstandard.ZstdError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("Invalid Zed thread data") from error
    if not isinstance(data, dict) or not isinstance(data.get("messages"), list):
        raise TypeError("Invalid Zed thread messages")
    folders = thread["folder_paths"]
    project = folders if isinstance(folders, str) and Path(folders).is_absolute() and "\n" not in folders else None
    started = _timestamp(thread["created_at"])
    snapshot_time = _timestamp(thread["updated_at"])
    turns = []
    for index, entry in enumerate(data["messages"]):
        if not isinstance(entry, dict):
            continue
        role = "user" if "User" in entry else "assistant" if "Agent" in entry else None
        if role is None:
            continue
        message = entry["User" if role == "user" else "Agent"]
        if not isinstance(message, dict):
            continue
        text = message.get("content")
        content = "\n".join(block["Text"] for block in text if isinstance(block, dict)
                            and isinstance(block.get("Text"), str)) if isinstance(text, list) else ""
        content = _visible_content(content)
        turns.append(ImportedTurn(f"message:{index}", role, content, snapshot_time))
    return ImportedSession(session_id, project, started, turns)


def _milliseconds(value: object) -> float:
    if not isinstance(value, int) or value <= 0:
        raise ValueError("Invalid OpenCode timestamp")
    return value / 1000


def parse_opencode(path: Path, session_id: str) -> ImportedSession:
    with _database(path) as db:
        session = db.execute("SELECT * FROM session WHERE id=?", (session_id,)).fetchone()
        if not session:
            raise ValueError("Unknown OpenCode session ID")
        project = session["directory"]
        if project is not None and (not isinstance(project, str) or not Path(project).is_absolute()):
            raise ValueError("Invalid OpenCode project directory")
        turns = []
        for msg in db.execute("SELECT id,time_created,data FROM message WHERE session_id=? ORDER BY time_created,rowid", (session_id,)):
            info = json.loads(msg["data"])
            if not isinstance(info, dict) or info.get("role") not in {"user", "assistant"}:
                continue
            parts = db.execute("SELECT data FROM part WHERE message_id=? AND session_id=? ORDER BY time_created,rowid",
                               (msg["id"], session_id))
            text = []
            for part in parts:
                data = json.loads(part["data"])
                if isinstance(data, dict) and data.get("type") == "text" and isinstance(data.get("text"), str):
                    text.append(data["text"])
            turns.append(ImportedTurn(msg["id"], info["role"], _visible_content("\n".join(text)),
                                      _milliseconds(msg["time_created"])))
    return ImportedSession(session_id, project, _milliseconds(session["time_created"]), turns)
