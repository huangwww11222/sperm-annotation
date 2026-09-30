# Packaging contract: fake Git and Docker; no network, images or business data.
$ErrorActionPreference = 'Stop'
$originalLocation = Get-Location
$root = Split-Path $PSScriptRoot -Parent
$testRoot = Join-Path $root ('work/offline-package-tests-' + [guid]::NewGuid().ToString('N'))
Add-Type -AssemblyName System.IO.Compression.FileSystem

function Assert-Package {
    param([bool]$Condition, [string]$Message)
    if (-not $Condition) { throw $Message }
}
function global:git {
    $global:LASTEXITCODE = 0
    $global:packageGitCalls += ,@($args)
    switch ($args[0]) {
        'status' { if ($global:packageCase -eq 'dirty') { ' M frontend/src/main.ts' } }
        'rev-parse' {
            if ($args[1] -eq '--is-shallow-repository') {
                if ($global:packageCase -eq 'shallow-clone') { 'true' } else { 'false' }
            } else { 'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa' }
        }
        'rev-list' {
            if ($global:packageCase -eq 'missing-history') { return }
            if ($global:packageCase -eq 'wrong-history-tip') { 'c' * 40 } else { 'a' * 40 }
            if ($global:packageCase -eq 'invalid-history') { 'not-a-full-commit' } else { 'd' * 40 }
        }
        'archive' {
            $archive = $args[[Array]::IndexOf($args, '--output') + 1]
            [IO.Compression.ZipFile]::CreateFromDirectory($global:packageSource, $archive)
        }
        default { throw "Unexpected mock Git command: $args" }
    }
}
function global:docker {
    $global:LASTEXITCODE = 0
    $global:packageDockerCalls += ,@($args)
    switch ($args[0]) {
        'info' { if ($global:packageCase -eq 'windows-engine') { 'windows' } else { 'linux' } }
        'build' {
            if ($global:packageCase -eq 'build-failed') { $global:LASTEXITCODE = 42; 'simulated build failure'; return }
            $tag = $args[[Array]::IndexOf($args, '-t') + 1]
            $labels = @{}
            for ($i = 0; $i -lt $args.Count; $i++) {
                if ($args[$i] -eq '--label') {
                    $parts = $args[$i + 1] -split '=', 2
                    $labels[$parts[0]] = $parts[1]
                }
            }
            $global:packageImages[$tag] = [ordered]@{
                Id = 'sha256:' + ('b' * 64); Size = 12345; Os = 'linux'; Architecture = 'amd64'
                Config = @{ Labels = $labels }
            }
        }
        'image' {
            $image = $global:packageImages[$args[2]]
            if ($global:packageCase -eq 'wrong-platform') { $image.Architecture = 'arm64' }
            if ($global:packageCase -eq 'wrong-label') { $image.Config.Labels['org.opencontainers.image.revision'] = 'c' * 40 }
            if ($global:packageCase -eq 'invalid-image') { $image.Id = 'missing' }
            ConvertTo-Json -InputObject @($image) -Depth 10 -Compress
        }
        'save' {
            $file = $args[[Array]::IndexOf($args, '--output') + 1]
            if ($global:packageCase -eq 'empty-save') { [IO.File]::WriteAllText($file, '') }
            else { [IO.File]::WriteAllText($file, 'mock docker images archive') }
            if ($global:packageCase -eq 'retag-during-save') {
                foreach ($image in $global:packageImages.Values) { $image.Id = 'sha256:' + ('c' * 64) }
            }
        }
        default { throw "Unexpected mock Docker command: $args" }
    }
}

try {
    New-Item -ItemType Directory -Path $testRoot | Out-Null
    foreach ($case in @('gpu', 'cpu', 'explicit-ref', 'dirty', 'existing-output', 'windows-engine', 'missing-source', 'build-failed', 'wrong-platform', 'wrong-label', 'invalid-image', 'empty-save', 'invalid-ref', 'invalid-version', 'retag-during-save', 'missing-history', 'invalid-history', 'wrong-history-tip', 'shallow-clone')) {
        $global:packageCase = $case
        $global:packageGitCalls = @(); $global:packageDockerCalls = @(); $global:packageImages = @{}
        $caseRoot = Join-Path $testRoot $case
        $global:packageSource = Join-Path $caseRoot 'committed'
        $checkout = Join-Path $caseRoot 'checkout'
        $output = Join-Path $caseRoot 'package'
        New-Item -ItemType Directory -Path $checkout, $global:packageSource -Force | Out-Null
        Copy-Item -LiteralPath (Join-Path $root 'build-offline.ps1') -Destination $checkout
        # Sentinels in checkout must never enter the archived build context/package.
        [IO.File]::WriteAllText((Join-Path $checkout '.env'), 'JWT_SECRET=do-not-package')
        [IO.File]::WriteAllText((Join-Path $checkout 'private-video.mp4'), 'do-not-package')
        foreach ($relative in @('update-offline.sh', 'export-audit.sh', 'scripts/offline_update.py', 'scripts/offline_legacy.json', 'compose.yaml', 'compose.gpu.yaml', '.env.docker.example', 'backend/Dockerfile', 'frontend/Dockerfile', '.dockerignore')) {
            if ($case -eq 'missing-source' -and $relative -eq 'scripts/offline_update.py') { continue }
            $file = Join-Path $global:packageSource $relative
            New-Item -ItemType Directory -Path (Split-Path $file -Parent) -Force | Out-Null
            [IO.File]::WriteAllText($file, "committed source: $relative`n")
        }
        if ($case -eq 'existing-output') {
            New-Item -ItemType Directory -Path $output | Out-Null
            [IO.File]::WriteAllText((Join-Path $output 'keep.txt'), 'existing package')
        }
        $entry = Join-Path $checkout 'build-offline.ps1'
        $success = $case -in @('gpu', 'cpu', 'explicit-ref')
        $arguments = @{ OutputDirectory = $output }
        if ($case -eq 'cpu') { $arguments.Mode = 'cpu' }
        if ($case -eq 'explicit-ref') { $arguments.Ref = 'v1.2.3'; $arguments.Version = 'v1.2.3' }
        if ($case -eq 'invalid-ref') { $arguments.Ref = '--help' }
        if ($case -eq 'invalid-version') { $arguments.Version = '../bad-version' }
        $failed = $false
        try { $null = & $entry @arguments } catch { $failed = $true }
        Assert-Package ($failed -ne $success) "Unexpected result for $case"
        if ($success) {
            $manifestPath = Join-Path $output 'manifest.json'
            $manifest = Get-Content -LiteralPath $manifestPath -Raw | ConvertFrom-Json
            $mode = if ($case -eq 'cpu') { 'cpu' } else { 'gpu' }
            $version = if ($case -eq 'explicit-ref') { 'v1.2.3' } else { 'git-aaaaaaaaaaaa' }
            Assert-Package ($manifest.revision -ceq ('a' * 40)) 'Revision must be the resolved full commit'
            Assert-Package (@($manifest.sourceHistory).Count -eq 2 -and $manifest.sourceHistory[0] -ceq ('a' * 40) -and $manifest.sourceHistory[1] -ceq ('d' * 40)) 'Package must preserve the complete pinned commit history'
            Assert-Package ($manifest.version -ceq $version) 'Wrong release version'
            Assert-Package ($manifest.mode -ceq $mode -and $manifest.platform -ceq 'linux/amd64') 'Wrong default GPU mode or platform'
            Assert-Package ($manifest.contract -eq 1 -and @($manifest.fromContracts).Count -eq 1 -and $manifest.fromContracts[0] -eq 1) 'Wrong update contract'
            Assert-Package ($manifest.images.backend.tag -ceq "sperm-annotation-backend:$version-$mode") 'Wrong backend tag'
            Assert-Package ($manifest.images.frontend.tag -ceq "sperm-annotation-frontend:$version") 'Wrong frontend tag'
            foreach ($property in $manifest.files.PSObject.Properties) {
                $hash = (Get-FileHash -LiteralPath (Join-Path $output $property.Name) -Algorithm SHA256).Hash.ToLowerInvariant()
                Assert-Package ($hash -ceq $property.Value) "Bad checksum: $($property.Name)"
            }
            $archiveCalls = @($global:packageGitCalls | Where-Object { $_[0] -eq 'archive' })
            Assert-Package ($archiveCalls.Count -eq 1 -and $archiveCalls[0][-1] -ceq ('a' * 40)) 'Archive must use pinned SHA, never moving ref'
            $historyCalls = @($global:packageGitCalls | Where-Object { $_[0] -eq 'rev-list' })
            Assert-Package ($historyCalls.Count -eq 1 -and $historyCalls[0][1] -ceq ('a' * 40)) 'History must use the same pinned SHA as the source archive'
            $buildCalls = @($global:packageDockerCalls | Where-Object { $_[0] -eq 'build' })
            Assert-Package ($buildCalls.Count -eq 2) 'Both complete images must be built'
            foreach ($call in $buildCalls) {
                Assert-Package ($call -contains 'linux/amd64') 'Build must set target architecture explicitly'
                Assert-Package ($call[-1] -ne $checkout) 'Build must use the committed archive'
                Assert-Package (-not (Test-Path (Join-Path $call[-1] '.env'))) 'Secret entered build context'
            }
            $expectedIndex = if ($mode -eq 'cpu') { '/cpu' } else { '/cu132' }
            Assert-Package ([bool]($buildCalls[0] | Where-Object { $_ -eq "PYTORCH_INDEX_URL=https://download.pytorch.org/whl$expectedIndex" })) 'Incorrect PyTorch runtime'
            Assert-Package (-not (Test-Path (Join-Path $output '.env'))) 'Secret entered package'
            Assert-Package (-not (Test-Path (Join-Path $output 'private-video.mp4'))) 'Business data entered package'
            $bytes = [IO.File]::ReadAllBytes($manifestPath)
            Assert-Package ($bytes[0] -eq 123) 'Manifest must be UTF-8 without BOM'
        } elseif ($case -eq 'existing-output') {
            Assert-Package ((Get-Content (Join-Path $output 'keep.txt') -Raw) -ceq 'existing package') 'Existing output was modified'
        } else {
            Assert-Package (-not (Test-Path $output)) 'Failed package was published'
            if ($case -in @('dirty', 'windows-engine', 'missing-source', 'missing-history', 'invalid-history', 'wrong-history-tip', 'shallow-clone')) {
                Assert-Package (-not [bool]($global:packageDockerCalls | Where-Object { $_[0] -eq 'build' })) 'Preflight failure still built images'
            }
        }
        Write-Output "PASS offline package $case"
    }
} finally {
    Set-Location $originalLocation
    Remove-Item Function:\git
    Remove-Item Function:\docker
    if (Test-Path $testRoot) { Remove-Item $testRoot -Recurse -Force }
    $global:LASTEXITCODE = 0
}
