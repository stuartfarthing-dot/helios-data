# Helios: create (or replace) the scheduled task for the execution program on the IG DEMO account.
# Run once from the helios-data folder:   powershell -ExecutionPolicy Bypass -File scripts\install_exec_task.ps1
# Runs Monday-Friday at 22:40 (after the US close: acts on the evening's H43 reading) and 08:10 (retries anything
# the market was closed for). Each run checks what is held and only does what is missing, so two runs are harmless.
$repo = Split-Path -Parent $PSScriptRoot
$script = Join-Path $repo "scripts\run_exec.ps1"
$name = "Helios execution (IG demo)"

$action = New-ScheduledTaskAction -Execute "powershell.exe" `
    -Argument "-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$script`" -Mode demo" -WorkingDirectory $repo
$days = @("Monday", "Tuesday", "Wednesday", "Thursday", "Friday")
$triggers = @(
    (New-ScheduledTaskTrigger -Weekly -DaysOfWeek $days -At 22:40),
    (New-ScheduledTaskTrigger -Weekly -DaysOfWeek $days -At 08:10)
)
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -ExecutionTimeLimit (New-TimeSpan -Minutes 20) `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries

Register-ScheduledTask -TaskName $name -Action $action -Trigger $triggers -Settings $settings `
    -Description "Helios execution program on the IG DEMO account (helios_exec). Stop it with a KILL file or exec.control.enabled = FALSE." `
    -Force | Out-Null
Get-ScheduledTask -TaskName $name | Select-Object TaskName, State
Write-Host "Next run:" (Get-ScheduledTaskInfo -TaskName $name).NextRunTime
