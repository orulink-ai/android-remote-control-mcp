/** PI owns the observe -> model -> tool -> observe loop. Python only supplies
 * model decisions and individual MCP operations over a persistent NDJSON pipe.
 * No credential is read or passed through the PI provider's dummy API key.
 */
import { spawn, type ChildProcessWithoutNullStreams } from "node:child_process";
import { randomUUID } from "node:crypto";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { performance } from "node:perf_hooks";
import {
  Type,
  createAssistantMessageEventStream,
  type Api,
  type AssistantMessage,
  type Model,
  type SimpleStreamOptions,
  type TranscriptContext,
  type Usage,
} from "@earendil-works/pi-ai";
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";

const directory = dirname(fileURLToPath(import.meta.url));
const API = "phone-benchmark-stdio";
type Backend = "jev" | "deepseek" | "jev-auto" | "skill";
type Command = "observe" | "act" | "decide" | "finish";
type Observation = { state: Record<string, unknown>; options: Record<string, string>; done: boolean };
type Pending = { resolve(value: any): void; reject(error: Error): void; cleanup(): void };

export class WorkerBridge {
  private child: ChildProcessWithoutNullStreams | undefined;
  private pending = new Map<string, Pending>();
  private buffer = "";
  private failed = false;

  readonly backend: Backend;
  private providerEnv: Record<string, string>;
  constructor(backend: Backend, providerEnv: Record<string, string> = {}) {
    this.backend = backend;
    this.providerEnv = providerEnv;
  }

  private start() {
    if (this.failed) throw new Error("Phone worker unavailable");
    if (this.child) return;
    const trial = process.env.PHONE_BENCH_TRIAL;
    if (!trial) throw new Error("PHONE_BENCH_TRIAL is required");
    const python = process.env.PHONE_BENCH_PYTHON ?? "python3";
    const worker = process.env.PHONE_BENCH_WORKER ?? join(directory, "worker.py");
    this.child = spawn(python, [worker, "--provider", this.backend, "--trial-id", trial], {
      cwd: directory, env: { ...process.env, ...this.providerEnv }, stdio: ["pipe", "pipe", "pipe"],
    });
    this.child.stdout.setEncoding("utf8");
    // Worker diagnostics can contain API errors or request data. Never forward them.
    this.child.stderr.resume();
    this.child.stdout.on("data", (chunk: string) => this.receive(chunk));
    this.child.on("error", () => this.kill());
    this.child.on("exit", () => this.kill());
    this.child.stdin.on("error", () => this.kill());
  }

  private receive(chunk: string) {
    this.buffer += chunk;
    if (this.buffer.length > 16 * 1024 * 1024) return this.kill();
    for (;;) {
      const newline = this.buffer.indexOf("\n");
      if (newline < 0) return;
      const line = this.buffer.slice(0, newline);
      this.buffer = this.buffer.slice(newline + 1);
      if (!line.trim()) continue;
      let reply: any;
      try { reply = JSON.parse(line); } catch { return this.kill(); }
      const entry = this.pending.get(String(reply.id));
      if (!entry) return this.kill();
      this.pending.delete(String(reply.id));
      entry.cleanup();
      if (reply.error !== undefined || !("result" in reply)) entry.reject(new Error("Phone worker rejected command"));
      else entry.resolve(reply.result);
    }
  }

  async request(command: Command, fields: Record<string, unknown> = {}, signal?: AbortSignal): Promise<any> {
    if (signal?.aborted) { this.kill(); throw new Error("Phone worker aborted"); }
    this.start();
    const id = randomUUID();
    return new Promise((resolve, reject) => {
      const abort = () => this.kill();
      const timer = setTimeout(() => this.kill(), 120_000);
      this.pending.set(id, {
        resolve, reject,
        cleanup: () => { clearTimeout(timer); signal?.removeEventListener("abort", abort); },
      });
      signal?.addEventListener("abort", abort, { once: true });
      if (signal?.aborted) return this.kill();
      this.child!.stdin.write(JSON.stringify({ ...fields, id, command }) + "\n", (error) => {
        if (error) this.kill();
      });
    });
  }

  get alive() { return this.child !== undefined && !this.failed; }

  kill() {
    if (this.failed) return;
    this.failed = true;
    for (const entry of this.pending.values()) {
      entry.cleanup();
      entry.reject(new Error("Phone worker stopped; action delivery may be uncertain"));
    }
    this.pending.clear();
    const child = this.child;
    if (child && child.exitCode === null && child.signalCode === null) {
      child.kill("SIGTERM");
      const timer = setTimeout(() => {
        if (child.exitCode === null && child.signalCode === null) child.kill("SIGKILL");
      }, 1500);
      timer.unref();
    }
  }

  async finish(piElapsedMs: number) {
    if (!this.alive) return;
    try { return await this.request("finish", { piElapsedMs }); }
    finally { this.kill(); }
  }
}

export function observation(value: any): Observation {
  if (!value || typeof value !== "object" || typeof value.done !== "boolean" ||
      !value.state || typeof value.state !== "object" || Array.isArray(value.state) ||
      !value.options || typeof value.options !== "object" || Array.isArray(value.options) ||
      Object.values(value.options).some((v) => typeof v !== "string")) {
    throw new Error("Malformed phone observation");
  }
  return value;
}

function number(value: any): number { return typeof value === "number" && Number.isFinite(value) && value >= 0 ? value : 0; }

export function decisionUsage(decision: any): Usage {
  const usage = decision?.usage ?? {};
  const cacheRead = number(usage.cache_read_input_tokens ?? usage.prompt_tokens_details?.cached_tokens);
  const cacheWrite = number(usage.cache_creation_input_tokens);
  const prompt = number(usage.input_tokens ?? usage.prompt_tokens);
  const input = usage.prompt_tokens !== undefined ? Math.max(0, prompt - cacheRead) : prompt;
  const output = number(usage.output_tokens ?? usage.completion_tokens);
  return {
    input, output, cacheRead, cacheWrite,
    totalTokens: number(usage.total_tokens) || input + output + cacheRead + cacheWrite,
    // OpenRouter's reported total is authoritative. Do not invent component costs.
    cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, total: number(decision?.cost ?? usage.cost) },
  };
}

function latestObservation(context: TranscriptContext): Observation | undefined {
  for (let i = context.messages.length - 1; i >= 0; i--) {
    const message: any = context.messages[i];
    if (message.role !== "toolResult" || !["phone_observe", "phone_action"].includes(message.toolName)) continue;
    if (message.isError) throw new Error("Previous phone tool failed; execution stopped");
    const text = message.content.filter((block: any) => block.type === "text").map((block: any) => block.text).join("\n");
    return observation(JSON.parse(text));
  }
}

export function registerPhoneBenchmark(pi: ExtensionAPI, config: { terminalText: string }) {
  let bridge: WorkerBridge | undefined;
  let startedAt: number | undefined;
  let bootstrapSent = false;
  let failed = false;
  const worker = (backend?: Backend, providerEnv?: Record<string, string>) => {
    if (!bridge) {
      if (!backend) throw new Error("Provider must select the phone backend first");
      bridge = new WorkerBridge(backend, providerEnv);
    }
    if (backend && bridge.backend !== backend) throw new Error("Cannot switch model during a phone trial");
    return bridge;
  };

  pi.on("agent_start", async () => {
    bridge?.kill(); bridge = undefined;
    startedAt = performance.now(); bootstrapSent = false; failed = false;
    pi.appendEntry("phone-benchmark-start", { trialId: process.env.PHONE_BENCH_TRIAL, execution: "pi-agent-loop" });
  });
  pi.on("agent_end", async () => {
    if (startedAt !== undefined) {
      const piElapsedMs = performance.now() - startedAt;
      const result = await bridge?.finish(piElapsedMs);
      pi.appendEntry("phone-benchmark-finish", {
        trialId: process.env.PHONE_BENCH_TRIAL, piElapsedMs,
        status: result?.status ?? "worker_unavailable", modelCalls: result?.modelCalls,
        operations: result?.operations, milestones: result?.milestones,
        decisionPolicy: result?.decisionPolicy,
        recipeExecution: result?.recipeExecution,
      });
    }
  });
  pi.on("session_shutdown", async () => { bridge?.kill(); });

  pi.registerTool({
    name: "phone_observe", label: "Observe phone",
    description: "Read current phone state and the controller's exact allowed next actions.",
    parameters: Type.Object({}, { additionalProperties: false }), executionMode: "sequential",
    async execute(_id, _params, signal) {
      const result = observation(await worker().request("observe", {}, signal));
      return { content: [{ type: "text", text: JSON.stringify(result) }], details: result };
    },
  });
  pi.registerTool({
    name: "phone_action", label: "Act on phone",
    description: "Execute one offered action choice, then return the newly observed phone state.",
    parameters: Type.Object({ choice: Type.String({ minLength: 1 }) }, { additionalProperties: false }),
    executionMode: "sequential",
    async execute(_id, params, signal) {
      const result = observation(await worker().request("act", { choice: params.choice }, signal));
      return { content: [{ type: "text", text: JSON.stringify(result) }], details: result };
    },
  });

  pi.registerProvider("phone-benchmark", {
    name: "Phone benchmark (local worker)",
    baseUrl: "stdio://phone-benchmark", apiKey: "local-worker-no-api-key", api: API,
    models: ["jev", "deepseek", "jev-auto", "skill"].map((id) => ({
      id, name: id === "skill" ? "Recorded phone skill with verified steps" : id === "jev-auto" ? "Jev 优先，DeepSeek 兜底" : id === "jev" ? "Jev 1.14 phone decisions" : "DeepSeek V4.1 Flash phone decisions",
      reasoning: false, input: ["text"], contextWindow: 1_000_000, maxTokens: 256,
      cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0 },
    })),
    streamSimple(model: Model<Api>, context: TranscriptContext, options?: SimpleStreamOptions) {
      const stream = createAssistantMessageEventStream();
      const output: AssistantMessage = {
        role: "assistant", content: [], api: model.api, provider: model.provider, model: model.id,
        timestamp: Date.now(), stopReason: "pending", usage: decisionUsage(undefined),
      };
      (async () => {
        try {
          if (failed) throw new Error("Phone trial already failed");
          if (options?.signal?.aborted) throw new Error("Aborted");
          if (model.id !== "jev" && model.id !== "deepseek" && model.id !== "jev-auto" && model.id !== "skill") throw new Error("Unsupported phone model");
          const child = worker(model.id, options?.env);
          const abort = () => child.kill();
          options?.signal?.addEventListener("abort", abort, { once: true });
          try {
            let name = "phone_observe";
            let args: Record<string, string> = {};
            let done = false;
            if (bootstrapSent) {
              const latest = latestObservation(context);
              if (!latest) throw new Error("Phone tool result missing");
              done = latest.done;
              if (!done) {
                // Instrumentation concerns the IPC decision request. HTTP credentials
                // and real provider payloads remain exclusively inside the worker.
                const proposed = { command: "decide" };
                const replacement: any = await options?.onPayload?.(proposed, model);
                const payload = replacement === undefined ? proposed : replacement;
                if (!payload || payload.command !== "decide") throw new Error("Invalid decision request override");
                const decision = await child.request("decide", payload, options?.signal);
                await options?.onResponse?.({ status: 200, headers: { "x-phone-benchmark-transport": "stdio" } }, model);
                await options?.onProviderStreamEvent?.(decision, model);
                if (decision?.kind === "refresh_required") {
                  // A refresh is a real PI tool call. The worker cannot replace the
                  // snapshot silently or dispatch a cached choice during fallback.
                  if (!["jev-auto", "skill"].includes(model.id) || decision.kind !== "refresh_required" ||
                      decision.fallbackPending !== true || Object.hasOwn(decision, "choice")) {
                    throw new Error("Invalid fallback observation request");
                  }
                  pi.appendEntry("phone-benchmark-refresh", { provider: model.id, fallbackPending: true });
                  name = "phone_observe";
                } else {
                  // Isolated providers/replays predate the tagged auto protocol.
                  // A tagged normal decision is not a request to refresh.
                  if (decision && (Object.hasOwn(decision, "fallbackPending") ||
                      (Object.hasOwn(decision, "kind") && decision.kind !== "decision"))) {
                    throw new Error("Invalid phone decision protocol");
                  }
                  if (!decision || typeof decision.choice !== "string" || !Object.hasOwn(latest.options, decision.choice)) {
                    throw new Error("Decision selected an unavailable phone action");
                  }
                  output.usage = decisionUsage(decision);
                  if (typeof decision.servedModel === "string") output.responseModel = decision.servedModel;
                  pi.appendEntry("phone-benchmark-decision", {
                    provider: model.id, choice: decision.choice, latencyMs: decision.latencyMs,
                    servedModel: decision.servedModel, usage: decision.usage, cost: decision.cost,
                    selectedBackend: decision.selectedBackend, fallbackUsed: decision.fallbackUsed,
                    attemptId: decision.attemptId,
                    modelRequest: decision.modelRequest,
                  });
                  name = "phone_action"; args = { choice: decision.choice };
                }
              }
            }
            if (options?.signal?.aborted) throw new Error("Aborted");
            stream.push({ type: "start", partial: output });
            if (done) {
              output.content.push({ type: "text", text: "" });
              stream.push({ type: "text_start", contentIndex: 0, partial: output });
              output.content[0] = { type: "text", text: config.terminalText };
              stream.push({ type: "text_delta", contentIndex: 0, delta: output.content[0].text, partial: output });
              stream.push({ type: "text_end", contentIndex: 0, content: output.content[0].text, partial: output });
              output.stopReason = "stop";
              stream.push({ type: "done", reason: "stop", message: output });
            } else {
              const toolCall = { type: "toolCall" as const, id: randomUUID(), name, arguments: args };
              output.content.push({ ...toolCall, arguments: {} });
              stream.push({ type: "toolcall_start", contentIndex: 0, partial: output });
              output.content[0] = toolCall;
              stream.push({ type: "toolcall_delta", contentIndex: 0, delta: JSON.stringify(args), partial: output });
              stream.push({ type: "toolcall_end", contentIndex: 0, toolCall, partial: output });
              bootstrapSent = true;
              output.stopReason = "toolUse";
              stream.push({ type: "done", reason: "toolUse", message: output });
            }
          } finally { options?.signal?.removeEventListener("abort", abort); }
        } catch {
          failed = true;
          // Never include worker stderr, request contents, API responses, or keys.
          output.stopReason = options?.signal?.aborted ? "aborted" : "error";
          output.errorMessage = "Phone benchmark bridge stopped; inspect the worker trial report before restarting.";
          stream.push({ type: "error", reason: output.stopReason, error: output });
        } finally { stream.end(); }
      })();
      return stream;
    },
  });
}
