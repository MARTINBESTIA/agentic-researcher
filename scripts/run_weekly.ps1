# Plánovaný beh Problem Radar: zapne Docker Desktop a SearXNG, skontroluje modely a prejde celý beh.
# Ak config.yaml ukazuje na llama-swap (port 8080), spustí ho a po behu vypne aj procesy llama-server.
# Ručné spustenie:  powershell -ExecutionPolicy Bypass -File scripts\run_weekly.ps1 -Minutes 480
param([int]$Minutes = 480, [string]$Profile = "balanced")

$ErrorActionPreference = "Continue"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root
$python = Join-Path $root ".venv\Scripts\python.exe"
New-Item -ItemType Directory -Force (Join-Path $root "logs") | Out-Null
$log = Join-Path $root "logs\weekly.log"
function Say($text) { $line = "$(Get-Date -Format s) $text"; Write-Output $line; Add-Content -Path $log -Value $line -Encoding utf8 }

# 1. Docker Desktop a SearXNG
docker info *> $null
if ($LASTEXITCODE -ne 0) {
    Say (& (Join-Path $PSScriptRoot "fix_docker_sockets.ps1") | Out-String).Trim()
    Say "Spúšťam Docker Desktop"
    Start-Process "C:\Program Files\Docker\Docker\Docker Desktop.exe"
    for ($i = 0; $i -lt 60; $i++) { Start-Sleep -Seconds 5; docker info *> $null; if ($LASTEXITCODE -eq 0) { break } }
}
docker compose up -d searxng *> $null
if ($LASTEXITCODE -ne 0) {
    # Kontajner s rovnakým názvom môže patriť staršej kópii projektu (agentic-researcher); stačí ho spustiť.
    docker start radar-searxng *> $null
    if ($LASTEXITCODE -ne 0) { Say "CHYBA: SearXNG sa nespustil (docker compose up -d searxng)"; exit 1 }
}

# 2. Modely: Ollama beží ako aplikácia; llama-swap len pri base_url na porte 8080
$usesSwap = Select-String -Path (Join-Path $root "config.yaml") -Pattern '^\s*base_url:\s*http://(localhost|127\.0\.0\.1):8080' -Quiet
if ($usesSwap) {
    Say "Spúšťam llama-swap (start_llm_swap.bat)"
    Start-Process -FilePath "cmd.exe" -ArgumentList "/c", "start_llm_swap.bat" -WorkingDirectory $root -WindowStyle Minimized
} else {
    try { Invoke-RestMethod -Uri "http://localhost:11434/api/version" -TimeoutSec 5 | Out-Null }
    catch { Say "Spúšťam Ollama"; Start-Process -FilePath "ollama" -ArgumentList "serve" -WindowStyle Hidden }
}

try {
    & $python -m radar doctor --wait 600
    if ($LASTEXITCODE -ne 0) { Say "CHYBA: kontrola (doctor) zlyhala, beh sa nespustí"; exit 1 }
    Say "Beh štartuje (limit $Minutes min, profil $Profile)"
    & $python -m radar --profile $Profile run-all --minutes $Minutes
    Say "Beh skončil s kódom $LASTEXITCODE; správa: reports\latest.md"
} finally {
    if ($usesSwap) {
        Stop-Process -Name "llama-swap" -Force -ErrorAction SilentlyContinue
        Stop-Process -Name "llama-server" -Force -ErrorAction SilentlyContinue
    }
}
