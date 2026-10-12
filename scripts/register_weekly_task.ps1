# Zaregistruje plánovaný beh v Plánovači úloh (Task Scheduler) pod tvojím používateľom.
# Beží len keď si prihlásený (Docker Desktop to potrebuje) a notebook je na sieti a na nabíjačke.
# Použitie:  powershell -ExecutionPolicy Bypass -File scripts\register_weekly_task.ps1 -Day Sunday -Time 02:00
#            powershell -ExecutionPolicy Bypass -File scripts\register_weekly_task.ps1 -Daily -Time 23:00 -Wake
# -Wake prebudí uspatý počítač (vo Windows musí byť povolené „Povoliť časovače prebudenia“ v nastaveniach napájania).
# Beh sa spustí ako úloha Správcu akcií (radar job run-all --mode queue): vidíš ho v GUI a nezrazí sa s akciou z GUI.
# Zrušenie:  Unregister-ScheduledTask -TaskName "Problem Radar 0.2 - planovany beh"
param(
    [ValidateSet("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")]
    [string]$Day = "Sunday",
    [string]$Time = "02:00",
    [int]$Minutes = 480,
    [switch]$Daily,
    [switch]$Wake
)

$root = Split-Path -Parent $PSScriptRoot
$script = Join-Path $PSScriptRoot "run_weekly.ps1"
$action = New-ScheduledTaskAction -Execute "powershell.exe" `
    -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$script`" -Minutes $Minutes" -WorkingDirectory $root
if ($Daily) { $trigger = New-ScheduledTaskTrigger -Daily -At $Time } else { $trigger = New-ScheduledTaskTrigger -Weekly -DaysOfWeek $Day -At $Time }
# StartWhenAvailable: ak bol počítač v čase behu vypnutý, spustí sa pri najbližšej príležitosti.
# Na batériu sa nespúšťa a pri odpojení nabíjačky sa zastaví (beh trvá hodiny a vyťaží GPU); prerušenú akciu
# spustíš znova v Správcovi akcií („Spustiť znova“). Limit +12 h: beh môže čakať v poradí za akciou z GUI.
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -WakeToRun:$Wake -ExecutionTimeLimit (New-TimeSpan -Minutes ($Minutes + 720))
Register-ScheduledTask -TaskName "Problem Radar 0.2 - planovany beh" -Action $action -Trigger $trigger -Settings $settings `
    -Description "Problem Radar: zber, overenie a register dôkazov ($root)" -Force | Out-Null
$when = if ($Daily) { "každý deň" } else { "každý $Day" }
$wakeNote = if ($Wake) { ", s prebudením" } else { "" }
Write-Output "Zaregistrované: $when o $Time$wakeNote. Výsledok: $root\reports\latest.md, log: $root\logs\weekly.log"
