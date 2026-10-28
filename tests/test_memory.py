import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest
from starlette.testclient import TestClient

from setauket.app import clean_blocks, create_app, parse_when
from setauket.config import Config
from setauket.models import LocalModels
from setauket.storage import Store
from setauket.worker import Worker


class FakeModels:
    text_path = Path("/missing")

    def embed(self, text, query=False):
        return [0.1] * 384

    def summarize(self, turns):
        return "Summary of " + str(len(turns)) + " turns"

    def unload_text(self):
        pass


@pytest.fixture
def setup(tmp_path):
    store = Store(tmp_path / "memory.sqlite3")
    harness = store.register_harness("persistent-key", "Oh My Pi")
    agent = store.register_agent(harness, "main")
    return store, harness, agent


def test_identity_and_attribution(setup):
    store, harness, agent = setup
    assert store.register_harness("persistent-key", "Oh My Pi") == harness
    assert store.register_agent(harness, "main") == agent
    sub = store.register_agent(harness, "sub", agent)
    other = store.register_harness("other-key", "Claude Code")
    other_agent = store.register_agent(other, "main")
    session = store.start_session(harness, agent, "repo/path")
    with pytest.raises(ValueError):
        store.add_turn(session, harness, other_agent, "1", "user", "No")
    turn = store.add_turn(session, harness, sub, "1", "assistant", "Hello")
    assert store.add_turn(session, harness, sub, "1", "assistant", "Hello") == turn
    assert len(store.get_session(session)["turns"]) == 1
    assert store.search_rows("Hello")[0]["session_id"] == session
    with pytest.raises(ValueError):
        store.register_agent(other, "sub", agent)


def test_supersession_scope_and_search(setup):
    store, harness, agent = setup
    first = store.remember("preference", "Use dark backgrounds", harness, agent)
    with pytest.raises(ValueError):
        store.remember("preference", "Use light backgrounds", harness, agent, "project", first)
    second = store.remember("preference", "Use light backgrounds", harness, agent, supersedes_id=first)
    assert [r["id"] for r in store.get_memory(first)] == [first, second]
    assert store.search_rows("dark", category="memory") == []
    assert store.search_rows("light", category="memory")[0]["source_id"] == second
    with pytest.raises(ValueError):
        store.remember("preference", "Old", harness, agent, supersedes_id=first)


def test_archive_is_atomic_and_does_not_delete_recent_changes(setup):
    store, harness, agent = setup
    session = store.start_session(harness, agent, "project")
    store.add_turn(session, harness, agent, "one", "user", "Original turn")
    old_stamp = store.get_session(session)["last_turn_at"]
    store.add_turn(session, harness, agent, "two", "assistant", "New turn")
    assert not store.archive(session, old_stamp, ["outdated summary"], "fake")
    assert len(store.get_session(session)["turns"]) == 2
    assert store.archive(session, store.get_session(session)["last_turn_at"], ["Faithful summary"], "fake")
    assert not store.archive(session, old_stamp, ["again"], "fake")
    assert store.get_session(session)["turns"] == []
    assert store.search_rows("Original") == []
    assert store.search_rows("Faithful")[0]["session_id"] == session


def test_archive_stores_one_summary_per_segment(setup):
    store, harness, agent = setup
    session = store.start_session(harness, agent)
    for n in range(30):
        store.add_turn(session, harness, agent, f"t{n}", "user", f"Turn {n}")
    stamp = store.get_session(session)["last_turn_at"]
    assert store.archive(session, stamp, ["First segment", "Second segment"], "fake")
    archived = store.get_session(session)
    assert [(s["segment"], s["content"]) for s in archived["summaries"]] == [
        (0, "First segment"), (1, "Second segment")]
    assert archived["turns"] == []
    assert store.search_rows("Second segment")[0]["session_id"] == session


def test_worker_retries_and_completes_archive(setup):
    store, harness, agent = setup
    session = store.start_session(harness, agent)
    store.add_turn(session, harness, agent, "one", "user", "Hello")
    with store.connect() as db:
        db.execute("UPDATE sessions SET last_turn_at=last_turn_at-? WHERE id=?", (73 * 3600, session))
    worker = Worker(store, FakeModels())
    worker.sweep()
    while worker.work_one():
        pass
    assert [s["content"] for s in store.get_session(session)["summaries"]] == ["Summary of 1 turns"]
    with store.connect() as db:
        assert db.execute("SELECT count(*) FROM jobs").fetchone()[0] == 0


def test_worker_deletes_empty_sessions_instead_of_archiving(setup):
    store, harness, agent = setup
    session = store.start_session(harness, agent, "project")
    with store.connect() as db:
        db.execute("UPDATE sessions SET last_turn_at=last_turn_at-? WHERE id=?", (73 * 3600, session))
    worker = Worker(store, FakeModels())
    worker.sweep()
    while worker.work_one():
        pass
    assert store.get_session(session) is None
    with store.connect() as db:
        assert db.execute("SELECT count(*) FROM jobs").fetchone()[0] == 0


def test_discard_is_atomic_against_new_turns(setup):
    store, harness, agent = setup
    session = store.start_session(harness, agent)
    stamp = store.get_session(session)["last_turn_at"]
    store.add_turn(session, harness, agent, "one", "user", "Too late")
    assert not store.discard(session, stamp)
    assert len(store.get_session(session)["turns"]) == 1
    assert not store.discard(session, store.get_session(session)["last_turn_at"])
    assert store.get_session(session) is not None


def test_version_two_database_prunes_empty_archives(tmp_path):
    path = tmp_path / "memory.sqlite3"
    store = Store(path)
    harness = store.register_harness("persistent-key", "Oh My Pi")
    agent = store.register_agent(harness, "main")
    kept = store.start_session(harness, agent)
    store.add_turn(kept, harness, agent, "one", "user", "Real work")
    assert store.archive(kept, store.get_session(kept)["last_turn_at"], ["Real summary"], "fake")
    emptied = store.start_session(harness, agent)
    assert store.archive(emptied, store.get_session(emptied)["last_turn_at"], [], "fake")
    with store.connect() as db:
        db.execute("PRAGMA user_version=2")
    reopened = Store(path)
    assert reopened.get_session(kept)["summaries"][0]["content"] == "Real summary"
    assert reopened.get_session(emptied) is None


def test_worker_summarizes_each_25_turn_segment(setup):
    store, harness, agent = setup
    session = store.start_session(harness, agent)
    for n in range(30):
        store.add_turn(session, harness, agent, f"t{n}", "user", f"Turn {n}")
    with store.connect() as db:
        db.execute("UPDATE sessions SET last_turn_at=last_turn_at-? WHERE id=?", (73 * 3600, session))
    worker = Worker(store, FakeModels())
    worker.sweep()
    while worker.work_one():
        pass
    summaries = store.get_session(session)["summaries"]
    assert [s["content"] for s in summaries] == ["Summary of 25 turns", "Summary of 5 turns"]
    assert [s["segment"] for s in summaries] == [0, 1]
    assert store.get_session(session)["turns"] == []


def test_worker_failure_keeps_unsummarized_history(setup):
    store, harness, agent = setup
    session = store.start_session(harness, agent)
    store.add_turn(session, harness, agent, "event", "user", "Keep this")
    with store.connect() as db:
        db.execute("UPDATE sessions SET last_turn_at=last_turn_at-? WHERE id=?", (73 * 3600, session))
        db.execute("DELETE FROM jobs WHERE kind='embed'")

    class BrokenModels(FakeModels):
        def summarize(self, turns):
            raise RuntimeError("Unavailable")

    worker = Worker(store, BrokenModels())
    worker.sweep()
    assert worker.work_one()
    assert store.get_session(session)["turns"][0]["content"] == "Keep this"
    with store.connect() as db:
        job = db.execute("SELECT attempts,error FROM jobs WHERE kind='archive'").fetchone()
        assert job["attempts"] == 1
        assert job["error"] == "Unavailable"


def test_rebuild_indexes_restores_search(setup):
    store, harness, agent = setup
    session = store.start_session(harness, agent)
    store.add_turn(session, harness, agent, "event", "user", "Remember the socket")
    worker = Worker(store, FakeModels())
    assert worker.work_one()
    with store.connect() as db:
        db.execute("DELETE FROM chunks_fts")
    assert not store.search_rows("socket")
    assert store.rebuild_indexes() == 1
    assert store.search_rows("socket")[0]["session_id"] == session
    with store.connect() as db:
        assert db.execute("SELECT embedded FROM chunks").fetchone()[0] == 0
    assert worker.work_one()
    assert store.search_rows("unlikely-word", vector=[0.1] * 384)


def test_summary_subprocess_uses_private_prompt_file(tmp_path, monkeypatch):
    model = LocalModels(tmp_path)
    private_content = "user: private test phrase"

    def fake_run(command, **kwargs):
        assert private_content not in " ".join(command)
        prompt = Path(command[command.index("-f") + 1])
        output = Path(command[command.index("-o") + 1])
        assert prompt.read_text() == private_content
        output.write_text(f"User:\n{private_content}\n\nAssistant:\nFaithful summary.\n")
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr("setauket.models.subprocess.run", fake_run)
    assert model._complete("Summarize", private_content) == "Faithful summary."


def test_reasoning_is_discarded():
    assert clean_blocks([{"type": "thinking", "text": "private"},
                         {"type": "text", "text": "visible"}]) == "visible"
    with pytest.raises(ValueError):
        clean_blocks([{"type": "image", "text": "unsupported"}])


def test_session_page_renders_markdown_safely(setup, tmp_path):
    store, harness, agent = setup
    session = store.start_session(harness, agent, "project")
    store.add_turn(session, harness, agent, "event", "assistant",
                   "**Bold** heading\n=============\n\n- item with `code`\n- [link](https://example.com)\n\n```python\nprint('hi')\n```\n\n<script>alert(1)</script>\n\n[bad](javascript:alert(1))")
    app = create_app(Config(data_dir=tmp_path), store, FakeModels(), start_worker=False)
    with TestClient(app, base_url="http://127.0.0.1:19005") as client:
        page = client.get("/sessions/" + session).text
    assert '<h1><strong>Bold</strong> heading</h1>' in page
    assert "<li>item with <code>code</code></li>" in page
    assert '<a href="https://example.com">link</a>' in page
    assert '<code class="language-python">' in page and "print('hi')" in page
    assert "<script>" not in page and "&lt;script&gt;" in page
    assert 'href="javascript:' not in page


def test_http_and_mcp_versions(setup, tmp_path):
    store, harness, agent = setup
    session = store.start_session(harness, agent, "project")
    store.add_turn(session, harness, agent, "event", "assistant", "Index the code")
    app = create_app(Config(data_dir=tmp_path), store, FakeModels(), start_worker=False)
    with TestClient(app, base_url="http://127.0.0.1:19005") as client:
        assert client.get("/health").json() == {"status": "ok"}
        assert session in client.get("/search?q=Index").text
        assert client.get("/sessions/" + session).status_code == 200
        assert client.get("/", headers={"host": "evil.example"}).status_code == 400
        assert client.post("/mcp", headers={"Origin": "https://evil.example"}, json={}).status_code == 403
        headers = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}
        def rpc(body, extra=None):
            result = client.post("/mcp", headers=headers | (extra or {}) | (
                {"Mcp-Method": body["method"], **({"Mcp-Name": body["params"]["name"]} if body["method"] == "tools/call" else {})} if extra else {}), json=body)
            assert result.status_code == 200, result.text
            return result.json()
        legacy = rpc({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
            "protocolVersion": "2025-11-25", "capabilities": {}, "clientInfo": {"name": "test", "version": "1"}}})
        assert legacy["result"]["protocolVersion"] == "2025-11-25"
        modern = rpc({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {"_meta": {
                          "io.modelcontextprotocol/protocolVersion": "2026-07-28",
                          "io.modelcontextprotocol/clientCapabilities": {}}}},
                     {"MCP-Protocol-Version": "2026-07-28"})
        assert any(t["name"] == "append_turn" for t in modern["result"]["tools"])
        assert rpc({"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {
            "name": "register_harness", "arguments": {"installation_key": "persistent-key", "name": "Oh My Pi"},
            "_meta": {"io.modelcontextprotocol/protocolVersion": "2026-07-28",
                      "io.modelcontextprotocol/clientCapabilities": {}}}},
            {"MCP-Protocol-Version": "2026-07-28"})["result"]["content"][0]["text"] == json.dumps({"harness_id": harness}, indent=2)


def test_parse_when_reads_iso_dates_and_datetimes():
    assert parse_when("2001-09-09") == 999993600.0  # midnight UTC
    assert 999993600 + 86399 <= parse_when("2001-09-09", end_of_day=True) <= 999993600 + 86400
    assert parse_when("2001-09-09T01:46:40Z") == 1000000000.0
    assert parse_when("2001-09-09T01:46:40") == 1000000000.0  # naive times read as UTC
    with pytest.raises(ValueError):
        parse_when("not-a-date")


def test_list_sessions_by_date_range(setup):
    store, harness, agent = setup
    september = store.start_session(harness, agent, "project")
    november = store.start_session(harness, agent, "other")
    with store.connect() as db:  # a session spans started_at to last_turn_at
        db.execute("UPDATE sessions SET started_at=?,last_turn_at=? WHERE id=?", (1789000000, 1789003600, september))
        db.execute("UPDATE sessions SET started_at=?,last_turn_at=? WHERE id=?", (1796000000, 1796003600, november))
    assert [s["id"] for s in store.list_sessions(after=1789000000, before=1789000060)] == [september]
    assert all(s["session_id"] == s["id"] for s in store.list_sessions(after=1789000000))  # get_session handoff
    assert [s["id"] for s in store.list_sessions(after=1789000000, before=1796000000 + 86400)] == [november, september]
    assert [s["id"] for s in store.list_sessions(after=1789003500, before=1789003501)] == [september]  # overlap counts
    assert store.list_sessions(after=1, before=2) == []
    assert [s["id"] for s in store.list_sessions(project_key="other", after=1789000000)] == [november]


def test_search_date_range_judges_sessions_not_write_time(setup):
    store, harness, agent = setup
    early = store.start_session(harness, agent)
    late = store.start_session(harness, agent)
    store.add_turn(early, harness, agent, "one", "user", "orbit deployment notes")
    store.add_turn(late, harness, agent, "one", "user", "orbit deployment notes")
    with store.connect() as db:
        db.execute("UPDATE sessions SET started_at=?,last_turn_at=? WHERE id=?", (1000000000, 1000000100, early))
        db.execute("UPDATE sessions SET started_at=?,last_turn_at=? WHERE id=?", (1400000000, 1400000100, late))
    # Archiving rewrites the chunk set at write time (today) for a session that ran in 2001.
    assert store.archive(early, 1000000100, ["Orbit summary"], "fake")
    assert {h["session_id"] for h in store.search_rows("orbit")} == {early, late}
    assert [h["session_id"] for h in store.search_rows("orbit", after=900000000, before=1100000000)] == [early]
    assert [h["session_id"] for h in store.search_rows("orbit", after=1300000000, before=1500000000)] == [late]


def test_date_filters_in_ui(setup, tmp_path):
    store, harness, agent = setup
    old = store.start_session(harness, agent, "project")
    store.add_turn(old, harness, agent, "one", "user", "legacy migration work")
    with store.connect() as db:
        db.execute("UPDATE sessions SET started_at=?,last_turn_at=? WHERE id=?", (1000000000, 1000000100, old))
    app = create_app(Config(data_dir=tmp_path), store, FakeModels(), start_worker=False)
    with TestClient(app, base_url="http://127.0.0.1:19005") as client:
        assert old in client.get("/sessions?from=2001-09-09&to=2001-09-10").text
        assert old not in client.get("/sessions?from=2026-01-01&to=2026-01-02").text
        assert old in client.get("/sessions?from=garbage").text  # invalid input filters nothing
        assert "legacy migration work" in client.get("/search?q=migration&from=2001-09-09").text
        assert "No matches." in client.get("/search?q=migration&from=2026-01-01").text
        assert 'name="from" value="2001-09-09"' in client.get("/sessions?from=2001-09-09").text  # form keeps state


def test_mcp_search_sessions_accepts_date_range(setup, tmp_path):
    store, harness, agent = setup
    old = store.start_session(harness, agent, "project")
    store.add_turn(old, harness, agent, "one", "user", "legacy migration work")
    with store.connect() as db:
        db.execute("UPDATE sessions SET started_at=?,last_turn_at=? WHERE id=?", (1000000000, 1000000100, old))
    app = create_app(Config(data_dir=tmp_path), store, FakeModels(), start_worker=False)
    with TestClient(app, base_url="http://127.0.0.1:19005") as client:
        headers = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}

        def rpc(body):
            extra = {"Mcp-Protocol-Version": "2026-07-28", "Mcp-Method": body["method"]}
            if body["method"] == "tools/call":
                extra["Mcp-Name"] = body["params"]["name"]
            result = client.post("/mcp", headers=headers | extra, json=body)
            assert result.status_code == 200, result.text
            return result.json()

        def call(arguments):
            body = rpc({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {
                "name": "search_sessions", "arguments": arguments, "_meta": {
                    "io.modelcontextprotocol/protocolVersion": "2026-07-28",
                    "io.modelcontextprotocol/clientCapabilities": {}}}})
            return json.loads(body["result"]["content"][0]["text"])

        dated = call({"after": "2001-09-09", "before": "2001-09-10"})  # no query, dates only
        assert [s["id"] for s in dated["sessions"]] == [old]
        assert [s["session_id"] for s in dated["sessions"]] == [old]  # feeds get_session directly
        results = call({"query": "migration", "after": "2001-09-09"})["results"]
        assert [r["session_id"] for r in results] == [old]
        assert call({"query": "migration", "after": "2026-01-01"})["results"] == []
        assert old in [s["id"] for s in call({})["sessions"]]  # dateless call lists recent sessions


def test_schema_is_version_three_and_reopens_without_migration(tmp_path):
    path = tmp_path / "setauket.sqlite3"
    store = Store(path)
    with store.connect() as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 3
    assert Store(path).get_session("unknown") is None
    with store.connect() as db:
        db.execute("PRAGMA user_version=99")
    with pytest.raises(ValueError, match="Unsupported database version"):
        Store(path)


def test_version_one_database_migrates_summaries_to_segments(tmp_path):
    path = tmp_path / "setauket.sqlite3"
    store = Store(path)
    harness = store.register_harness("migrate-key", "Zed")
    agent = store.register_agent(harness, "main")
    session = store.start_session(harness, agent)
    store.add_turn(session, harness, agent, "one", "user", "Original turn")
    assert store.archive(session, store.get_session(session)["last_turn_at"], ["Faithful summary"], "fake")
    raw = sqlite3.connect(path)
    try:
        # Rewind the database to the schema-v1 summaries shape and version.
        raw.execute("ALTER TABLE summaries RENAME TO segmented")
        raw.execute("CREATE TABLE summaries (id TEXT PRIMARY KEY, "
                    "session_id TEXT UNIQUE NOT NULL REFERENCES sessions(id), "
                    "content TEXT NOT NULL, model TEXT NOT NULL, created_at REAL NOT NULL)")
        raw.execute("INSERT INTO summaries SELECT id,session_id,content,model,created_at FROM segmented")
        raw.execute("DROP TABLE segmented")
        raw.execute("PRAGMA user_version=1")
        raw.commit()
    finally:
        raw.close()
    migrated = Store(path)
    with migrated.connect() as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 3
        assert db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    summaries = migrated.get_session(session)["summaries"]
    assert [(s["segment"], s["content"]) for s in summaries] == [(0, "Faithful summary")]
    assert migrated.search_rows("Faithful")[0]["session_id"] == session
