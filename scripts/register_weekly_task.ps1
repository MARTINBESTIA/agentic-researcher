# Zaregistruje týždenný beh v Plánovači úloh (Task Scheduler) pod tvojím používateľom.
# Beží len keď si prihlásený (Docker Desktop to potrebuje) a notebook je na sieti.
# Použitie:  powershell -ExecutionPolicy Bypass -File scripts\register_weekly_task.ps1 -Day Sunday -Time 02:00
# Zrušenie:  Unregister-ScheduledTask -TaskName "Problem Radar 0.2 - planovany beh"
param(
    [ValidateSet("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")]
    [string]$Day = "Sunday",
    [string]$Time = "02:00",
    [int]$Minutes = 480
)

$root = Split-Path -Parent $PSScriptRoot
$script = Join-Path $PSScriptRoot "run_weekly.ps1"
$action = New-ScheduledTaskAction -Execute "powershell.exe" `
    -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$script`" -Minutes $Minutes" -WorkingDirectory $root
$trigger = New-ScheduledTaskTrigger -Weekly -DaysOfWeek $Day -At $Time
# StartWhenAvailable: ak bol počítač v čase behu vypnutý, spustí sa pri najbližšej príležitosti.
# Na batériu sa nespúšťa (beh trvá hodiny a vyťaží GPU).
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -ExecutionTimeLimit (New-TimeSpan -Minutes ($Minutes + 60))
Register-ScheduledTask -TaskName "Problem Radar 0.2 - planovany beh" -Action $action -Trigger $trigger -Settings $settings `
    -Description "Problem Radar: zber, overenie a register dôkazov ($root)" -Force | Out-Null
Write-Output "Zaregistrované: každý $Day o $Time. Výsledok: $root\reports\latest.md, log: $root\logs\weekly.log"
