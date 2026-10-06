"""Backup/restore drill: a copied database must reopen, migrate, and recall identically."""

import json
import os
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from setauket.storage import Store
from setauket.worker import Worker

VECTOR = [0.1] * 384


class FakeModels:
    text_path = Path("/missing")

    def embed(self, text, query=False):
        return VECTOR

    def summarize(self, turns):
        return "Summary of " + str(len(turns)) + " turns"

    def unload_text(self):
        pass


def run_cli(command: list[str], data_dir: Path, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, "-m", "setauket.cli", *command],
                          env=os.environ | {"SETAUKET_DATA_DIR": str(data_dir)},
                          capture_output=True, text=True, check=check)


def counts(path: Path) -> dict:
    db = sqlite3.connect(path)
    try:
        return {table: db.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
                for table in ["harnesses", "agents", "sessions", "turns", "chunks", "memories"]}
    finally:
        db.close()


@pytest.fixture
def seeded(tmp_path):
    store = Store(tmp_path / "setauket.sqlite3")
    harness = store.register_harness("drill-install", "Zed")
    agent = store.register_agent(harness, "drill-main")
    session = store.start_session(harness, agent, "repo/drill")
    store.add_turn(session, harness, agent, "t1", "user", "The spreadsheet parser drops quoted commas")
    store.add_turn(session, harness, agent, "t2", "assistant", "Fixed the quoted-comma bug and added a regression test")
    memory = store.remember("decision", "Prefer parametrized tests over copy-pasted cases", harness, agent,
                            project_key="repo/drill")
    worker = Worker(store, FakeModels())
    while worker.work_one():
        pass  # Embed every seeded chunk so the restored copy can also recall semantically.
    return store, {"harness": harness, "agent": agent, "session": session, "memory": memory}


def test_backup_and_restore_roundtrip(seeded, tmp_path):
    store, ids = seeded
    live = store.path.parent
    backup = tmp_path / "backup.sqlite3"

    result = run_cli(["backup", "--output", str(backup)], live)
    assert backup.exists()
    assert backup.stat().st_mode & 0o777 == 0o600
    assert "Backup written to" in result.stdout

    # The backup itself is a consistent snapshot: same rows, clean integrity.
    assert counts(backup) == counts(store.path)
    check = sqlite3.connect(backup)
    try:
        assert check.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert check.execute("PRAGMA user_version").fetchone()[0] == 2
    finally:
        check.close()

    # Restore into an isolated data directory and reopen it with the real store path.
    restored_dir = tmp_path / "restored-data"
    restored_dir.mkdir()
    restored_db = restored_dir / "setauket.sqlite3"
    shutil.copyfile(backup, restored_db)
    restored_db.chmod(0o600)
    restored = Store(restored_db)
    with restored.connect() as db:
        assert db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert db.execute("PRAGMA user_version").fetchone()[0] == 2

    # Attribution and history survived the round trip.
    session = restored.get_session(ids["session"])
    assert [turn["content"] for turn in session["turns"]] == [
        "The spreadsheet parser drops quoted commas",
        "Fixed the quoted-comma bug and added a regression test"]
    assert session["harness_id"] == ids["harness"] and session["agent_id"] == ids["agent"]
    assert session["project_key"] == "repo/drill"

    # Keyword and semantic recall work from the restored copy alone.
    assert restored.search_rows("quoted commas")[0]["session_id"] == ids["session"]
    assert restored.search_rows("parametrized", category="memory")[0]["source_id"] == ids["memory"]
    semantic = restored.search_rows("parser regression fix", vector=VECTOR)
    assert any(row["session_id"] == ids["session"] for row in semantic)

    # The CLI operates on the restored directory as if it were home.
    doctor = json.loads(run_cli(["doctor"], restored_dir).stdout)
    assert doctor == {"integrity": "ok", "failed_jobs": []}
    status = json.loads(run_cli(["status"], restored_dir).stdout)
    assert status["database"] == str(restored_db) and status["database_exists"] is True


def test_backup_requires_existing_database(tmp_path):
    result = run_cli(["backup", "--output", str(tmp_path / "unused.sqlite3")], tmp_path / "empty", check=False)
    assert result.returncode != 0
