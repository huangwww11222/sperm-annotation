param(
    [ValidateSet('', 'cpu', 'gpu')][string]$Mode = '',
    [switch]$Pull
)
$ErrorActionPreference = 'Stop'
Set-Location $PSScriptRoot
function Invoke-Docker {
    param([string[]]$DockerArgs)
    & docker @DockerArgs
    if ($LASTEXITCODE -ne 0) { throw 'Docker 命令失败；检查输出及 docker compose logs --tail=100 backend frontend。配置和数据均保留。' }
}
if (-not (Get-Command docker -ErrorAction SilentlyContinue)) { throw '请先安装 Docker Desktop（WSL2 Linux containers）。' }
Invoke-Docker -DockerArgs @('info', '--format', '{{.OSType}}')
Invoke-Docker -DockerArgs @('compose', 'version')
if (-not (Test-Path .env)) {
    if (-not $Mode) { $Mode = 'cpu' }
    $files = if ($Mode -eq 'gpu') { 'compose.yaml,compose.gpu.yaml' } else { 'compose.yaml' }
    $bytes = New-Object byte[] 32
    $rng = [Security.Cryptography.RandomNumberGenerator]::Create()
    try { $rng.GetBytes($bytes) } finally { $rng.Dispose() }
    $key = [BitConverter]::ToString($bytes).Replace('-', '').ToLowerInvariant()
    $text = [IO.File]::ReadAllText((Join-Path $PSScriptRoot '.env.docker.example'))
    $text = [regex]::Replace($text, '(?m)^JWT_SECRET=.*$', "JWT_SECRET=$key")
    $text = [regex]::Replace($text, '(?m)^COMPOSE_FILE=.*$', "COMPOSE_FILE=$files")
    $stream = [IO.File]::Open((Join-Path $PSScriptRoot '.env'), [IO.FileMode]::CreateNew)
    try { $encoded = [Text.Encoding]::UTF8.GetBytes($text); $stream.Write($encoded, 0, $encoded.Length) } finally { $stream.Dispose() }
    $key = $null; $text = $null
    Write-Host "已生成 .env（$Mode 模式）；请长期保留，不要提交到仓库。"
} elseif ($Mode) {
    $expected = if ($Mode -eq 'gpu') { 'compose.yaml,compose.gpu.yaml' } else { 'compose.yaml' }
    $actual = (Get-Content .env | Where-Object { $_ -match '^COMPOSE_FILE=' }) -replace '^COMPOSE_FILE=', ''
    if ($actual -ne $expected) { throw "已有 .env 未被覆盖。请设置 COMPOSE_PATH_SEPARATOR=, 和 COMPOSE_FILE=$expected 后重试。" }
}
Invoke-Docker -DockerArgs @('compose', 'config', '--quiet')
if ($Pull) { Invoke-Docker -DockerArgs @('compose', 'pull') } else { Invoke-Docker -DockerArgs @('compose', 'build', '--pull') }
Invoke-Docker -DockerArgs @('compose', 'up', '-d', '--no-build', '--wait', '--wait-timeout', '180')
Invoke-Docker -DockerArgs @('compose', 'ps')
Write-Host '部署已就绪。默认访问 http://服务器IP:8080，端口以 .env 的 WEB_PORT 为准。首次使用请注册账号。'
