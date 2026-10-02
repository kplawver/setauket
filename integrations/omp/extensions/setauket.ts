import { spawn } from "node:child_process";

// OMP exposes the same extension event interface as Pi; no MCP client is needed here.
function capture(event: Record<string, unknown>): Promise<void> {
  return new Promise((resolve) => {
    const child = spawn("setauket", ["capture-omp", "--hook"], {
      stdio: ["pipe", "ignore", "ignore"],
      timeout: 5000,
    });
    child.once("error", () => resolve());
    child.once("close", () => resolve());
    child.stdin.on("error", () => resolve());
    child.stdin.end(JSON.stringify(event));
  });
}

function session(ctx: any) {
  const manager = ctx?.sessionManager;
  const transcript_path = manager?.getSessionFile?.();
  const session_id = manager?.getSessionId?.();
  if (typeof transcript_path !== "string" || typeof session_id !== "string" || !session_id) return null;
  return { session_id, transcript_path, cwd: ctx.cwd };
}

export default function (pi: any) {
  let sawInput = false;
  pi.on("input", async (event: any, ctx: any) => {
    if (event.source !== "interactive" && event.source !== "rpc") return;
    const current = session(ctx);
    if (current && typeof event.text === "string" && event.text.trim()) {
      sawInput = true;
      await capture({ ...current, hook_event_name: "input", source: event.source, text: event.text });
    }
  });

  pi.on("agent_end", async (event: any, ctx: any) => {
    const current = session(ctx);
    if (!current || !Array.isArray(event.messages)) return;
    // OMP print mode does not emit input; its finalized user message is the fallback.
    if (ctx.mode === "print" && !sawInput) {
      const user = [...event.messages].reverse().find((message) => message?.role === "user");
      if (user) {
        const prompt = typeof user.content === "string" ? user.content :
          Array.isArray(user.content) ? user.content
            .filter((block: any) => block?.type === "text" && typeof block.text === "string")
            .map((block: any) => block.text).join("\n") : "";
        if (prompt.trim()) await capture({ ...current, hook_event_name: "input", source: "rpc", text: prompt });
      }
    }
    sawInput = false;
    const final = [...event.messages].reverse().find((message) =>
      message?.role === "assistant" && (message.stopReason === "stop" || message.endTurn === true));
    if (!final || !Array.isArray(final.content)) return;
    const text = final.content
      .filter((block: any) => block?.type === "text" && typeof block.text === "string")
      .map((block: any) => block.text).join("\n").trim();
    if (text) await capture({ ...current, hook_event_name: "agent_end", text });
  });
}
