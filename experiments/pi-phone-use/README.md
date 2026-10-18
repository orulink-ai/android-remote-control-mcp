# PI 手机控制：日常终端入口与实验模块

## 日常使用先看这里：pi-phone

本机已安装的新 `pi-phone` 使用 **`phone-control / auto`**：普通小红书搜索＋互动请求优先进入经验辅助，每一步由 Jev 根据当前界面判断，已验证示教只提供参考；需要时重新观察后由 DeepSeek 兜底。显式 `/phone-replay` 保留固定录制流程。动作通过本项目 OwnMcp 连接 `watcher_android` 的 **LAN HTTP MCP**，终端入口不调用 ADB。

这个入口用本地手机控制 provider 替代普通聊天模型，仍不是通用聊天入口。任务成功后会直接显示 PI 全程和控制器耗时；“花了多久”读取上次记录，不调用手机。“再来一次吧”仅复用当前会话上次已验证成功的完整任务，重新读屏执行；上次失败或结果不确定时不直接重试。记录随 PI 会话保存，恢复同一会话可继续追问。其他不支持的请求明确提示范围，不能把任务解析失败说成动作投递不确定。普通聊天继续使用 `pi`。

App 录制后一键提交、PI 验证、生成、复用和安装新 Skill 的链路见 [App 闭环安装与验收](../../docs/research/app-workflow-loop.md)。它与下文保留的专用 benchmark 入口分开；新链路全部通过 LAN MCP。

```text
pi-phone
  → 确认当前 provider / model 为 phone-control / auto
  → /phone-status
  → 读取当前手机屏幕
  → 打开小红书
  → 小红书搜索“小狮日记”，停在搜索结果页
  → /phone-replay
```

各行在 PI 中逐条输入；最后一项会真实执行固定帖子的点赞、收藏及取消两项，属于完整录制流程，不是读屏命令。也可单独输入精确别名 `/skill:xhs-recorded-roundtrip`：扩展将其转换为同一固定任务授权；**不支持参数，也不执行任意 SKILL 文本**。不要把它与最后一项连续执行两次。

开始前在手机启动本项目服务，解锁并保持亮屏。运行前查看 [终端操作步骤、配置引用与排查表](../../docs/research/pi-phone-terminal.md)，其中区分已验证结果、失败记录和未完成验收。普通任务还可输入 `在小红书搜索“露营装备”，打开第一篇，只收藏`，或指定精确标题及可选作者；只收藏不会授权点赞，已满足的状态不重复点击。能力限于同 App 的搜索与点赞/收藏任务族，语法、经验开关和实机待验收矩阵见 [经验辅助说明](../../docs/research/phone-experience.md)。App 录制生成入口及 `/phone-replay` 仍是固定示例；当前是把其 verified source 供新终端引擎参考，并未扩大为任意录制生成。

**下文是专用 benchmark 的安装、测速与录像说明。** `phone-benchmark`、`run_pi_trial.py` 和 `record_pi_trial.py` 属于旧实验入口，其中准备或录像使用 ADB；它们不是日常 `pi-phone` 的必要启动步骤。不要为使用新终端入口照抄下文 USB 转发或 ADB 重置命令，也不要用旧实验成功记录替代当前入口验收。

## 专用实验接入

这是专用实验模块，通过 PI extension 和自定义 `phone-benchmark` provider，让真实模型逐步选择手机动作，或复用配套的已验证步骤，再由本项目 Android MCP 执行。扩展本身不是额外的 MCP server；`skill` 模式加载由示教生成的 SKILL.md 配套 `recipe.json`。未注册到用户 PI 默认配置。

支持 `jev`、`deepseek` 两种独立测试模式，以及显式选择的 `jev-auto` 模式：每个决策步骤优先 Jev，模型失败后经 PI 刷新观察，再由 DeepSeek 兜底一次。任务固定为小红书搜索“小狮日记”、打开指定首帖、点赞、收藏、取消点赞、取消收藏。阶段目标、候选过滤和截图颜色验证器对两种模型共用，不是任意自然语言任务的通用 phone agent。

`skill` 是严格复用模式：从一次成功 PI 示教中提炼步骤，在每次新观察中重新定位并校验前后条件；正常路径无需模型密钥或 HTTP client，界面漂移时停止。新增 `skill-auto` 保留这个本地路径，仅在可恢复的界面漂移时把本轮剩余步骤交给 Jev；Jev 请求失败后由 PI 重新观察，再请求 DeepSeek 兜底一次。它不会把模型动作强行接回原配方。录制 UI 的完整产品方向见 [产品方案](../../docs/plans/phone-record-replay.md)。

研究结果和计时边界见 [阶段记录](../../docs/research/pi-phone-use-20261017.md)。2026-10-17 的真实测试基于归档前的冻结代码；本目录另做了路径配置、会话释放与错误诊断修复，**没有把修复后的代码冒充成原测速代码重新计时**。

## 结构

```text
PI AgentLoop（临时 -e 加载 pi-extension.ts）
  → phone_observe / phone_action
  → worker.py：持久 NDJSON 通道
  → controller.py：共同任务状态、动作候选、结果验证
  → providers.py：OpenRouter Jev / DeepSeek 结构化决策
  → own_mcp.py：本项目 Android MCP 执行与读屏
```

Jev 的 operation / target heads 在一次 Decisions API 请求中返回；DeepSeek Flash 关闭 reasoning，返回同一动作空间中的 JSON 选择。模型 HTTP client 在一轮内持久复用，支持 HTTP/2 协商，日志以实际响应协议为准。MCP 生命周期独立：初始化 → 工具调用 → DELETE session → 关闭连接。

## 实验依赖安装与离线验证

需要 Python 3.11+，以及兼容的 PI CLI。已验证版本为 npm 包 `@earendil-works/pi-coding-agent` **1.3.0**，对应 `@earendil-works/pi-ai` API；其它发行版的扩展 API 可能不同。扩展由 PI 自身加载和解析依赖，不需要把全局 Node 模块复制进仓库。

从仓库根目录执行：

```bash
python3 -m venv experiments/pi-phone-use/.venv
experiments/pi-phone-use/.venv/bin/python -m pip install -r experiments/pi-phone-use/requirements.txt
experiments/pi-phone-use/.venv/bin/python -m pytest -q experiments/pi-phone-use
```

单元测试使用 HTTP/process doubles，不连接真实手机或真实模型。安装 PI 后还会运行真实 PI 的回放、回退协议及“实际 worker＋实际策略＋手机/模型替身”跨层测试；未安装 PI 时相关测试跳过。测试无需模型密钥，也不会改变用户 PI 默认配置。

## 专用实验配置

运行数据默认保存到仓库 `artifacts/pi-phone-use/`，已被 Git 忽略。手机截图、界面文本、PI 日志、模型响应和录像只保存在本地运行目录。模型密钥及 MCP 配置通过文件或环境读取，不放进 Git，也不出现在启动命令参数里。

| 环境变量 | 用途 / 默认值 |
| --- | --- |
| `PHONE_BENCH_PYTHON` | Worker 使用的 Python；默认 `python3`，应指向安装依赖的 venv |
| `PHONE_BENCH_OUTPUT_DIR` | 运行数据目录；默认 `artifacts/pi-phone-use/` |
| `PHONE_BENCH_DEVICE` | 实机 ADB 序列号；实机启动和录像必须显式配置 |
| `PI_BIN` / `ADB` | CLI 路径；默认 `pi` / `adb` |
| `OPENROUTER_API_KEY` | 模型密钥，优先于密钥文件 |
| `OPENROUTER_API_KEY_FILE` | 密钥文件；默认 `~/.config/android-jev/openrouter.key` |
| `ANDROID_MCP_CONFIG` | PI 格式 MCP 配置；默认 `~/.pi/agent/mcp.json` |
| `ANDROID_MCP_SERVER` | 配置中的 server 名；默认 `watcher_android` |
| `ANDROID_MCP_URL` | MCP URL；默认 `http://127.0.0.1:18080/mcp` |
| `PHONE_BENCH_MODE` | `warm` 持久 client 或 `cold` 每请求新建 client；正式比较使用 `warm` |
| `PHONE_BENCH_JEV_TIMEOUT` | `jev-auto` / `skill-auto` 中 Jev 单次决策的整体时限，秒；默认 `5.0` |
| `PHONE_BENCH_DEEPSEEK_TIMEOUT` | 自动模式中 DeepSeek 单次兜底决策的整体时限，秒；默认 `15.0` |
| `PHONE_BENCH_RECORDING` | 设为 `1` 时从真实 PI 动作保存配对示教 `TRIAL/recording.json`；默认不记录 |
| `PHONE_BENCH_RECIPE` | `skill` / `skill-auto` 读取的 `recipe.json`；在手机重置或录像开始之前校验内容和适配器任务身份 |

专用 benchmark 的配置层只读取指定 server 的 `headers`，URL 单独显式配置，避免沿用失效的手机 LAN 地址。新终端启动器则读取同一 server 的 URL 并绑定到运行环境；两者不要混用。也可连接无需鉴权的本机测试服务；需要鉴权的真实服务必须在私有配置中提供正确 headers。

密钥文件建议权限 `0600`。不要将带 token 的完整 MCP 配置放进示例、报告或提交。

两个自动策略时限都必须为有限数字，且 `0 < 秒数 <= 60`；空值、非数字、NaN 和无穷值均拒绝。`jev-auto` / `skill-auto` 启动器在操作手机前验证配置，错误只报环境变量名。独立 `jev` / `deepseek` 基线保留原来的 30 秒 HTTP client timeout，不应用自动策略的额外整体时限。严格 `skill` 不读取模型预算或密钥。

## 专用 benchmark 实机运行（包含 ADB 准备）

先启动手机 Android MCP，解锁并保持亮屏。USB 转发只是传输方式；实际点击和读屏由 MCP 完成。以下命令会真实操作当前账号，对同一帖子切换点赞和收藏；每轮从桌面开始，轮前会冷停止小红书。

```bash
export PHONE_BENCH_PYTHON="$PWD/experiments/pi-phone-use/.venv/bin/python"
export PHONE_BENCH_DEVICE="你的设备序列号"
export OPENROUTER_API_KEY_FILE="$HOME/.config/android-jev/openrouter.key"
adb -s "$PHONE_BENCH_DEVICE" forward tcp:18080 tcp:8080

"$PHONE_BENCH_PYTHON" experiments/pi-phone-use/run_pi_trial.py jev final-r1-jev
"$PHONE_BENCH_PYTHON" experiments/pi-phone-use/run_pi_trial.py deepseek final-r1-deepseek

# 显式启用 Jev 优先、DeepSeek 仅一次兜底；不写入默认 PI 配置。
"$PHONE_BENCH_PYTHON" experiments/pi-phone-use/run_pi_trial.py jev-auto auto-r1
```

每轮 trial ID 必须唯一，已有产物不覆盖。同一时刻只运行一个实机流程。首帖必须是代码固定的目标标题，否则不应将停止解释成模型慢；搜索排序、APP 布局或分辨率改变后，应先调整与验证专用任务适配。

正式比较前需确认目标帖为未点赞、未收藏，记录原状态；每轮结束检查最终两项未激活，失败轮先检查已完成阶段再恢复。`reset_baseline.py` 仅适用于当前已打开且已验证的目标详情页，属于计时外清理，不能替代完整任务。原状态恢复由实验操作者在结束后验证，不由下一轮盲目重放完成。

## 示教生成与 PI 复用

参考 [官方 Record & Replay](https://learn.chatgpt.com/docs/extend/record-and-replay) 的演示、整理、检查和复用方式，本阶段提供以下独立实验路径。所有手机动作仍由真实 PI 的 `phone_observe` / `phone_action` 工具驱动，生成器不控制手机：

```bash
export PHONE_BENCH_OUTPUT_DIR="$PWD/artifacts/pi-phone-use/record-replay-20261018"

# 完整成功的 PI 示教，记录每步之前/之后的共同观察。
PHONE_BENCH_RECORDING=1 "$PHONE_BENCH_PYTHON" experiments/pi-phone-use/run_pi_trial.py jev demo

# 从实际记录生成 SKILL.md、recipe.json 和 generation.json；不调用模型。
"$PHONE_BENCH_PYTHON" experiments/pi-phone-use/workflow.py compile \
  "$PHONE_BENCH_OUTPUT_DIR/demo/recording.json" \
  "$PHONE_BENCH_OUTPUT_DIR/skill-bundle"

export PHONE_BENCH_RECIPE="$PHONE_BENCH_OUTPUT_DIR/skill-bundle/recipe.json"
"$PHONE_BENCH_PYTHON" experiments/pi-phone-use/run_pi_trial.py skill rr-r1-skill

# 显式允许经核实的界面漂移由 Jev 接管，仍由 PI 逐步派发手机动作。
"$PHONE_BENCH_PYTHON" experiments/pi-phone-use/run_pi_trial.py skill-auto replay-auto-r1

# 额外录像轮，保留完整原速流程，不并入正式三轮。
"$PHONE_BENCH_PYTHON" experiments/pi-phone-use/record_pi_trial.py skill video-skill \
  --report-dir "$PHONE_BENCH_OUTPUT_DIR/report"
```

编译器保留来源摘要，要求动作与当时可用控件匹配、前后观察连续且任务身份一致，完成状态有证据。执行时仅使用当前观察中的控件引用；每步校验后置条件，最终还需检查所有步骤和最终状态。配方内容修改后旧验证失效。当前适配器固定已示教的搜索词与帖子，更换输入需要新示教；不支持任意参数直接替换。

示教中的导航或搜索输入之后，若先出现过渡界面再通过 `WAIT` 到达稳定结果，编译器会保留这些已观察的过渡状态（`settleStates`）和最终后置条件。复用时页面直接就绪即可完成该步；只在已示教过的过渡状态继续等待，单步最多 8 次，不机械复制示教的 WAIT 次数，也不重发导航、点赞或收藏动作。共同控制器的短等待、重新读屏和结果验证仍计入耗时；这不是取消验证或固定坐标快放。未知界面、预算耗尽或动作结果不确定时停止。

控制器能够证明尚未派发任何变更工具时，可以返回 `execution.status=not_dispatched`：同时绑定旧选择、语义意图和 `stale_reference` 原因。Skill 策略核对任务与当前步骤前置条件后，仅释放待执行引用、不前进步骤；下一次由 PI 重新选择并通过新的 `phone_action` 派发，单步最多 3 次。共同控制器原有的严格 click 失效处理另可在同一次 `phone_action` 内重绑定一次，前提是角色、几何、标签、目标及状态完全相同；此路径由两组共用。两类恢复耗时均计入全程，不算模型 API 调用。部分执行、投递不确定或未知状态不能进入这些路径；即使页面碰巧满足后置条件，也必须停止，不能靠结果相似掩盖投递异常。未携带投递标记或显式 `applied` 时仍走正常结果验证。

### `skill-auto` 恢复边界

`SkillAutoPolicy` 组合严格 `SkillPolicy` 和既有 `DecisionPolicy(jev-auto)`。仅有带类型的 `RecipeDrift` 可启动恢复：已验证的前置界面变化、语义选择器找不到控件、已执行动作后的可恢复后置界面变化，以及离开已示教过渡界面。它不笼统捕获 `DecisionPolicyError` 来切换模型。语义目标存在多个匹配、任务搜索词或目标帖子变化、未知/部分投递、取消、步骤或等待预算耗尽，均停止。

正常命中时 `LazyDecisionClient` 不读取 OpenRouter 密钥，不创建模型 HTTP client。首次可恢复漂移才加载凭据并请求 Jev；之后本轮余下步骤全部走共同任务状态机的模型决策，不重新尝试原配方。Jev 超时、提供方失败或非法选择只返回刷新标记；PI 必须实际执行 `phone_observe`，成功取得更新的 observation ID，才允许 DeepSeek 请求一次。DeepSeek 再失败则停止，成功后下一步继续使用 Jev。任何模型都不能从策略内部派发手机动作。

恢复后的成功必须同时满足共同状态机 `done`、搜索到取消收藏的完整六阶段证据、原始任务身份、目标详情页可见，以及点赞/收藏均为 `false`。`verifiedSteps` 保留本轮实际验证的配方游标，不伪造为全部步骤已通过。`recipeExecution` / `decisionPolicy` 中的 `recoveryActive`、`drifts`、`recipeToModelRecoveries` 和 `modelFallbacks` 分别说明是否接管、漂移原因、接管次数及接管后的模型尝试次数；`fallbackRequests` / `fallbackSuccesses` 另统计 DeepSeek 兜底。模型失败、PI 刷新观察和兜底耗时均计入全程。

离线跨层测试使用真实 PI、真实 worker 和实际组合策略，只有手机与模型 I/O 注入替身，覆盖本地零调用、漂移接管、刷新后兜底、双失败停止及关键停机边界。这些测试不代表已经在设备或真实模型上观测到对应故障。

手机 App 无障碍录制导出可用另一条命令生成可读草稿：

```bash
"$PHONE_BENCH_PYTHON" experiments/pi-phone-use/workflow.py import-android \
  path/to/exported-workflow.json \
  "$PHONE_BENCH_OUTPUT_DIR/android-draft"
```

**这个手工导入命令只生成 `needs-verification` 草稿，不生成可直接执行的配方。** 新 App 入口由伴随服务领取手机队列中的录制，先由 PI 消费并验证全部原始意图，再生成并复用 Skill；最终实机状态以 [App 验收记录](../../docs/research/app-workflow-loop.md) 为准。`verified-demonstration` 表示成功示教已被共同观察器校验，不代表人工审阅已经发生。产品原型中的参数编辑、浮条和任意任务恢复不等于当前已实现能力。

新对比报告读取预设的 `rr-r{1,2,3}-jev` 与 `rr-r{1,2,3}-skill` 六轮。将技能包中的 `generation.json` 放在运行数据根目录，可单列生成成本；额外录像 ID 为 `video-jev` / `video-skill`：

运行数据根目录的 `generation.json` 还应由实验操作者或编排器显式记录 `sourceDemoTrialId`（例如 `demo-v2`）及 `demonstrationMs`，关联实际采用的示教轮。来源未声明时报告不把旧 `demo` 轮自动当成配方来源，早期失败或已淘汰的示教不混入正式统计。

```bash
"$PHONE_BENCH_PYTHON" experiments/pi-phone-use/build_record_replay_report.py \
  --base "$PHONE_BENCH_OUTPUT_DIR" \
  --report-dir "$PHONE_BENCH_OUTPUT_DIR/report"
```

输出 `record-replay.html` 与脱敏 `record-replay-data.json`，视频采用相对路径 `phone-use-videos/jev-full-flow.mp4` 和 `phone-use-videos/skill-full-flow.mp4`。缺失实测保持 pending；正式轮未完成或任一模式不足两次有效成功，不发布加速收益。报告中的交互原型、App 实机截图和性能证据分区，不能互相替代。

## 录制与报告

录像是额外一轮真实 PI 流程，与正式三轮统计分开：

```bash
"$PHONE_BENCH_PYTHON" experiments/pi-phone-use/record_pi_trial.py jev video-jev \
  --report-dir "$PWD/artifacts/pi-phone-use/report"
"$PHONE_BENCH_PYTHON" experiments/pi-phone-use/record_pi_trial.py deepseek video-deepseek \
  --report-dir "$PWD/artifacts/pi-phone-use/report"
```

自动策略也可用 `record_pi_trial.py jev-auto video-jev-auto --report-dir ...` 单独录制，产物名为 `jev-auto-full-flow.mp4`。固定研究报告只统计对应独立模型；自动策略不会因误用正式轮次 ID 而混入旧基线。

需要 `ffprobe`（FFmpeg）校验。录制直接使用 Android `screenrecord`，保存 720×1560、1×原速 MP4，不转码、不剪辑；只停止本次启动且校验路径匹配的录制 PID。HTML 与 `phone-use-videos/` 目录保持相对位置。视频长度包括 PI 计时边界之外少量画面，不能替代 PI 日志耗时。

`replay_worker.py` / `run_replay_pi.py` 只读回放六个实际手机快照，绝不派发手机动作；回放结束不代表手机任务成功。`cold` 使用 HTTP/1 client，`warm` 使用 HTTP/2 持久 client，同时改变协议与连接策略，不能将差异全部归因于连接复用。

报告生成器按本次研究的固定 trial IDs 汇总，不访问手机或模型：

```bash
"$PHONE_BENCH_PYTHON" experiments/pi-phone-use/render_report.py \
  --base "$PWD/artifacts/pi-phone-use" \
  --report-dir "$PWD/artifacts/pi-phone-use/report"
```

缺失轮次、录像或恢复记录会显示 `pending`；不会用零耗时或模拟结果补齐。公开报告仅作为本地查看产物，分享前应检查任务内容与运行日志。仓库仅保存精简研究证据，不保存账号界面或大录像。

## Jev 优先、DeepSeek 兜底

`jev-auto` 放在电脑端 PI 决策适配层，仍通过 `-e` 临时加载扩展，由 `phone-benchmark` provider 驱动 PI AgentLoop；Android MCP 负责读屏和执行。

1. Jev 请求在派发动作前发生超时、连接失败或输出无效，产生回退标记，PI 显式调用 `phone_observe` 刷新界面；旧观察上的选择不再使用。
2. DeepSeek 只在该新观察上尝试一次。它也失败就停止；成功动作完成后，下一个决策步骤重新优先 Jev，没有无限重试。
3. MCP 动作异常可能已经作用于手机，策略只回读一次并停止，不切换模型重放该动作。只有模型选择成功且通过当前候选校验，才允许派发动作。
4. PI 整体耗时完整包含主模型失败、观察刷新与兜底成本。日志保留各次模型尝试、失败分类及实际选用 backend，启动元数据的 `decisionMode` 和 `decisionPolicyDetails` 说明显式选择的策略，自动模式另记录 `decisionTimeoutsSeconds`。
5. 两模型当前共用 OpenRouter；切换模型不能保证解决同一个网关或本地网络故障。

离线 TDD 覆盖决策回退及 PI 工具调用顺序，具体边界见 [策略与验证计划](../../docs/plans/pi-jev-fallback.md)。历史研究证据保持原样；新策略的离线测试通过不代表已取得新一轮实机耗时或成功率。该扩展仍是专用任务入口，未全局安装为默认 PI agent。

命令行 worker 的输入等待可取消，SIGTERM 会在等待输入或模型响应时触发清理、保存失败结果并以 143 退出。同步 MCP HTTP 调用正在阻塞时仍可能需要父进程的有界强制退出；这种情况下不保证会话终止已完成，也不会自动重放动作。
