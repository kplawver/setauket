"""Shared visible-turn model and tree-session reader for OMP and Pi."""

import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

MAX_SESSION_BYTES = 50 * 1024 * 1024
MAX_TURN_CHARS = 250_000


@dataclass(frozen=True)
class ImportedTurn:
    event_id: str
    role: str
    content: str
    created_at: float


@dataclass(frozen=True)
class ImportedSession:
    source_id: str
    project_key: str | None
    started_at: float
    turns: list[ImportedTurn]


def _timestamp(value: str) -> float:
    when = datetime.fromisoformat(value)
    if when.tzinfo is None:
        raise ValueError("OMP timestamps must include a timezone")
    return when.timestamp()


def _visible_content(content: object) -> str:
    if isinstance(content, str):
        text = content
    elif isinstance(content, list):
        text = "\n".join(block["text"] for block in content if isinstance(block, dict)
                         and block.get("type") == "text" and isinstance(block.get("text"), str))
    else:
        text = ""
    if len(text) > MAX_TURN_CHARS:
        raise ValueError("Visible turn exceeds 250,000 characters; no data imported")
    return text.strip()


def parse_session(path: Path, harness: str = "OMP") -> ImportedSession:
    """Follow the active leaf; alternative branches and tool results stay in the source."""
    path = Path(path).expanduser().resolve(strict=True)
    if path.suffix != ".jsonl" or path.name.startswith("__"):
        raise ValueError(f"Provide a main {harness} session .jsonl file, not a sidecar")
    if path.stat().st_size > MAX_SESSION_BYTES:
        raise ValueError("Session exceeds the 50 MiB import limit")

    header = None
    entries: dict[str, dict] = {}
    leaf = None
    with path.open(encoding="utf-8") as stream:
        for line_number, line in enumerate(stream, 1):
            try:
                entry = json.loads(line)
            except json.JSONDecodeError as error:
                if line_number > 1 and not line.endswith("\n"):
                    break  # A session still being written may have one partial tail entry.
                raise ValueError(f"Invalid session JSON at line {line_number}") from error
            if not isinstance(entry, dict):
                raise TypeError(f"Invalid session entry at line {line_number}")
            if entry.get("type") == "session":
                if header is not None:
                    raise ValueError("More than one session header")
                header = entry
                continue
            event_id = entry.get("id")
            if isinstance(event_id, str):
                if event_id in entries:
                    raise ValueError(f"Duplicate session entry ID at line {line_number}")
                entries[event_id] = entry
                leaf = event_id
    if not header or not isinstance(header.get("id"), str) or not isinstance(header.get("cwd"), str):
        raise ValueError(f"Missing {harness} session header, session ID, or project path")
    if not Path(header["cwd"]).is_absolute():
        raise ValueError(f"{harness} session project path must be absolute")
    path_entries = []
    visited = set()
    while leaf is not None:
        if leaf in visited or leaf not in entries:
            raise ValueError(f"Broken or cyclic {harness} session tree")
        visited.add(leaf)
        entry = entries[leaf]
        if "parentId" not in entry:
            raise ValueError(f"{harness} session entry is missing its parent link")
        path_entries.append(entry)
        leaf = entry["parentId"]
    path_entries.reverse()

    turns = []
    for entry in path_entries:
        if entry.get("type") != "message":
            continue
        message = entry.get("message")
        if not isinstance(message, dict) or message.get("role") not in {"user", "assistant"}:
            continue
        content = _visible_content(message.get("content"))
        turns.append(ImportedTurn(entry["id"], message["role"], content,
                                  _timestamp(entry["timestamp"])))
    return ImportedSession(header["id"], header["cwd"], _timestamp(header["timestamp"]), turns)
