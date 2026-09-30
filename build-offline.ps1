param(
    [string]$Ref = 'HEAD',
    [string]$Version = '',
    [ValidateSet('gpu', 'cpu')][string]$Mode = 'gpu',
    [Alias('OutputDir')][string]$OutputDirectory = ''
)
# Windows PowerShell 5.1: only Git and Docker Desktop (Linux containers) required.
$ErrorActionPreference = 'Stop'
$originalLocation = Get-Location
$repoRoot = $PSScriptRoot
$buildRoot = Join-Path $repoRoot ('work/offline-build-' + [guid]::NewGuid().ToString('N'))
$buildLog = Join-Path $buildRoot 'build.log'
$stage = $null

function Invoke-PackageCommand {
    param([string]$Program, [string[]]$CommandArgs, [switch]$Capture)
    Add-Content -LiteralPath $buildLog -Value ('> ' + $Program + ' ' + ($CommandArgs -join ' ')) -Encoding UTF8
    $previousPreference = $ErrorActionPreference
    try {
        # Docker uses stderr for ordinary progress. PS 5.1 otherwise treats it
        # as NativeCommandError with Stop even when the native process succeeds.
        $ErrorActionPreference = 'Continue'
        if ($Capture) {
            $result = @(& $Program @CommandArgs 2>&1)
            $code = $LASTEXITCODE
            foreach ($line in $result) { Add-Content -LiteralPath $buildLog -Value ([string]$line) -Encoding UTF8 }
        } else {
            & $Program @CommandArgs 2>&1 | ForEach-Object {
                Add-Content -LiteralPath $buildLog -Value ([string]$_) -Encoding UTF8
                Write-Host ([string]$_)
            }
            $code = $LASTEXITCODE
        }
    } finally { $ErrorActionPreference = $previousPreference }
    if ($code -ne 0) { throw "$Program failed (exit $code). See $buildLog" }
    if ($Capture) { return ($result -join "`n").Trim() }
}

function Get-PackageImage {
    param([string]$Tag, [string]$Revision, [string]$ImageVersion, [string]$ImageMode)
    $json = Invoke-PackageCommand -Program docker -CommandArgs @('image', 'inspect', $Tag) -Capture
    $items = @($json | ConvertFrom-Json)
    if ($items.Count -ne 1) { throw "Expected one image for $Tag" }
    $item = $items[0]
    if ($item.Os -ne 'linux' -or $item.Architecture -ne 'amd64') { throw "Image must be linux/amd64: $Tag" }
    if ($item.Id -notmatch '^sha256:[a-f0-9]{64}$' -or [long]$item.Size -le 0) { throw "Invalid image identity: $Tag" }
    $labels = $item.Config.Labels
    if ($labels.'org.opencontainers.image.revision' -ne $Revision -or
        $labels.'org.opencontainers.image.version' -ne $ImageVersion -or
        $labels.'io.sperm-annotation.offline-contract' -ne '1' -or
        $labels.'io.sperm-annotation.mode' -ne $ImageMode) { throw "Image version labels do not match the selected source: $Tag" }
    return [ordered]@{ tag = $Tag; id = $item.Id; size = [long]$item.Size }
}

try {
    Set-Location $repoRoot
    New-Item -ItemType Directory -Path $buildRoot -Force | Out-Null
    foreach ($program in @('git', 'docker')) {
        if (-not (Get-Command $program -ErrorAction SilentlyContinue)) { throw "Install $program before preparing an offline package." }
    }
    if (-not $Ref -or $Ref.StartsWith('-')) { throw 'Ref must identify a Git commit, tag or branch.' }
    $dirty = Invoke-PackageCommand -Program git -CommandArgs @('status', '--porcelain', '--untracked-files=normal') -Capture
    if ($dirty) { throw 'The repository has uncommitted files. Commit or preserve them before packaging a release.' }
    # Resolve exactly once: subsequent archive/build steps never use a moving branch.
    $revision = Invoke-PackageCommand -Program git -CommandArgs @('rev-parse', '--verify', ($Ref + '^{commit}')) -Capture
    if ($revision -notmatch '^[a-f0-9]{40}$') { throw 'Could not resolve Ref to a full Git commit.' }
    $shallow = Invoke-PackageCommand -Program git -CommandArgs @('rev-parse', '--is-shallow-repository') -Capture
    if ($shallow -ne 'false') { throw 'A complete Git history is required. Run git fetch --unshallow in a shallow clone, then retry.' }
    $historyText = Invoke-PackageCommand -Program git -CommandArgs @('rev-list', $revision) -Capture
    $sourceHistory = @($historyText -split '\r?\n')
    if (-not $historyText -or $sourceHistory[0] -cne $revision -or
        ($sourceHistory | Where-Object { $_ -cnotmatch '^[a-f0-9]{40}$' }) -or
        @($sourceHistory | Select-Object -Unique).Count -ne $sourceHistory.Count) {
        throw 'Git history must contain the selected commit and its complete valid ancestor hashes.'
    }
    if (-not $Version) { $Version = 'git-' + $revision.Substring(0, 12) }
    if ($Version -notmatch '^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,63}$') { throw 'Version must be 1-64 letters, digits, dots, underscores or hyphens, starting with a letter or digit.' }
    if (-not $OutputDirectory) { $OutputDirectory = Join-Path $repoRoot "output/sperm-annotation-$Version-$Mode-linux-amd64" }
    $OutputDirectory = [IO.Path]::GetFullPath($OutputDirectory)
    if (Test-Path -LiteralPath $OutputDirectory) { throw "Output already exists; use another directory: $OutputDirectory" }
    $osType = Invoke-PackageCommand -Program docker -CommandArgs @('info', '--format', '{{.OSType}}') -Capture
    if ($osType -ne 'linux') { throw 'Docker Desktop must run Linux containers.' }
    $parent = Split-Path $OutputDirectory -Parent
    New-Item -ItemType Directory -Path $parent -Force | Out-Null
    $stage = Join-Path $parent ('.offline-partial-' + [guid]::NewGuid().ToString('N'))
    New-Item -ItemType Directory -Path $stage | Out-Null
    $archive = Join-Path $buildRoot 'source.zip'
    $source = Join-Path $buildRoot 'source'
    Invoke-PackageCommand -Program git -CommandArgs @('archive', '--format=zip', '--output', $archive, $revision)
    Expand-Archive -LiteralPath $archive -DestinationPath $source

    $packageFiles = @('update-offline.sh', 'export-audit.sh', 'scripts/offline_update.py', 'scripts/offline_legacy.json', 'compose.yaml', 'compose.gpu.yaml', '.env.docker.example')
    foreach ($relative in $packageFiles + @('backend/Dockerfile', 'frontend/Dockerfile', '.dockerignore')) {
        if (-not (Test-Path -LiteralPath (Join-Path $source $relative) -PathType Leaf)) {
            throw "Selected revision does not contain $relative. Choose a release that includes the offline update tools."
        }
    }
    Write-Host "Preparing $Version ($revision), $Mode, linux/amd64. Model weights are reused from the hospital installation."
    $backendTag = "sperm-annotation-backend:$Version-$Mode"
    $frontendTag = "sperm-annotation-frontend:$Version"
    $labels = @('--label', "org.opencontainers.image.revision=$revision", '--label', "org.opencontainers.image.version=$Version",
        '--label', 'org.opencontainers.image.source=https://github.com/huangwww11222/sperm-annotation',
        '--label', 'io.sperm-annotation.offline-contract=1', '--label', "io.sperm-annotation.mode=$Mode")
    $index = if ($Mode -eq 'gpu') { 'https://download.pytorch.org/whl/cu132' } else { 'https://download.pytorch.org/whl/cpu' }
    Invoke-PackageCommand -Program docker -CommandArgs (@('build', '--pull', '--platform', 'linux/amd64', '-f', (Join-Path $source 'backend/Dockerfile'),
        '--build-arg', "PYTORCH_INDEX_URL=$index", '--build-arg', "APP_RELEASE_REVISION=$revision", '-t', $backendTag) + $labels + @($source))
    Invoke-PackageCommand -Program docker -CommandArgs (@('build', '--pull', '--platform', 'linux/amd64', '-f', (Join-Path $source 'frontend/Dockerfile'),
        '-t', $frontendTag) + $labels + @($source))
    $backend = Get-PackageImage -Tag $backendTag -Revision $revision -ImageVersion $Version -ImageMode $Mode
    $frontend = Get-PackageImage -Tag $frontendTag -Revision $revision -ImageVersion $Version -ImageMode $Mode
    Invoke-PackageCommand -Program docker -CommandArgs @('save', '--output', (Join-Path $stage 'images.tar'), $backendTag, $frontendTag)
    if (-not (Test-Path -LiteralPath (Join-Path $stage 'images.tar')) -or (Get-Item -LiteralPath (Join-Path $stage 'images.tar')).Length -eq 0) {
        throw 'Docker did not produce a nonempty images.tar.'
    }
    # A second packager or manual retag must not silently swap images while save runs.
    $savedBackend = Get-PackageImage -Tag $backendTag -Revision $revision -ImageVersion $Version -ImageMode $Mode
    $savedFrontend = Get-PackageImage -Tag $frontendTag -Revision $revision -ImageVersion $Version -ImageMode $Mode
    if ($savedBackend.id -ne $backend.id -or $savedFrontend.id -ne $frontend.id) {
        throw 'An image tag changed during export. Retry without concurrent builds using the same version.'
    }
    foreach ($relative in $packageFiles) {
        $destination = Join-Path $stage $relative
        New-Item -ItemType Directory -Path (Split-Path $destination -Parent) -Force | Out-Null
        Copy-Item -LiteralPath (Join-Path $source $relative) -Destination $destination
    }
    $hashes = [ordered]@{}
    foreach ($relative in @('images.tar') + $packageFiles) {
        $hashes[$relative] = (Get-FileHash -LiteralPath (Join-Path $stage $relative) -Algorithm SHA256).Hash.ToLowerInvariant()
    }
    $manifest = [ordered]@{
        format = 1; version = $Version; revision = $revision; sourceHistory = $sourceHistory; createdAt = [DateTime]::UtcNow.ToString('o')
        platform = 'linux/amd64'; mode = $Mode; contract = 1; fromContracts = @(1)
        images = [ordered]@{ backend = $backend; frontend = $frontend }; files = $hashes
    }
    # UTF-8 without BOM is consumed by Linux's standard JSON reader.
    [IO.File]::WriteAllText((Join-Path $stage 'manifest.json'), ($manifest | ConvertTo-Json -Depth 10) + "`n", (New-Object Text.UTF8Encoding($false)))
    Move-Item -LiteralPath $stage -Destination $OutputDirectory
    $stage = $null
    Write-Host "Complete offline update package: $OutputDirectory"
    Write-Host "Transfer this entire directory to the hospital server. Build log: $buildLog"
    Write-Host 'On the server: bash /path/to/package/update-offline.sh /path/to/existing-deployment'
} catch {
    Write-Host "Packaging failed. Log: $buildLog"
    if ($stage) { Write-Host "Incomplete files (not an installable package): $stage" }
    throw
} finally { Set-Location $originalLocation }
