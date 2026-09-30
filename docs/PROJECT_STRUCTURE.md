# 代码地图与运行入口

Vue 3 + TypeScript + Vite；FastAPI + SQLite；SAM3 在后端按需加载。前端依赖见 [package.json](../frontend/package.json)，Python 依赖见 [requirements.txt](../backend/requirements.txt)，不在说明文档复制版本号。

## 按职责找文件

| 职责 | 实现入口 |
| --- | --- |
| Docker 部署入口 | 根目录 `deploy.sh` / `deploy.ps1`、`compose.yaml` / `compose.gpu.yaml`、`.env.docker.example` |
| 完整离线更新包与服务器升级 | 根目录 `build-offline.ps1` / `update-offline.sh`、`scripts/offline_update.py` / `offline_legacy.json`；制作、版本识别、备份、恢复与回滚见 Docker 部署说明 |
| Chrome 93 / HTTP 兼容 | `frontend/src/utils/browserCompat.ts`；`scripts/build_compat_update.py` / `install_compat_update.sh` 仅保留历史专项修复用途 |
| 容器启动前检查 | `backend/app/deployment_check.py`、`backend/docker-entrypoint.sh` |
| 模型随仓库交付 | `model-distribution/manifest.json` 与 `LICENSE-SAM.txt`、`scripts/prepare_model.py` / `build_model_bundle.py` |
| 仓库与容器验收 | `scripts/check_repository.py`、`scripts/docker_smoke.py`、`.github/workflows/` |
| 路由/认证/布局/主题 | `frontend/src/router/index.ts`、`stores/auth.ts`、`layouts/AppLayout.vue`、`stores/appearance.ts`、`style.css` |
| 三页共享工作台 | `components/WorkbenchHeader.vue` / `WorkbenchLayout.vue`、`workbench/workbench.css`；`stores/workbench.ts` 保存跨页视频列表收起偏好，布局约束见 WORKFLOW |
| 人工标注 UI / 状态 | `pages/AnnotatePage.vue`、`stores/workspace.ts`、`annotation/annotation.css` |
| 框绘制/坐标/缓存 | `components/AnnotationOverlay.vue`、`annotation/geometry.ts`、`annotation/frameCache.ts` |
| 送审 | `components/SendToReview.vue`、`api/reviewWorkflowApi.ts`、`backend/app/annotation_completion.py` |
| B 审查 | `pages/ReviewPage.vue`、`review/geometry.ts`、`review/review.css`、`backend/app/review_workflow.py` 与 `_routes.py` |
| C 对比确认 | `pages/ConfirmationPage.vue`、`confirmation/ConfirmationImage.vue`、`confirmation/geometry.ts`、`api/confirmationApi.ts`、`backend/app/confirmation_workflow.py` 与 `_routes.py` |
| 视频进度 | `components/WorkflowProgress.vue`，B/C 均使用工作区顶部横条；人工标注顶部展示帧位置、含标注帧数、保存与送审 |
| 用户使用说明 | `components/UserGuide.vue`；`help/user-guide.md` 为页面阅读和下载的唯一内容源，`AppLayout.vue` 提供入口 |
| 训练集导出 | `components/TrainingDatasetExport.vue`、`api/trainingExportApi.ts`、`backend/app/training_export.py` 与 `_routes.py` |
| 自动质量审计与只读提取 | `backend/app/quality_audit.py` / `audit_export.py`、根 `export-audit.sh`；统计仓库单独检出到 `标注统计/`，不纳入主仓库或镜像 |
| 记录查询 | `pages/ResultsPage.vue`、`backend/app/db.py` |
| 媒体/工作区/Tracking API | `api/trackApi.ts`、`api/httpAnnotationApi.ts`、`backend/app/main.py` |
| Tracking 编排 / 模型 / 异常 | `backend/app/tracker.py`、`services/sam3_engine.py`、`services/anomaly_detector.py`、`services/annotation_seed.py` |
| 数据库和增量迁移 | `db.py`、`review_schema.py`、三个 workflow/export 模块中的 `migrate()` |
| 日志/来源锁 | `review_logging.py`、`review_source_lock.py` |

表内未带根路径的前端项均相对 `frontend/src/`，后端项相对 `backend/app/`。具体路由与结构见 [API.md](API.md)。

### 兼容代码不等于当前流程

`review_repository.py`、`review_routes.py` 保留底层/历史能力，活动写入由新 workflow 路由控制。`mockAnnotationApi.ts`、旧 annotation 接口注释、EffectsPage 等兼容代码不能作为“当前登录是 Mock”或“Tracking 只从第 0 帧开始”的依据。当前登录使用真实后端 JWT；实际请求以活动页面调用和 `main.py` 注册的路由为准。

## 本地运行

第三方部署先使用根 README 的 Docker 入口。以下为开发模式；后端使用 Python 3.12（锁定 NumPy 需要 3.12+），前端 Node 22。项目根目录，已安装后端依赖的环境：

```bash
python -m uvicorn app.main:app --app-dir backend --host 127.0.0.1 --port 3000
```

另一终端：

```bash
npm ci --prefix frontend
npm run dev --prefix frontend
```

Vite 默认 5173，`/api` 代理到 `http://localhost:3000`。隔离测试使用其他端口：

```bash
API_PROXY_TARGET=http://127.0.0.1:3301 npm run dev --prefix frontend -- --host 127.0.0.1 --port 5373 --strictPort
npm run build --prefix frontend
```

构建含 `vue-tsc --noEmit`；部署 `frontend/dist` 并将 `/api` 代理到 FastAPI。不要把开发测试服务指向业务数据，隔离步骤见 [TESTING.md](TESTING.md)。Windows 命令见 [DEPLOYMENT_WINDOWS.md](DEPLOYMENT_WINDOWS.md)。

## 配置与进程约束

- 后端配置集中在 `backend/app/config.py`，读取 `backend/.env`；模板 `backend/.env.example`。
- `APP_DATA_DIR`：数据库默认目录及日志；`APP_DB_FILE`：数据库文件；`APP_STORAGE_DIR`：媒体和训练 ZIP。覆盖其中一个不自动迁移其他路径。
- `SAM3_MODEL_ID`：默认 `backend/track_modul/facebook--sam3/snapshots/master`；模型随仓库 Release 提供，默认部署先用 `scripts/prepare_model.py` 自动准备；Git 仅保存 `model-distribution/` 中的清单和许可。
- `SAM3_DEVICE` / `SAM3_DTYPE` 默认 `cuda` / `bfloat16`；真实 GPU 可用性需单独验收。
- `SAM3_TRACK_FRAMES` 当前代码默认 **120**，包含 seed 帧；环境变量可覆盖。异常参数见 `services/anomaly_detector.py`，不要在 UI 写死单轮帧数。
- `JWT_SECRET` 生产环境应配置；不把密钥/token 写入文档或日志。
- GPU 任务、来源锁及训练导出队列使用进程内状态，保持 **单后端进程、单 Uvicorn worker**。扩容前需要单独设计，不能直接增加 workers。
- Docker 首次通过部署脚本或配置模板默认启用 GPU + AI Tracking（`compose.yaml` + `compose.gpu.yaml`）；显式 `cpu` 仅人工模式使用基础 Compose，不启用 AI。已有 `.env` 保留原模式。两种模式均支持完整人工标注→审查→确认→导出。`SAM3_ENABLED` 的本机开发默认值仍为 true，Compose 明确覆盖。
- Docker 设置 `APP_DATA_DIR=/data/database`，日志与 DB 同卷持久化；启动前校验密钥、目录和 GPU 模型条件。部署细节见 [DOCKER_DEPLOYMENT.md](DOCKER_DEPLOYMENT.md)。
- 医院升级使用固定 Git 提交构建的完整离线包。升级器沿用现有运行配置和绝对数据路径；成功后 `.env` 指向 `.offline/current-compose.json`。部署状态、日志、备份和中断恢复记录都在原部署目录的 `.offline/`，不能作为代码缓存清理。`offline_legacy.json` 记录已核对旧后端的全体 Python 源码指纹；`contract` 是人工维护的存储与运行兼容契约，不是现有 SQLite schema 版本号。改变不兼容的数据格式、运行配置或模型要求时必须同时设计迁移并更新契约，不能只递增应用版本。
