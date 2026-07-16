param(
    [Parameter(Mandatory = $true)]
    [string]$BotRoot
)

$ErrorActionPreference = "Stop"

$RepoRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$SourceDir = Join-Path $RepoRoot "plugin\wxsph_parser"
$PluginsDir = Join-Path $BotRoot "wxbot\plugins"
$TargetDir = Join-Path $PluginsDir "wxsph_parser"
$SourceMain = Join-Path $SourceDir "main.py"
$SourceConfig = Join-Path $SourceDir "config.example.json"
$TargetMain = Join-Path $TargetDir "main.py"
$TargetConfig = Join-Path $TargetDir "config.json"

if (-not (Test-Path -LiteralPath $PluginsDir)) {
    throw "wxbot plugin directory not found: $PluginsDir"
}

New-Item -ItemType Directory -Path $TargetDir -Force | Out-Null
Copy-Item -LiteralPath $SourceMain -Destination $TargetMain -Force

if (-not (Test-Path -LiteralPath $TargetConfig)) {
    Copy-Item -LiteralPath $SourceConfig -Destination $TargetConfig
    Write-Host "Created plugin config: $TargetConfig"
}
else {
    Write-Host "Existing config preserved: $TargetConfig"
}

Write-Host "Installed plugin source: $TargetMain"
Write-Host "Review config.json, then restart wxbot."
