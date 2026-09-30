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

**联网源码部署的 CPU/GPU 模式切换**：保留 `.env` 的 JWT_SECRET 和 APP_DATA_ROOT，在 `.env` 设置：

```dotenv
COMPOSE_PATH_SEPARATOR=,
COMPOSE_FILE=compose.yaml,compose.gpu.yaml
SAM3_MODEL_HOST_PATH=./models/sam3
```

再运行 `bash deploy.sh gpu` / `.\deploy.ps1 -Mode gpu`。切回 CPU 则设置 `COMPOSE_FILE=compose.yaml`，并清除或改成 CPU 对应的 `BACKEND_IMAGE`。如果脚本检测到指定模式与旧 `.env` 不一致，会停止并说明如何修改，不默默覆盖配置。

第 6 节的离线升级沿用当前模式，不负责 CPU/GPU 切换；不能将已托管部署的 `.offline/current-compose.json` 改回上述源码配置来切模式。

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

首次离线部署需在联网机器 `docker save` 导出对应前后端镜像，到目标服务器 `docker load`；复制源码中的 scripts/、model-distribution/、compose.yaml、compose.gpu.yaml（需要 GPU 时）、为该服务器准备的 `.env` 和完整模型，设置 SAM3_AUTO_DOWNLOAD=false，再执行 `docker compose up -d --no-build --pull never --wait`。CPU/GPU 镜像、平台和 `.env` 名称必须匹配。已有医院部署的后续更新使用下面的升级工具，沿用服务器原配置。

### 医院内网：可复用的离线升级流程

`build-offline.ps1` 在联网 Windows 电脑制作完整更新包，`update-offline.sh` 在已有 Linux Docker 部署上安装更新。医院服务器全程使用本地镜像，不需要访问 GitHub、镜像仓库、PyPI 或 npm。此流程只升级已运行的项目；全新服务器仍需完成 Docker/GPU 环境、首次配置和模型准备，不能以升级包代替首次安装包。

服务器需有 Bash、Python 3、Docker + Compose，以及读取运行容器、读写实际数据库/Storage 和创建备份的权限；Ubuntu 24.04 通常已带 Python 3。沿用现有部署管理员账号执行；以 root 安装的数据不能换成无权限账号备份。检查和升级均要求原前后端正常运行，已中断的升级按第 8 节恢复。

#### A. 联网 Windows 电脑制作更新包

准备 Git、Docker Desktop 的 Linux 容器模式和 PowerShell 5.1+。准备电脑无需 GPU、Python 或 Node.js；应用依赖在 Docker 内构建。确认 `docker info --format '{{.OSType}}'` 输出 `linux`。镜像构建和导出占用额外磁盘，包大小取决于 GPU 依赖；首次构建需要下载基础镜像、Python 与 npm 依赖。

在已克隆的仓库根目录执行：

```powershell
git pull --ff-only
.\build-offline.ps1 -Ref HEAD
```

脚本拒绝未提交的工作区改动，将指定 ref 一次解析为固定提交号，再从该提交的快照构建 Linux amd64 前后端镜像；后续远端 main 移动不会改变已经生成的包。选择的提交必须已包含离线升级工具，不能拿引入工具前的旧标签直接制作新式包。脚本不自动 fetch 或替你合并代码，先确认本地仓库已取得待发布版本。

| 参数 | 含义 |
| --- | --- |
| `-Ref` | 本地可解析的分支、标签或提交号，默认 `HEAD` |
| `-Version` | 显示和镜像版本，默认 `git-<提交号前12位>`；发布标签可显式指定，如 `v1.2.0` |
| `-Mode` | 默认 `gpu`；仅人工部署才显式 `cpu`。更新包模式必须匹配医院当前模式 |
| `-OutputDirectory` / `-OutputDir` | 完整包的输出目录；默认 `output/sperm-annotation-<版本>-<模式>-linux-amd64`。已有目录拒绝覆盖 |

例如，已经创建并测试过 `v1.2.0` 标签后制作该版本：

```powershell
git fetch --tags
.\build-offline.ps1 -Ref v1.2.0 -Version v1.2.0
```

输出目录包括完整前后端 `images.tar`、含提交号/镜像身份/文件校验的 `manifest.json`、`update-offline.sh`、配套 `scripts/` 和部署配置参考文件。包不含 `.env`、密码、业务视频、数据库或模型；现有医院模型继续复用。构建日志位于制作机器 `work/offline-build-<编号>/build.log`，构建失败不会发布完整目标目录。交付时传入**整个目录及其子目录**，不能只发 `.sh` 或只换校验清单。

#### B. 医院服务器先检查

把更新包放入独立目录，例如 `/opt/sperm-updates/<版本>/`，保留原部署 `/opt/sperm-annotation/sperm-annotation-offline/`。以下 `/新包目录` 和 `/原部署目录` 均替换为实际绝对路径：

```bash
bash /新包目录/update-offline.sh /原部署目录 --status
bash /新包目录/update-offline.sh /原部署目录 --check
```

`--status` 显示已保存的版本和未完成操作记录，不验证实际运行版本；`--check` 才校验包和当前部署，不停止服务、不导入镜像、不写业务数据。两者可生成运维日志。升级器核对实际容器、Compose 项目名、运行环境及数据库/Storage/模型挂载，识别 CPU/GPU 模式，避免相对路径变化使服务指向空目录。不要先把新包解压覆盖原 `.env`，也不要在新包目录直接执行 `docker compose up`。

第一次从历史部署接入时，工具对容器内整个后端 Python 源码集合计算指纹，核对 `scripts/offline_legacy.json` 中人工审查过的基线。目前包含医院最初离线版本、浏览器兼容修复版本及后续统一工作台版本。这里的“包含”不代表任何同名镜像都受支持；以服务器实际检查结果为准。旧镜像无法识别、源码已另行修改或配置与容器不符时停止，并输出需要核对的原因；不能手工改版本文件、跳过指纹或强行覆盖。

后续由工具管理的部署，按持久化版本信息、镜像身份和运行配置核验。版本标签只方便识别，源码提交号和镜像标识才定位具体产物。更新包带有目标提交的祖先清单，当前提交必须位于其中，避免把旧包或分叉版本误当升级；制作电脑需要完整 Git 历史，浅克隆先执行 `git fetch --unshallow`。降级统一走带数据恢复的回滚。包中的 `contract` 是维护者声明的存储与运行兼容契约，不是数据库内已经存在的 schema 版本号；契约不兼容时必须先提供对应迁移，不能直接套用旧升级步骤。

完全相同的镜像包重复执行会提示已安装。同一版本号或同一源码提交却带有不同镜像时拒绝覆盖，应明确发布新版本；不要在对外发布后移动同名标签或替换已交付包。

#### C. 安装更新

确认所有使用者保存并退出，当前没有追踪或导出任务，再运行：

```bash
bash /新包目录/update-offline.sh /原部署目录
```

升级会短暂停机。工具先保留旧配置与镜像标记，导入并核对新镜像，执行新后端的环境预检；GPU 模式同时检查 CUDA 和模型条件。预检通过后停止前端入口、确认后台任务空闲、停止后端，备份完整 database 和 storage。备份成功后，在保持现有数据、端口、密钥和模型路径的配置上启动新服务，最后检查后端健康与前端访问。磁盘空间必须足以同时保留新镜像、完整数据备份及失败恢复时的额外副本；升级器按文件系统合计检查，并在导入镜像后复查。业务目录须为磁盘挂载点内的子目录，不能直接用挂载点本身作为 database/storage；原视频较多时，备份时间与数据量有关。

成功后在原部署目录 `.offline/` 保存版本、日志和备份，`.env` 的 `COMPOSE_FILE` 指向 `.offline/current-compose.json`。旧 `deploy-offline.sh` 被备份并由新入口接管，避免它再次导入旧 `images.tar` 撤销升级。日后仍在原部署目录查看服务和日志；下一次更新继续用新包中的升级入口，不能用旧首次安装包反复覆盖。

```bash
cd /原部署目录
bash manage-offline.sh ps
bash manage-offline.sh logs --tail=100 backend frontend
bash /新包目录/update-offline.sh /原部署目录 --status
```

升级成功后，浏览器按 Ctrl+F5。核对原账号、素材、标注和审查/确认结果，再用测试视频检查上传抽帧、送审、删除后刷新、训练包和一次实际 AI Tracking。健康检查或 CUDA 张量计算不能代替真实模型推理验收。

普通 Git push 只发布源码；每次医院升级仍需制作、传入并安装对应更新包。现有版本标签工作流发布镜像与此本地打包流程并行，不能把“仓库有工作流文件”视为医院自动升级或 GPU 已通过验收。

### 服务器批量提取质量审计

正常“导出训练数据集”自动固定审计，无需标注人员另点按钮。安装新完整更新包后，`export-audit.sh` 会随包校验并安装到原部署目录；原脚本也纳入升级备份/恢复。提取过程只读、无网络、无需停止服务。不要复制正在写入的 app.db 代替正式提取。

```bash
bash /opt/sperm-annotation/sperm-annotation-offline/export-audit.sh /opt/sperm-annotation/sperm-annotation-offline --list
bash /opt/sperm-annotation/sperm-annotation-offline/export-audit.sh /opt/sperm-annotation/sperm-annotation-offline --output /tmp/quality-audit.zip
bash /opt/sperm-annotation/sperm-annotation-offline/export-audit.sh /opt/sperm-annotation/sperm-annotation-offline --dataset-id train_xxx --output /tmp/one-dataset-audit.zip
bash /opt/sperm-annotation/sperm-annotation-offline/export-audit.sh /opt/sperm-annotation/sperm-annotation-offline --lookup train_xxx__v000__frame_000123.jpg
```

`--media-id` 可筛选视频/媒体修订 ID；`--since/--until` 按数据集创建时间筛选，必须带时区，例如 `2026-09-30T00:00:00+08:00`，until 不含上界。默认成功包，`--include-failed` 另包含失败尝试；历史 F 的审计可提取，训练包下载仍遵守当前 F 门禁。目标文件存在时拒绝覆盖。旧任务缺快照时 list 显示未覆盖，lookup 可利用旧清单查来源，不伪造旧审计。

将审计 ZIP 交给独立 [statistics_system](https://github.com/huangwww11222/statistics_system) 导入；需看图时，再绑定同一数据集训练 ZIP。完整人员/事件证据保留在服务端数据库，训练 ZIP 默认只带图片来源和审计摘要。

## 7. 迁移现有数据库和 Storage

先停旧后端和新后端，完整备份，确认没有写入或 Tracking：

```bash
docker compose stop
```

复制旧 `backend/data/app.db` 到实际 `APP_DATA_ROOT/database/app.db`，复制整个旧 `backend/storage` 内容到 `APP_DATA_ROOT/storage`。SQLite 若仍存在有效 WAL，不能只拷主文件；优先正常停机后备份整个数据库目录。旧 `backend/track_data/<id>` 逐个迁移至 `storage/media/<id>`，不覆盖同名素材。

启动会增量建表，不删除 A/B/F 历史。缺少完整快照的旧任务保持只读，不能猜测补帧。已有数据若记录了跨机器绝对路径，需要确认原视频能按媒体 ID 找到后再做业务验收，不能以登录成功代替迁移成功。

## 8. 更新、备份与恢复

联网源码部署：安排停写，完整备份实际 database、storage、`.env` 和模型来源，更新至已验收的源码标签后运行 `deploy.sh` / `deploy.ps1`；预构建方式改 `.env` 镜像版本后 pull/up。源码和数据分开存放，不对业务目录执行 git clean。SQLite 有 WAL 时不能只复制 app.db；停后端后备份整个数据库目录。

离线管理的部署按第 6 节升级。运维文件保存在**原部署目录**：

```text
.offline/
  current-compose.json  当前实际运行配置（包含部署密钥，不外传）
  state.json            当前已安装版本、源码提交、镜像标识
  pending.json          仅未完成操作存在，供中断恢复使用
  logs/                 每次检查、升级、恢复的日志
  backups/<备份编号>/   对应升级前配置、镜像身份及完整数据库/素材备份
```

旧镜像保留在本机 Docker 中，并打上 `annotation-offline-backup` 标签；备份目录记录镜像身份，不重复导出整份旧镜像归档。需要回滚时不能清理这些镜像。上述目录不是源码缓存，不能随着代码整理、容器清理或 Git 更新删除。磁盘备份只保护本机升级过程，部署方仍需安排独立的数据备份。

升级中失败时，查看脚本给出的日志与恢复状态。出现断电或终端中断，工具保留未完成操作记录；在重试升级前恢复：

```bash
bash /新包目录/update-offline.sh /原部署目录 --recover
```

恢复依据已记录的阶段和备份，不把未完成的升级标为成功。命令失败时继续依据该次日志定位，不删除恢复记录来绕过检查。若新包被移走，可使用备份内保存的工具执行 `python3 /原部署目录/.offline/backups/<备份编号>/offline_update.py /原部署目录 --recover`。文件 SHA256 用于发现传输损坏，不是发布者数字签名；升级包应通过既定可信交付渠道取得。

需要主动回退已完成的升级时，使用当前版本对应的上一次升级备份编号；工具不允许跨过后续升级直接恢复更早备份：

```bash
bash /新包目录/update-offline.sh /原部署目录 --rollback <备份编号> --restore-data
```

`--restore-data` 明确表示恢复升级前的同批数据库和 Storage。**回退后，升级之后产生的新标注不会出现在当前旧版本数据中。** 工具先保留回滚时的当前数据副本，再恢复旧配置、镜像和数据，供必要时人工核对。不能只换旧镜像继续使用可能已经迁移的新数据库，也不能把多次备份混合恢复。

### 历史专项修复

`scripts/build_compat_update.py` / `install_compat_update.sh` 仅保留 2026-09-29 Chrome 93 / HTTP 和媒体删除专项修复的追溯用途；仅支持其固定基线，不用于以后版本升级。常规更新统一使用完整镜像包和 `update-offline.sh`，验收当前素材列表的确认删除与刷新结果。历史专项测试记录见 [TESTING.md](TESTING.md#最近验证记录)。

## 9. 交付与验收

- 全新机器能从 README 完成安装，启动日志无 preflight_failed。
- 注册、登录、真实视频上传、标注保存、重新打开可恢复。
- B 逐帧显式提交、C 显式完成、YOLO/COCO 图像标签一致；旧接口和旧最终版本不能绕过导出门禁。
- 重建容器后账号、工作区、最终版本、训练 ZIP 和日志保留。
- GPU 模式额外用真实视频验证 Tracking、异常暂停、续追及显存表现；CPU CI 不替代 GPU 验收。
- 共享团队使用边界见根 README；公开访问要配 HTTPS 和组织访问控制。

项目提供 `scripts/docker_smoke.py` 对隔离容器执行真实上传、完整工作流、训练包与重建恢复验收；复现环境与结果统一记录在 [TESTING.md](TESTING.md)。
