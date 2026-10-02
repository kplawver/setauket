"""Opt-in, text-only readers for Claude Code and Codex JSONL sessions."""

import json
from pathlib import Path

from setauket.session_import import (
    MAX_SESSION_BYTES,
    ImportedSession,
    ImportedTurn,
    _timestamp,
    _visible_content,
)


def _entries(path: Path):
    if path.suffix != ".jsonl" or path.stat().st_size > MAX_SESSION_BYTES:
        raise ValueError("Provide a session JSONL file of at most 50 MiB")
    with path.open(encoding="utf-8") as stream:
        for number, line in enumerate(stream, 1):
            try:
                value = json.loads(line)
            except json.JSONDecodeError as error:
                if number > 1 and not line.endswith("\n"):
                    break
                raise ValueError(f"Invalid session JSON at line {number}") from error
            if not isinstance(value, dict):
                raise TypeError(f"Invalid session entry at line {number}")
            yield number, value


def _project(value: object) -> str:
    if not isinstance(value, str) or not Path(value).is_absolute():
        raise ValueError("Session is missing an absolute project directory")
    return value


def parse_claude(path: Path) -> ImportedSession:
    """Walk the most recent main-chain leaf, ignoring tool-result user messages."""
    entries = {}
    leaf = None
    sidechain = path.parent.name == "subagents"
    for _, entry in _entries(path):
        event_id = entry.get("uuid")
        if not isinstance(event_id, str):
            continue
        if event_id in entries:
            raise ValueError("Duplicate Claude Code entry UUID")
        entries[event_id] = entry
        if entry.get("type") in {"user", "assistant"} and bool(entry.get("isSidechain")) == sidechain:
            leaf = event_id
    if leaf is None:
        raise ValueError("No Claude Code messages found for this session")
    active = []
    seen = set()
    while leaf is not None:
        if leaf in seen or leaf not in entries:
            raise ValueError("Broken or cyclic Claude Code session tree")
        seen.add(leaf)
        current = entries[leaf]
        active.append(current)
        leaf = current.get("parentUuid")
    active.reverse()
    source_ids = {e.get("sessionId") for e in active if isinstance(e.get("sessionId"), str)}
    if len(source_ids) != 1:
        raise ValueError("Claude Code session ID is missing or inconsistent")
    source_id = source_ids.pop()
    if sidechain:
        agent_ids = {e.get("agentId") for e in active if isinstance(e.get("agentId"), str)}
        if len(agent_ids) != 1:
            raise ValueError("Claude Code subagent ID is missing or inconsistent")
        source_id += f":agent:{agent_ids.pop()}"
    project = next((e["cwd"] for e in active if isinstance(e.get("cwd"), str)), None)
    turns = []
    for entry in active:
        if entry.get("type") not in {"user", "assistant"} or bool(entry.get("isSidechain")) != sidechain:
            continue
        message = entry.get("message")
        if not isinstance(message, dict) or message.get("role") != entry["type"]:
            continue
        turns.append(ImportedTurn(entry["uuid"], entry["type"],
                                  _visible_content(message.get("content")), _timestamp(entry["timestamp"])))
    return ImportedSession(source_id, _project(project), _timestamp(active[0]["timestamp"]), turns)


def parse_codex(path: Path) -> ImportedSession:
    """Use visible event messages, not duplicate response items or hidden instructions."""
    metadata = None
    turns = []
    for number, entry in _entries(path):
        payload = entry.get("payload")
        if not isinstance(payload, dict):
            continue
        if entry.get("type") == "session_meta":
            if not turns:
                metadata = payload  # Forked sessions may start with the parent's metadata.
            elif metadata is None or payload.get("id") != metadata.get("id"):
                raise ValueError("Codex session metadata changed after conversation start")
        if entry.get("type") != "event_msg":
            continue
        kind = payload.get("type")
        if kind not in {"user_message", "agent_message"}:
            continue
        text = payload.get("message")
        role = "user" if kind == "user_message" else "assistant"
        turns.append(ImportedTurn(f"line:{number}", role, _visible_content(text), _timestamp(entry["timestamp"])))
    if not metadata or not isinstance(metadata.get("id"), str):
        raise ValueError("Missing Codex session metadata")
    return ImportedSession(metadata["id"], _project(metadata.get("cwd")),
                           _timestamp(metadata["timestamp"]), turns)
