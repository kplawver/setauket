import type { AgentPlugin } from "@cline/sdk";
import { spawn } from "node:child_process";

// Cline plugin: forwards the visible run prompt and final reply to the Setauket
// CLI. Consent lives in Setauket's per-project allowlist; hooks are purely
// observational and swallow their own errors.
function capture(event: Record<string, unknown>): Promise<void> {
  return new Promise((resolve) => {
    const child = spawn("setauket", ["capture-cline", "--hook"], {
      stdio: ["pipe", "ignore", "ignore"],
      timeout: 5000,
    });
    child.once("error", () => resolve());
    child.once("close", () => resolve());
    child.stdin.on("error", () => resolve());
    child.stdin.end(JSON.stringify(event));
  });
}

function visibleText(value: any): string | undefined {
  if (typeof value === "string") return value.trim() || undefined;
  if (Array.isArray(value)) {
    const text = value
      .filter((block) => block?.type === "text" && typeof block.text === "string")
      .map((block) => block.text)
      .join("\n")
      .trim();
    return text || undefined;
  }
  if (value && typeof value === "object") return visibleText(value.content ?? value.text);
  return undefined;
}

function createSetauketPlugin(): AgentPlugin {
  let cwd = process.cwd();
  let session = "cline";
  return {
    name: "setauket-capture",
    manifest: { capabilities: ["hooks"] },
    setup(_api, ctx: any) {
      if (typeof ctx?.cwd === "string") cwd = ctx.cwd;
      if (typeof ctx?.sessionId === "string") session = ctx.sessionId;
    },
    hooks: {
      beforeRun(context: any) {
        const prompt = visibleText(context?.prompt ?? context?.input ?? context?.messages?.at(-1));
        if (prompt) return capture({ hook_event_name: "input", cwd, session_id: session, text: prompt });
      },
      afterRun(context: any) {
        const result = context?.result;
        const reply = visibleText(result?.output ?? result?.text ?? result?.message ?? result?.response);
        if (reply) return capture({ hook_event_name: "agent_end", cwd, session_id: session, text: reply });
      },
    },
  };
}

export const setauketCapture = createSetauketPlugin();
export default setauketCapture;
