import io
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from setauket.capture import capture_hook, set_enabled
from setauket.cli import main
from setauket.config import Config
from setauket.identity import CAPTURE_HARNESSES
from setauket.storage import Store


def event(name, project, **extra):
    return {"hook_event_name": name, "cwd": str(project), "session_id": "claude-session-1",
            "prompt_id": "prompt-1", "transcript_path": "/nonexistent/private-transcript.jsonl", **extra}


def test_explicit_consent_visible_fields_and_duplicate_events(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    config = Config(data_dir=tmp_path / "data")
    user = event("UserPromptSubmit", project, prompt="Visible question",
                 thinking="private reasoning", tool_input={"secret": "private tool arguments"})
    assistant = event("Stop", project, last_assistant_message="Visible final answer",
                      tool_result="private tool output", last_assistant_thinking="private reasoning")
    assert not capture_hook(config, user)
    assert not config.database.exists()
    assert set_enabled(config, project, True)
    assert (config.data_dir / "claude-capture.json").stat().st_mode & 0o777 == 0o600
    assert config.data_dir.stat().st_mode & 0o777 == 0o700
    assert capture_hook(config, user)
    assert not capture_hook(config, user)
    assert capture_hook(config, assistant)
    assert not capture_hook(config, assistant)
    store = Store(config.database)
    with store.connect() as db:
        session = db.execute("SELECT * FROM sessions").fetchone()
        assert session["project_id"]
        assert db.execute("SELECT installation_key FROM harnesses").fetchone()[0] == "setauket:claude:hook"
        assert db.execute("SELECT external_id FROM agents").fetchone()[0] == "claude-session:claude-session-1"
        assert db.execute("SELECT count(*) FROM capture_events").fetchone()[0] == 2
    turns = store.get_session(session["id"])["turns"]
    assert [turn["role"] for turn in turns] == ["user", "assistant"]
    assert [turn["content"] for turn in turns] == ["Visible question", "Visible final answer"]
    assert not store.search_rows("private")
    assert not capture_hook(config, event("UserPromptSubmit", tmp_path, prompt="Not allowed"))
    assert not set_enabled(config, project, False)
    assert not capture_hook(config, event("UserPromptSubmit", project, prompt="After disable", prompt_id="p2"))
    assert len(store.get_session(session["id"])["turns"]) == 2


def test_missing_prompt_id_repeated_prompt_and_archived_segment(tmp_path):
    config = Config(data_dir=tmp_path / "data")
    set_enabled(config, tmp_path, True)
    prompt = event("UserPromptSubmit", tmp_path, prompt="Same text", prompt_id=None)
    reply = event("Stop", tmp_path, last_assistant_message="Same reply", prompt_id=None)
    assert capture_hook(config, prompt)
    assert not capture_hook(config, prompt)
    assert capture_hook(config, reply)
    assert capture_hook(config, prompt)
    assert capture_hook(config, reply)
    store = Store(config.database)
    with store.connect() as db:
        session_id = db.execute("SELECT id FROM sessions").fetchone()[0]
    assert len(store.get_session(session_id)["turns"]) == 4
    session = store.get_session(session_id)
    assert store.archive(session_id, session["last_turn_at"], ["Earlier answers"], "fake")
    assert not capture_hook(config, reply)  # No duplicate after the raw turns expire.
    assert capture_hook(config, event("UserPromptSubmit", tmp_path, prompt="New prompt", prompt_id="new-id"))
    with store.connect() as db:
        assert db.execute("SELECT segment FROM capture_sources").fetchone()[0] == 1
        assert db.execute("SELECT count(*) FROM sessions").fetchone()[0] == 2
    assert store.get_session(session_id)["summaries"][0]["content"] == "Earlier answers"


def test_prompt_id_reuse_with_different_content_rejected(tmp_path):
    config = Config(data_dir=tmp_path / "data")
    set_enabled(config, tmp_path, True)
    capture_hook(config, event("UserPromptSubmit", tmp_path, prompt="First"))
    with pytest.raises(ValueError, match="reused with different text"):
        capture_hook(config, event("UserPromptSubmit", tmp_path, prompt="Changed"))
    with Store(config.database).connect() as db:
        assert db.execute("SELECT count(*) FROM turns").fetchone()[0] == 1


def test_cli_hook_fail_open_and_per_project_status(tmp_path, monkeypatch, capsys):
    data_dir = tmp_path / "data"
    monkeypatch.setenv("SETAUKET_DATA_DIR", str(data_dir))
    monkeypatch.setattr(sys, "argv", ["setauket", "capture-claude", "--project", str(tmp_path), "--enable"])
    main()
    assert json.loads(capsys.readouterr().out)["capture_enabled"] is True
    monkeypatch.setattr(sys, "argv", ["setauket", "capture-claude", "--hook"])
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(event("UserPromptSubmit", tmp_path, prompt="CLI hook"))))
    main()
    assert capsys.readouterr().out == ""
    monkeypatch.setattr(sys, "stdin", io.StringIO("not JSON (private)"))
    main()
    output = capsys.readouterr()
    assert output.out == ""
    assert "private" not in output.err
    monkeypatch.setattr(sys, "argv", ["setauket", "capture-claude", "--project", str(tmp_path), "--disable"])
    main()
    assert json.loads(capsys.readouterr().out)["capture_enabled"] is False


def test_manual_import_and_hook_cannot_duplicate_same_claude_session(tmp_path, monkeypatch, capsys):
    source = tmp_path / "conversation.jsonl"
    entries = [
        {"type": "user", "uuid": "user-event", "parentUuid": None, "sessionId": "claude-session-1",
         "timestamp": "2026-09-30T10:00:00Z", "cwd": str(tmp_path),
         "message": {"role": "user", "content": "Already discussed"}},
        {"type": "assistant", "uuid": "assistant-event", "parentUuid": "user-event",
         "sessionId": "claude-session-1", "timestamp": "2026-09-30T10:00:01Z", "cwd": str(tmp_path),
         "message": {"role": "assistant", "content": [{"type": "text", "text": "Already answered"}]}}
    ]
    source.write_text("\n".join(json.dumps(entry) for entry in entries) + "\n")
    config = Config(data_dir=tmp_path / "data")
    monkeypatch.setenv("SETAUKET_DATA_DIR", str(config.data_dir))
    set_enabled(config, tmp_path, True)
    hook_event = event("UserPromptSubmit", tmp_path, prompt="Already discussed", transcript_path=str(source))
    assert capture_hook(config, hook_event)
    monkeypatch.setattr(sys, "argv", ["setauket", "import-claude", "--source", str(source)])
    with pytest.raises(ValueError, match="already captured by hook"):
        main()
    assert capsys.readouterr().out == ""
    with Store(config.database).connect() as db:
        assert db.execute("SELECT count(*) FROM turns").fetchone()[0] == 1

    second = tmp_path / "import-first.jsonl"
    second.write_text(source.read_text().replace("claude-session-1", "another-session"))
    monkeypatch.setattr(sys, "argv", ["setauket", "import-claude", "--source", str(second)])
    main()
    capsys.readouterr()
    with pytest.raises(ValueError, match="already imported manually"):
        capture_hook(config, event("UserPromptSubmit", tmp_path, session_id="another-session",
                                   prompt="Already discussed", transcript_path=str(second)))



def test_omp_capture_is_independently_opted_in_and_excludes_other_fields(tmp_path):
    config = Config(data_dir=tmp_path / "data")
    set_enabled(config, tmp_path, True)  # Claude consent is not OMP consent.
    user = event("input", tmp_path, prompt_id=None, text="OMP visible prompt", source="interactive",
                 transcript_path=str(tmp_path / "omp.jsonl"), thinking="private OMP reasoning")
    assistant = event("agent_end", tmp_path, prompt_id=None, text="OMP final answer",
                      transcript_path=str(tmp_path / "omp.jsonl"), tool_result="private OMP tool output")
    assert not capture_hook(config, user, "omp")
    assert not (config.data_dir / "omp-capture.json").exists()
    assert set_enabled(config, tmp_path, True, "omp")
    assert (config.data_dir / "omp-capture.json").stat().st_mode & 0o777 == 0o600
    assert not capture_hook(config, {**user, "source": "extension"}, "omp")
    assert not capture_hook(config, {**user, "transcript_path": None}, "omp")
    assert capture_hook(config, user, "omp")
    assert not capture_hook(config, user, "omp")
    assert capture_hook(config, assistant, "omp")
    store = Store(config.database)
    with store.connect() as db:
        session_id = db.execute("SELECT id FROM sessions").fetchone()[0]
        assert db.execute("SELECT installation_key FROM harnesses").fetchone()[0] == "setauket:omp:hook"
    assert [t["content"] for t in store.get_session(session_id)["turns"]] == ["OMP visible prompt", "OMP final answer"]
    assert not store.search_rows("private")
    assert not set_enabled(config, tmp_path, False, "omp")
    assert not capture_hook(config, {**user, "text": "No longer captured"}, "omp")


def test_omp_extension_filters_interactive_events(tmp_path):
    if not shutil.which("node") or int(subprocess.check_output(["node", "-p", "process.versions.node.split('.')[0]"], text=True)) < 22:
        pytest.skip("Node 22+ required to load the TypeScript extension")
    config = Config(data_dir=tmp_path / "data")
    set_enabled(config, tmp_path, True, "omp")
    source = tmp_path / "session.jsonl"
    source.write_text("{}\n")
    extension = Path(__file__).resolve().parents[1] / "integrations/omp/extensions/setauket.ts"
    script = """
        const {default: load} = await import(process.argv[1]);
        const handlers = {};
        load({on: (name, handler) => handlers[name] = handler});
        const ctx = {cwd: process.argv[3], mode: 'tui', sessionManager: {
            getSessionFile: () => process.argv[2], getSessionId: () => 'omp-fixture'}};
        await handlers.input({source: 'extension', text: 'private injected prompt'}, ctx);
        await handlers.input({source: 'interactive', text: 'Visible OMP prompt'}, ctx);
        await handlers.agent_end({messages: [{role: 'assistant', stopReason: 'stop',
            content: [{type: 'thinking', thinking: 'private reasoning'},
                      {type: 'toolCall', arguments: {token: 'private argument'}},
                      {type: 'text', text: 'Visible OMP reply'}]}]}, ctx);
    """
    env = {**os.environ, "SETAUKET_DATA_DIR": str(config.data_dir),
           "PATH": f"{Path(sys.executable).parent}:{os.environ.get('PATH', '')}"}
    subprocess.run(["node", "--input-type=module", "-e", script, extension.as_uri(),
                    str(source), str(tmp_path)], env=env, capture_output=True, check=True, timeout=15)
    with Store(config.database).connect() as db:
        assert [(r[0], r[1]) for r in db.execute("SELECT role,content FROM turns ORDER BY rowid")] == [
            ("user", "Visible OMP prompt"), ("assistant", "Visible OMP reply")]
        assert db.execute("SELECT count(*) FROM capture_events").fetchone()[0] == 2


def test_omp_capture_refuses_manual_import_of_same_source(tmp_path, monkeypatch):
    from setauket.session_import import parse_session

    source = tmp_path / "omp-session.jsonl"
    lines = [
        {"type": "session", "id": "claude-session-1", "version": 3,
         "cwd": str(tmp_path), "timestamp": "2026-09-30T10:00:00Z"},
        {"type": "message", "id": "u", "parentId": None, "timestamp": "2026-09-30T10:00:01Z",
         "message": {"role": "user", "content": "OMP prompt"}},
    ]
    source.write_text("\n".join(json.dumps(line) for line in lines) + "\n")
    config = Config(data_dir=tmp_path / "data")
    set_enabled(config, tmp_path, True, "omp")
    e = event("input", tmp_path, prompt_id=None, text="OMP prompt", source="interactive",
              transcript_path=str(source))
    assert capture_hook(config, e, "omp")
    store = Store(config.database)
    assert store.was_captured_session("omp", str(tmp_path), parse_session(source).source_id)
    monkeypatch.setenv("SETAUKET_DATA_DIR", str(config.data_dir))
    monkeypatch.setattr(sys, "argv", ["setauket", "import-omp", "--source", str(source)])
    with pytest.raises(ValueError, match="already captured by hook"):
        main()


def test_codex_capture_reuses_claude_payload_shape(tmp_path):
    config = Config(data_dir=tmp_path / "data")
    user = event("UserPromptSubmit", tmp_path, prompt="Codex prompt")
    reply = event("Stop", tmp_path, last_assistant_message="Codex reply")
    assert not capture_hook(config, user, "codex")  # Codex consent is independent of Claude consent.
    assert not (config.data_dir / "codex-capture.json").exists()
    assert set_enabled(config, tmp_path, True, "codex")
    assert capture_hook(config, user, "codex")
    assert not capture_hook(config, user, "codex")
    assert capture_hook(config, reply, "codex")
    store = Store(config.database)
    with store.connect() as db:
        assert db.execute("SELECT name FROM harnesses WHERE installation_key='setauket:codex:hook'").fetchone()[0] == "OpenAI Codex"
        assert db.execute("SELECT external_id FROM agents").fetchone()[0] == "codex-session:claude-session-1"
        session = db.execute("SELECT id FROM sessions").fetchone()[0]
    assert [t["content"] for t in store.get_session(session)["turns"]] == ["Codex prompt", "Codex reply"]


def test_devin_capture_needs_project_env_and_skips_missing_reply(tmp_path, monkeypatch):
    config = Config(data_dir=tmp_path / "data")
    set_enabled(config, tmp_path, True, "devin")
    payload = {"hook_event_name": "UserPromptSubmit", "session_id": "devin-1", "prompt_id": "p1",
               "prompt": "Devin prompt"}
    assert not capture_hook(config, payload, "devin")  # No cwd field and no DEVIN_PROJECT_DIR.
    monkeypatch.setenv("DEVIN_PROJECT_DIR", str(tmp_path))
    assert capture_hook(config, payload, "devin")
    assert not capture_hook(config, payload, "devin")  # Consecutive duplicate collapses.
    stop = {"hook_event_name": "Stop", "session_id": "devin-1", "stop_hook_active": False}
    assert not capture_hook(config, stop, "devin")  # Devin publishes no reply text yet; fail closed.
    with pytest.raises(ValueError, match="Unsupported capture harness"):
        Store(config.database).capture_turn("bogus", str(tmp_path), "s", None, "user", "x")


def test_copilot_capture_tolerates_event_and_field_variants(tmp_path):
    config = Config(data_dir=tmp_path / "data")
    set_enabled(config, tmp_path, True, "copilot")
    assert capture_hook(config, event("userPromptSubmitted", tmp_path, prompt="Copilot prompt"), "copilot")
    assert capture_hook(config, event("UserPromptSubmit", tmp_path, prompt="Second", prompt_id="p2"), "copilot")
    stop = event("agentStop", tmp_path, response="Copilot reply", prompt_id="r1")
    assert capture_hook(config, stop, "copilot")  # Reply arrives under the fallback field.
    assert not capture_hook(config, event("agentStop", tmp_path, prompt_id="r2"), "copilot")  # No text field.


def test_opencode_and_cline_wire_format_is_independently_opted_in(tmp_path):
    config = Config(data_dir=tmp_path / "data")
    user = {"hook_event_name": "input", "cwd": str(tmp_path), "session_id": "ext-1", "text": "Visible"}
    assert not capture_hook(config, user, "opencode")
    set_enabled(config, tmp_path, True, "opencode")
    assert not capture_hook(config, user, "cline")  # OpenCode consent is not Cline consent.
    assert capture_hook(config, user, "opencode")
    assert not capture_hook(config, user, "opencode")  # Consecutive duplicate collapses.
    assert capture_hook(config, {**user, "hook_event_name": "agent_end", "text": "Visible reply"}, "opencode")
    set_enabled(config, tmp_path, True, "cline")
    assert capture_hook(config, {**user, "session_id": "ext-2"}, "cline")
    store = Store(config.database)
    with store.connect() as db:
        names = dict(db.execute("SELECT installation_key,name FROM harnesses").fetchall())
    assert names["setauket:opencode:hook"] == "OpenCode"
    assert names["setauket:cline:hook"] == "Cline"


def test_cli_accepts_every_capture_harness_and_fails_open(tmp_path, monkeypatch, capsys):
    data_dir = tmp_path / "data"
    for harness in sorted(CAPTURE_HARNESSES - {"claude", "omp"}):
        monkeypatch.setenv("SETAUKET_DATA_DIR", str(data_dir))
        monkeypatch.setattr(sys, "argv", ["setauket", f"capture-{harness}", "--project", str(tmp_path), "--enable"])
        main()
        assert json.loads(capsys.readouterr().out)["capture_enabled"] is True
        monkeypatch.setattr(sys, "argv", ["setauket", f"capture-{harness}", "--hook"])
        monkeypatch.setattr(sys, "stdin", io.StringIO("not JSON (private)"))
        main()
        output = capsys.readouterr()
        assert output.out == ""
        assert "private" not in output.err


def test_opencode_plugin_forwards_visible_text_only(tmp_path):
    if not shutil.which("node") or int(subprocess.check_output(["node", "-p", "process.versions.node.split('.')[0]"], text=True)) < 22:
        pytest.skip("Node 22+ required to load the TypeScript plugin")
    config = Config(data_dir=tmp_path / "data")
    set_enabled(config, tmp_path, True, "opencode")
    plugin = Path(__file__).resolve().parents[1] / "integrations/opencode/setauket.ts"
    script = """
        const mod = await import(process.argv[1]);
        const plugin = await mod.SetauketCapture({
          directory: process.argv[2],
          client: { session: { messages: async () => ({ data: [
            { info: { id: 'u1', role: 'user' }, parts: [
              { type: 'thinking', thinking: 'private reasoning' },
              { type: 'text', text: 'Visible OpenCode prompt' }] },
            { info: { id: 'a1', role: 'assistant' }, parts: [
              { type: 'tool', callID: 't1' },
              { type: 'text', text: 'Visible OpenCode reply' }] },
          ] }) } },
        });
        await plugin.event({ event: { type: 'session.idle', properties: { sessionID: 'oc-1' } } });
        await plugin.event({ event: { type: 'session.idle', properties: { sessionID: 'oc-1' } } });
    """
    env = {**os.environ, "SETAUKET_DATA_DIR": str(config.data_dir),
           "PATH": f"{Path(sys.executable).parent}:{os.environ.get('PATH', '')}"}
    subprocess.run(["node", "--input-type=module", "-e", script, plugin.as_uri(), str(tmp_path)],
                   env=env, capture_output=True, check=True, timeout=15)
    store = Store(config.database)
    with store.connect() as db:
        assert db.execute("SELECT count(*) FROM turns").fetchone()[0] == 2  # Second idle records nothing new.
    assert not store.search_rows("private")  # thinking and tool parts are never stored.


def test_cline_plugin_forwards_visible_text_only(tmp_path):
    if not shutil.which("node") or int(subprocess.check_output(["node", "-p", "process.versions.node.split('.')[0]"], text=True)) < 22:
        pytest.skip("Node 22+ required to load the TypeScript plugin")
    config = Config(data_dir=tmp_path / "data")
    set_enabled(config, tmp_path, True, "cline")
    plugin = Path(__file__).resolve().parents[1] / "integrations/cline/setauket.ts"
    script = """
        const mod = await import(process.argv[1]);
        const plugin = mod.default;
        plugin.setup(undefined, { cwd: process.argv[2], sessionId: 'cline-fixture' });
        await plugin.hooks.beforeRun({ prompt: 'Visible Cline prompt' });
        await plugin.hooks.beforeRun({ prompt: 'Visible Cline prompt' });
        await plugin.hooks.afterRun({ result: { output: 'Visible Cline reply' } });
        await plugin.hooks.afterRun({ result: { output: 'Visible Cline reply' } });
    """
    env = {**os.environ, "SETAUKET_DATA_DIR": str(config.data_dir),
           "PATH": f"{Path(sys.executable).parent}:{os.environ.get('PATH', '')}"}
    subprocess.run(["node", "--input-type=module", "-e", script, plugin.as_uri(), str(tmp_path)],
                   env=env, capture_output=True, check=True, timeout=15)
    store = Store(config.database)
    with store.connect() as db:
        assert [(row[0], row[1]) for row in db.execute("SELECT role,content FROM turns ORDER BY rowid")] == [
            ("user", "Visible Cline prompt"), ("assistant", "Visible Cline reply")]
    assert not store.search_rows("private")


def test_plugin_files_are_valid_json():
    root = Path(__file__).resolve().parents[1]
    plugin = root / "plugins/setauket"
    manifest = json.loads((plugin / ".claude-plugin/plugin.json").read_text())
    codex_manifest = json.loads((plugin / ".codex-plugin/plugin.json").read_text())
    marketplace = json.loads((root / ".claude-plugin/marketplace.json").read_text())
    mcp = json.loads((plugin / ".mcp.json").read_text())
    hooks = json.loads((plugin / "hooks/hooks.json").read_text())
    assert manifest["name"] == marketplace["plugins"][0]["name"] == codex_manifest["name"] == "setauket"
    assert marketplace["owner"]["name"] and isinstance(marketplace["plugins"], list)
    assert marketplace["plugins"][0]["source"] == "./plugins/setauket"
    assert mcp["mcpServers"]["setauket"]["url"] == "http://127.0.0.1:19005/mcp"
    assert set(hooks["hooks"]) == {"UserPromptSubmit", "Stop"}
    assert {hook["command"] for group in hooks["hooks"].values() for hook in group[0]["hooks"]} == {"setauket"}
    assert codex_manifest["hooks"] == "./codex/hooks.json"
    for harness, path, wrapper in (("codex", "codex/hooks.json", True), ("devin", "devin/hooks.v1.json", False),
                                   ("copilot", "copilot/hooks.json", False)):
        document = json.loads((plugin / path).read_text())
        events = document["hooks"] if wrapper else document
        commands = {hook["command"] for group in events.values() for hook in group[0]["hooks"]}
        assert commands == {f"setauket capture-{harness} --hook"}, path
    assert (plugin / "skills/setauket-memory/SKILL.md").exists()
    omp = root / "integrations/omp"
    package = json.loads((omp / "package.json").read_text())
    assert package["pi"]["extensions"] == ["./extensions/setauket.ts"]
    assert (omp / "extensions/setauket.ts").exists()
    assert (omp / "skills/setauket-memory/SKILL.md").exists()
    assert (root / "integrations/opencode/setauket.ts").is_file()
    cline = root / "integrations/cline"
    cline_package = json.loads((cline / "package.json").read_text())
    assert cline_package["cline"]["plugins"] == [{"paths": ["./setauket.ts"], "capabilities": ["hooks"]}]
    assert (cline / "setauket.ts").is_file()
    assert (cline / "skills/setauket-memory/SKILL.md").is_file()


def test_agents_standard_layout_serves_one_copy_of_the_skill():
    """`.agents`, Claude Code, and OMP must not drift: all three resolve to one file."""
    root = Path(__file__).resolve().parents[1]
    skill = root / "plugins/setauket/skills/setauket-memory/SKILL.md"
    assert skill.is_file() and not skill.is_symlink()  # the plugin holds the real file
    text = skill.read_bytes()
    frontmatter = skill.read_text().split("---")[1]
    assert "name: setauket-memory" in frontmatter
    assert "description:" in frontmatter, "most harnesses require description frontmatter"

    for relative in (".agents/skills/setauket-memory", "integrations/omp/skills/setauket-memory",
                     "integrations/cline/skills/setauket-memory"):
        linked = root / relative
        assert linked.is_symlink(), f"{relative} must bridge to the plugin, not hold a copy"
        assert linked.resolve() == skill.parent.resolve()
        assert linked.joinpath("SKILL.md").read_bytes() == text

    # The composed instruction fragment Tallmadge merges into ~/.agents/agents.md.
    fragment = root / "plugins/setauket/agents.md"
    assert fragment.is_file() and fragment.read_text().strip()
    assert (root / "AGENTS.md").is_file(), "canonical repo instructions are required"
