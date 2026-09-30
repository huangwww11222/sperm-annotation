#!/usr/bin/env bash
# Run from any directory; no network, database copies, service restarts or writes.
set -euo pipefail
if [[ $# -lt 1 || "$1" == '--help' || "$1" == '-h' ]]; then
  cat <<'HELP'
用法：bash export-audit.sh /原部署目录 [筛选参数] --output /本机输出.zip
      bash export-audit.sh /原部署目录 --list
      bash export-audit.sh /原部署目录 --lookup 图片原文件名
筛选：--dataset-id train_xxx（可重复）、--media-id 视频ID、
      --since 2026-09-30T00:00:00+08:00、--until 2026-10-01T00:00:00+08:00
      --include-failed（包含失败尝试；默认只提取成功生成包）
无需停止服务。完整证据保留在数据库；旧任务无固定审计时明确显示未覆盖。
HELP
  exit 0
fi
deployment=$(cd -- "$1" && pwd -P)
shift
args=()
output=''
text_mode=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --output) [[ $# -ge 2 ]] || { echo '--output 缺少路径' >&2; exit 2; }; output=$2; shift 2 ;;
    --list|--lookup) text_mode=1; args+=("$1"); shift ;;
    --db|--db=*) echo 'Docker 提取使用容器实际数据库，不能覆盖 --db' >&2; exit 2 ;;
    *) args+=("$1"); shift ;;
  esac
done
if [[ $text_mode -eq 0 && -z "$output" ]]; then
  output="quality-audit-$(date +%Y%m%d-%H%M%S)-$$.zip"
fi
invoke() {
  if [[ -f "$deployment/manage-offline.sh" ]]; then
    bash "$deployment/manage-offline.sh" exec -T backend python -m app.audit_export ${args[@]+"${args[@]}"}
  else
    [[ -f "$deployment/.env" ]] || { echo '部署目录缺少 .env；请指向实际原部署目录' >&2; return 1; }
    # .env chooses the installed CPU/GPU Compose configuration. Never source it as shell code.
    (cd -- "$deployment" && docker compose --project-directory "$deployment" --env-file .env exec -T backend python -m app.audit_export ${args[@]+"${args[@]}"})
  fi
}
if [[ $text_mode -eq 1 ]]; then
  [[ -z "$output" ]] || { echo '--list/--lookup 输出 JSON，请用 > 文件.json 保存' >&2; exit 2; }
  invoke
else
  [[ ! -e "$output" ]] || { echo "输出已存在：$output" >&2; exit 2; }
  parent=$(dirname -- "$output")
  temp=$(mktemp "$parent/.quality-audit.XXXXXXXX")
  trap 'rm -f -- "$temp"' EXIT
  if invoke > "$temp"; then
    :
  else
    status=$?
    exit "$status"
  fi
  # Hard-link is atomic and refuses to replace an existing file on this filesystem.
  ln -- "$temp" "$output"
  echo "审计包已保存：$output" >&2
fi
