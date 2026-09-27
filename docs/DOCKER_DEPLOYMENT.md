# Docker 部署与运维

面向把工具安装到自己服务器的使用者。浏览器共享同一个账号库和素材库，服务器保管所有视频和标注。当前只支持一个后端容器、一个 Uvicorn worker；SQLite、任务状态和锁不支持横向扩容。

## 1. 选择部署模式

| 模式 | 启动文件 | 可用功能 |
| --- | --- | --- |
| CPU 基础模式 | `compose.yaml` | 人工标注、逐帧审查、对比确认、YOLO/COCO 导出；不启用 AI Tracking |
| GPU 模式（默认） | 上述文件 + `compose.gpu.yaml` | 完整工作流和 SAM3 Tracking |

服务器安装 Docker + Compose 2.20+。Windows 使用 Docker Desktop 的 WSL2 Linux containers；Linux GPU 服务器还需 NVIDIA 驱动及 [NVIDIA Container Toolkit](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/latest/install-guide.html)。[Docker 的 GPU 支持说明](https://docs.docker.com/compose/how-tos/gpu-support/)列出了设备挂载条件。

客户端只需浏览器。首次部署脚本不传参数、或按模板配置 `.env` 时，默认选择 GPU 并启用 AI；先按第 3 节准备模型及驱动。仅需人工标注时显式执行 `bash deploy.sh cpu` / `.\deploy.ps1 -Mode cpu`，此模式不需要模型。缺少 GPU 或模型时会报错，不自动关闭 AI。一键脚本完成应用配置、镜像构建和启动，不代替操作系统、GPU 驱动或模型授权。

## 2. 首次启动

```bash
git clone https://github.com/huangwww11222/sperm-annotation.git
cd sperm-annotation
# 先把完整 SAM3 模型放入 models/sam3/，再启动
bash deploy.sh
```

Windows：

```powershell
.\deploy.ps1
```

PowerShell 如果阻止运行下载脚本，先检查脚本内容，再根据组织策略放行本次脚本；无需永久修改全局执行策略。

脚本首次生成 `.env`，使用独立随机密钥；重复执行保留配置。已有 CPU 配置不会因更新代码而自动切换，需按第 3 节明确修改 `.env`。成功时会等待 frontend/backend 健康并显示状态。访问 `http://服务器IP:8080`，先注册再登录，没有预设公共账号。服务器防火墙仅开放选定的 Web 端口；后端 3000 端口不映射到宿主机。

源码构建会下载基础镜像和依赖，耗时受网络影响。Python 镜像为 3.12，以满足固定 NumPy 依赖的 Python 要求；先从 CPU/CUDA wheel 源安装匹配的 torch/torchvision，再安装应用依赖。生产镜像不装 pytest；测试依赖单独在 `backend/requirements-dev.txt`。

失败时先看脚本输出和 `docker compose logs --tail=100 backend frontend`。初始化失败不清空数据，不通过换密钥或删数据库来“修复”。Docker Hub / PyPI / npm 连接失败时修复服务器网络、配置可信镜像源，或使用已经发布/离线导入的镜像。

## 3. GPU 与 SAM3 模型

GPU 当前发布目标为 Linux amd64 / Windows x64 WSL2，不提供 Apple Silicon 的 CUDA。驱动须支持选择的 CUDA PyTorch wheel；当前 GPU 默认 cu132。配置 GPU 前先验证宿主机 `nvidia-smi` 和 Docker GPU 访问。

从 [SAM3 官方模型页面](https://huggingface.co/facebook/sam3)申请访问、按其条款下载完整快照，放在：

```text
models/sam3/
  config.json
  preprocessor_config.json 或 processor_config.json
  model.safetensors 或全部权重分片与索引
  其他模型文件
```

也可从已能运行项目的机器复制完整快照；若原快照包含指向缓存目录的符号链接，需要连同实际文件复制，不能留下指向容器外的断链。不要只复制某个权重文件。

全新配置执行：

```bash
bash deploy.sh gpu
```

Windows 使用 `.\deploy.ps1 -Mode gpu`。模型不在默认目录时，先修改 `.env` 中的 `SAM3_MODEL_HOST_PATH` 后重新运行。配置可使用 Linux 绝对路径或 Windows 正斜杠路径，如 `D:/sperm-models/sam3`。

**已有 CPU 配置或旧版本配置升级**：保留 `.env` 的 JWT_SECRET 和 APP_DATA_ROOT，在 `.env` 设置：

```dotenv
COMPOSE_PATH_SEPARATOR=,
COMPOSE_FILE=compose.yaml,compose.gpu.yaml
SAM3_MODEL_HOST_PATH=./models/sam3
```

再运行 `bash deploy.sh gpu` / `.\deploy.ps1 -Mode gpu`。切回 CPU 则设置 `COMPOSE_FILE=compose.yaml`，并清除或改成 CPU 对应的 `BACKEND_IMAGE`。如果脚本检测到指定模式与旧 `.env` 不一致，会停止并说明如何修改，不默默覆盖配置。

启动前检查会验证目录可写、密钥有效、模型配置/权重分片完整，以及 CUDA 和 Transformers 类能否导入。失败记录 `deployment.preflight_failed`；成功记录 `deployment.ready`。这不等于真实推理已经验收，首次 Tracking 才加载权重，仍需用实际视频测试。

```bash
docker compose exec backend python -c "import torch; print(torch.__version__, torch.cuda.is_available()); print(torch.cuda.get_device_name(0) if torch.cuda.is_available() else 'CPU')"
```

## 4. 配置与持久化

`.env.docker.example` 是模板，实际配置仅放根目录 `.env`。脚本和 Compose 不读取 `backend/.env` 来部署容器；后者仅用于本机 Python 开发。不要将 `.env` 当 shell 脚本 source。

| 配置 | 默认/用途 |
| --- | --- |
| COMPOSE_FILE | 默认 compose.yaml,compose.gpu.yaml（AI）；仅人工模式为 compose.yaml |
| COMPOSE_PATH_SEPARATOR | 固定逗号，让文件列表在 Linux/Windows 一致 |
| WEB_PORT / WEB_BIND_ADDRESS | 8080 / 0.0.0.0；反向代理同机时可只绑定 127.0.0.1 |
| JWT_SECRET | 首次生成的随机密钥；长期保留，修改会使旧登录令牌失效 |
| APP_DATA_ROOT | ./runtime；可改为本机数据盘，不能把 SQLite 放在 SMB/NFS |
| SAM3_MODEL_HOST_PATH | ./models/sam3；只读挂载，GPU 必须存在 |
| SAM3_DTYPE / SAM3_TRACK_FRAMES | bfloat16 / 120；仅 GPU 启用推理 |
| PYTORCH_INDEX_URL | GPU wheel 源，默认 cu132；CPU 构建固定使用 CPU wheel |
| BACKEND_IMAGE / FRONTEND_IMAGE | 源码构建可留空；拉取预构建镜像时必须写实际发布的同版本名称 |

```text
runtime/
  database/app.db        账号、人工记录、审查、确认和导出任务
  database/logs/         review.log 及轮转日志
  storage/media/         原视频、seed、Tracking、工作区与帧缓存
  storage/datasets/      训练包
```

保持旧部署的 `/data/database/app.db` 和 `/data/storage` 挂载位置，新增 `APP_DATA_DIR=/data/database` 使业务日志同样持久化。Docker 标准输出日志限制为 10 MB × 3；业务文件日志也轮转。重建容器不会删除宿主机绑定目录。

## 5. 常用操作与故障定位

```bash
docker compose ps
docker compose logs --tail=100 backend frontend
docker compose logs -f backend
docker compose stop
docker compose start
```

`/api/health` 检查应用进程；`sam3.enabled` 区分是否启用 AI，`modelLoaded` 表示是否已实际加载。健康检查通过不代表 GPU 推理成功。CPU 模式的 AI 请求及追踪回退返回明确 503 提示，避免在未启用 AI 时先截断旧追踪结果；人工 seed JSON 保存仍可使用。

| 现象 | 首先检查 |
| --- | --- |
| list_annotations/source 参数异常 | 是否部署了合并修复后的版本，重建后端镜像；不要混用两份 db.py |
| Cannot install numpy / Python version | 后端基础镜像须 Python 3.12；不要改回 3.11 |
| 无法获取镜像令牌、连接重置、下载超时 | 镜像仓库/软件源网络，尚未运行到业务代码 |
| nvidia device driver / CUDA 不可用 | 驱动、Container Toolkit、GPU Compose 文件、CPU/GPU 镜像是否选对 |
| preflight_failed | 日志中具体缺失的密钥、挂载目录、模型配置或权重；不要只重启循环 |
| 502，尤其后端重建后 | backend 是否健康；执行 docker compose restart frontend 刷新 Nginx 上游 |
| 上传 413 | 默认 2 GiB；更大文件须同时调整 Nginx 和后端 MAX_VIDEO_BYTES |
| 保存失败 | 保留前端修改，记录请求号；检查 database/logs/review.log，按 [TESTING.md](TESTING.md) 排查 |
| 视频有标注但不能导出 | 先完成 B、C 显式完成操作；重新确认后不能使用旧版本训练包 |

## 6. 预构建镜像与离线部署

源码部署执行的是：

```bash
docker compose build --pull
docker compose up -d --no-build --wait
```

免构建部署需要维护者先发布镜像。仓库的 `.github/workflows/release-images.yml` 在推送 `vX.Y.Z` 标签后执行 CI，通过后发布三种 Linux amd64 镜像：

```text
ghcr.io/huangwww11222/sperm-annotation-backend:vX.Y.Z-cpu
ghcr.io/huangwww11222/sperm-annotation-backend:vX.Y.Z-gpu
ghcr.io/huangwww11222/sperm-annotation-frontend:vX.Y.Z
```

发布源码前运行 `python3 scripts/check_repository.py`。运行目录应从 Git 跟踪中移除但保留宿主机文件；`.gitignore` 不会自动移除已经跟踪的文件，也不会清除已推送的历史。如果旧提交含真实业务数据，应由维护者单独安排历史清理，不能把当前目录干净等同于历史无数据。不要在部署或文档整理时自动改写远端历史。

这些是命名规则，**不是已存在的版本声明**。维护者在 GitHub Packages 确认三个镜像构建成功并设为对使用者可访问；公共源码不自动意味着镜像包已经公开。参考 [GitHub 官方镜像发布说明](https://docs.github.com/en/actions/tutorials/publish-packages/publish-docker-images)。

部署方在 `.env` 填写实际版本对应的 BACKEND_IMAGE / FRONTEND_IMAGE，然后：

```bash
bash deploy.sh gpu --pull
# 仅人工 CPU 配置使用 bash deploy.sh cpu --pull
```

Windows：`.\deploy.ps1 -Mode gpu -Pull`。等价命令是 `docker compose pull` 后 `docker compose up -d --no-build --wait`。ARM64 机器目前从源码构建 CPU 版；发布工作流不承诺 ARM64 预构建镜像。

离线环境在联网机器 `docker save` 导出对应前后端镜像，到目标服务器 `docker load`；复制 compose.yaml、compose.gpu.yaml（需要 GPU 时）、实际 `.env` 和模型，执行 `docker compose up -d --no-build --wait`。不运行会联网拉取的 `--pull` 模式。CPU/GPU 镜像、平台和 `.env` 名称必须匹配。

## 7. 迁移现有数据库和 Storage

先停旧后端和新后端，完整备份，确认没有写入或 Tracking：

```bash
docker compose stop
```

复制旧 `backend/data/app.db` 到实际 `APP_DATA_ROOT/database/app.db`，复制整个旧 `backend/storage` 内容到 `APP_DATA_ROOT/storage`。SQLite 若仍存在有效 WAL，不能只拷主文件；优先正常停机后备份整个数据库目录。旧 `backend/track_data/<id>` 逐个迁移至 `storage/media/<id>`，不覆盖同名素材。

启动会增量建表，不删除 A/B/F 历史。缺少完整快照的旧任务保持只读，不能猜测补帧。已有数据若记录了跨机器绝对路径，需要确认原视频能按媒体 ID 找到后再做业务验收，不能以登录成功代替迁移成功。

## 8. 更新、备份与恢复

升级前安排停写，记录当前 Git 标签/镜像摘要及模型版本，并备份：实际 database 整个目录、storage 整个目录、`.env` 和模型来源。源码和数据目录分开存放；不要对业务目录执行 git clean。

升级：先备份，再更新到已验收的源码标签，执行部署脚本；预构建方式改 `.env` 镜像版本后 pull/up。新镜像包含新增迁移时，不保证旧程序能直接读取升级后的 DB；回滚时恢复同批次数据库与 storage，再启动旧镜像。

恢复：停服务，恢复完整备份，保持挂载路径与文件权限正确，再启动并抽查已有视频、审查/确认状态和训练包下载。

## 9. 交付与验收

- 全新机器能从 README 完成安装，启动日志无 preflight_failed。
- 注册、登录、真实视频上传、标注保存、重新打开可恢复。
- B 逐帧显式提交、C 显式完成、YOLO/COCO 图像标签一致；旧接口和旧最终版本不能绕过导出门禁。
- 重建容器后账号、工作区、最终版本、训练 ZIP 和日志保留。
- GPU 模式额外用真实视频验证 Tracking、异常暂停、续追及显存表现；CPU CI 不替代 GPU 验收。
- 共享团队使用边界见根 README；公开访问要配 HTTPS 和组织访问控制。

项目提供 `scripts/docker_smoke.py` 对隔离容器执行真实上传、完整工作流、训练包与重建恢复验收；复现环境与结果统一记录在 [TESTING.md](TESTING.md)。
