# Docker Desktop na tomto počítači po vypnutí nechá v AppData unixové sockety, ktoré Windows nevie
# zmazať ("The file cannot be accessed by the system", nepomôže ani fsutil). Pri ďalšom štarte ich
# Docker nevie odstrániť a spadne s chybou "initializing Inference manager" alebo "Secrets Engine".
# Skript presunie priečinky s takými socketmi bokom (nič nemaže); Docker si vytvorí nové.
# Spúšťa sa len keď Docker Desktop nebeží.

$procs = Get-Process -Name "Docker Desktop", "com.docker.backend" -ErrorAction SilentlyContinue
if ($procs) { Write-Output "Docker Desktop beží, sockety nechávam tak."; exit 0 }

$stamp = Get-Date -Format "yyyyMMdd-HHmmss"
$target = Join-Path $env:LOCALAPPDATA "Docker-stale-sockets\$stamp"
$moved = 0
foreach ($dir in @("$env:LOCALAPPDATA\Docker\run", "$env:LOCALAPPDATA\docker-secrets-engine")) {
    if (-not (Test-Path -LiteralPath $dir)) { continue }
    $sockets = Get-ChildItem -LiteralPath $dir -Force -ErrorAction SilentlyContinue |
        Where-Object { -not $_.PSIsContainer -and ($_.Attributes -band [IO.FileAttributes]::ReparsePoint) }
    if (-not $sockets) { continue }
    New-Item -ItemType Directory -Force $target | Out-Null
    $name = (Split-Path -Parent $dir | Split-Path -Leaf) + "_" + (Split-Path -Leaf $dir)
    Move-Item -LiteralPath $dir -Destination (Join-Path $target $name)
    $moved++
}
Write-Output "Presunuté priečinky so starými socketmi: $moved$(if ($moved) { " (do $target)" })"
