import { spawn } from "node:child_process";

// OpenCode in-process plugin: forwards the newest visible user and assistant
// text per session to the Setauket CLI. Consent lives in Setauket's per-project
// allowlist; this plugin never writes on its own and never touches tool parts.
function capture(event: Record<string, unknown>): Promise<void> {
  return new Promise((resolve) => {
    const child = spawn("setauket", ["capture-opencode", "--hook"], {
      stdio: ["pipe", "ignore", "ignore"],
      timeout: 5000,
    });
    child.once("error", () => resolve());
    child.once("close", () => resolve());
    child.stdin.on("error", () => resolve());
    child.stdin.end(JSON.stringify(event));
  });
}

function visibleText(message: { parts?: Array<{ type?: string; text?: string }> } | undefined): string {
  return (message?.parts ?? [])
    .filter((part) => part?.type === "text" && typeof part.text === "string")
    .map((part) => part.text)
    .join("\n")
    .trim();
}

export const SetauketCapture = async (ctx: { client?: any; directory?: string; worktree?: string }) => {
  // Message IDs are stable, so re-firing session.idle for an already captured
  // turn records nothing new.
  const seen = new Map<string, { submit?: string; stop?: string }>();
  return {
    event: async ({ event }: { event: any }) => {
      try {
        if (event?.type !== "session.idle") return;
        const session_id: string | undefined = event.properties?.sessionID ?? event.properties?.sessionId;
        if (typeof session_id !== "string" || !session_id) return;
        const response: any = await ctx.client?.session?.messages?.({ path: { id: session_id } });
        const messages: Array<any> = response?.data ?? response;
        if (!Array.isArray(messages)) return;
        const last = (role: string) => [...messages].reverse().find((entry) => entry?.info?.role === role);
        const state = seen.get(session_id) ?? {};
        const user = last("user");
        const userText = visibleText(user);
        if (user?.info?.id && userText && user.info.id !== state.submit) {
          await capture({ hook_event_name: "input", cwd: ctx.directory ?? ctx.worktree, session_id, text: userText });
          state.submit = user.info.id;
        }
        const assistant = last("assistant");
        const assistantText = visibleText(assistant);
        if (assistant?.info?.id && assistantText && assistant.info.id !== state.stop) {
          await capture({ hook_event_name: "agent_end", cwd: ctx.directory ?? ctx.worktree, session_id, text: assistantText });
          state.stop = assistant.info.id;
        }
        if (state.submit || state.stop) seen.set(session_id, state);
      } catch {
        // Capture must never break the session.
      }
    },
  };
};
