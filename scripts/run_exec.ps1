# Helios execution program (foundations A7), run by Windows Task Scheduler (see install_exec_task.ps1).
# Trades only the IG DEMO account, and only when exec.control.enabled = TRUE in the warehouse.
# A file called KILL in the helios-data folder stops it from doing anything.
param([string]$Mode = "demo")
$ErrorActionPreference = "Continue"
$repo = Split-Path -Parent $PSScriptRoot
Set-Location $repo
New-Item -ItemType Directory -Force -Path "$repo\logs" | Out-Null
$log = "$repo\logs\exec_$(Get-Date -Format yyyy-MM-dd).log"
$python = "$repo\.venv\Scripts\python.exe"
"=== $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') start ($Mode) ===" | Out-File -Append -Encoding utf8 $log
& $python -m helios_exec --mode $Mode 2>&1 | ForEach-Object { "$_" } | Out-File -Append -Encoding utf8 $log
$code = $LASTEXITCODE
"=== $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') finished, exit code $code ===" | Out-File -Append -Encoding utf8 $log
exit $code
