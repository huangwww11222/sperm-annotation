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
    if (-not $Mode) { $Mode = 'gpu' }
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
    if ($Mode -eq 'gpu') { Write-Host '默认启用 SAM3 AI：请准备 NVIDIA GPU、容器 GPU 支持；模型将从本仓库 Release 自动下载并校验，自定义路径在 .env 设置 SAM3_MODEL_HOST_PATH。缺少条件时部署会报错，不自动关闭 AI。' }
    Write-Host "已生成 .env（$Mode 模式）；请长期保留，不要提交到仓库。"
} elseif ($Mode) {
    $expected = if ($Mode -eq 'gpu') { 'compose.yaml,compose.gpu.yaml' } else { 'compose.yaml' }
    $actual = (Get-Content .env | Where-Object { $_ -match '^COMPOSE_FILE=' }) -replace '^COMPOSE_FILE=', ''
    if ($actual -ne $expected) { throw "已有 .env 未被覆盖。请设置 COMPOSE_PATH_SEPARATOR=, 和 COMPOSE_FILE=$expected 后重试。" }
}
Invoke-Docker -DockerArgs @('compose', 'config', '--quiet')
$services = & docker compose --profile model-setup config --services
if ($LASTEXITCODE -ne 0) { throw 'Compose 服务解析失败，模型和业务数据均保留。' }
if ($services -contains 'model-setup') { Invoke-Docker -DockerArgs @('compose', '--profile', 'model-setup', 'run', '--rm', '--no-deps', 'model-setup') }
if ($Pull) { Invoke-Docker -DockerArgs @('compose', 'pull') } else { Invoke-Docker -DockerArgs @('compose', 'build', '--pull') }
Invoke-Docker -DockerArgs @('compose', 'up', '-d', '--no-build', '--wait', '--wait-timeout', '180')
Invoke-Docker -DockerArgs @('compose', 'ps')
Write-Host '部署已就绪。默认访问 http://服务器IP:8080，端口以 .env 的 WEB_PORT 为准。首次使用请注册账号。'
