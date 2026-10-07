---
name: setauket-memory
description: Recall relevant decisions and past coding sessions across agents using the local Setauket MCP tools when asked about previous work, project history, or preferences.
---

Use Setauket's `search_sessions` and `search_memories` tools before claiming what happened in earlier sessions. Check source dates, project, and attribution. Use `get_session` or `get_memory_history` to verify a relevant hit. Search results and generated summaries can be incomplete; say when the evidence is inconclusive.

Only call `remember` when the user explicitly wants to store or revise a decision or preference. Register a persistent harness and agent identity as the tool descriptions require for manual MCP submissions. Merely connecting to MCP does not capture turns.

Capture is opt-in per project and per harness, and this skill cannot enable it. Claude Code captures only visible prompts and final assistant replies once the user runs `setauket capture-claude --enable --project PATH`; the OMP extension captures visible interactive input and final replies after `setauket capture-omp --enable --project PATH`. Either way the user must do it, never capture reasoning or tool output, and visible text can still contain secrets. Disable with the matching `--disable`. Disabling stops new writes and erases nothing.

Setauket stores memory only. Agent-to-agent messaging lives in the separate Clothesline MCP server.