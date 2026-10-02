param(
    [ValidateSet('query','launch','status','stop','diagnose')]
    [string]$Action = 'query',
    [string]$ThreadId = $env:CODEX_THREAD_ID,
    [switch]$MainOnly
)
$taskPluginRoot = Split-Path -Parent $PSScriptRoot
$taskExecutable = Join-Path $taskPluginRoot 'runtime\session-usage.exe'
if (-not (Test-Path -LiteralPath $taskExecutable -PathType Leaf)) {
    throw "未找到插件运行程序：$taskExecutable"
}
$taskArguments = @($Action)
if ($Action -eq 'query') {
    if ($ThreadId) { $taskArguments += @('--thread-id', $ThreadId) }
    if ($MainOnly) { $taskArguments += '--no-descendants' }
}
[Console]::OutputEncoding = [Text.UTF8Encoding]::new($false)
& $taskExecutable @taskArguments
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
