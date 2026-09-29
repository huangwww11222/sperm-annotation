#!/usr/bin/env bash
# Packaged by build_compat_update.py. Uses only the installed local Docker images.
set -Eeuo pipefail
umask 077
if [[ $# -ne 1 ]]; then
  echo "用法：bash $0 /opt/sperm-annotation/sperm-annotation-offline"
  exit 2
fi
SCRIPT=$(realpath "$0")
DEPLOY=$(cd "$1" && pwd)
cd "$DEPLOY"
[[ -f .env && -f compose.yaml ]] || { echo "目录中缺少 .env 或 compose.yaml"; exit 2; }
for command in docker python3 sha256sum base64 tar; do command -v "$command" >/dev/null; done
STAMP=$(date +%Y%m%d-%H%M%S)-$$
BACKUP="$DEPLOY/update-backups/$STAMP"
mkdir -p "$BACKUP"
LOG="$BACKUP/update.log"
exec > >(tee -a "$LOG") 2>&1
echo "检查离线更新环境。日志：$LOG"
COMPOSE=(docker compose --env-file .env -f compose.yaml)
# Use the exact images of the running services, not the shell's defaults.
BACKEND_CID=$("${COMPOSE[@]}" ps -q backend)
FRONTEND_CID=$("${COMPOSE[@]}" ps -q frontend)
[[ -n "$BACKEND_CID" && -n "$FRONTEND_CID" ]] || { echo "未找到本目录已运行的前后端容器，未更新。"; exit 2; }
if docker inspect --format '{{json .Config.Env}}' "$BACKEND_CID" | python3 -c 'import json,sys; sys.exit(0 if "SAM3_ENABLED=true" in json.load(sys.stdin) else 1)'; then
  [[ -f compose.gpu.yaml ]] || { echo "当前启用了 AI，但缺少 compose.gpu.yaml，未更新。"; exit 2; }
  COMPOSE+=(-f compose.gpu.yaml)
fi
export BACKEND_IMAGE=$(docker inspect --format '{{.Config.Image}}' "$BACKEND_CID")
export FRONTEND_IMAGE=$(docker inspect --format '{{.Config.Image}}' "$FRONTEND_CID")
for ref in "$BACKEND_IMAGE" "$FRONTEND_IMAGE"; do
  [[ "$ref" != *@* && "$ref" != sha256:* ]] || { echo "当前使用摘要固定镜像，请由部署人员更新镜像引用。"; exit 2; }
done
WORK=$(mktemp -d "$BACKUP/payload.XXXXXX")
CHANGED=false
recover() {
  local code=$?
  trap - ERR
  echo "更新失败（退出码 ${code}），请查看 ${LOG}。"
  if [[ "$CHANGED" == true ]]; then
    echo "正在恢复原镜像…"
    bash "$BACKUP/rollback.sh" || echo "自动恢复失败，请保留日志并联系部署人员。"
  fi
  exit "$code"
}
trap recover ERR
awk '/^__COMPAT_PAYLOAD_BELOW__$/ {found=1;next} found {print}' "$SCRIPT" | base64 -d | tar -xz -C "$WORK"
(cd "$WORK" && sha256sum -c SHA256SUMS)
# The backup directory is private (umask 077), but nginx workers must be able
# to traverse the static directories copied into the image.
chmod -R a+rX "$WORK/frontend/dist"
docker cp "$BACKEND_CID:/app/backend/app/main.py" "$WORK/installed-main.py"
python3 - "$WORK" <<'PY'
import hashlib, json, pathlib, sys
root = pathlib.Path(sys.argv[1])
manifest = json.loads((root/'manifest.json').read_text())
actual = hashlib.sha256((root/'installed-main.py').read_bytes()).hexdigest()
if actual not in manifest['acceptedBackendHashes']:
    raise SystemExit('服务器后端版本与本补丁不匹配，已停止且未修改服务。请提供本日志核对版本。实际 SHA256：' + actual)
PY
# All runtime data must already be persistent. Reject code mounts which would
# hide the patched image, without printing environment variables or secrets.
docker inspect "$BACKEND_CID" "$FRONTEND_CID" | python3 -c '
import json,sys,subprocess
b,f=json.load(sys.stdin)
mounts=b["Mounts"]
assert all(any(p==m["Destination"] or p.startswith(m["Destination"].rstrip("/")+"/") for m in mounts) for p in ["/data/database","/data/storage"]), "数据库/视频未持久化，停止更新"
for c,path in [(b,"/app/backend/app/main.py"),(f,"/usr/share/nginx/html")]:
 assert not any(path==m["Destination"] or path.startswith(m["Destination"].rstrip("/")+"/") for m in c["Mounts"]), "代码被宿主机挂载覆盖，停止更新"
expected=json.loads(subprocess.check_output(sys.argv[1:]))["services"]
for name,c in [("backend",b),("frontend",f)]:
 runtime_env=dict(item.split("=",1) for item in c["Config"]["Env"] if "=" in item)
 for key,value in expected[name].get("environment",{}).items():
  assert value is not None and runtime_env.get(key)==str(value), "Compose 环境配置与运行容器不一致，停止更新："+name+"/"+key
 for v in expected[name].get("volumes",[]):
  assert v["type"]=="bind" and any(m.get("Source")==v["source"] and m["Destination"]==v["target"] and m.get("RW",True)==(not v.get("read_only",False)) for m in c["Mounts"]), "Compose 挂载与运行容器不一致，停止更新："+name
' "${COMPOSE[@]}" config --format json
docker exec "$BACKEND_CID" python -c '
import json,urllib.request
s=json.load(urllib.request.urlopen("http://127.0.0.1:3000/api/health",timeout=10))["sam3"]
assert not (s.get("running") or s.get("queued") or s.get("trackingBusy")), "AI 正在运行，请等任务结束再更新"
'
OLD_BACKEND="annotation-update-backup:backend-$STAMP"
OLD_FRONTEND="annotation-update-backup:frontend-$STAMP"
docker image tag "$(docker inspect --format '{{.Image}}' "$BACKEND_CID")" "$OLD_BACKEND"
docker image tag "$(docker inspect --format '{{.Image}}' "$FRONTEND_CID")" "$OLD_FRONTEND"
{
  echo '#!/usr/bin/env bash'
  echo 'set -euo pipefail'
  printf 'cd %q\n' "$DEPLOY"
  printf 'export BACKEND_IMAGE=%q FRONTEND_IMAGE=%q\n' "$BACKEND_IMAGE" "$FRONTEND_IMAGE"
  printf 'docker image tag %q %q\n' "$OLD_BACKEND" "$BACKEND_IMAGE"
  printf 'docker image tag %q %q\n' "$OLD_FRONTEND" "$FRONTEND_IMAGE"
  printf '%q ' "${COMPOSE[@]}" up -d --no-deps --no-build --pull never --force-recreate --wait --wait-timeout 180 backend frontend
  echo
} > "$BACKUP/rollback.sh"
NEW_BACKEND="annotation-compat:backend-$STAMP"
NEW_FRONTEND="annotation-compat:frontend-$STAMP"
echo "使用本地原镜像构建补丁，不下载依赖或模型。"
# The legacy builder is shipped with the server's Docker; it needs no buildx
# bootstrap image or Dockerfile frontend download. Dockerfiles contain COPY only.
DOCKER_BUILDKIT=0 docker build --pull=false --network=none --build-arg "BASE_IMAGE=$OLD_BACKEND" -t "$NEW_BACKEND" "$WORK/backend"
DOCKER_BUILDKIT=0 docker build --pull=false --network=none --build-arg "BASE_IMAGE=$OLD_FRONTEND" -t "$NEW_FRONTEND" "$WORK/frontend"
docker run --rm --network none --entrypoint python "$NEW_BACKEND" -c 'from pathlib import Path; p=Path("/app/backend/app/main.py"); compile(p.read_text(),str(p),"exec")'
CHANGED=true
docker image tag "$NEW_BACKEND" "$BACKEND_IMAGE"
docker image tag "$NEW_FRONTEND" "$FRONTEND_IMAGE"
"${COMPOSE[@]}" up -d --no-deps --no-build --pull never --force-recreate --wait --wait-timeout 180 backend frontend
"${COMPOSE[@]}" exec -T frontend wget -qO- http://127.0.0.1/ > /dev/null
"${COMPOSE[@]}" exec -T frontend wget -qO- http://127.0.0.1/api/health
echo
echo "更新完成。请在浏览器按 Ctrl+F5，再验收上传、换帧、送审和删除。"
echo "原镜像已保留。回滚命令：bash '$BACKUP/rollback.sh'"
echo "本更新保留 .env、账号、数据库、视频和模型；不重启或修改其他服务。"
exit 0
