---
name: session-model-usage
description: 查询 Codex 当前会话及其子智能体按模型和推理强度统计的 token 用量，启动、检查或停止 Windows 会话用量悬浮条。
---

# 会话模型用量

此插件的 Windows 程序与查询入口共用统计核心。插件根目录是本文件上方的 `../..`，运行根目录内的 `scripts/session-usage.ps1`。

- 查询当前会话：`& '<插件根目录>/scripts/session-usage.ps1' -Action query`。脚本使用 `CODEX_THREAD_ID`，可通过 `-ThreadId '<会话 UUID>'` 指定其他会话；`-MainOnly` 只统计主会话。
- 启动悬浮条：使用 `-Action launch`。如果返回 `waiting_for_restart`，说明当前 Codex 未启用本机调试连接，启动器已在托盘等待；用户保存工作并手动退出 Codex 后，它会自动重新打开当前安装版本并恢复悬浮条，无需再点击启动器。`restart_required` 表示多个实例等情况，需要手动退出后再使用启动器。不要强制结束应用或正在运行的任务。
- 查看悬浮条状态：使用 `-Action status`。
- 关闭悬浮条：使用 `-Action stop`，这不会关闭 Codex。

查询返回 JSON：`totals` 为总量，`models` 为每个模型及主会话/子智能体分项；各模型的 `reasoning_efforts` 按实际推理强度分组，同组也含 `totals`、`main`、`subagents`。`reasoning_effort: null` 表示未记录，字符串 `none` 表示明确关闭推理。不要用当前模型选择或推理强度填补历史缺失。`threads` 为来源，`warnings` 为证据缺口。`pending` 与空 `totals` 表示还没有用量记录，不能解释为零。`partial` 表示统计不完整，回答时保留这一限制。

总 token 已包含输入和输出；缓存输入和推理输出属于分项，不能再次相加。模型归属以本地记录为准，无法确认的用量列在 `unattributed`。查询读已有记录，不调用模型。

悬浮条独立运行，位于输入栏底部权限与背景信息控件之间，首版只识别当前本机的 Codex 会话。调试连接、当前会话识别或控件位置识别失效时，程序隐藏悬浮条并在托盘和 `status` 中说明原因，不根据最近更新的会话猜测。`overlay_visible` 表示实际显示状态。

Codex 失去焦点后悬浮条继续显示，并跟随已确认窗口的层级；最小化、隐藏或切换虚拟桌面时才隐藏。它不是全局置顶窗口，其他应用覆盖 Codex 时也会盖住对应的悬浮条。
