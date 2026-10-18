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
import { conversationRoute, timingSummary, type PreviousTask } from "./terminal-conversation.ts";

const directory = dirname(fileURLToPath(import.meta.url));
const API = "phone-benchmark-stdio";
type Backend = "jev" | "deepseek" | "jev-auto" | "skill" | "skill-auto" | "terminal-auto";
type Command = "observe" | "act" | "decide" | "finish";
type Observation = { state: Record<string, unknown>; options: Record<string, string>; done: boolean };
type Pending = { resolve(value: any): void; reject(error: Error): void; cleanup(): void };

export class WorkerBridge {
  private child: ChildProcessWithoutNullStreams | undefined;
  private pending = new Map<string, Pending>();
  private buffer = "";
  private failed = false;
  terminalDiagnostic: string | undefined;

  readonly backend: Backend;
  private providerEnv: Record<string, string>;
  constructor(backend: Backend, providerEnv: Record<string, string> = {}) {
    this.backend = backend;
    this.providerEnv = providerEnv;
  }

  private start() {
    if (this.failed) throw new Error("Phone worker unavailable");
    if (this.child) return;
    const trial = this.providerEnv.PHONE_BENCH_TRIAL ?? process.env.PHONE_BENCH_TRIAL;
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
      if (reply.error !== undefined || !("result" in reply)) {
        const messages: Record<string, string> = {
          LOCKED: "手机当前处于锁屏状态，请手动解锁后重试；未尝试解锁。",
          UNKNOWN_FOCUS: "无法确认手机当前前台窗口，已停止。",
          HOME_TIMEOUT: "返回桌面后界面未稳定，已停止。",
          OPEN_TIMEOUT: "应用启动后仍未确认目标前台，已停止；请检查手机界面。",
          BLOCKED: "当前界面没有可确认的下一步动作，已停止；请检查手机界面。",
          NO_PROGRESS: "相同操作多次没有进展，已停止；请先检查当前手机界面。",
          BUDGET: "手机任务预算已用完，已停止。",
          DELIVERY_UNCERTAIN: "动作投递结果不确定，已回读并停止；请检查手机状态。",
          TOGGLE_UNVERIFIED: "动作结果未取得配对验证，已停止；未重复点击。",
          TARGET_CHANGED: "目标帖子或未授权改变的状态发生变化，已停止。",
          STOPPED: "手机任务已经停止。",
          UNSUPPORTED_TASK: "这条请求不在当前手机控制器支持范围内，未执行手机操作。pi-phone 当前使用专用控制器；普通聊天请使用 pi。手机任务支持读屏、打开应用及小红书搜索/点赞/收藏。",
        };
        const code = reply.errorDetails?.code;
        if (["TerminalRuntimeError", "ExperienceRuntimeError", "TerminalTaskError"].includes(reply.errorDetails?.exceptionType) && typeof code === "string" && Object.hasOwn(messages, code)) {
          this.terminalDiagnostic = messages[code];
        }
        entry.reject(new Error(this.terminalDiagnostic ?? "Phone worker rejected command"));
      }
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
    // A new user turn must obtain its own observation before making decisions.
    if (message.role === "user") return;
    if (message.role !== "toolResult" || !["phone_observe", "phone_action"].includes(message.toolName)) continue;
    if (message.isError) throw new Error("Previous phone tool failed; execution stopped");
    const text = message.content.filter((block: any) => block.type === "text").map((block: any) => block.text).join("\n");
    return observation(JSON.parse(text));
  }
}

export function terminalGoal(context: TranscriptContext): string {
  for (let i = context.messages.length - 1; i >= 0; i--) {
    const message: any = context.messages[i];
    if (message.role !== "user") continue;
    const text = typeof message.content === "string" ? message.content : message.content
      .filter((block: any) => block.type === "text").map((block: any) => block.text).join("\n");
    if (text.trim() && text.length <= 4000) return text.trim();
    throw new Error("A bounded user phone request is required");
  }
  throw new Error("A user phone request is required");
}

function terminalFailure(result: any): string | undefined {
  const attempts = result?.decisionPolicy?.attempts;
  const last = Array.isArray(attempts) ? attempts.at(-1) : undefined;
  if (last?.backend === "deepseek" && last.status !== "success" && last.httpStatus === 429) {
    return "DeepSeek 兜底请求受到限流（HTTP 429），手机任务已停止；请稍后重试。";
  }
  return undefined;
}

function terminalSummary(result: any): string {
  if (result?.status !== "success") return terminalFailure(result) ?? "手机任务未完成，已停止；请查看本次运行记录。";
  const policy = result.decisionPolicy ?? {};
  const attempts = Array.isArray(policy.attempts) ? policy.attempts : [];
  const jev = attempts.filter((a: any) => a.backend === "jev").length;
  const deepseek = attempts.filter((a: any) => a.backend === "deepseek").length;
  const steps = result.recipeExecution?.verifiedSteps;
  const experience = result.terminalTask?.kind === "experience";
  const lines = ["手机任务已完成并验证。", timingSummary(result), "控制通道：本项目 Android MCP；未使用 ADB。",
    experience
      ? `录制经验辅助：${policy.experienceLoaded === true ? "已加载" : "未加载"}；每步实时判断；自动配方回放 0 步；Jev 请求 ${jev} 次；DeepSeek 兜底 ${deepseek} 次。`
      : `录制 Skill 验证：${typeof steps === "number" ? `${steps} 步` : "本次未使用"}；Jev 请求 ${jev} 次；DeepSeek 兜底 ${deepseek} 次。`];
  const observation = result.finalState ?? {};
  if (observation.locked === true) lines.push("手机当前处于锁屏状态；本次仅读取锁屏，请解锁后再操作应用。");
  if (observation.focusedPackage) lines.push(`当前应用：${observation.focusedPackage}`);
  if (result.terminalTask?.kind === "read" && Array.isArray(observation.visibleTexts)) {
    lines.push(`屏幕文字：${observation.visibleTexts.slice(0, 12).join("；").slice(0, 400)}`);
  }
  return lines.join("\n");
}

export function registerPhoneBenchmark(pi: ExtensionAPI, config: { terminalText: string; terminal?: boolean }) {
  let bridge: WorkerBridge | undefined;
  let startedAt: number | undefined;
  let bootstrapSent = false;
  let failed = false;
  let finalized: any;
  let previousTerminal: PreviousTask | undefined;
  let currentTerminalGoal: string | undefined;
  const providerId = config.terminal ? "phone-control" : "phone-benchmark";
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
    startedAt = performance.now(); bootstrapSent = false; failed = false; finalized = undefined;
    currentTerminalGoal = undefined;
    pi.appendEntry("phone-benchmark-start", { trialId: process.env.PHONE_BENCH_TRIAL, execution: "pi-agent-loop" });
  });
  pi.on("agent_end", async () => {
    if (startedAt !== undefined) {
      const piElapsedMs = performance.now() - startedAt;
      const result = finalized ?? await bridge?.finish(piElapsedMs);
      if (config.terminal && currentTerminalGoal) {
        previousTerminal = { goal: currentTerminalGoal, result: {
          status: result?.status ?? "failed", elapsedMs: result?.elapsedMs,
          piElapsedMs: result?.piElapsedMs ?? piElapsedMs,
        } };
        pi.appendEntry("phone-terminal-result", previousTerminal);
      }
      pi.appendEntry("phone-benchmark-finish", {
        trialId: result?.trialId ?? process.env.PHONE_BENCH_TRIAL, piElapsedMs,
        status: result?.status ?? "worker_unavailable", modelCalls: result?.modelCalls,
        operations: result?.operations, milestones: result?.milestones,
        decisionPolicy: result?.decisionPolicy,
        recipeExecution: result?.recipeExecution,
      });
    }
  });
  pi.on("session_shutdown", async () => { bridge?.kill(); });
  if (config.terminal) {
    pi.on("input", async (event) => {
      // This exact command names the fixed verified recipe. Normalize before
      // PI expands the explanatory SKILL.md; never infer authorization from
      // arbitrary skill prose or silently ignore user-supplied arguments.
      if (event.text.trim() === "/skill:xhs-recorded-roundtrip") {
        return { action: "transform", text: "执行录制的小红书完整闭环", images: event.images };
      }
      return { action: "continue" };
    });
    pi.on("session_start", async (_event, ctx) => {
      const saved = ctx.sessionManager.getBranch().filter((entry: any) => entry.type === "custom" && entry.customType === "phone-terminal-result").at(-1) as any;
      if (typeof saved?.data?.goal === "string" && saved.data.result) previousTerminal = saved.data;
      if (ctx.hasUI) ctx.ui.setStatus("phone-control", "录制经验辅助 Jev → DeepSeek · 本项目 Android MCP · 无 ADB");
    });
    pi.registerCommand("phone-replay", {
      description: "固定回放模式：小狮日记 → 点赞收藏 → 取消两项（非默认经验辅助）",
      handler: async (args, ctx) => {
        if (args.trim()) {
          ctx.ui.notify("/phone-replay 当前不接受参数；没有执行。请核对固定搜索词和目标帖子后使用无参数命令。", "warning");
          return;
        }
        pi.sendUserMessage("执行录制的小红书完整闭环");
      },
    });
    pi.registerCommand("phone-status", {
      description: "显示手机决策路径与已配置的录制 Skill",
      handler: async (_args, ctx) => {
        ctx.ui.notify(`手机决策：Jev 每步实时判断，录制经验仅提供建议；请求失败重读后 DeepSeek。\n执行通道：本项目 Android MCP；无 ADB。\n经验来源：${process.env.PHONE_BENCH_RECIPE || "未配置，使用普通 Jev"}\n支持：小红书换搜索词、首篇或指定标题、按要求点赞/收藏/取消；/phone-replay 单独保留固定回放。`, "info");
      },
    });
  }

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

  pi.registerProvider(providerId, {
    name: config.terminal ? "Phone control · Skill / Jev / DeepSeek" : "Phone benchmark (local worker)",
    baseUrl: "stdio://phone-benchmark", apiKey: "local-worker-no-api-key", api: API,
    models: (config.terminal ? ["auto"] : ["jev", "deepseek", "jev-auto", "skill", "skill-auto"]).map((id) => ({
      id, name: id === "auto" ? "Jev + 录制 Skill → DeepSeek" : id === "skill-auto" ? "Recorded skill → Jev → DeepSeek" : id === "skill" ? "Recorded phone skill with verified steps" : id === "jev-auto" ? "Jev 优先，DeepSeek 兜底" : id === "jev" ? "Jev 1.14 phone decisions" : "DeepSeek V4.1 Flash phone decisions",
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
        const completeText = (text: string) => {
          output.content = [{ type: "text", text: "" }];
          stream.push({ type: "text_start", contentIndex: 0, partial: output });
          output.content[0] = { type: "text", text };
          stream.push({ type: "text_delta", contentIndex: 0, delta: text, partial: output });
          stream.push({ type: "text_end", contentIndex: 0, content: text, partial: output });
          output.stopReason = "stop";
          stream.push({ type: "done", reason: "stop", message: output });
        };
        try {
          if (failed) throw new Error("Phone trial already failed");
          if (options?.signal?.aborted) throw new Error("Aborted");
          if (config.terminal ? model.id !== "auto" : !["jev", "deepseek", "jev-auto", "skill", "skill-auto"].includes(model.id)) throw new Error("Unsupported phone model");
          const backend = config.terminal ? "terminal-auto" : model.id as Backend;
          if (config.terminal && !bootstrapSent) {
            const goal = terminalGoal(context);
            const route = conversationRoute(goal, previousTerminal);
            if (route?.kind === "reply") {
              stream.push({ type: "start", partial: output });
              completeText(route.text);
              return;
            }
            currentTerminalGoal = route?.kind === "repeat" ? route.goal : goal;
          }
          const environment = config.terminal ? {
            ...options?.env,
            PHONE_TERMINAL_GOAL: currentTerminalGoal!,
            PHONE_BENCH_TRIAL: `terminal-${randomUUID()}`,
          } : options?.env;
          const child = worker(backend, environment);
          if (config.terminal && !bootstrapSent) pi.appendEntry("phone-terminal-route", { goal: currentTerminalGoal, provider: providerId, transport: "own-android-mcp", adb: false });
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
                  if (!["jev-auto", "skill-auto", "terminal-auto"].includes(backend) || decision.kind !== "refresh_required" ||
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
                    modelRequest: decision.modelRequest, recipeRecovery: decision.recipeRecovery,
                  });
                  name = "phone_action"; args = { choice: decision.choice };
                }
              }
            }
            if (options?.signal?.aborted) throw new Error("Aborted");
            stream.push({ type: "start", partial: output });
            if (done) {
              if (config.terminal) finalized = await child.finish(performance.now() - (startedAt ?? performance.now()));
              const completedText = config.terminal ? terminalSummary(finalized) : config.terminalText;
              completeText(completedText);
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
          if (config.terminal && !options?.signal?.aborted && bridge?.alive) {
            try { finalized = await bridge.finish(performance.now() - (startedAt ?? performance.now())); }
            catch { /* Keep only static diagnostics if finalization is unavailable. */ }
          }
          // Never include worker stderr, request contents, API responses, or keys.
          const diagnostic = config.terminal
            ? bridge?.terminalDiagnostic ?? terminalFailure(finalized) ?? "手机任务已停止。可指定小红书搜索词、首篇或标题及点赞/收藏目标；请查看本次记录，勿盲目重复。"
            : "Phone benchmark bridge stopped; inspect the worker trial report before restarting.";
          if (config.terminal && !options?.signal?.aborted) {
            // A bounded phone task failure is a final task result. Reporting an
            // LLM rate-limit error here would let PI auto-retry the entire task,
            // including actions that may already have changed the phone.
            output.content = [{ type: "text", text: diagnostic }];
            output.stopReason = "stop";
            stream.push({ type: "done", reason: "stop", message: output });
          } else {
            output.stopReason = options?.signal?.aborted ? "aborted" : "error";
            output.errorMessage = diagnostic;
            stream.push({ type: "error", reason: output.stopReason, error: output });
          }
        } finally { stream.end(); }
      })();
      return stream;
    },
  });
}
