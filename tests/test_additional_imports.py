"""Synthetic transcripts verify privacy boundaries without reading real conversations."""

import json
import sqlite3
import sys

import pytest
import zstandard

from setauket.cli import main
from setauket.database_import import list_session_ids, parse_opencode, parse_zed
from setauket.file_import import parse_claude, parse_codex
from setauket.session_import import parse_session
from setauket.storage import Store

DATE = "2026-09-30T10:00:00Z"


def write_lines(path, *records):
    with path.open("a") as stream:
        for record in records:
            stream.write(json.dumps(record) + "\n")


def claude_record(event_id, parent, role, content, project, **extra):
    return {"type": role, "uuid": event_id, "parentUuid": parent, "sessionId": "claude-id",
            "timestamp": DATE, "cwd": str(project), "isSidechain": False,
            "message": {"role": role, "content": content}, **extra}


def make_claude(tmp_path):
    path = tmp_path / "claude.jsonl"
    write_lines(path,
                claude_record("u1", None, "user", "Claude question", tmp_path),
                claude_record("a1", "u1", "assistant", [
                    {"type": "thinking", "thinking": "private reasoning"},
                    {"type": "tool_use", "input": {"token": "private tool input"}},
                    {"type": "text", "text": "Claude answer"}], tmp_path),
                claude_record("u-tool", "a1", "user", [
                    {"type": "tool_result", "content": "private tool result"}], tmp_path),
                claude_record("a2", "u-tool", "assistant", [{"type": "text", "text": "Final answer"}], tmp_path),
                claude_record("side", "a2", "assistant", [{"type": "text", "text": "Subagent work"}],
                              tmp_path, isSidechain=True))
    return path


def make_codex(tmp_path):
    path = tmp_path / "codex.jsonl"
    write_lines(path,
                {"type": "session_meta", "timestamp": DATE,
                 "payload": {"id": "codex-id", "cwd": str(tmp_path), "timestamp": DATE,
                             "base_instructions": "private system prompt"}},
                {"type": "response_item", "timestamp": DATE, "payload": {
                    "type": "message", "role": "user", "content": [
                        {"type": "input_text", "text": "private environment scaffolding"}]}},
                {"type": "event_msg", "timestamp": DATE,
                 "payload": {"type": "user_message", "message": "Codex question"}},
                {"type": "response_item", "timestamp": DATE,
                 "payload": {"type": "reasoning", "summary": "private reasoning"}},
                {"type": "event_msg", "timestamp": DATE,
                 "payload": {"type": "agent_message", "message": "Codex answer"}},
                {"type": "response_item", "timestamp": DATE, "payload": {
                    "type": "message", "role": "assistant", "content": [
                        {"type": "output_text", "text": "Codex answer"}]}})
    return path


def make_pi(tmp_path):
    path = tmp_path / "pi.jsonl"
    write_lines(path, {"type": "session", "id": "pi-id", "version": 3, "cwd": str(tmp_path),
                       "timestamp": DATE},
                {"type": "message", "id": "u", "parentId": None, "timestamp": DATE,
                 "message": {"role": "user", "content": "Pi question"}},
                {"type": "message", "id": "a", "parentId": "u", "timestamp": DATE,
                 "message": {"role": "assistant", "content": [
                     {"type": "thinking", "thinking": "private Pi reasoning"},
                     {"type": "text", "text": "Pi answer"}]}},
                {"type": "message", "id": "t", "parentId": "a", "timestamp": DATE,
                 "message": {"role": "toolResult", "content": [
                     {"type": "text", "text": "private Pi tool output"}]}})
    return path


def zed_blob(messages):
    return zstandard.ZstdCompressor().compress(json.dumps({"messages": messages,
                                                            "detailed_summary": "private summary"}).encode())


def make_zed(tmp_path):
    path = tmp_path / "threads.db"
    messages = [{"User": {"id": "u", "content": [{"Text": "Zed question"}]}},
                {"Agent": {"content": [{"Thinking": {"text": "private Zed reasoning"}},
                                         {"ToolUse": {"input": {"password": "private Zed input"}}},
                                         {"Text": "Zed answer"}],
                           "tool_results": {"call": "private Zed tool output"}}}]
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE threads (id TEXT PRIMARY KEY, data_type TEXT, data BLOB, folder_paths TEXT, created_at TEXT, updated_at TEXT)")
        db.execute("INSERT INTO threads VALUES (?,?,?,?,?,?)", ("zed-id", "zstd", zed_blob(messages), str(tmp_path), DATE, DATE))
    return path


def make_opencode(tmp_path):
    path = tmp_path / "opencode.db"
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE session (id TEXT PRIMARY KEY, directory TEXT, time_created INTEGER)")
        db.execute("CREATE TABLE message (id TEXT PRIMARY KEY, session_id TEXT, time_created INTEGER, data TEXT)")
        db.execute("CREATE TABLE part (id TEXT PRIMARY KEY, message_id TEXT, session_id TEXT, time_created INTEGER, data TEXT)")
        db.execute("INSERT INTO session VALUES (?,?,?)", ("oc-id", str(tmp_path), 1_780_224_000_000))
        for n, role, text in [(1, "user", "OpenCode question"), (2, "assistant", "OpenCode answer")]:
            db.execute("INSERT INTO message VALUES (?,?,?,?)", (f"msg-{n}", "oc-id", 1_780_224_000_000+n,
                        json.dumps({"role": role, "summary": "private summary"})))
            db.execute("INSERT INTO part VALUES (?,?,?,?,?)", (f"text-{n}", f"msg-{n}", "oc-id", n,
                        json.dumps({"type": "text", "text": text})))
            db.execute("INSERT INTO part VALUES (?,?,?,?,?)", (f"thought-{n}", f"msg-{n}", "oc-id", n+10,
                        json.dumps({"type": "reasoning", "text": "private OpenCode reasoning"})))
            db.execute("INSERT INTO part VALUES (?,?,?,?,?)", (f"tool-{n}", f"msg-{n}", "oc-id", n+20,
                        json.dumps({"type": "tool", "state": {"output": "private OpenCode tool output"}})))
    return path


@pytest.mark.parametrize("harness,maker,reader", [
    ("claude", make_claude, parse_claude),
    ("codex", make_codex, parse_codex),
    ("pi", make_pi, lambda path: parse_session(path, "Pi")),
    ("zed", make_zed, lambda path: parse_zed(path, "zed-id")),
    ("opencode", make_opencode, lambda path: parse_opencode(path, "oc-id")),
])
def test_private_data_excluded_and_import_idempotent(harness, maker, reader, tmp_path):
    source = maker(tmp_path)
    parsed = reader(source)
    assert parsed.project_key == str(tmp_path)
    assert [turn.role for turn in parsed.turns if turn.content] == ["user", "assistant"] if harness != "claude" else ["user", "assistant", "assistant"]
    assert all("private" not in turn.content for turn in parsed.turns)

    store = Store(tmp_path / "memory.db")
    registered = store.register_harness(harness, harness)
    agent = store.register_agent(registered, parsed.source_id)
    source_key = f"{source}#{parsed.source_id}" if harness in {"zed", "opencode"} else str(source)
    first = store.import_session(source_key, parsed, registered, agent)
    assert first["imported"] == sum(bool(turn.content) for turn in parsed.turns)
    assert store.import_session(source_key, reader(source), registered, agent)["imported"] == 0
    assert not any("private" in row["content"] for row in store.get_session(first["session_id"])["turns"])
    assert not store.search_rows("private")


def test_claude_branch_and_changed_history_refused(tmp_path):
    source = make_claude(tmp_path)
    parsed = parse_claude(source)
    assert [t.event_id for t in parsed.turns] == ["u1", "a1", "u-tool", "a2"]
    store = Store(tmp_path / "memory.db")
    h = store.register_harness("claude", "Claude")
    a = store.register_agent(h, "main")
    store.import_session(str(source), parsed, h, a)
    write_lines(source, claude_record("other-branch", "a1", "user", "Different path", tmp_path))
    with pytest.raises(ValueError, match="active branch changed"):
        store.import_session(str(source), parse_claude(source), h, a)


def test_claude_subagent_file_has_distinct_identity(tmp_path):
    sidecar = tmp_path / "subagents" / "agent-test.jsonl"
    sidecar.parent.mkdir()
    write_lines(sidecar, claude_record("u", None, "user", "Subagent prompt", tmp_path,
                                       agentId="agent-1", isSidechain=True),
                claude_record("a", "u", "assistant", [{"type": "text", "text": "Subagent reply"}],
                              tmp_path, agentId="agent-1", isSidechain=True))
    parsed = parse_claude(sidecar)
    assert parsed.source_id == "claude-id:agent:agent-1"
    assert [turn.content for turn in parsed.turns] == ["Subagent prompt", "Subagent reply"]


def test_codex_fork_metadata_uses_child_id(tmp_path):
    path = make_codex(tmp_path)
    lines = path.read_text().splitlines()
    parent = json.loads(lines[0])
    parent["payload"]["id"] = "parent-id"
    parent["payload"]["forked_from_id"] = "earlier-session"
    path.write_text(json.dumps(parent) + "\n" + "\n".join(lines) + "\n")
    parsed = parse_codex(path)
    assert parsed.source_id == "codex-id"
    assert len(parsed.turns) == 2


def test_zed_snapshot_edit_refused_and_append_imported(tmp_path):
    source = make_zed(tmp_path)
    h_store = Store(tmp_path / "memory.db")
    h = h_store.register_harness("zed", "Zed")
    a = h_store.register_agent(h, "main")
    parsed = parse_zed(source, "zed-id")
    key = str(source) + "#zed-id"
    first = h_store.import_session(key, parsed, h, a)
    with sqlite3.connect(source) as db:
        row = db.execute("SELECT data FROM threads WHERE id='zed-id'").fetchone()
        data = json.loads(zstandard.ZstdDecompressor().decompress(row[0]))
        data["messages"].append({"User": {"id": "new", "content": [{"Text": "Next question"}]}})
        db.execute("UPDATE threads SET data=?, updated_at=?",
                   (zed_blob(data["messages"]), "2026-10-01T10:00:00Z"))
    assert h_store.import_session(key, parse_zed(source, "zed-id"), h, a)["imported"] == 1
    turns = h_store.get_session(first["session_id"])["turns"]
    assert len(turns) == 3
    assert turns[2]["created_at"] > turns[1]["created_at"]
    with sqlite3.connect(source) as db:
        data["messages"][0]["User"]["content"][0]["Text"] = "Previously changed"
        db.execute("UPDATE threads SET data=?", (zed_blob(data["messages"]),))
    with pytest.raises(ValueError, match="Previously imported messages changed"):
        h_store.import_session(key, parse_zed(source, "zed-id"), h, a)


def test_database_listing_and_cli_preview_do_not_write(tmp_path, monkeypatch, capsys):
    data_dir = tmp_path / "unused"
    monkeypatch.setenv("SETAUKET_DATA_DIR", str(data_dir))
    for name, maker, sid in [("zed", make_zed, "zed-id"), ("opencode", make_opencode, "oc-id")]:
        path = maker(tmp_path)
        assert list_session_ids(path, name) == [sid]
        monkeypatch.setattr(sys, "argv", ["setauket", f"import-{name}", "--source", str(path), "--list"])
        main()
        assert json.loads(capsys.readouterr().out) == [sid]
        monkeypatch.setattr(sys, "argv", ["setauket", f"import-{name}", "--source", str(path),
                                        "--session-id", sid, "--dry-run"])
        main()
        assert json.loads(capsys.readouterr().out)["database_changed"] is False
    assert not data_dir.exists()


@pytest.mark.parametrize("harness,maker,source_id", [
    ("claude", make_claude, None), ("codex", make_codex, None),
    ("pi", make_pi, None), ("zed", make_zed, "zed-id"),
    ("opencode", make_opencode, "oc-id"),
])
def test_cli_wires_each_importer_to_shared_store(harness, maker, source_id, tmp_path, monkeypatch, capsys):
    source = maker(tmp_path)
    data_dir = tmp_path / "data"
    monkeypatch.setenv("SETAUKET_DATA_DIR", str(data_dir))
    args = ["setauket", f"import-{harness}", "--source", str(source)]
    if source_id:
        args += ["--session-id", source_id]
    monkeypatch.setattr(sys, "argv", args)
    main()
    first = json.loads(capsys.readouterr().out)
    main()
    again = json.loads(capsys.readouterr().out)
    assert first["imported"] >= 2
    assert again["imported"] == 0
    assert first["session_id"] == again["session_id"]
    with Store(data_dir / "setauket.sqlite3").connect() as db:
        assert db.execute("SELECT installation_key FROM harnesses").fetchone()[0] == f"setauket:{harness}:local-import"


