"""Small command-line interface for service management."""

import argparse
import json
import sqlite3
import sys
import time
from pathlib import Path

from setauket.config import load
from setauket.identity import CAPTURE_HARNESSES

CAPTURE_COMMANDS = sorted(f"capture-{harness}" for harness in CAPTURE_HARNESSES)


def main():
    parser = argparse.ArgumentParser(prog="setauket")
    parser.add_argument("command", choices=["serve", "setup", "status", "doctor", "backup", "rebuild-index",
                                            "import-omp", "import-pi", "import-claude", "import-codex",
                                            "import-zed", "import-opencode", *CAPTURE_COMMANDS])
    parser.add_argument("--config", type=Path)
    parser.add_argument("--output", type=Path, help="Backup destination")
    parser.add_argument("--source", type=Path, help="OMP main-session JSONL file to import")
    parser.add_argument("--dry-run", action="store_true", help="Preview without writing to Setauket")
    parser.add_argument("--session-id", help="Zed thread or OpenCode session ID")
    parser.add_argument("--list", action="store_true", help="List available Zed/OpenCode session IDs only")
    parser.add_argument("--project", type=Path, default=Path.cwd(), help="Project directory for capture consent")
    capture_action = parser.add_mutually_exclusive_group()
    capture_action.add_argument("--enable", action="store_true", help="Allow turn capture for this project")
    capture_action.add_argument("--disable", action="store_true", help="Stop turn capture for this project")
    capture_action.add_argument("--hook", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    config = load(args.config)
    if args.command == "serve":
        import uvicorn

        from setauket.app import create_app

        uvicorn.run(create_app(config), host=config.host, port=config.port, workers=1)
    elif args.command == "setup":
        from setauket.models import LocalModels

        LocalModels(config.model_dir).install()
        if config.database.exists():
            from setauket.storage import Store

            with Store(config.database).connect() as db:
                db.execute("UPDATE jobs SET next_at=?,error=NULL WHERE kind IN ('embed','archive')", (time.time(),))
        print("Local models installed; pending jobs are ready to retry")
    elif args.command.startswith("capture-"):
        from setauket.capture import capture_hook, enabled, set_enabled

        harness = args.command.removeprefix("capture-")
        if args.hook:
            try:
                capture_hook(config, json.load(sys.stdin), harness)
            except Exception as error:  # noqa: BLE001 - Capture must never interrupt an agent's turn.
                print(f"Setauket capture skipped ({type(error).__name__})", file=sys.stderr)
            return  # An observational hook must never block the agent.
        if args.enable or args.disable:
            active = set_enabled(config, args.project, args.enable, harness)
        else:
            active = enabled(config, args.project, harness)
        print(json.dumps({"project": str(args.project.resolve()), "capture_enabled": active}, indent=2))
    elif args.command.startswith("import-"):
        from setauket.database_import import (
            list_session_ids,
            parse_opencode,
            parse_zed,
        )
        from setauket.file_import import parse_claude, parse_codex
        from setauket.session_import import parse_session
        from setauket.storage import Store

        harness = args.command.removeprefix("import-")
        if not args.source:
            parser.error(f"{args.command} requires --source PATH")
        source = args.source.expanduser().resolve(strict=True)
        if args.list:
            if harness not in {"zed", "opencode"}:
                parser.error("--list is only available for Zed and OpenCode")
            print(json.dumps(list_session_ids(source, harness), indent=2))
            return
        if harness in {"zed", "opencode"} and not args.session_id:
            parser.error(f"{args.command} requires --session-id (use --list to find IDs)")
        parsers = {"omp": lambda: parse_session(source), "pi": lambda: parse_session(source, "Pi"),
                   "claude": lambda: parse_claude(source), "codex": lambda: parse_codex(source),
                   "zed": lambda: parse_zed(source, args.session_id),
                   "opencode": lambda: parse_opencode(source, args.session_id)}
        parsed = parsers[harness]()
        visible = sum(bool(turn.content) for turn in parsed.turns)
        if args.dry_run:
            print(json.dumps({"source": str(source), "project": parsed.project_key,
                              "session_id": parsed.source_id, "messages": len(parsed.turns),
                              "visible_turns": visible, "reasoning_and_tool_results": "excluded",
                              "database_changed": False}, indent=2))
        elif visible:
            store = Store(config.database)
            if harness in {"claude", "omp"} and store.was_captured_session(harness, parsed.project_key, parsed.source_id):
                raise ValueError("Session already captured by hook; manual import would duplicate it")
            names = {"omp": "Oh My Pi", "pi": "Pi", "claude": "Claude Code",
                     "codex": "Codex", "zed": "Zed", "opencode": "OpenCode"}
            harness_id = store.register_harness(f"setauket:{harness}:local-import", names[harness])
            agent_id = store.register_agent(harness_id, f"{harness}-session:{parsed.source_id}")
            source_key = str(source) if harness not in {"zed", "opencode"} else f"{source}#{harness}:{parsed.source_id}"
            result = store.import_session(source_key, parsed, harness_id, agent_id)
            print(json.dumps({"source": str(source), "agent_id": agent_id, **result}, indent=2))
        else:
            print("No visible user or assistant text in this session")
    elif args.command == "rebuild-index":
        from setauket.storage import Store

        if not config.database.exists():
            parser.error("No database exists to reindex")
        count = Store(config.database).rebuild_indexes()
        print(f"Rebuilt keyword search for {count} chunks; queued embedding refresh")
    elif args.command == "backup":
        if not args.output or not config.database.exists():
            parser.error("--output and an existing database are required")
        target = sqlite3.connect(args.output)
        args.output.chmod(0o600)
        try:
            with sqlite3.connect(config.database) as source:
                source.backup(target)
        finally:
            target.close()
        print(f"Backup written to {args.output}")
    elif args.command == "status":
        from setauket.models import LocalModels

        print(json.dumps({"database": str(config.database), "database_exists": config.database.exists(),
                          "embedding_cache_exists": (LocalModels(config.model_dir).embedding_path / "model_optimized.onnx").exists(),
                          "text_model_exists": LocalModels(config.model_dir).text_path.exists(),
                          "address": f"{config.host}:{config.port}"}, indent=2))
    elif args.command == "doctor":
        from setauket.storage import Store

        store = Store(config.database)
        with store.connect() as db:
            integrity = db.execute("PRAGMA integrity_check").fetchone()[0]
            jobs = [dict(row) for row in db.execute("SELECT kind,source_id,attempts,error FROM jobs WHERE error IS NOT NULL")]
        print(json.dumps({"integrity": integrity, "failed_jobs": jobs}, indent=2))


if __name__ == "__main__":
    main()