# Zaregistruje kontrolu pošty (M41) v Plánovači úloh pod tvojím používateľom: pri prihlásení a potom každých N minút.
# Spúšťa „python -m radar mail --sync“ bez okna: iba hlavičky správ od a pre kontakty z oslovení, bez modelu; odpovede
# na rozbor zaradí ako úlohu „mail“ do poradia Správcu akcií. Heslo aplikácie musí byť v premennej prostredia
# RADAR_GMAIL_APP_PASSWORD (používateľská) a adresa schránky v config.local.yaml (mail: enabled: true, user: ...).
# Použitie:  powershell -ExecutionPolicy Bypass -File scripts\register_mail_task.ps1 -Minutes 15
# Zrušenie:  Unregister-ScheduledTask -TaskName "Problem Radar 0.2 - posta" -Confirm:$false
param([int]$Minutes = 15)

$root = Split-Path -Parent $PSScriptRoot
$pythonw = Join-Path $root ".venv\Scripts\pythonw.exe"
if (-not (Test-Path $pythonw)) { Write-Error "Chýba $pythonw (vytvor .venv podľa README)"; exit 1 }
$action = New-ScheduledTaskAction -Execute $pythonw -Argument "-m radar mail --sync" -WorkingDirectory $root
$atLogon = New-ScheduledTaskTrigger -AtLogOn -User "$env:USERDOMAIN\$env:USERNAME"
$repeat = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) -RepetitionInterval (New-TimeSpan -Minutes $Minutes)
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Minutes 10)
Register-ScheduledTask -TaskName "Problem Radar 0.2 - posta" -Action $action -Trigger @($atLogon, $repeat) `
    -Settings $settings -Description "Problem Radar: odoslané oslovenia a odpovede z Gmailu (iba čítanie)" -Force | Out-Null
Write-Output "Zaregistrované: kontrola pošty pri prihlásení a každých $Minutes min (úloha „Problem Radar 0.2 - posta“)."
