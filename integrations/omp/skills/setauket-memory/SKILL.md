---
name: setauket-memory
description: Recall previous work, decisions, or preferences from Setauket's local MCP tools when asked about history across coding agents.
---

Use Setauket MCP's `search_sessions` and `search_memories` before claiming what happened in another session or harness. Inspect relevant results with `get_session` or `get_memory`; cite project, date, and attribution. Summaries can omit details, so state uncertainty.

Do not store a decision or preference with `remember` unless the user explicitly requests it. A server connection alone does not capture turns. The optional OMP extension captures only visible user text and final assistant text in projects the user enabled with `setauket capture-omp --enable --project PATH`. Never enable capture on a user's behalf. Visible text may contain secrets; reasoning, tool calls, tool output, and images are not captured by the extension.

Setauket stores memory only. Agent-to-agent messaging lives in the separate Clothesline MCP server.