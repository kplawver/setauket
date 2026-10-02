---
description: Recall relevant decisions and past coding sessions across agents using the local Setauket MCP tools when asked about previous work, project history, or preferences.
---

Use Setauket's `search_sessions` and `search_memories` tools before claiming what happened in earlier sessions. Check source dates, project, and attribution. Use `get_session` or `get_memory` to verify a relevant hit. Search results and generated summaries can be incomplete; say when the evidence is inconclusive.

Only call `remember` when the user explicitly wants to store or revise a decision or preference. Register a persistent harness and agent identity as the tool descriptions require for manual MCP submissions. Merely connecting to MCP does not capture turns.

The companion hooks capture only visible prompts and final assistant replies, and only when the user enables capture for the current project with `setauket capture-claude --enable --project PATH`. Do not enable it on behalf of the user. It never captures reasoning or tool output; visible text can still contain secrets. Disable with `setauket capture-claude --disable --project PATH`.

Setauket is the memory service only. Agent-to-agent messaging lives in the separate Clothesline MCP server.