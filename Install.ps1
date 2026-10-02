[CmdletBinding()]
param()
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [Text.UTF8Encoding]::new()
$taskExecutable = Join-Path $PSScriptRoot 'runtime\session-usage.exe'
if (-not (Test-Path -LiteralPath $taskExecutable -PathType Leaf)) {
    throw '安装包缺少程序，请完整解压后再安装。'
}
$taskInstalled = Join-Path $env:USERPROFILE 'plugins\session-model-usage\.codex-plugin\plugin.json'
$taskAction = if (Test-Path -LiteralPath $taskInstalled) { 'upgrade' } else { 'install' }
& $taskExecutable $taskAction --source-root $PSScriptRoot
if ($LASTEXITCODE -ne 0) { throw '安装没有完成，请查看上面的具体原因。' }
