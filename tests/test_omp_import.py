import json
import sys
from datetime import UTC, datetime

import pytest

from setauket.cli import main
from setauket.omp_import import parse_session
from setauket.storage import Store


def entry(kind, event_id, parent_id, role=None, content=None):
    record = {"type": kind, "id": event_id, "parentId": parent_id,
              "timestamp": "2026-09-30T10:00:00Z"}
    if role:
        record["message"] = {"role": role, "content": content}
    return record


def append(path, *records):
    with path.open("a") as stream:
        for record in records:
            stream.write(json.dumps(record) + "\n")


@pytest.fixture
def source(tmp_path):
    path = tmp_path / "omp-session.jsonl"
    append(path, {"type": "session", "version": 3, "id": "omp-uuid",
                  "timestamp": "2026-09-30T09:00:00Z", "cwd": str(tmp_path)},
           entry("message", "u1", None, "user", [{"type": "text", "text": "Hello from OMP"}]),
           entry("message", "a1", "u1", "assistant", [
               {"type": "thinking", "thinking": "private reasoning secret"},
               {"type": "toolCall", "arguments": {"token": "private argument secret"}},
               {"type": "text", "text": "Visible answer"}]),
           entry("message", "t1", "a1", "toolResult", [{"type": "text", "text": "private tool output secret"}]),
           entry("message", "u2", "t1", "user", "A follow-up"),
           entry("title", "last", "u2"))
    return path


def test_import_visible_active_branch_and_resume(source, tmp_path):
    parsed = parse_session(source)
    assert [turn.event_id for turn in parsed.turns] == ["u1", "a1", "u2"]
    assert parsed.turns[1].content == "Visible answer"
    assert parsed.turns[0].created_at == datetime(2026, 9, 30, 10, tzinfo=UTC).timestamp()

    store = Store(tmp_path / "memory.sqlite3")
    harness = store.register_harness("omp-import", "OMP")
    agent = store.register_agent(harness, "omp-session:omp-uuid")
    result = store.import_omp(str(source.resolve()), parsed, harness, agent)
    assert result["imported"] == 3
    assert store.import_omp(str(source.resolve()), parsed, harness, agent)["imported"] == 0
    assert store.get_session(result["session_id"])["project_key"] == str(tmp_path)
    assert len(store.get_session(result["session_id"])["turns"]) == 3
    for secret in ["reasoning secret", "argument secret", "tool output secret"]:
        assert not store.search_rows(secret)
    append(source, entry("message", "a2", "last", "assistant", [{"type": "text", "text": "New result"}]))
    updated = store.import_omp(str(source.resolve()), parse_session(source), harness, agent)
    assert updated["session_id"] == result["session_id"]
    assert updated["imported"] == 1
    assert store.search_rows("result")[0]["session_id"] == result["session_id"]


def test_archived_session_continues_in_new_segment(source, tmp_path):
    store = Store(tmp_path / "db.sqlite3")
    harness = store.register_harness("omp-import", "OMP")
    agent = store.register_agent(harness, "main")
    first = store.import_omp(str(source.resolve()), parse_session(source), harness, agent)
    session_id = first["session_id"]
    assert store.archive(session_id, store.get_session(session_id)["last_turn_at"], ["Earlier work"], "fake")
    assert store.import_omp(str(source.resolve()), parse_session(source), harness, agent)["imported"] == 0
    append(source, entry("message", "new", "last", "user", "After archive"))
    resumed = store.import_omp(str(source.resolve()), parse_session(source), harness, agent)
    assert resumed["segment"] == 1
    assert resumed["session_id"] != session_id
    assert store.get_session(session_id)["summaries"][0]["content"] == "Earlier work"
    assert store.get_session(resumed["session_id"])["turns"][0]["content"] == "After archive"


def test_changed_active_branch_refused_without_partial_write(source, tmp_path):
    store = Store(tmp_path / "db.sqlite3")
    harness = store.register_harness("omp-import", "OMP")
    agent = store.register_agent(harness, "main")
    first = store.import_omp(str(source.resolve()), parse_session(source), harness, agent)
    append(source, entry("message", "alternate", "a1", "user", "Alternative approach"))
    with pytest.raises(ValueError, match="active branch changed"):
        store.import_omp(str(source.resolve()), parse_session(source), harness, agent)
    assert len(store.get_session(first["session_id"])["turns"]) == 3


def test_dry_run_does_not_create_database(source, tmp_path, monkeypatch, capsys):
    db_dir = tmp_path / "missing-dir"
    monkeypatch.setattr(sys, "argv", ["setauket", "import-omp", "--source", str(source), "--dry-run"])
    monkeypatch.setenv("SETAUKET_DATA_DIR", str(db_dir))
    main()
    result = json.loads(capsys.readouterr().out)
    assert result["visible_turns"] == 3
    assert result["database_changed"] is False
    assert not db_dir.exists()


def test_cli_import_is_idempotent(source, tmp_path, monkeypatch, capsys):
    db_dir = tmp_path / "data"
    monkeypatch.setenv("SETAUKET_DATA_DIR", str(db_dir))
    monkeypatch.setattr(sys, "argv", ["setauket", "import-omp", "--source", str(source)])
    main()
    first = json.loads(capsys.readouterr().out)
    main()
    again = json.loads(capsys.readouterr().out)
    assert first["imported"] == 3
    assert again["imported"] == 0
    assert first["session_id"] == again["session_id"]
    assert Store(db_dir / "setauket.sqlite3").get_session(first["session_id"])["turns"][0]["content"] == "Hello from OMP"


def test_partial_tail_and_reasoning_only_turn(source):
    append(source, entry("message", "thinking-only", "last", "assistant", [
        {"type": "thinking", "thinking": "never store this"}]))
    with source.open("a") as stream:
        stream.write('{"type": "message"')
    parsed = parse_session(source)
    assert parsed.turns[-1].event_id == "thinking-only"
    assert parsed.turns[-1].content == ""


def test_rejects_malformed_or_oversized_content(source):
    append(source, entry("message", "too-long", "last", "user", "x" * 250_001))
    with pytest.raises(ValueError, match="250,000"):
        parse_session(source)
    source.write_text('{"type": "session"}\nnot json\n')
    with pytest.raises(ValueError, match="Invalid session JSON"):
        parse_session(source)


