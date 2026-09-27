# 微流控标注工作台

用于视频人工标注、SAM3 辅助追踪、逐帧审查、A/B 对比确认及 YOLO / COCO 训练数据集导出。浏览器即可操作；数据保存在部署方自己的服务器。

**训练集必须经过审查和对比确认后才能导出。** 同一账号可以自审、自确认，也可由团队分工完成。

## 快速开始：默认启用 AI 的 Docker 部署

默认部署启用 SAM3 AI Tracking，面向半自动视频标注。服务器先准备 NVIDIA GPU、驱动、容器 GPU 支持、Docker + Compose（2.20+），以及完整 SAM3 模型（见下方及[部署说明](docs/DOCKER_DEPLOYMENT.md)）。使用者的电脑只需要浏览器，无需 Python、Node.js 或模型环境。

```bash
git clone https://github.com/huangwww11222/sperm-annotation.git
cd sperm-annotation
# 先把完整 SAM3 模型放入 models/sam3/，再启动
bash deploy.sh
```

Windows PowerShell：

```powershell
git clone https://github.com/huangwww11222/sperm-annotation.git
cd sperm-annotation
# 先把完整 SAM3 模型放入 models/sam3/，再启动
.\deploy.ps1
```

成功后访问 **http://服务器IP:8080**，在登录页注册账号。脚本自动生成 `.env` 和随机登录密钥，构建镜像并等待两个服务健康；再次执行不会覆盖已有配置和数据。首次构建需要访问镜像、Python 和 npm 下载源，不能离线凭空安装依赖。

| 模式 | 功能 | 额外条件 |
| --- | --- | --- |
| `cpu` | 人工标注 → 审查 → 确认 → 训练集导出 | 不需要 GPU 或模型；AI Tracking 显示未启用提示 |
| `gpu`（默认） | 上述功能 + SAM3 AI Tracking | NVIDIA GPU、驱动、Container Toolkit / WSL2 GPU 支持和完整 SAM3 模型 |

首次不传模式即选择 GPU + AI，也可显式运行 `bash deploy.sh gpu`（Windows：`.\deploy.ps1 -Mode gpu`）。仅需人工标注时才显式选择 `bash deploy.sh cpu` / `.\deploy.ps1 -Mode cpu`。缺少 GPU 或模型时会报错，不自动降级。已有 `.env` 的模式切换、模型准备及驱动要求见 [Docker 部署说明](docs/DOCKER_DEPLOYMENT.md)。模型不随源码或镜像提供，需要部署方取得访问权限。

默认数据保存在 `runtime/database/` 与 `runtime/storage/`，日志在 `runtime/database/logs/`。`.env` 和这些目录需要保留、备份，不能提交到 Git 或随代码清理。

本工具面向**可信团队共享使用**，注册账号不等于素材隔离。默认不提供租户隔离、管理员审批注册或实时协同编辑；多人避免同时修改同一视频。公网部署应由 HTTPS 反向代理和组织访问控制保护，不直接将共享标注环境开放给所有人。

## 使用与维护

- [用户使用说明](frontend/src/help/user-guide.md)：操作步骤、快捷键、保存续做、常见问题；前端顶部也可阅读、搜索和下载。
- [Docker 部署说明](docs/DOCKER_DEPLOYMENT.md)：模型配置、预构建镜像、日志、升级、备份与恢复。
- 查看状态：`docker compose ps`；排查：`docker compose logs --tail=100 backend frontend`。
- 更新前先备份数据；更新代码后执行 `bash deploy.sh`，Windows 执行 `.\deploy.ps1`。
- 仅运行 **一个 backend 实例、一个 worker**，SQLite 放在服务器本地磁盘。

## 仓库组织与开发

```text
README.md / deploy.sh / deploy.ps1     第三方安装入口
compose.yaml / compose.gpu.yaml        基础服务与默认启用的 GPU 覆盖配置
.env.docker.example                   可提交的配置模板
backend/                              API、数据库、追踪、测试、Dockerfile
frontend/                             界面、用户手册、测试、Dockerfile、Nginx
scripts/                              仓库检查和隔离容器验收
.github/workflows/                    PR 验证与版本镜像发布
AGENTS.md / docs/                     AI 开发入口和当前技术文档
docs/archive/                         历史资料，仅供追溯
runtime/ / models/ / work/ / output/   本机数据、模型与测试产物，不纳入 Git
```

开发者先读 [AGENTS.md](AGENTS.md) 和 [当前文档索引](docs/README.md)。Python 基线为 **3.12**，Node 为 **22**；测试方法见 [TESTING.md](docs/TESTING.md)。合并前运行 `python3 scripts/check_repository.py`，避免重复定义、冲突标记及业务数据再次进入仓库。

维护者推送 `vX.Y.Z` 标签后，镜像发布工作流会先验证再构建 Linux amd64 的 CPU/GPU 后端和前端镜像。**工作流文件不代表镜像已经发布**；以 GitHub Actions / Packages 的实际结果为准。首次发布后确认 Packages 可见性，第三方才可免构建拉取；具体流程见部署文档。发布前还需由代码所有者明确项目 LICENSE；本次不擅自选择授权协议，SAM3 模型按其独立条款获取。
