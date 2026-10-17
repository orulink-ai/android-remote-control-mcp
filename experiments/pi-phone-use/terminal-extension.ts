/** Interactive entry: the PI model is explicitly phone-control/auto, not a default chat LLM. */
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import { registerPhoneBenchmark } from "./phone-extension.ts";

export default function phoneTerminal(pi: ExtensionAPI) {
  registerPhoneBenchmark(pi, { terminal: true, terminalText: "手机任务已完成并验证。" });
}
