# Docker 多用户部署说明

本文档适用于把本项目部署到一台中央 Windows/Linux 电脑，由局域网内多名用户通过浏览器共同使用。部署后的数据库、原视频、人工标注、Tracking 结果和训练数据集都保存在服务器宿主机，不会随容器更新而丢失。

> 当前版本使用 SQLite，并且 GPU 任务队列、任务状态和锁都保存在单个 FastAPI 进程内。因此必须只运行 **一个 backend 容器、一个 Uvicorn worker**。不要在多台电脑分别启动 backend 后共同读写一个 `app.db`。

## 1. 部署架构

```text
用户 A/B/C 的浏览器
        │
        │ http://服务器IP:8080
        ▼
frontend 容器（Nginx）
  ├─ Vue 静态页面
  └─ /api/* 反向代理
        │
        ▼
backend 容器（FastAPI，单实例）
  ├─ NVIDIA GPU / SAM3
  ├─ /data/database/app.db
  ├─ /data/storage/media
  └─ /data/storage/datasets
```

客户端电脑不需要安装 Docker、Python、Node.js 或 SAM3，只需要浏览器。所有人访问同一台服务器的 Web 地址，即可共享同一个账号库、素材库和标注结果。

## 2. 项目已提供的 Docker 文件

```text
compose.yaml                 # 统一启动 frontend + backend
.env.docker.example          # Docker 环境变量模板
.dockerignore                # 排除模型、数据库、storage 等大文件
backend/Dockerfile           # Python、CUDA PyTorch、FastAPI、OpenCV
frontend/Dockerfile          # Node 构建 + Nginx 运行
frontend/nginx.conf          # SPA、/api 代理、2GB 上传和长超时
```

后端镜像不会包含以下内容，它们必须通过宿主机目录挂载：

- `app.db`；
- `storage/`；
- SAM3 模型权重；
- 密钥和生产环境配置。

## 3. 服务器要求

### 3.1 Windows 服务器

推荐：

- Windows 10/11 x64；
- NVIDIA 显卡和支持当前 CUDA PyTorch 的驱动；
- Docker Desktop，使用 WSL2 Linux containers；
- 至少预留模型、镜像、原视频和导出数据集所需磁盘空间；
- 建议 16GB 以上内存；显存大小决定可处理的视频规模。

安装完成后检查：

```powershell
docker version
docker compose version
nvidia-smi
```

Docker Desktop 中需要启用 WSL2 backend，并确认 Docker 可以访问 NVIDIA GPU。

### 3.2 Linux 服务器

需要安装 Docker Engine、Docker Compose 插件、NVIDIA 驱动和 NVIDIA Container Toolkit，并保证普通 `docker run --gpus all ...` 能看到显卡。

## 4. 准备持久化目录

推荐在服务器单独的数据盘建立：

```text
D:\sperm-annotation-data\
├─ database\
│  └─ app.db                 # 首次启动时自动创建
├─ storage\
│  ├─ media\                # 原视频、seed、Tracking 结果、overlay
│  └─ datasets\             # COCO/YOLO 导出包
└─ backups\

D:\sperm-models\sam3\       # SAM3 模型快照
```

PowerShell：

```powershell
New-Item -ItemType Directory -Force D:\sperm-annotation-data\database
New-Item -ItemType Directory -Force D:\sperm-annotation-data\storage\media
New-Item -ItemType Directory -Force D:\sperm-annotation-data\storage\datasets
New-Item -ItemType Directory -Force D:\sperm-annotation-data\backups
New-Item -ItemType Directory -Force D:\sperm-models\sam3
```

不要把 SQLite 的 `app.db` 放在 SMB/NFS 网络共享盘上。SQLite 文件应位于运行 backend 的 Docker 服务器本地磁盘，所有用户通过 FastAPI 访问它，而不是直接访问数据库文件。

## 5. 准备 SAM3 模型

模型默认从宿主机目录只读挂载到容器 `/models/sam3`。

可以选择：

1. 从当前开发电脑复制现有模型快照；
2. 使用项目对应模型仓库的官方方式下载到服务器；
3. 在有网络的电脑下载后，通过移动硬盘迁移。

最终目录必须包含模型加载所需的配置、权重和处理器文件。例如：

```text
D:\sperm-models\sam3\
├─ config.json
├─ model.safetensors 或模型分片
├─ preprocessor_config.json
└─ 其他模型文件
```

如果你当前模型位于：

```text
backend\track_modul\facebook--sam3\snapshots\master
```

可以直接把 `master` 目录中的全部内容复制到 `D:\sperm-models\sam3`。不要只复制某一个权重文件。

## 6. 配置根目录 `.env`

在项目根目录执行：

```powershell
Copy-Item .env.docker.example .env
notepad .env
```

至少修改：

```dotenv
WEB_PORT=8080
JWT_SECRET=替换为至少32字节的随机密钥
APP_DATA_ROOT=D:/sperm-annotation-data
SAM3_MODEL_HOST_PATH=D:/sperm-models/sam3
SAM3_DEVICE=cuda
SAM3_DTYPE=bfloat16
SAM3_TRACK_FRAMES=120
```

Windows 路径建议使用正斜杠 `/`。`JWT_SECRET` 必须固定保存；以后修改它会使所有用户当前登录令牌失效。

可用 PowerShell 生成密钥：

```powershell
$bytes = New-Object byte[] 32
[Security.Cryptography.RandomNumberGenerator]::Fill($bytes)
[Convert]::ToHexString($bytes)
```

将输出复制到 `.env` 的 `JWT_SECRET=` 后面。

## 7. 从源码构建并启动

### 7.1 构建

在项目根目录执行：

```powershell
docker compose build --pull
```

后端构建会执行两段安装：

1. 从 `PYTORCH_INDEX_URL` 安装项目锁定的 CUDA PyTorch；
2. 安装 `backend/requirements.txt` 中的 FastAPI、Transformers、OpenCV 等依赖。

Windows 的 `install-pytorch-cu132.bat` 不会在 Linux 容器内运行，因此 CUDA wheel 源已经放入 `backend/Dockerfile`。

如果目标机器无法访问 PyTorch 或 npm 下载源，应先配置代理/镜像源，或者在可联网电脑构建镜像后再推送/导出镜像。

### 7.2 启动

```powershell
docker compose up -d
docker compose ps
```

查看日志：

```powershell
docker compose logs -f backend
docker compose logs -f frontend
```

默认访问：

```text
http://服务器IP:8080
```

服务器本机可以访问：

```text
http://127.0.0.1:8080
```

其他电脑使用 `ipconfig` 查到的服务器局域网 IPv4 地址，例如：

```text
http://192.168.1.20:8080
```

需要在 Windows 防火墙中仅对可信局域网开放 `.env` 中的 `WEB_PORT`。后端 3000 端口没有映射到宿主机，不应单独对外开放。

## 8. 验证 GPU 和后端环境

容器启动后执行：

```powershell
docker compose exec backend python -c "import torch; print('torch=', torch.__version__); print('cuda=', torch.cuda.is_available()); print('device=', torch.cuda.get_device_name(0) if torch.cuda.is_available() else 'CPU')"
```

期望：

```text
cuda= True
device= 你的 NVIDIA 显卡名称
```

后端健康检查：

```powershell
Invoke-RestMethod http://127.0.0.1:8080/api/health
```

第一次点击 AI Tracking 时才会真正加载 SAM3，因此第一次运行会比后续运行慢。

## 9. 迁移现有数据库和 Storage

迁移前先停止旧后端和 Docker backend，防止复制过程中仍有写入：

```powershell
docker compose stop backend
```

复制：

```text
旧 backend/data/app.db
    → D:/sperm-annotation-data/database/app.db

旧 backend/storage/*
    → D:/sperm-annotation-data/storage/*
```

如果旧版本还有 `backend/track_data/<media-id>`，将它下面的素材子目录逐个复制到：

```text
D:/sperm-annotation-data/storage/media/<media-id>
```

不要覆盖同名目录。迁移前保留完整备份。

完成后：

```powershell
docker compose start backend
docker compose logs -f backend
```

## 10. 多用户共享方式

所有用户都访问同一个网址：

```text
http://服务器IP:8080
```

共享关系如下：

- 账号和人工标注索引：同一个 `app.db`；
- 原视频：同一个 `storage/media`；
- Tracking JSON、异常结果、overlay：对应视频目录；
- 训练数据集：同一个 `storage/datasets`；
- 每个用户使用自己的登录账号。

当前版本有以下限制：

1. GPU Tracking 全局串行，一次只处理一个任务；
2. 任意 Tracking 运行时，当前逻辑可能暂时阻止其他用户保存人工标注；
3. 不要启动 `--workers 2`，也不要扩展多个 backend 容器；
4. 两个人不要同时修改同一个视频的同一帧；
5. 浏览器中的部分编辑状态保存在各自浏览器 localStorage，尚不支持实时协同编辑。

小团队可以按以上方式使用。若后续需要多后端、高并发或实时协作，应将 SQLite 迁移到 PostgreSQL，并用 Redis/持久任务表保存队列和锁，同时保留单一 GPU Worker。

## 11. 构建后推送到镜像仓库

如果不想在对方电脑上安装 Python/Node 依赖并现场构建，可以把前后端镜像推送到 Docker Hub、Harbor、GHCR 等镜像仓库。

在构建电脑的 `.env` 中设置：

```dotenv
BACKEND_IMAGE=你的仓库地址/rare-sperm-backend:v1
FRONTEND_IMAGE=你的仓库地址/rare-sperm-frontend:v1
```

然后：

```powershell
docker login 你的仓库地址
docker compose build
docker compose push
```

对方电脑只需要取得以下文件：

- `compose.yaml`；
- `.env`；
- SAM3 模型目录；
- 需要迁移的 database/storage 数据。

对方执行：

```powershell
docker login 你的仓库地址
docker compose pull
docker compose up -d --no-build
```

镜像只包含运行环境和代码，不包含数据库、Storage、模型及 `.env` 密钥。

## 12. 完全离线部署

联网电脑构建后：

```powershell
docker save -o rare-sperm-images.tar rare-sperm-annotation-backend:local rare-sperm-annotation-frontend:local
```

将以下内容复制到离线服务器：

- `rare-sperm-images.tar`；
- `compose.yaml`；
- `.env`；
- SAM3 模型目录；
- database/storage 数据目录。

离线服务器执行：

```powershell
docker load -i rare-sperm-images.tar
docker compose up -d --no-build
```

如果 `.env` 中的 `BACKEND_IMAGE`、`FRONTEND_IMAGE` 名称与导入镜像不一致，需要改成 `docker images` 显示的名称。

## 13. 更新项目

源码构建方式：

```powershell
docker compose build --pull
docker compose up -d
```

镜像仓库方式：

```powershell
docker compose pull
docker compose up -d --no-build
```

不要删除 `APP_DATA_ROOT` 指向的目录。重新创建容器不会删除绑定目录中的 `app.db`、视频或训练数据集。

## 14. 备份与恢复

SQLite 和 Storage 应作为同一个备份批次处理。

建议备份步骤：

1. 通知用户停止保存和 Tracking；
2. `docker compose stop backend`；
3. 复制 `database/app.db`；
4. 复制整个 `storage/`；
5. 记录当前镜像版本和 SAM3 模型版本；
6. `docker compose start backend`。

至少备份：

```text
D:/sperm-annotation-data/database/app.db
D:/sperm-annotation-data/storage/media/
D:/sperm-annotation-data/storage/datasets/
根目录 .env（安全保存，不公开）
```

恢复时先停止 backend，再恢复数据库和 Storage，确认目录权限后重新启动。

## 15. 常见问题

### 15.1 `could not select device driver "nvidia"`

Docker 没有获得 GPU。检查 NVIDIA 驱动、Docker Desktop WSL2 GPU 支持或 NVIDIA Container Toolkit，然后重新启动 Docker。

### 15.2 `torch.cuda.is_available()` 为 `False`

检查：

- `.env` 中 `SAM3_DEVICE=cuda`；
- Compose 中 GPU reservation 未被删除；
- 宿主机驱动支持所安装的 CUDA PyTorch；
- `docker compose exec backend nvidia-smi` 是否正常。

### 15.3 找不到 SAM3 模型

检查 `.env` 的 `SAM3_MODEL_HOST_PATH` 是否是模型文件所在目录，而不是它的父目录；再检查：

```powershell
docker compose exec backend ls -la /models/sam3
```

### 15.4 上传出现 `413 Request Entity Too Large`

确认运行的是项目提供的 `frontend/nginx.conf`，其中 `client_max_body_size 2g`。若需要更大文件，还要同步修改后端 `MAX_VIDEO_BYTES`。

### 15.5 AVI 能上传但浏览器不能直接播放

项目会使用后端逐帧预览。检查容器中的 OpenCV/FFmpeg、视频是否能解码，以及 `/api/track/frame/...` 请求。

### 15.6 `401 Unauthorized`

登录令牌过期或 `JWT_SECRET` 被修改。重新登录；若每次容器重启都失效，检查 `.env` 是否一直使用同一个 `JWT_SECRET`。

### 15.7 容器重建后视频或数据库消失

检查 `docker inspect` 或 `docker compose config`，确认：

```text
宿主机 APP_DATA_ROOT/storage  → /data/storage
宿主机 APP_DATA_ROOT/database → /data/database
```

不要把生产数据只写在容器可写层中。

## 16. 上线验收清单

- [ ] `docker compose ps` 中 frontend/backend 均正常；
- [ ] `/api/health` 返回 `ok: true`；
- [ ] 容器中 CUDA 可用；
- [ ] SAM3 模型目录完整；
- [ ] 注册、登录、401 跳转正常；
- [ ] 上传 MP4 和 AVI 正常；
- [ ] 原始 FPS、逐帧预览、人工框正常；
- [ ] AI Tracking、异常暂停、续追正常；
- [ ] 关闭前端素材后，后端 Storage 文件仍存在且可重新加载；
- [ ] 数据集导出文件出现在 `storage/datasets`；
- [ ] 另一台电脑可以通过服务器 IP 使用；
- [ ] 防火墙只开放 Web 端口；
- [ ] 数据库和 Storage 备份、恢复测试通过。
