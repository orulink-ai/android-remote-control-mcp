/** Read-only replay entry point; never reports phone task completion. */
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import { registerPhoneBenchmark } from "./phone-extension.ts";

export default function phoneReplayBenchmark(pi: ExtensionAPI) {
  registerPhoneBenchmark(pi, { terminalText: "只读手机状态回放已完成；没有派发手机动作。" });
}
