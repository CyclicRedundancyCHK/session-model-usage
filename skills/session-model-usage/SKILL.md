---
name: session-model-usage
description: 查询 Codex 会话及子智能体按模型、推理强度和 Fast 请求设置统计的 token 用量，启动、检查或停止 Windows 悬浮条。
---

# 会话模型用量

此插件的 Windows 程序与查询入口共用统计核心。插件根目录是本文件上方的 `../..`，运行根目录内的 `scripts/session-usage.ps1`。

- 查询当前会话：`& '<插件根目录>/scripts/session-usage.ps1' -Action query`。脚本使用 `CODEX_THREAD_ID`，可通过 `-ThreadId '<会话 UUID>'` 指定其他会话；`-MainOnly` 只统计主会话。
- 启动悬浮条：使用 `-Action launch`。这是独立托盘工具；与官方 Codex 可以按任意顺序启动，普通启动没有调试端口时自动使用 Windows 辅助功能与当前进程的窗口路由日志。Codex 关闭后工具保持待机，官方图标重新打开后自动连接。`ambiguous_host` 表示多个主进程，程序等待唯一实例，不猜测会话。不要强制结束应用或正在运行的任务。
- 查看悬浮条状态：使用 `-Action status`。`running` 表示管理进程运行，待机时 `overlay_visible=false`；`healthy` 表示心跳新鲜。
- 诊断异常：使用 `-Action diagnose`，只读本地近期事件；没有自动上传。`recovery_paused` 表示一分钟内五次恢复失败，可点击托盘“恢复连接”或再次 launch。
- 关闭悬浮条：使用 `-Action stop`，这不会关闭 Codex。

查询返回 JSON：`totals` 为总量，`models` 为每个模型及主会话/子智能体分项；各模型的 `reasoning_efforts` 按实际推理强度分组，同组也含 `totals`、`main`、`subagents`。`reasoning_effort: null` 表示未记录，字符串 `none` 表示明确关闭推理。不要用当前模型选择或推理强度填补历史缺失。`threads` 为来源，`warnings` 为证据缺口。`pending` 与空 `totals` 表示还没有用量记录，不能解释为零。`partial` 表示统计不完整，回答时保留这一限制。

`models[].configurations` 按模型、推理强度和请求服务等级分组，同样含主会话、子智能体与总量。`service_tier` 保留日志等级；`fast_mode: true/false/null` 表示 Fast 请求设置、普通或无法确认，不代表服务端实际等级。迟到的同轮元数据会补齐归属，但不会改变总量；累计缺口和冲突继续标明。

`status.connection_mode` 表示 `windows_accessibility` 或 `cdp`。托盘“恢复连接”只重新尝试连接；不会关闭或重启 Codex。原生路径仅接受明确的 `/local/` 窗口路由与唯一窗口类型，缺少证据或同类窗口冲突时隐藏。

总 token 已包含输入和输出；缓存输入和推理输出属于分项，不能再次相加。模型归属以本地记录为准，无法确认的用量列在 `unattributed`。查询读已有记录，不调用模型。

悬浮条独立运行，位于输入栏底部权限与背景信息控件之间，首版只识别当前本机的 Codex 会话。调试连接、当前会话识别或控件位置识别失效时，程序隐藏悬浮条并在托盘和 `status` 中说明原因，不根据最近更新的会话猜测。`overlay_visible` 表示实际显示状态。

Codex 失去焦点后悬浮条继续显示，并跟随已确认窗口的层级；最小化、隐藏或切换虚拟桌面时才隐藏。它不是全局置顶窗口，其他应用覆盖 Codex 时也会盖住对应的悬浮条。
