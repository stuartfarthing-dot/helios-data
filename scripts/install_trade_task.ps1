# Helios: create (or replace) the Windows scheduled task for IBKR paper trading.
# Run once from the helios-data folder:   powershell -ExecutionPolicy Bypass -File scripts\install_trade_task.ps1
# Runs every hour at 10 past, every day, while you are logged in. Each market's entry deadline is checked
# in the code, so the hourly rhythm works in any time zone (Melbourne now, the UK from January).
# To pause: Disable-ScheduledTask -TaskName "Helios IBKR paper trading"   (or create a STOP_TRADING file)
$repo = Split-Path -Parent $PSScriptRoot
$script = Join-Path $repo "scripts\run_trade.ps1"
$name = "Helios IBKR paper trading"

$start = (Get-Date).Date.AddHours((Get-Date).Hour + 1).AddMinutes(10)
$action = New-ScheduledTaskAction -Execute "powershell.exe" `
    -Argument "-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$script`"" -WorkingDirectory $repo
$trigger = New-ScheduledTaskTrigger -Once -At $start -RepetitionInterval (New-TimeSpan -Hours 1) `
    -RepetitionDuration (New-TimeSpan -Days 3650)
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -ExecutionTimeLimit (New-TimeSpan -Minutes 30) `
    -MultipleInstances IgnoreNew -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries

Register-ScheduledTask -TaskName $name -Action $action -Trigger $trigger -Settings $settings `
    -Description "Helios H1a/H14/H18 on the IBKR paper account (helios_data.ibkr_trade --send --refresh)." `
    -Force | Out-Null
Get-ScheduledTask -TaskName $name | Select-Object TaskName, State
Write-Host "Next run:" (Get-ScheduledTaskInfo -TaskName $name).NextRunTime
