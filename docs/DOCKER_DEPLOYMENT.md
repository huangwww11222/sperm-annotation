# Docker 部署与运维

面向把工具安装到自己服务器的使用者。浏览器共享同一个账号库和素材库，服务器保管所有视频和标注。当前只支持一个后端容器、一个 Uvicorn worker；SQLite、任务状态和锁不支持横向扩容。

## 1. 选择部署模式

| 模式 | 启动文件 | 可用功能 |
| --- | --- | --- |
| CPU 基础模式 | `compose.yaml` | 人工标注、逐帧审查、对比确认、YOLO/COCO 导出；不启用 AI Tracking |
| GPU 模式（默认） | 上述文件 + `compose.gpu.yaml` | 完整工作流和 SAM3 Tracking |

服务器安装 Docker + Compose 2.20+。Windows 使用 Docker Desktop 的 WSL2 Linux containers；Linux GPU 服务器还需 NVIDIA 驱动及 [NVIDIA Container Toolkit](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/latest/install-guide.html)。[Docker 的 GPU 支持说明](https://docs.docker.com/compose/how-tos/gpu-support/)列出了设备挂载条件。

客户端只需浏览器。首次部署脚本不传参数、或按模板配置 `.env` 时，默认选择 GPU 并启用 AI；模型由脚本自动准备，先按第 3 节准备驱动。仅需人工标注时显式执行 `bash deploy.sh cpu` / `.\deploy.ps1 -Mode cpu`，此模式不需要模型。缺少 GPU 或模型时会报错，不自动关闭 AI。一键脚本完成模型下载与校验、应用配置、镜像构建和启动；GPU 驱动由部署方准备。

前端构建目标为 Chrome 93，上传抽帧和审查流程不依赖新版 AbortSignal.timeout 或仅安全上下文可用的 crypto.randomUUID。内网 HTTP 可使用这些功能；此兼容目标不代表已在所有 Windows 7 终端实测。

## 2. 首次启动

```bash
git clone https://github.com/huangwww11222/sperm-annotation.git
cd sperm-annotation
# 首次自动下载本仓库 SAM3 模型
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

SAM3 模型由[本仓库的模型 Release](https://github.com/huangwww11222/sperm-annotation/releases/tag/sam3-model-6d06f0a5)提供，默认部署自动准备，无需部署方另行到模型站申请下载。模型来自 [facebook/sam3](https://huggingface.co/facebook/sam3)，固定来源快照和每个文件的 SHA256 见 `model-distribution/manifest.json`，许可见 `model-distribution/LICENSE-SAM.txt` 和模型包内 `LICENSE`。SAM License 允许在其条款下附带完整许可再分发；模型使用仍遵循该许可。[官方许可](https://github.com/facebookresearch/sam3/blob/main/LICENSE)

权重约 3.44 GB，加上配置约 3.45 GB。GitHub 普通 Git 不允许超过 100 MiB 的文件，Release 单附件须小于 2 GiB，所以权重拆为四个 Release 附件，源码只保存清单和安装工具；不是 Git LFS 占位文件。[GitHub 大文件说明](https://docs.github.com/en/repositories/working-with-files/managing-large-files/about-large-files-on-github)、[Release 限制](https://docs.github.com/en/repositories/releasing-projects-on-github/about-releases)

默认 `bash deploy.sh` / `.\deploy.ps1` 会在启动后端前执行独立 `model-setup` 容器：下载 → 校验每个分块 → 组装并验证完整权重 → 写入安装完成标记，然后启动 AI 服务。首次需约 6.5 GiB 可用空间；已下载分块可复用，网络中断保留 `.sam3-download/` 缓存，重试可续传。模型准备失败会停止部署，并在控制台及模型目录的 `.sam3-setup.log`（2 MiB × 3 轮转）记录 `model.prepare_failed`，不会启动只具备一半能力的服务。可显式运行：

```bash
docker compose --profile model-setup run --rm --no-deps model-setup
```

默认目录为 `models/sam3/`，包含 `model.safetensors`、`config.json`、处理器/分词器文件、模型 README 与 LICENSE；不重复下载当前 Transformers 实现未使用的 `sam3.pt`。模型版本由源码清单固定；重复部署校验后复用，不自动升级到上游最新版本。

可修改 `.env` 中的 `SAM3_MODEL_HOST_PATH` 使用其他目录，如 `D:/sperm-models/sam3`。已有完整自定义模型会复用，启动前检查仍验证配置、分片和 CUDA；不完整或不同文件不会被自动覆盖。自行准备或离线复制模型时，设置 `SAM3_AUTO_DOWNLOAD=false` 禁止下载。复制符号链接快照时需带上实际文件，不能留下断链。

只执行 `docker compose up` 会跳过模型准备；首次请使用部署脚本，或先手动执行上方 model-setup 命令。已有旧 `.env` 没有 SAM3_AUTO_DOWNLOAD 时仍默认自动下载；GPU 模式和路径设置沿用原值。

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
| SAM3_MODEL_HOST_PATH | ./models/sam3；安装容器写入，后端只读挂载 |
| SAM3_AUTO_DOWNLOAD | true；缺少模型时自动准备本仓库固定 Release，false 使用自备模型 |
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
| model.prepare_failed | 网络/下载空间/校验日志；缓存保留可重试，已有自定义模型不自动覆盖 |
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

离线环境在联网机器 `docker save` 导出对应前后端镜像，到目标服务器 `docker load`；复制源码中的 scripts/、model-distribution/、compose.yaml、compose.gpu.yaml（需要 GPU 时）、实际 `.env` 和已准备好的完整模型，并设置 SAM3_AUTO_DOWNLOAD=false，执行 `docker compose up -d --no-build --wait`。不运行会联网拉取的 `--pull` 模式。CPU/GPU 镜像、平台和 `.env` 名称必须匹配。

### 医院内网：交付最新完整版本

常规交付使用同一源码版本生成的完整离线包，不要求安装者先安装旧版再打补丁。联网准备电脑更新到已修复的源码版本，构建 **linux/amd64 的 GPU 后端和前端镜像**，再导出新的 `images.tar`。同时更新包内的 Compose 配置、`scripts/`、`model-distribution/`、一键安装/部署脚本和校验清单，记录源码提交号及镜像标签/摘要。不要沿用旧包的 `images.tar` 或旧校验值；Windows 准备电脑使用 Docker 的 Linux 容器模式，无需具备目标服务器的 NVIDIA GPU，但最终 GPU 验收必须在服务器完成。

医院当前使用的 `install-offline.sh` / `deploy-offline.sh` 是离线包入口，不是根目录的联网源码构建入口。更新这两个入口时，保持“本地校验 → docker load → 复用配置和模型 → 不构建、不拉取地启动 → 健康检查与日志”的流程。首次安装默认 GPU + AI；已有部署升级必须保留 `.env`、登录密钥、Compose 项目名和实际数据/模型挂载路径，不能拿新包的示例配置覆盖服务器配置。目标服务器已具备完整模型时可复用，设置 `SAM3_AUTO_DOWNLOAD=false`；首次离线安装须在包中准备完整模型。

升级现有医院部署前按第 8 节备份并暂停写入，把新包解压到独立目录，再核对与原部署的数据挂载和项目名一致后执行一键更新。离线启动使用 `docker compose up -d --no-build --pull never --wait`，禁止在内网执行源码构建或拉取。保留旧镜像和备份用于恢复，更新后客户端按 Ctrl+F5，并验收上传抽帧、送审、删除后刷新、原有账号/标注恢复及真实 GPU 追踪。

仓库代码推送成功不代表医院已有离线包已经更新；必须重新生成并交付上述镜像和配套文件。下面的专项补丁只作为既有旧版本的临时修复方案。

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

### 2026-09-29 医院 Chrome 93 专项离线补丁

此小包只用于已部署版本的浏览器兼容与视频删除修复，不是通用升级器。制作机器先执行 `npm run build --prefix frontend`，再执行 `python3 scripts/build_compat_update.py`，生成单文件 `output/hospital-compat-update.sh`（内含已构建页面与修复后的 main.py）。模型、Python/Node 依赖、业务数据不在包内。

把文件传到服务器，在无人编辑、没有追踪或导出任务时执行（更新会短暂停服务）：

```bash
bash hospital-compat-update.sh /opt/sperm-annotation/sperm-annotation-offline
```

参数必须是实际含 `.env` 和 `compose.yaml` 的部署目录。脚本识别当前 CPU/GPU 模式、校验包、后端代码版本以及运行容器与 Compose 的环境/数据挂载一致性，仅接受固定已核对基线或本次已修复版本；版本不符时停止，不覆盖服务器上其他修改。使用现有本地前后端镜像添加代码层，构建无联网步骤，不下载模型、不修改 .env，也不清理业务数据。原镜像和 `rollback.sh` 保存到该部署目录 `update-backups/<时间>/`，重建容器失败时自动尝试回滚。补丁继续使用原镜像标签，正常重建容器会保留修复；以后重新导入旧 images.tar 会覆盖该标签，应避免导入旧包或重新应用补丁。

更新后客户端按 Ctrl+F5，验收上传/换帧、送审、审查与确认、训练 ZIP，以及未送审测试视频的取消删除/确认删除/刷新。垃圾桶表示服务器删除；“×”仍表示临时关闭。脚本日志位于备份目录 `update.log`。需要人工回滚时运行脚本最后输出的 `bash .../rollback.sh`；本补丁没有数据库迁移。

## 9. 交付与验收

- 全新机器能从 README 完成安装，启动日志无 preflight_failed。
- 注册、登录、真实视频上传、标注保存、重新打开可恢复。
- B 逐帧显式提交、C 显式完成、YOLO/COCO 图像标签一致；旧接口和旧最终版本不能绕过导出门禁。
- 重建容器后账号、工作区、最终版本、训练 ZIP 和日志保留。
- GPU 模式额外用真实视频验证 Tracking、异常暂停、续追及显存表现；CPU CI 不替代 GPU 验收。
- 共享团队使用边界见根 README；公开访问要配 HTTPS 和组织访问控制。

项目提供 `scripts/docker_smoke.py` 对隔离容器执行真实上传、完整工作流、训练包与重建恢复验收；复现环境与结果统一记录在 [TESTING.md](TESTING.md)。
