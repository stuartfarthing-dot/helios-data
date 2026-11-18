# Helios: create (or replace) the Windows scheduled task for the nightly IBKR load.
# Run once from the helios-data folder:   powershell -ExecutionPolicy Bypass -File scripts\install_ibkr_task.ps1
# Runs Monday-Friday at 18:30 (after the London close and the IBKR end-of-day bar), while you are logged in.
# If the PC was asleep or off at 18:30, it runs as soon as it can.
$repo = Split-Path -Parent $PSScriptRoot
$script = Join-Path $repo "scripts\run_ibkr.ps1"
$name = "Helios IBKR nightly load"

$action = New-ScheduledTaskAction -Execute "powershell.exe" `
    -Argument "-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$script`"" -WorkingDirectory $repo
$trigger = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Monday, Tuesday, Wednesday, Thursday, Friday -At 18:30
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -ExecutionTimeLimit (New-TimeSpan -Hours 3) `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries

Register-ScheduledTask -TaskName $name -Action $action -Trigger $trigger -Settings $settings `
    -Description "Loads LSE daily and 5-minute bars with real volume from IB Gateway into the Helios warehouse (helios_data.ibkr_ingest)." `
    -Force | Out-Null
Get-ScheduledTask -TaskName $name | Select-Object TaskName, State
Write-Host "Next run:" (Get-ScheduledTaskInfo -TaskName $name).NextRunTime
