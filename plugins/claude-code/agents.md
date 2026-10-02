# Setauket

Local, cross-harness memory for coding agents. Install the service and run `setauket setup` before relying on semantic search; connecting to MCP captures nothing and registers nothing.

Register a harness with a persistent installation key, then an agent, then a session. Submit each visible turn with `append_turn` and a stable source ID. Search before assuming a previous decision is still current, and verify a hit with `get_session` or `get_memory_history`.

Store a decision or preference with `remember` only when the user explicitly asks. A revision supersedes rather than deletes. Raw turns are summarized and removed after three days of inactivity, and those summaries are lossy: never turn one into a preference without asking.

Capture is opt-in per project and only ever enabled by the user. Do not enable it on their behalf. Visible prompts and replies can still contain secrets.

Agent-to-agent messaging is a different service, Clothesline, on port 19004. Do not look for a message bus here.