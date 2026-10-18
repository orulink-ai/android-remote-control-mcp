/** Local follow-ups use recorded evidence; they never observe or act on a phone. */
export type PreviousTask = { goal: string; result: any };
type ConversationRoute = { kind: "reply"; text: string } | { kind: "repeat"; goal: string };

function seconds(value: unknown): string | undefined {
  return typeof value === "number" && Number.isFinite(value) && value >= 0
    ? `${(value / 1000).toFixed(1)} 秒` : undefined;
}

export function timingSummary(result: any): string {
  const total = seconds(result?.piElapsedMs);
  const controller = seconds(result?.elapsedMs);
  if (total) return `耗时：约 ${total}（PI 任务全程）${controller ? `；手机控制器 ${controller}` : ""}。`;
  if (controller) return `耗时：手机控制器约 ${controller}；没有 PI 全程计时。`;
  return "没有可用的任务耗时记录。";
}

export function conversationRoute(goal: string, previous?: PreviousTask | null): ConversationRoute | undefined {
  const text = goal.trim().replace(/[\s。！!？?]/g, "");
  if (/多久|耗时|用时|花了多少时间|花费了多少时间/.test(text) &&
      !/打开|启动|搜索|点赞|收藏|取消|点击|输入|滑动|执行|运行|重放|复用/.test(text)) {
    return { kind: "reply", text: previous ? `上一次手机任务${previous.result?.status === "success" ? "已完成" : "未完成"}。${timingSummary(previous.result)}` : "当前会话还没有可用的手机任务耗时记录。" };
  }
  if (/^(?:请|帮我|请帮我)?(?:再来一次|再来一遍|再试一次|重试|再执行一次|再运行一次)(?:吧)?$/.test(text)) {
    if (!previous) return { kind: "reply", text: "当前会话还没有可重复的手机任务，请提供完整任务。" };
    if (previous.result?.status !== "success") return { kind: "reply", text: "上一次手机任务没有确认成功，不能直接重做可能已执行的动作。请先读取当前手机屏幕，再指定完整任务。" };
    return { kind: "repeat", goal: previous.goal };
  }
}
