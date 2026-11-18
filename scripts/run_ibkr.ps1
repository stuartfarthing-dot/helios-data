# Helios: nightly IBKR load, run by Windows Task Scheduler (see install_ibkr_task.ps1).
# Needs IB Gateway running and logged in on this PC. Writes a dated log to logs\ and
# the result also shows in the warehouse table ingest_log (source 'ibkr').
$ErrorActionPreference = "Continue"
$repo = Split-Path -Parent $PSScriptRoot
Set-Location $repo
New-Item -ItemType Directory -Force -Path "$repo\logs" | Out-Null
$log = "$repo\logs\ibkr_$(Get-Date -Format yyyy-MM-dd).log"
$python = "$repo\.venv\Scripts\python.exe"
"=== $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') start ===" | Out-File -Append -Encoding utf8 $log
& $python -m helios_data.ibkr_ingest 2>&1 | ForEach-Object { "$_" } | Out-File -Append -Encoding utf8 $log
$code = $LASTEXITCODE
"=== $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') finished, exit code $code ===" | Out-File -Append -Encoding utf8 $log
exit $code
