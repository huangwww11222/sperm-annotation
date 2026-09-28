# Installer contract only: fake Docker, no network, services or business data.
$ErrorActionPreference = 'Stop'
$root = Split-Path $PSScriptRoot -Parent
$originalLocation = Get-Location
$testRoot = Join-Path ([IO.Path]::GetTempPath()) ('annotation-installer-' + [guid]::NewGuid().ToString('N'))
function global:docker {
    $global:LASTEXITCODE = 0
    $global:dockerCalls += ($args -join ' ')
    if ($global:modelSetupFail -and ($args -join ' ') -eq 'compose --profile model-setup run --rm --no-deps model-setup') { $global:LASTEXITCODE = 42; return }
    if ($args[0] -eq 'info') { Write-Output 'linux' }
    if (($args -join ' ') -eq 'compose --profile model-setup config --services') {
        Write-Output 'backend'; Write-Output 'frontend'
        if ((Get-Content .env | Where-Object { $_ -match '^COMPOSE_FILE=' }) -match 'compose.gpu') { Write-Output 'model-setup' }
    }
}
try {
    foreach ($mode in @('default', 'cpu', 'gpu')) {
        $dir = Join-Path $testRoot $mode
        New-Item -ItemType Directory -Path $dir -Force | Out-Null
        Copy-Item (Join-Path $root 'deploy.ps1') $dir
        Copy-Item (Join-Path $root '.env.docker.example') $dir
        $entry = Join-Path $dir 'deploy.ps1'
        $global:dockerCalls = @()
        $global:modelSetupFail = $false
        $selected = if ($mode -eq 'default') { 'gpu' } else { $mode }
        if ($mode -eq 'default') { $null = & $entry } else { $null = & $entry -Mode $mode }
        $before = [IO.File]::ReadAllText((Join-Path $dir '.env'))
        if ($before -notmatch '(?m)^JWT_SECRET=[0-9a-f]{64}\r?$') { throw 'Random secret was not generated' }
        $expected = if ($selected -eq 'gpu') { 'compose.yaml,compose.gpu.yaml' } else { 'compose.yaml' }
        if (($before -split "`n" | Where-Object { $_ -match '^COMPOSE_FILE=' }).Trim() -ne "COMPOSE_FILE=$expected") { throw 'Incorrect initial AI mode' }
        $null = & $entry -Pull
        if ([IO.File]::ReadAllText((Join-Path $dir '.env')) -ne $before) { throw 'Existing configuration changed' }
        $other = if ($selected -eq 'cpu') { 'gpu' } else { 'cpu' }
        $failed = $false
        try { $null = & $entry -Mode $other } catch { $failed = $true }
        if (-not $failed) { throw 'Mode mismatch should not overwrite existing configuration' }
        $prepared = [bool]($global:dockerCalls | Where-Object { $_ -eq 'compose --profile model-setup run --rm --no-deps model-setup' })
        if ($prepared -ne ($selected -eq 'gpu')) { throw 'Model setup must run only for GPU before startup' }
        if ($selected -eq 'gpu') {
            $global:dockerCalls = @(); $global:modelSetupFail = $true
            $failed = $false
            try { $null = & $entry } catch { $failed = $true }
            if (-not $failed) { throw 'Failed model preparation must stop deployment' }
            if ($global:dockerCalls | Where-Object { $_ -match '^compose (build|up)' }) { throw 'App must not start after model failure' }
            if ([IO.File]::ReadAllText((Join-Path $dir '.env')) -ne $before) { throw 'Model failure changed configuration' }
            $global:modelSetupFail = $false
        }
        Write-Output "PASS $mode installer: random secret, repeat/pull, mode protection"
    }
} finally {
    Set-Location $originalLocation
    Remove-Item Function:\docker
    if (Test-Path $testRoot) { Remove-Item $testRoot -Recurse -Force }
}
