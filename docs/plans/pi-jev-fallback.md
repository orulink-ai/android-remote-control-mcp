# PI Jev 优先与 DeepSeek 兜底

## 范围

在已合并的 PI phone-use 专用实验模块中新增 `jev-auto`。通过命令行显式选择，继续临时加载 PI extension 与自定义 `phone-benchmark` provider，不修改用户默认 PI 配置，也不改变 Android MCP 的执行职责。保留 `jev`、`deepseek` 独立模式作为对照。

任务仍为小红书固定目标帖的搜索、点赞、收藏和取消闭环。通用自然语言规划、全局 PI 安装和 Android 端模型推理不在本阶段范围内。

## 策略

每个决策步骤先请求 Jev。若连接失败、超过决策时限或选择无效，先记录失败和回退标记；PI 必须显式调用 `phone_observe` 刷新界面，随后 DeepSeek 在新观察和新候选上只决策一次。不能复用失败前的节点引用。DeepSeek 失败、观察失败或无有效动作时停止。成功派发动作后，下步重新优先 Jev。

模型失败发生在派发动作之前，允许切换决策模型。动作失败可能发生在手机已接受请求之后，因此只回读一次并停止，不把相同点击交给另一个模型重放。不确定结果无法通过换模型消除。

## 时限与记录

自动模式整体决策时限默认 Jev 5 秒、DeepSeek 15 秒，分别由 `PHONE_BENCH_JEV_TIMEOUT`、`PHONE_BENCH_DEEPSEEK_TIMEOUT` 配置。必须是有限数字且大于 0、最多 60 秒。启动器在任何手机 reset 前验证；错误只包含变量名，不回显输入值。独立模式保留原来的 30 秒 HTTP client timeout。

模型失败、等待、PI 读屏刷新、兜底请求和验证全部计入本轮 PI 耗时。各尝试记录实际 backend、成功/失败、时长与安全失败分类；不向公共日志写入鉴权或原始异常内容。启动元数据记录 `decisionMode`、`decisionPolicyDetails`，自动模式另记 `decisionTimeoutsSeconds`。Worker 结果中的 `decisionPolicy` 汇总对象包含 mode，供审计区分独立模式与自动策略。

两种模型仍共用 OpenRouter；本策略可绕过模型特定失败，无法保证绕过共同的网关或本地网络故障。离线测试不能替代新的实机成功率或速度数据；2026-10-17 的历史比较不重新标记为自动策略结果。

## TDD 验证

先添加失败测试，再实现策略：配置边界与安全错误、启动前拒绝无效预算、Jev 成功不触发兜底、Jev 失败后 PI 观察先于 DeepSeek、兜底只尝试一次、后续步骤重新优先 Jev、动作异常不重放、deadline 取消、日志不含密钥。离线使用 HTTP/process doubles，不连接真实手机和真实模型。兼容 PI 版本可用时，运行真实 PI 加假模型的工具顺序 smoke test。

实现包含三层验证：独立决策状态机、worker 协议与实际 PI 跨层执行。跨层用实际 PI、实际 worker 和实际策略，仅替换手机与模型边界；另有真实子进程 SIGTERM 测试，覆盖等待 stdin 和模型响应时的取消清理。同步 MCP 调用阻塞期间仍保留父进程最终强制退出的边界。

原研究 JSON 与冻结结果不改写。新的审计关联每次 `attemptId`、实际 `selectedBackend` 和 `fallbackUsed`；失败请求也计入模型阶段与 `decisionPolicy.totalAttemptMs`。固定报告拒绝将 `jev-auto` 或后端不匹配的记录当成独立模型正式轮。

2026-10-18 本阶段验证：`python -m pytest -q experiments/pi-phone-use` **198 passed**，Ruff 格式/静态检查、Python 编译检查及 `git diff --check` 通过。关键路径先写失败测试，再实现；包含真实 PI＋实际 worker/策略的故障注入和真实子进程取消验证，手机与模型边界使用替身。尚未重新进行自动策略的真实手机或真实模型测速，也未修改用户 PI 默认配置。
