"""Opt-in, per-project capture of visible harness turns."""

import json
import os
import re
import tempfile
from pathlib import Path

from setauket.config import Config
from setauket.identity import CAPTURE_HARNESSES
from setauket.storage import Store

# Canonical lifecycle events -> (role, content key) per harness. The aliases map
# each harness's event spelling onto a canonical name, so one Claude Code-format
# payload shape serves every shell-out harness and the bundled extensions emit
# the omp shape. Capture stays fail-closed: an unknown event or a missing text
# field records nothing.
_ROLES = {
    "claude": {"submit": ("user", "prompt"), "stop": ("assistant", "last_assistant_message")},
    "codex": {"submit": ("user", "prompt"), "stop": ("assistant", "last_assistant_message")},
    "devin": {"submit": ("user", "prompt"), "stop": ("assistant", "last_assistant_message")},
    "copilot": {"submit": ("user", "prompt"), "stop": ("assistant", "last_assistant_message")},
    "omp": {"submit": ("user", "text"), "stop": ("assistant", "text")},
    "opencode": {"submit": ("user", "text"), "stop": ("assistant", "text")},
    "cline": {"submit": ("user", "text"), "stop": ("assistant", "text")},
}
_ALIASES = {
    "userpromptsubmit": "submit",
    "userpromptsubmitted": "submit",
    "input": "submit",
    "stop": "stop",
    "agentstop": "stop",
    "agentend": "stop",
}
# Alternate visible-text fields where a harness has not settled on one name.
_CONTENT_FALLBACKS = {("copilot", "stop"): ("lastAssistantMessage", "response")}


def _event(name: object) -> str | None:
    if not isinstance(name, str):
        return None
    return _ALIASES.get(re.sub(r"[^a-z]", "", name.lower()))


def _project(event: dict, harness: str) -> object:
    value = event.get("cwd")
    if harness == "devin" and not isinstance(value, str):
        value = os.environ.get("DEVIN_PROJECT_DIR")  # Devin sets this instead of a cwd field.
    return value


def _path(config: Config, harness: str) -> Path:
    if harness not in CAPTURE_HARNESSES:
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
    if harness not in CAPTURE_HARNESSES:
        raise ValueError("Unsupported capture harness")
    kind = _event(event.get("hook_event_name"))
    if kind is None:
        return False
    role, content_key = _ROLES[harness][kind]
    project = _project(event, harness)
    if not isinstance(project, str) or not Path(project).is_absolute() or not enabled(config, Path(project), harness):
        return False
    if harness == "omp" and role == "user" and event.get("source") not in {"interactive", "rpc"}:
        return False
    session_id = event.get("session_id")
    prompt_id = event.get("prompt_id")
    content = event.get(content_key)
    if not isinstance(content, str) or not content.strip():
        for alternate in _CONTENT_FALLBACKS.get((harness, kind), ()):
            content = event.get(alternate)
            if isinstance(content, str) and content.strip():
                break
    if not isinstance(session_id, str) or not session_id or not isinstance(content, str) or not content.strip():
        return False
    if prompt_id is not None and not isinstance(prompt_id, str):
        raise ValueError("Invalid prompt ID")
    transcript = event.get("transcript_path")
    if harness == "omp" and not isinstance(transcript, str):
        return False  # Honor --no-session: ephemeral OMP sessions are not captured.
    transcript_path = str(Path(transcript).expanduser().resolve()) if isinstance(transcript, str) else None
    return Store(config.database).capture_turn(harness, str(Path(project).resolve()), session_id,
                                               prompt_id, role, content, transcript_path)
