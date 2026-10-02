# Refactor Plan: Separate Clothesline (completed)

This repository is Setauket, the memory half. See [kplawver/clothesline](https://github.com/kplawver/clothesline) for the messaging half.

Clothesline was really two separate things: a context store and a messaging system. Those things didn't go together, and people who want one might not want the other. They are now two projects, both at **0.7.0**.

| | Repository | Port | Holds |
|---|---|---|---|
| **Clothesline** | `kplawver/clothesline` (this repo) | 19004 | Identity, presence, conversations, messages |
| **Setauket** | `kplawver/setauket` | 19005 | Sessions, turns, memories, embeddings, archiving, importers, capture |

## What went where

**Setauket:** `models.py`, `worker.py`, `capture.py`, `session_import.py`, `file_import.py`, `database_import.py`, `omp_import.py`, the session/memory half of `storage.py`, 9 MCP tools, 5 web routes, 6 templates, all `import-*` and `capture-*` commands, the Claude Code capture hooks, and `integrations/omp/`.

**Clothesline:** `bus.py`, the identity and conversation half of `storage.py`, 12 MCP tools, 4 web routes, 4 templates, and `serve`/`status`/`doctor`/`backup`/`rebuild-index`.

**Both:** a new `identity.py` holding the `harnesses`/`agents`/`projects` schema and the four registration helpers, so `bus.py` stops reaching into `Store`'s private methods. Each service keeps its own copy of those tables. Registering the same `(installation_key, external_id)` pair resolves to the same logical agent within each service, but row IDs are not comparable across services — nothing joins across them, so there is no drift to manage.

## What was given up

`conversations.session_id` was a foreign key into `sessions` with a check that the conversation's project matched its session. Across two databases that check cannot exist. It is now an opaque `TEXT` reference: `open_conversation` validates only its shape, and the conversation page renders the ID as text instead of a link. Everything else cross-cut cleanly.

## Dependency result

Clothesline dropped `sqlite-vec`, `fastembed`, `huggingface-hub`, and `zstandard`. It has no background worker, no `setup` command, and no `llama.cpp` dependency — four pure-Python packages and a SQLite file. Setauket inherited the models and the worker unchanged.

## Data migration

`setauket/scripts/split_legacy_db.py` splits a schema-v5 database into both. It preserves harness, agent, and project IDs on both sides so an agent keeps its identity, copies already-embedded vectors so nothing needs re-embedding, and refuses to run on a wrong schema version, an existing output path, or a source indexed with a different embedding model. It verifies `integrity_check` and `foreign_key_check` on both outputs and prints a per-table row reconciliation.

Drilled against a backup of the live database: 26,581 turns, 39,216 chunks, 488 sessions, 490 agents, 36 projects to Setauket; 1 conversation, 1 message, 2 presence rows to Clothesline. Every count matched. Both sides served their browser pages and MCP tools.

Both schemas restart at `user_version=1`. There are no in-place migrations from v5 in either repo, because after the split no legacy schema remains.

## Both still have

A webserver the user can browse, configurable `host`/`port`/`data_dir` via TOML and environment, loopback-only binding, `status`/`doctor`/`backup`, an MCP server, and skills that tell agents how to use it.