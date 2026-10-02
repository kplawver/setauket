import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from starlette.testclient import TestClient

from setauket.app import clean_blocks, create_app
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
    assert not store.archive(session, old_stamp, "outdated summary", "fake")
    assert len(store.get_session(session)["turns"]) == 2
    assert store.archive(session, store.get_session(session)["last_turn_at"], "Faithful summary", "fake")
    assert not store.archive(session, old_stamp, "again", "fake")
    assert store.get_session(session)["turns"] == []
    assert store.search_rows("Original") == []
    assert store.search_rows("Faithful")[0]["session_id"] == session


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
    assert store.get_session(session)["summary"]["content"] == "Summary of 1 turns"
    with store.connect() as db:
        assert db.execute("SELECT count(*) FROM jobs").fetchone()[0] == 0


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


def test_schema_is_version_one_and_reopens_without_migration(tmp_path):
    path = tmp_path / "setauket.sqlite3"
    store = Store(path)
    with store.connect() as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 1
    assert Store(path).get_session("unknown") is None
    with store.connect() as db:
        db.execute("PRAGMA user_version=99")
    with pytest.raises(ValueError, match="Unsupported database version"):
        Store(path)
