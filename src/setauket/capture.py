"""Opt-in, per-project capture of visible harness turns."""

import json
import os
import tempfile
from pathlib import Path

from setauket.config import Config
from setauket.storage import Store


def _path(config: Config, harness: str) -> Path:
    if harness not in {"claude", "omp"}:
        raise ValueError("Unsupported capture harness")
    return config.data_dir / f"{harness}-capture.json"


def _projects(config: Config, harness: str) -> set[str]:
    path = _path(config, harness)
    if not path.exists():
        return set()
    if path.stat().st_mode & 0o077:
        raise ValueError("Capture allowlist must have 0600 permissions")
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict) or not isinstance(value.get("projects"), list) or not all(
        isinstance(project, str) and Path(project).is_absolute() for project in value["projects"]
    ):
        raise ValueError("Invalid capture allowlist")
    return set(value["projects"])


def enabled(config: Config, project: Path, harness: str = "claude") -> bool:
    return str(project.resolve(strict=True)) in _projects(config, harness)


def set_enabled(config: Config, project: Path, enable: bool, harness: str = "claude") -> bool:
    """Persist an exact-path allowlist atomically with restrictive permissions."""
    key = str(project.expanduser().resolve(strict=True))
    if not Path(key).is_dir():
        raise ValueError("Capture project must be a directory")
    projects = _projects(config, harness)
    if enable:
        projects.add(key)
    else:
        projects.discard(key)
    path = _path(config, harness)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.parent.chmod(0o700)
    fd, temporary = tempfile.mkstemp(prefix=f"{harness}-capture-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump({"projects": sorted(projects)}, stream, indent=2)
            stream.write("\n")
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)
    return key in projects


def capture_hook(config: Config, event: dict, harness: str = "claude") -> bool:
    """Use only visible input and final reply fields, never transcript/tool content."""
    if not isinstance(event, dict):
        raise TypeError("Invalid hook input")
    project = event.get("cwd")
    if not isinstance(project, str) or not Path(project).is_absolute() or not enabled(config, Path(project), harness):
        return False
    kind = event.get("hook_event_name")
    roles = {"UserPromptSubmit": "user", "Stop": "assistant"} if harness == "claude" else {
        "input": "user", "agent_end": "assistant"}
    if kind not in roles:
        return False
    if harness == "omp" and kind == "input" and event.get("source") not in {"interactive", "rpc"}:
        return False
    session_id = event.get("session_id")
    prompt_id = event.get("prompt_id")
    content_key = ("prompt" if kind == "UserPromptSubmit" else "last_assistant_message") if harness == "claude" else "text"
    content = event.get(content_key)
    if not isinstance(session_id, str) or not session_id or not isinstance(content, str) or not content.strip():
        return False
    if prompt_id is not None and not isinstance(prompt_id, str):
        raise ValueError("Invalid prompt ID")
    transcript = event.get("transcript_path")
    if harness == "omp" and not isinstance(transcript, str):
        return False  # Honor --no-session: ephemeral OMP sessions are not captured.
    transcript_path = str(Path(transcript).expanduser().resolve()) if isinstance(transcript, str) else None
    return Store(config.database).capture_turn(harness, str(Path(project).resolve()), session_id,
                                               prompt_id, roles[kind], content, transcript_path)
