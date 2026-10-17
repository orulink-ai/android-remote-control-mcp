/** Live phone workflow entry point; worker paths resolve beside the shared factory. */
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import { registerPhoneBenchmark } from "./phone-extension.ts";

export { WorkerBridge, observation, decisionUsage } from "./phone-extension.ts";

export default function phoneBenchmark(pi: ExtensionAPI) {
  registerPhoneBenchmark(pi, { terminalText: "手机控制任务已完成，最终状态已由控制器确认。" });
}
