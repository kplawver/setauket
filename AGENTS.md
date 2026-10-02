# Setauket

Local, cross-harness context storage for coding agents. One ASGI process serves a Streamable HTTP MCP endpoint (`/mcp`) and a read-only browser (`/`).

**Scope.** Setauket stores **session history and durable decisions**, and nothing else. Agent-to-agent messaging is [Clothesline](https://github.com/kplawver/clothesline) on port 19004. Do not add presence, conversations, or message delivery here. Each service keeps its own copy of the `harnesses`/`agents`/`projects` tables on purpose, so neither depends on the other.

## Layout

| Path | What |
|---|---|
| `src/setauket/identity.py` | Harness/agent/project schema and registration. Duplicated in Clothesline by design. |
| `src/setauket/storage.py` | Sessions, turns, summaries, memories, chunks, FTS, vector indexes, and job queue. |
| `src/setauket/models.py` | Pinned local embedding and summarization models. |
| `src/setauket/worker.py` | Background embedding and catch-up archival. |
| `src/setauket/app.py` | MCP tools and read-only browser routes. |
| `src/setauket/cli.py` | `serve`, `setup`, `status`, `doctor`, `backup`, `rebuild-index`, `import-*`, `capture-*`. |
| `scripts/split_legacy_db.py` | One-shot migration from the old combined Clothesline database. Not on the CLI. |
| `.agents/skills/` | Generic `.agents` standard. Each entry is a symlink into the plugin's `skills/`. |
| `plugins/claude-code/` | Claude Code packaging. Holds the real skill files, `.mcp.json`, capture `hooks/`, and an `agents.md` fragment Tallmadge composes into `~/.agents/agents.md`. |
| `integrations/omp/` | Oh My Pi extension and skill. Its `skills/` entry is a symlink into the plugin. |

The links run from `.agents/` and `integrations/omp/` into the plugin, not the reverse, because `claude plugin validate --strict` treats a symlinked plugin component as a warning it will not follow. One copy of the text serves all three readers.

## Ground rules

- **Loopback only.** `Config` rejects any host that is not `127.0.0.1`, `::1`, or `localhost`. Keep it that way; the service is unauthenticated.
- **Capturing is attributed, not authenticated.** Never describe a harness or agent name as proof of identity.
- **Retention is explicit.** Raw turns live three days past the last turn, then a generated summary replaces them in one transaction. Summaries are lossy and must never be promoted into preferences on their own.
- **Captured and imported text is not a secret scanner.** Only visible user and assistant text is stored, and that text can still contain secrets.
- **A session reference is owned here.** Clothesline stores `conversations.session_id` opaquely and cannot validate it. Do not add validation there, and do not assume Clothesline can see these tables.
- **Nothing leaves the machine** except model downloads from Hugging Face and whatever the user explicitly imports from their own files.

## Change checklist

1. `uv run ruff check .`
2. `uv run pytest -q`
3. `claude plugin validate . --strict && claude plugin validate plugins/claude-code --strict`
4. Database schema changes: bump `user_version` and add a migration, since `Store.__init__` gates on it. The schema is at version 1; it restarts at 1 because the v0.7 split left no legacy schema to migrate.
5. Changing `pyproject.toml` dependencies means `uv lock`, and a lock change means the Homebrew tarball hash changes.