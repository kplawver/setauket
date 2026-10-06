# Setauket

Local, cross-harness memory for coding agents. A single ASGI process serves a Streamable HTTP MCP endpoint (`/mcp`) and a read-only browser (`/`). Submitted session turns are searchable for three days after inactivity; then they are summarized and raw turns are removed. Decisions and preferences remain versioned until explicitly superseded.

**Setauket is a context store only.** Durable messaging between agents is a separate project, [Clothesline](https://github.com/kplawver/clothesline), on port 19004. You can install either service without the other.

**Status: 0.8.0.** Connecting to MCP does not capture sessions. Claude Code and OMP offer optional per-project live capture; other clients must explicitly submit turns or opt in to an existing-session importer. Never submit secrets or private reasoning as text blocks.

## Why "Setauket"?

Naming things is hard. Starting with my other project, [Tallmadge](https://tallmadge.dev), I picked the Culper Ring, George Washington's spy ring, lead by Benjamin Talmadge.  Their headquarters was in Setauket, New York, hence the name!  The team worked in codes and ciphers, keeping meticulous records about British activity and plans, and I figured it was probably kept at their headquarters at least _sometimes_.

## Install with Homebrew

```sh
brew tap kplawver/tap
brew install setauket
setauket setup  # downloads ~67 MB embeddings and ~676 MB GGUF, once
brew services start setauket
```

Open http://127.0.0.1:19005/ for the browser and connect agents to http://127.0.0.1:19005/mcp. The formula uses the locked `uv` dependencies at install time, which requires access to the Python package index; models download separately on `setup`. The user-level brew service uses the default data directory, preserving memory across upgrades. Use `brew services restart setauket` after changing configuration.

## Development

Requires Python 3.12+ and Homebrew's `llama.cpp` for local session summaries (`brew install llama.cpp`). Native `llama-cpp-python` proved too slow to build reliably on the test machine, so summarization invokes the bottled `llama-cli` as a short-lived local subprocess; the HTTP and browser interfaces remain in one process.

```sh
uv venv --python 3.12
brew install llama.cpp
uv pip install --python .venv/bin/python -e '.[dev]'
.venv/bin/setauket setup  # downloads ~67 MB embedding model and ~676 MB GGUF
.venv/bin/setauket serve
```

`setauket setup` downloads both models explicitly, verifies their pinned SHA-256 hashes, and retries previously blocked jobs. The server does not download models when processing requests. Without installed models, keyword search and session submission work, but embedding jobs and archive jobs remain pending and retry until dependencies are available. `setauket doctor` shows failed jobs.

UI: http://127.0.0.1:19005/ · MCP: http://127.0.0.1:19005/mcp

Test without downloading models:

```sh
uv pip install --python .venv/bin/python -e '.[dev]'
.venv/bin/python -m pytest -q
```

## Configuration

Place TOML at `~/.config/setauket/config.toml`, or use `SETAUKET_CONFIG` to select another path:

```toml
host = "127.0.0.1"
port = 19005
data_dir = "~/Library/Application Support/Setauket"
```

`SETAUKET_HOST`, `SETAUKET_PORT` and `SETAUKET_DATA_DIR` override these values. The service rejects non-loopback addresses. Browser pages are read-only; settings are changed via the file and a restart. Paths, model files and database live outside the installed package. `setauket backup --output /path/to/backup.sqlite3` creates a consistent SQLite backup. `setauket rebuild-index` recreates keyword indexes and requeues all embeddings from stored chunks.

## MCP workflow

Register a harness with a persistent installation key, then an agent ID. Start a session, submit each visible turn via `append_turn` (use stable source event IDs for deduplication), and optionally end the session. For a sub-agent, register a separate agent with its parent's ID. Search and inspect memories at any time. Memory writes require explicit user intent; a revision supersedes rather than deletes the previous memory. Tool descriptions contain the usage guidance.

Claude Code supports HTTP MCP servers. This checkout has a project-scoped `.mcp.json` for Setauket; Claude Code may require approval before first use. Claude Code has successfully called `search_sessions` and recalled a Zed-submitted test turn across harnesses. Tool use is *cooperative*: no automatic transcript capture or identity persistence is provided by this server.

Zed can register Setauket as a remote MCP server through `context_servers`:

```json
"setauket": { "enabled": true, "url": "http://127.0.0.1:19005/mcp" }
```

The local development server must already be running. If Zed loaded this setting before the server started, restart the MCP connection in Zed's Settings → AI → MCP Servers. The separate Pi CLI (`pi`) does not include a built-in MCP client in the installed version inspected; it would need an adapter.

Neither MCP transport sessions nor model-facing harness names act as authenticated identities.

## Retention and search

After 72 hours without new turns, a daily sweep schedules an archive job. The session is compacted in 25-turn segments: one summary per segment is indexed before raw turns and their search indexes are deleted in one database transaction. Bounded segments keep each summarizer call small so long sessions cannot degenerate into echoing or truncating output. Failed summary jobs keep the originals and show errors in the dashboard. Generated summaries are lossy and should not be promoted automatically into preferences. Original turn content and associated source event IDs are no longer available after archival; project, session dates, and attribution remain.

FTS5 handles literal keyword queries; sqlite-vec adds local semantic search when embeddings are installed and processed. The web UI uses keyword search. All content remains on this machine except model downloads from Hugging Face.

## Import existing sessions (opt-in)

Connecting via MCP does **not** capture a session. Select **one** saved session at a time for manual import. First run `--dry-run` to preview counts without opening or changing the Setauket database:

```sh
setauket import-omp --source ~/.omp/agent/sessions/PROJECT/SESSION.jsonl --dry-run
setauket import-pi --source ~/.pi/agent/sessions/PROJECT/SESSION.jsonl --dry-run
setauket import-claude --source ~/.claude/projects/PROJECT/SESSION.jsonl --dry-run
setauket import-codex --source ~/.codex/sessions/YEAR/MONTH/DAY/SESSION.jsonl --dry-run

setauket import-zed --source ~/Library/'Application Support'/Zed/threads/threads.db --list
setauket import-zed --source ~/Library/'Application Support'/Zed/threads/threads.db --session-id THREAD_ID --dry-run
setauket import-opencode --source ~/.local/share/opencode/opencode.db --list
setauket import-opencode --source ~/.local/share/opencode/opencode.db --session-id SESSION_ID --dry-run
```

Omit `--dry-run` to import. `--list` prints only IDs, not titles or message content. Pi and OMP import the active tree branch; Claude Code imports the main branch, or a **separately selected** `subagents/agent-*.jsonl` file with distinct agent attribution. Codex imports visible conversation events, not duplicate response items. Zed and OpenCode databases are opened **read-only** and require a specific thread/session ID. Zed does not provide per-message dates in its thread data, so imported turns use the thread snapshot's update time.

Only visible user and assistant text is copied. Structured thinking/reasoning, system prompts, tool arguments and results, attachments/images, and alternative branches are excluded. **This is not a secret scanner:** visible text can still contain secrets; review sessions before importing. Each batch of turns and its checkpoint is atomic, and re-imports add only new turns. If previously imported text changes or a branch is replaced, Setauket refuses to silently mix histories. Once a session is archived, new turns start a separate segment. Session files are limited to 50 MiB and individual visible turns to 250,000 characters.

Original harness files/databases are never modified. Historical imports may be archived at the next daily sweep based on their original timestamps. There is no background scanning of saved session files.

## Installing the skills and MCP server

Both packaging paths read the same skill text. Pick whichever fits your harness.

### Generic `.agents` standard (Tallmadge, most harnesses)

[Tallmadge](https://github.com/kplawver/tallmadge) (`clpr`) resolves plugins from the same `.claude-plugin/marketplace.json` this repo publishes, so one catalog serves both systems:

```sh
clpr marketplace add kplawver/setauket
clpr activate setauket@setauket
```

That symlinks the `setauket-memory` skill and the MCP server into `~/.agents/` and composes the plugin's `agents.md` fragment into `~/.agents/agents.md`, so every harness bridged by `clpr` gets the memory guidance. The OMP skill under `integrations/omp/skills/` points at the same file, so all three stay in step.

The repository is in the canonical layout — `AGENTS.md` at the root, skills bridged through `.agents/skills/`, and `CLAUDE.md` plus `.claude/skills` as committable relative symlinks — so a teammate who clones it inherits the standards with nothing to install. `clpr repo check` audits this and runs in CI.

As long as your coding harness supports marketplaces and skills with hooks, Setauket should work fine.  If not, open an issue and let me know what's wrong and let's figure it out!

### Claude Code plugin: opt-in live capture

Install Setauket with Homebrew and start its service first. Then install the Setauket plugin, which bundles the MCP connection, the `setauket-memory` skill, and hooks for `UserPromptSubmit` and `Stop`:

```sh
claude plugin marketplace add kplawver/setauket
claude plugin install setauket@setauket --scope local  # or --scope user
setauket capture-claude --enable --project /absolute/path/to/project
setauket capture-claude --project /absolute/path/to/project  # show status
```

Restart Claude Code after installation. If the project also has a Setauket `.mcp.json`, the plugin may expose a second connection; keep only one MCP configuration. Installing the plugin alone **does not enable capture**: the allowlist is stored at `<data_dir>/claude-capture.json` (mode 0600), with **exact project directory matches**. The hooks submit the visible user prompt and final assistant text to Setauket's turn storage. They do not read the transcript, thinking, tool calls, tool results, or images. Claude Code writes its transcript asynchronously; the hooks use the event's current text fields rather than relying on a possibly stale file. The hook fails open if capture fails and never blocks a user prompt.

```sh
setauket capture-claude --disable --project /absolute/path/to/project
```

Disabling stops **new** capture; it does not delete turns already stored, archived summaries, or backups. Visible prompts and final replies can contain secrets, including pasted text. The plugin does not scan or redact them; only enable projects you're comfortable storing locally. Existing manually imported Claude sessions must not be captured again (and vice versa); Setauket refuses this combination for the same session. This plugin does not automate message-bus presence, heartbeats, or polling — that is [Clothesline's](https://github.com/kplawver/clothesline) job. For development, load `plugins/claude-code` with `claude --plugin-dir /path/to/setauket/plugins/claude-code`. The bundled MCP URL assumes the default loopback port 19005.

## OMP extension: opt-in live capture

OMP supports native HTTP MCP; keep your normal Setauket MCP connection for recall. The separate OMP package under `integrations/omp/` adds an extension and a `setauket-memory` skill. Try it for one session from a checkout before installing it:

```sh
setauket capture-omp --enable --project /absolute/path/to/project
omp --extension /path/to/setauket/integrations/omp/extensions/setauket.ts
# Or install the extension and skill package for future sessions:
omp plugin install /path/to/setauket/integrations/omp
```

The allowlist at `<data_dir>/omp-capture.json` is separate from Claude Code's. Installing or loading the extension alone **does not enable capture**. It records visible interactive/RPC user input and the final assistant response, dropping structured thinking, tool calls, tool output, and images. OMP print mode does not emit an input event; the extension uses the finalized user text from the completed turn instead. Ephemeral `--no-session` runs are not captured. Do not manually import an already captured OMP session; Setauket refuses duplicates. Disable new capture with `setauket capture-omp --disable --project /absolute/path/to/project`.

On the installed OMP 18.4.4, `omp plugin install` links the local package into the user-level plugin store even when `--scope project` is passed. Its uninstall command requires `bun` on `PATH`; use `--extension` for a one-session trial if you do not want a user-wide plugin link. The extension requires the `setauket` CLI on `PATH` and never writes without per-project consent.

## Migrating from the 0.6.x combined service

0.7.0 splits the former `clothesline` service in two. Session history moved here; conversations and messages stayed in [Clothesline](https://github.com/kplawver/clothesline).

Against a **backup** of the old schema-v5 database, run:

```sh
python scripts/split_legacy_db.py \
  --source ~/Library/'Application Support'/Clothesline/clothesline.sqlite3 \
  --setauket-out ~/Library/'Application Support'/Setauket/setauket.sqlite3 \
  --clothesline-out ~/Library/'Application Support'/Clothesline/clothesline.sqlite3
```

Both stores must be importable, so install both packages first: `uv pip install -e '.[dev]' -e ../clothesline`.

The script copies session history here and conversations and messages to Clothesline, keeps harness, agent, and project IDs identical in both so an agent keeps its identity, and leaves already-embedded vectors intact so nothing needs re-embedding. It refuses to overwrite existing outputs, refuses a source that is not schema v5, and refuses to split if the legacy database was indexed with a different embedding model than Setauket pins. It then verifies row counts, `integrity_check`, and `foreign_key_check` on both sides and prints a reconciliation table.

Setauket's schema is at version 2: v2 replaced the one-summary-per-session table with ordered per-segment summaries, and v1 databases migrate in place on first open. There are no migrations from schema v5, because after the split no legacy schema remains.

## Release notes for 0.8.0

Session compaction now summarizes each 25-turn segment of a session separately instead of producing one whole-session summary. Each summarizer call sees a bounded slice of the transcript, which preserves more detail and avoids the degenerate echo-and-truncate behavior the whole-session map/combine passes showed on long or repetitive sessions. The database migrates to schema v2 in place on first open; existing summaries are preserved unchanged as segment 0. Sessions with no submitted turns now archive without a placeholder summary row. The `get_session` tool returns `summaries`, an ordered list, instead of a single `summary`.

## Release notes for 0.7.1

Makes the lockfile installable on Intel Macs. onnxruntime, pulled in by fastembed, has published no macOS x86_64 wheel since 1.23.2, so `brew install setauket` could not build an environment there. The lockfile now resolves against macOS x86_64 as well, pinning onnxruntime 1.23.2 for that platform only and leaving Apple Silicon, Linux, and Windows on the current release. `requires-python` is capped below 3.14 because from 3.14 on fastembed requires onnxruntime>=1.24.2, which has no x86_64 macOS wheel at any version. No behavior change on existing Apple Silicon installs.

CI now pins uv's managed CPython build. The runner's system Python is compiled without SQLite extension support, so sqlite-vec could not load there.

## Release notes for 0.7.0

Split out of Clothesline. This repository inherits sessions, turns, memories, embeddings, the archive worker, all session importers, and capture. Harnesses and agents are registered here independently of Clothesline's copy: the same `(installation_key, external_id)` pair resolves to the same logical agent within each service, but row IDs are not comparable across the two. Messages, presence, and conversations are not stored here.

Setauket is licensed under the MIT License; see `LICENSE`.
