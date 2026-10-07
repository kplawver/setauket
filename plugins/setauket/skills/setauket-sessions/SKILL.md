---
name: setauket-sessions
description: "Use at the beginning of every new session to initialize a session."
---

Initialize once per harness installation, not per connection. Generate an installation key locally — any stable unique string such as a UUID — and persist it before first use. Call `register_harness(installation_key, name)`, then `register_agent(harness_id, external_id)` with a stable external_id per agent (pass parent_id for sub-agents). Setauket generates and returns the harness_id and agent_id to persist. Registration is idempotent: the same installation_key and external_id always resolve to the same IDs, and only the installation key is unrecoverable — lose it and the next run forks identity. Connecting to MCP alone registers nothing.

Start a session with `start_session(harness_id, agent_id, project_key)`, where project_key is a stable repository identifier or path. Store the returned session_id for the rest of the session.

Submit each visible turn with `append_turn(session_id, harness_id, agent_id, source_id, role, blocks)` and a stable source_id so retries are safe. MCP cannot capture turns automatically. Submit only visible user and assistant text; reasoning and thinking blocks are discarded before storage, and never submit tool output. Visible text can still contain secrets.

Close with `end_session(session_id, harness_id)` when work finishes. A new turn reopens an ended session until archival: after 72 hours of inactivity, raw turns are replaced by lossy generated summaries. Use the setauket-memory skill to search past sessions and to store decisions.
