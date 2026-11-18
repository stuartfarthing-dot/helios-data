# Helios: hourly IBKR paper-trading run, started by Windows Task Scheduler (see install_trade_task.ps1).
# Refreshes the Dukascopy hourly bars for the traded markets, reconciles fills, then places tonight's
# orders on the PAPER account (helios_data.ibkr_trade --send --refresh). Needs the paper IB Gateway
# running (port 4002). Create a file called STOP_TRADING in the helios-data folder to pause all sending.
$ErrorActionPreference = "Continue"
$repo = Split-Path -Parent $PSScriptRoot
Set-Location $repo
New-Item -ItemType Directory -Force -Path "$repo\logs" | Out-Null
$log = "$repo\logs\trade_$(Get-Date -Format yyyy-MM-dd).log"
$python = "$repo\.venv\Scripts\python.exe"
"=== $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') start ===" | Out-File -Append -Encoding utf8 $log
& $python -m helios_data.ibkr_trade --send --refresh 2>&1 | ForEach-Object { "$_" } | Out-File -Append -Encoding utf8 $log
$code = $LASTEXITCODE
"=== $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') finished, exit code $code ===" | Out-File -Append -Encoding utf8 $log
exit $code
