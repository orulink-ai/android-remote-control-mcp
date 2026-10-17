# PI phone-use 实验接入

这是已跑通的专用实验模块，通过 PI extension 和自定义 `phone-benchmark` provider，让真实模型逐步选择手机动作，再由本项目 Android MCP 执行。它不是额外的 MCP server，也不是 skill；未注册到用户 PI 默认配置。

本阶段支持 `jev`、`deepseek` 两种独立测试模式，**没有自动模型回退**。任务固定为小红书搜索“小狮日记”、打开指定首帖、点赞、收藏、取消点赞、取消收藏。阶段目标、候选过滤和截图颜色验证器对两种模型共用，不是任意自然语言任务的通用 phone agent。

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

## 安装与离线验证

需要 Python 3.11+，以及兼容的 PI CLI。已验证版本为 npm 包 `@earendil-works/pi-coding-agent` **1.3.0**，对应 `@earendil-works/pi-ai` API；其它发行版的扩展 API 可能不同。扩展由 PI 自身加载和解析依赖，不需要把全局 Node 模块复制进仓库。

从仓库根目录执行：

```bash
python3 -m venv experiments/pi-phone-use/.venv
experiments/pi-phone-use/.venv/bin/python -m pip install -r experiments/pi-phone-use/requirements.txt
experiments/pi-phone-use/.venv/bin/python -m pytest -q experiments/pi-phone-use
```

单元测试使用 HTTP/process doubles，不连接真实手机或真实模型。安装 PI 后还会运行一次**真实 PI＋假模型、只读回放**的 smoke test；未安装 PI 时仅该项跳过。测试无需模型密钥，也不会改变用户 PI 默认配置。

## 配置

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

MCP 配置只读取指定 server 的 `headers`，URL 单独显式配置，避免沿用失效的手机 LAN 地址。也可连接无需鉴权的本机测试服务；需要鉴权的真实服务必须在私有配置中提供正确 headers。

密钥文件建议权限 `0600`。不要将带 token 的完整 MCP 配置放进示例、报告或提交。

## 实机运行

先启动手机 Android MCP，解锁并保持亮屏。USB 转发只是传输方式；实际点击和读屏由 MCP 完成。以下命令会真实操作当前账号，对同一帖子切换点赞和收藏；每轮从桌面开始，轮前会冷停止小红书。

```bash
export PHONE_BENCH_PYTHON="$PWD/experiments/pi-phone-use/.venv/bin/python"
export PHONE_BENCH_DEVICE="你的设备序列号"
export OPENROUTER_API_KEY_FILE="$HOME/.config/android-jev/openrouter.key"
adb -s "$PHONE_BENCH_DEVICE" forward tcp:18080 tcp:8080

"$PHONE_BENCH_PYTHON" experiments/pi-phone-use/run_pi_trial.py jev final-r1-jev
"$PHONE_BENCH_PYTHON" experiments/pi-phone-use/run_pi_trial.py deepseek final-r1-deepseek
```

每轮 trial ID 必须唯一，已有产物不覆盖。同一时刻只运行一个实机流程。首帖必须是代码固定的目标标题，否则不应将停止解释成模型慢；搜索排序、APP 布局或分辨率改变后，应先调整与验证专用任务适配。

正式比较前需确认目标帖为未点赞、未收藏，记录原状态；每轮结束检查最终两项未激活，失败轮先检查已完成阶段再恢复。`reset_baseline.py` 仅适用于当前已打开且已验证的目标详情页，属于计时外清理，不能替代完整任务。原状态恢复由实验操作者在结束后验证，不由下一轮盲目重放完成。

## 录制与报告

录像是额外一轮真实 PI 流程，与正式三轮统计分开：

```bash
"$PHONE_BENCH_PYTHON" experiments/pi-phone-use/record_pi_trial.py jev video-jev \
  --report-dir "$PWD/artifacts/pi-phone-use/report"
"$PHONE_BENCH_PYTHON" experiments/pi-phone-use/record_pi_trial.py deepseek video-deepseek \
  --report-dir "$PWD/artifacts/pi-phone-use/report"
```

需要 `ffprobe`（FFmpeg）校验。录制直接使用 Android `screenrecord`，保存 720×1560、1×原速 MP4，不转码、不剪辑；只停止本次启动且校验路径匹配的录制 PID。HTML 与 `phone-use-videos/` 目录保持相对位置。视频长度包括 PI 计时边界之外少量画面，不能替代 PI 日志耗时。

`replay_worker.py` / `run_replay_pi.py` 只读回放六个实际手机快照，绝不派发手机动作；回放结束不代表手机任务成功。`cold` 使用 HTTP/1 client，`warm` 使用 HTTP/2 持久 client，同时改变协议与连接策略，不能将差异全部归因于连接复用。

报告生成器按本次研究的固定 trial IDs 汇总，不访问手机或模型：

```bash
"$PHONE_BENCH_PYTHON" experiments/pi-phone-use/render_report.py \
  --base "$PWD/artifacts/pi-phone-use" \
  --report-dir "$PWD/artifacts/pi-phone-use/report"
```

缺失轮次、录像或恢复记录会显示 `pending`；不会用零耗时或模拟结果补齐。公开报告仅作为本地查看产物，分享前应检查任务内容与运行日志。仓库仅保存精简研究证据，不保存账号界面或大录像。

## 下一阶段：Jev 优先、DeepSeek 兜底

建议放在电脑端 PI 决策适配层，不改 Android MCP 的职责：

1. 模型请求在派发手机动作前发生超时、连接失败或输出无效，可以在同一有效观察上调用 DeepSeek；如果等待过久，先重读。
2. MCP 动作返回超时、连接中断或结果不确定，先回读目标状态，不直接让另一个模型重放点击。
3. 记录主模型失败、回退原因、两次模型耗时和最终选用模型，整段耗时包含回退成本。
4. 两模型当前共用 OpenRouter；切换模型不能保证解决同一个网关或本地网络故障。

该策略尚未实现，也未作为默认 PI 配置启用。先完成独立模型实验归档，后续再为回退、失败恢复和通用 phone-use 任务补测试。
