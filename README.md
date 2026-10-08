# Codex 会话模型用量

Windows 上的 Codex 会话用量插件与伴随悬浮条。只读本机日志，显示主会话和子智能体按模型、推理强度统计的 token 用量，统计过程不调用模型。

当前本地修复版本为 **0.1.5-local.1**，兼容 Codex 26.1002.7124.0 的额外启动窗口和辅助功能元素失效。此修复未推送 GitHub，Windows 安装包由本机 `dist` 目录提供，也可按下方说明从源码构建。

![深色用量详情](assets/preview-dark.png)

## 安装

1. 使用本地 `session-model-usage-v0.1.5-local.1-windows-x64.zip`。
2. 完整解压，保留 `runtime` 及其 `_internal` 文件夹。无需安装 Python。
3. 双击 `安装插件.cmd`，再从 Windows 开始菜单打开 **Codex 会话用量**。
4. 工具与 Codex 可以按任意顺序打开。通过官方图标启动 Codex 后，托盘工具会自动连接；关闭 Codex 后工具继续待机，重开后恢复。无需退出当前会话或更换官方图标。

安装位置为 `%USERPROFILE%\plugins\session-model-usage`，通过 Codex CLI 注册本项目独立的插件市场，不修改其他插件的市场文件。已有用户双击安装脚本会自动升级：先备份、只停止本插件、保留原市场和快捷方式，注册失败时回滚。备份保留在个人插件目录的 `.session-model-usage-backups` 中。

首次安装后，插件技能在新的 Codex 会话中加载。也可直接打开 `runtime/CodexSessionUsage.exe` 启动伴随程序。

## 使用

用量条位于输入栏底部权限与背景信息控件之间。点击后查看模型、推理强度、输入、缓存输入、输出、推理输出，以及主会话和子智能体的贡献。

- 默认递归汇总主会话和所有子智能体，包括已结束的子智能体。
- 总量保留日志的 `total_tokens`；缓存输入和推理输出是分项，不重复相加。
- 同一模型的不同推理强度和 Fast 请求设置分别列出；迟到的同轮元数据会补齐已记录用量，不用当前设置补填历史。
- 跟随会话、窗口位置、缩放与深浅主题。Codex 失去焦点后继续显示，其他应用遮挡它时也遮挡用量条；最小化、隐藏或无法确认会话时隐藏。
- 默认独立启动托盘工具，不自动打开 Codex；官方图标、开始菜单、任务栏原入口均保留。

![浅色用量详情](assets/preview-light.png)

独立托盘管理进程在 Codex 关闭后保持待机，不自行重开。官方启动没有调试端口时，直接读取当前 Codex 进程日志中的明确窗口路由，再通过 Windows UI Automation 定位输入栏。先开工具、先开 Codex、关闭后重新打开均自动连接。托盘“恢复连接”只重试连接，不安排重启 Codex。

悬浮条异常退出或心跳停止时自动恢复。一分钟内五次失败会暂停，并提供“恢复连接”。临时连接错误按 1、2、4、8、15 秒重试。

程序每 250 毫秒检查会话和位置，每秒增量刷新用量。只读取本机的会话索引、窗口路由日志和辅助功能控件，不读取输入文本或操作剪贴板；已有调试连接仍验证为 Codex 自身在回环地址监听。没有开机自启。

## 命令

在解压目录的 PowerShell 中执行：

```powershell
.\runtime\session-usage.exe launch
.\runtime\session-usage.exe query --thread-id '<会话 UUID>'
.\runtime\session-usage.exe query --thread-id '<会话 UUID>' --no-descendants
.\runtime\session-usage.exe status
.\runtime\session-usage.exe diagnose
.\runtime\session-usage.exe --version
.\runtime\session-usage.exe stop
```

`query` 返回 JSON，默认使用 `CODEX_THREAD_ID` 或正在跟随的会话。`--codex-home <目录>` 可指定日志目录。`stop` 退出管理进程和悬浮条并取消恢复，Codex 继续运行。`status.running` 表示管理进程运行，待机时 `overlay_visible=false`；`connection_mode` 为 `windows_accessibility` 或 `cdp`；`healthy` 表示心跳新鲜；`supervisor_pid` 和 `worker_pid` 分别表示管理进程与实际悬浮条子进程。`overlay_pid` 兼容旧缓存，待机时可能指向管理进程。`diagnose` 只读取本地轮转诊断，不上传日志。

`complete` 表示现有记录可以完成统计，`partial` 表示存在证据缺口，`pending` 和 `totals: null` 表示尚无用量记录。`reasoning_effort: null` 表示没有记录，`none` 表示明确关闭推理。

`models[].reasoning_efforts` 保留原有按强度汇总。新增 `models[].configurations`，按推理强度与服务等级联合分组，含 `service_tier`、`fast_mode`、`totals`、`main`、`subagents` 和记录数量。`priority/fast` 对应 Fast，`default/standard` 对应普通；缺失或其他等级对应 `fast_mode: null`，其他等级保留原值。Fast 是日志中的请求设置，不代表服务端实际采用的等级。

## 开发与构建

源码在 `source`，支持 Windows x64、Python 3.13。

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r source/requirements.txt PyInstaller==6.22.3
$env:PYTHONPATH = 'source'
.\.venv\Scripts\python.exe -m unittest discover -s source/tests -v
.\.venv\Scripts\python.exe scripts/build_release.py
```

构建结果位于 `dist`，包含 Windows ZIP 安装包和 SHA-256 校验文件。脚本使用固定清单组装发布包，并检查敏感文件名、个人路径与非示例会话 UUID。仓库和发行包不包含本机日志、浏览器配置、认证文件、SQLite 索引或开发缓存。

## 兼容范围

已在 Codex Windows 安装包 `26.924.2738.0` 、`26.928.1915.0` 与 `26.930.2377.0` 验证会话识别与输入栏位置，并验证新版恢复流程。`26.930.3748.0` 另支持无调试端口的官方启动方式。使用了内部界面标识和只读路由，后续 Codex 更新可能需要适配。

首版支持本机 Codex 会话。云端、ChatGPT Work 和远程主机会话暂不支持。原生连接要求日志中的窗口类型与可见窗口唯一对应；多个同类型窗口无法唯一对应时隐藏，不按标题匹配。跨物理屏幕混合 DPI 与多窗口的完整实机验收范围见 [验证说明](docs/verification.md)。

这是社区项目，不是 OpenAI 官方产品。本项目源码采用 MIT 许可证；发行包的第三方组件遵循各自许可证，见 [第三方说明](THIRD_PARTY_NOTICES.md) 和 `licenses`。
