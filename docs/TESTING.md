# 测试与日志排查

测试需要证明目标行为和核心边界；不要为纯文案反复跑 GPU，也不要以构建通过代替交互验证。

## 先隔离数据

后端导入/启动会初始化 DB 和表。运行 pytest、夹具、调试服务器前同时指定 `APP_DATA_DIR`、`APP_DB_FILE`、`APP_STORAGE_DIR`，不要使用业务默认目录。夹具只允许 `work/` 下规定的路径。旧 `backend/track_data` 仍可能被兼容读取，浏览器标注测试应筛选测试媒体，不能随便点用户素材。

下列命令在项目根目录执行。`work/review-venv/bin/python` 是本机已有测试解释器；其他机器替换为安装了后端依赖的 Python，例如 `backend/.venv/bin/python`；Windows 见 [部署说明](DEPLOYMENT_WINDOWS.md)。本机测试额外安装 `backend/requirements-dev.txt`（pytest、httpx）；生产镜像不安装测试依赖。

```bash
APP_DATA_DIR="$PWD/work/agent-test-data" \
APP_DB_FILE="$PWD/work/agent-test-data/app.db" \
APP_STORAGE_DIR="$PWD/work/agent-test-storage" \
PYTHONPATH=backend work/review-venv/bin/python -m pytest backend/tests -q

npm run test:compatibility --prefix frontend
npm run test:annotation --prefix frontend
npm run test:review --prefix frontend
npm run test:confirmation --prefix frontend
npm run build --prefix frontend
```

构建包含类型检查。已有混合静态/动态导入和 Python 弃用警告应与新错误区分，不能因为日志有 warning 就猜测业务失败。

## 按修改选择测试

| 修改面 | 后端/单元 | 浏览器 |
| --- | --- | --- |
| 标注手势、性能、续标、快捷键 | `test:annotation`；必要时 `test_frontend_tracking_contract.py`、`test_manual_baseline_restore.py` | `annotation-ux-browser.mjs`（48）；`annotation-ui-consistency.mjs`（14） |
| 删除范围、人工正常反馈、重试 | `test:annotation`（含写入队列重放/账号隔离/版本冲突）；`test_annotation_controls.py`、`test_anomaly_detector.py`、`test_tracking_adaptive_feedback.py` | `annotation-delete-feedback-browser.mjs`（56；反馈/删除真后端，GPU模拟） |
| Tracking 调用/完成定位 | seed、rewind、timing、manual baseline、frontend tracking contract pytest | `annotation-tracking-ui.mjs`（14，模拟响应，无真实 GPU） |
| Chrome 93 / HTTP、素材删除 | `test:compatibility`、`test_media_deletion.py` | `browser-compat-media.mjs`；下述旧 API 回归模式 |
| 送审 | `test_review_completion.py` | `review-ingress-browser.mjs`（6） |
| B 草稿/提交/恢复 | `test_review_workflow.py`、`test:review` | `review-browser.mjs`（27）、`review-failure-browser.mjs`（17） |
| C 选择/恢复/最终版本 | `test_confirmation_workflow.py`、`test:confirmation` | `confirmation-browser.mjs`（39）、`confirmation-failure-browser.mjs`（30） |
| 训练导出/门禁/版本 | `test_training_export.py` | `training-export-browser.mjs`（29） |
| 全窗口专注、视频进度 | 上述对应页面回归、构建 | `workspace-layout-browser.mjs`（49） |
| 数据库查询/升级、Docker 发布 | `test_annotation_result_dedupe.py`、`test_deployment_preflight.py`、`test_docker_deployment_contract.py`、`test_model_distribution.py` | 下方容器验收；不是只运行前端构建 |
| 核心操作优先级、用户说明 | 上述 B/C/导出回归、构建 | `workspace-priority-browser.mjs`（35） |

括号为当前脚本检查数量，**不是每次修改自动通过的结果**。测试源位于 `backend/tests/` 与 `frontend/tests/`。

## 真实浏览器复现

使用 Playwright 库和已安装 Chrome，项目保存的 `.mjs` 脚本是测试入口。脚本默认导入 `playwright`；若库在其他位置，以环境变量 `PLAYWRIGHT_MODULE` 指向其绝对 `index.mjs` 路径。不要把某个开发机的绝对路径当项目要求。

### 1. 准备同一套环境和夹具

以下环境仅用于测试终端，后续夹具和后端服务共用：

```bash
export APP_DATA_DIR="$PWD/work/e2e-confirm-ux-agent-data"
export APP_DB_FILE="$PWD/work/e2e-confirm-ux-agent-data/app.db"
export APP_STORAGE_DIR="$PWD/work/e2e-confirm-ux-agent-storage"
export PYTHONPATH=backend
mkdir -p work output/playwright
work/review-venv/bin/python backend/tests/confirmation_browser_fixture.py
work/review-venv/bin/python backend/tests/annotation_ux_fixture.py
work/review-venv/bin/python backend/tests/review_browser_fixture.py
```

先生成 C 夹具建立固定测试用户 ID，再生成其他夹具。UX 脚本要求路径含 `/work/e2e-confirm-ux`，生成横/竖两个 60 帧真实 AVI，并**重置测试媒体工作区**。C 夹具生成 main、zero、failure 三个独立任务，B 夹具生成一个三帧任务。token 在 `work/*fixture.json`，不要输出到报告。

### 2. 启动测试服务

检查 3301 / 5373 未被占用；已占用时选择其他端口并设置脚本对应 `REVIEW_ORIGIN` / `CONFIRMATION_ORIGIN`，不要结束不属于自己的进程。

同一环境启动后端：

```bash
work/review-venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port 3301
```

另一个终端启动前端：

```bash
API_PROXY_TARGET=http://127.0.0.1:3301 npm run dev --prefix frontend -- --host 127.0.0.1 --port 5373 --strictPort
```

### 3. 按状态消耗顺序运行

```bash
node frontend/tests/workspace-layout-browser.mjs
```

这个脚本同时使用 B/C/UX 夹具，检查横竖素材、纵向增益、原图比例、退出恢复、手势取消、窗口缩高、离页清理、吸顶进度、100% 与完成、零修改。它会改变 B 与 C 状态，跑其他状态测试前重新生成相应夹具。

`node frontend/tests/workspace-priority-browser.mjs` 同样需要 B/C/UX 夹具；检查 1366×768、1180×760 的右上进度卡片和提交可见性、C 首屏操作与完成后导出顺序、说明章节/搜索/深色主题/Markdown 原文下载，以及说明窗口中的键盘隔离、焦点循环、退出滚动恢复和章节滚动重置。它会完成 C main，运行其他 C 正常测试前需重新生成夹具。

其他脚本按上表选择 `node frontend/tests/<脚本>.mjs`：

- `annotation-ux-browser` 前重置 UX；外观、模拟 Tracking 不需要新 C 任务，但需要可用账号。
- `annotation-delete-feedback-browser` 前在同一隔离环境运行 `backend/tests/annotation_controls_fixture.py`，只重置五个 `controls-*` 媒体，凭证留在 `work/annotation-controls-browser-fixture.json`。环境路径支持 `/work/e2e-confirm-ux*` 或单独 `/work/e2e-annotation-controls*`；前端地址用 `ANNOTATION_ORIGIN`。覆盖当前帧/全视频删除、统计取消/确认、跨帧撤销、刷新、正常/仅本次/实际修框、校准重置、两类写入成功但响应丢失的原键重放、帮助下载一致及小窗口首屏。其后续 GPU job 响应模拟，保存/反馈/seed/rewind与原视频帧走真实后端；不能称真实模型验收。
- `review-browser` 和 `review-failure-browser` 每组前重新生成 B 夹具。
- C 正常用 main/zero，C 故障用 failure，二者可依次运行；整组重跑需新 C 夹具。
- `training-export-browser` 前重新生成 C 夹具，因为它消费 main/zero。
- 测试执行时不修改前端文件，避免 HMR 改变正在运行的实例。

Vite 调试脚本若需要访问 workspace，须导入浏览器实际已加载的模块 URL（包含可能的 `?t=`），不要无条件二次 `import('/src/stores/workspace.ts')` 建立独立实例。

结束后只停止自己启动的测试服务器；业务数据不清理。截图/控制台保存在 `output/playwright/`，命令日志放 `work/`。

### Chrome 93 / HTTP 专项回归

`browser-compat-media.mjs` 使用真实上传的短 AVI、送审接口和删除接口；拒绝非 `work/` 夹具路径。沿用以上 B/C/UX 夹具，推荐独立数据根 `work/e2e-confirm-ux-compat-*`。启动后端 3307；前端用 `__VITE_ADDITIONAL_SERVER_ALLOWED_HOSTS=hospital-test.local API_PROXY_TARGET=http://127.0.0.1:3307 npm run dev --prefix frontend -- --host 127.0.0.1 --port 5377 --strictPort`。

```bash
# 库路径按前述 PLAYWRIGHT_MODULE 设置；COMPAT_VIDEO 可指定生成的 AVI。
node frontend/tests/browser-compat-media.mjs
LEGACY_BROWSER=1 REVIEW_ORIGIN=http://127.0.0.1:5377 node frontend/tests/review-browser.mjs
LEGACY_BROWSER=1 CONFIRMATION_ORIGIN=http://127.0.0.1:5377 node frontend/tests/confirmation-browser.mjs
LEGACY_BROWSER=1 CONFIRMATION_ORIGIN=http://127.0.0.1:5377 node frontend/tests/confirmation-failure-browser.mjs
# 重新生成 C 夹具后再执行导出测试。
LEGACY_BROWSER=1 CONFIRMATION_ORIGIN=http://127.0.0.1:5377 node frontend/tests/training-export-browser.mjs
```

兼容配置禁用 AbortSignal.timeout、crypto.randomUUID、requestVideoFrameCallback；专项脚本另用仅在测试 Chrome 内解析到 127.0.0.1 的 hospital-test.local 验证真正的非安全 HTTP 上下文。覆盖上传、真实提帧、换帧、送审响应丢失后沿用原键、取消删除、删除失败保留、成功后立即刷新、过期缓存清理、A 快照保护。其余流程脚本可用 `LEGACY_BROWSER=1` 复用缺失 API 配置。它不是 Chrome 93 整个引擎的模拟，交付后仍需医院 Windows 7 / Chrome 93 客户端验收。

## 仓库与 Docker 交付验收

`python3 scripts/check_repository.py` 检查 Git 跟踪文件，拒绝业务数据、模型、本机密钥、冲突标记和重复 Python 顶层定义。它读取索引中的文件列表；新文件提交后同样受 CI 检查，不扫描用户运行目录内容。

容器测试使用实际生产 Dockerfile、Python 3.12 和隔离存储。CPU 容器覆盖基本流程，不能代替 CUDA/真实权重推理。以下为 POSIX 示例，必须使用未占用端口和新的测试数据目录，不指向生产数据：

```bash
export COMPOSE_FILE="$PWD/compose.yaml"
export COMPOSE_PROJECT_NAME=annotation-ci
export APP_DATA_ROOT="$PWD/work/docker-smoke-data"
export WEB_PORT=18080
export WEB_BIND_ADDRESS=127.0.0.1
export SMOKE_ORIGIN=http://127.0.0.1:18080
export JWT_SECRET="$(python3 -c 'import secrets; print(secrets.token_hex(32))')"
export BACKEND_IMAGE=annotation-ci-backend:cpu
export FRONTEND_IMAGE=annotation-ci-frontend:local

docker build -f backend/Dockerfile -t "$BACKEND_IMAGE" .
docker build -f frontend/Dockerfile -t "$FRONTEND_IMAGE" .
docker compose config --quiet
docker compose -f compose.yaml -f compose.gpu.yaml config --quiet
docker compose up -d --no-build --wait --wait-timeout 180
python3 scripts/docker_smoke.py --fresh
docker compose down
docker compose up -d --no-build --wait --wait-timeout 180
python3 scripts/docker_smoke.py --verify-restart
docker compose logs --tail=100 backend frontend
docker compose down
```

`--fresh` 验证空数据库起步，已有测试数据时使用新隔离目录。脚本生成真实 AVI，验证注册、人工保存和查询（防止 db.py 参数回退）、工作区、B/C 显式完成、YOLO 图像/标签及重建后令牌/数据/训练包。只允许 loopback 地址和 `work/` 下数据；恢复凭据写入已忽略的 `work/docker-smoke-state.json`，不要上传它。

后端也在同一 Python 3.12 镜像中回归，隔离变量设置方法见 `.github/workflows/ci.yml`。Bash 初始化由 pytest 的模拟 Docker 用例验证随机密钥、重复运行和模式冲突保护；Windows CI 通过 `scripts/test_deploy.ps1` 做相同的 PowerShell 配置测试，不代表 WSL2/GPU 的实际服务器验收。

`.github/workflows/ci.yml` 在 PR/main 验证；`release-images.yml` 在版本标签推送后调用验证，再发布镜像。新增工作流需推送后实际运行，不能把本地配置校验说成 GitHub Actions 已通过。镜像需要在 Packages 确认访问权限。

## 日志驱动排查

1. 从页面错误获取请求号和动作；查看控制台与 Network 的实际响应，先分清客户端模拟故障还是服务端真实拒绝。
2. B/C/训练导出服务日志在 `$APP_DATA_DIR/logs/review.log`；默认 `backend/data/logs/review.log`，约 5 MB/文件、保留 5 个历史文件。按 `req_...` 查请求，再以幂等键、session/change/export ID 关联业务记录。
3. 追踪与媒体读取同时检查 Uvicorn 输出、`[ai-track]` / `[tracking-rewind]` 和对应媒体 JSONL；不要凭画面推测请求发到了哪里。
4. 复现后修复并重跑失败用例及相关正常路径，保留证据；不删契约断言来让测试通过。

| 事件 | 关注内容 |
| --- | --- |
| `review.request`、`review.*` | 路由、状态、请求号、幂等及事务失败 |
| `confirmation.committed/replay/rejected/storage_failed` | 选择、版本冲突、原键恢复、回滚堆栈 |
| `dataset.queued/started/progress/ready/download` | 任务阶段与真实处理帧数 |
| `dataset.failed/enqueue_failed/interrupted` | 提帧/写包/发布/重启失败 |
| `annotation.frame_slow/frame_failed/prefetch_failed` | 媒体、目标帧、耗时；慢阈值 250ms |
| `annotation.workspace_save_failed/tracking_load_failed` | 媒体、保存序号与实际读取异常 |
| `media.deleted/delete_failed/delete_rejected`、`annotation.media_deleted/media_delete_failed/media_list_failed` | 删除结果、媒体 ID、操作者、文件系统异常；前端网络失败不得当删除成功 |
| `annotation.local_save_failed/selection_save_failed`、`ui.preference_save_failed` | 本机存储不可用 |

日志不记录 JWT/密码；不要记录每个鼠标移动。浏览器 route 注入的 503 不会出现在服务端日志；数据库触发器故障测试验证真实事务回滚与日志。

## 最近验证记录

2026-09-29 Chrome 93 / HTTP 与素材删除修复：后端和离线更新脚本 **169 项通过**（其中更新脚本 7 项验证 CPU/GPU 配置选择、版本/运行配置不符停止、AI 忙/构建失败不切换服务、重启失败自动恢复原镜像）；前端兼容/几何/缓存 **5+5+6+4 项通过**，类型检查与 Chrome 93 目标构建通过。浏览器专项 **21**、B **27**、C **39**、C 故障恢复 **30**、训练导出 **29**、离线生产页面 **4**，合计 **150 项通过**；业务流程回归禁用了新版超时、UUID 和视频帧回调 API，专项/生产页面另验证真正的非安全 HTTP。

实际隔离 CPU Compose 在旧镜像上运行小型补丁；发现并修正解压目录权限导致的 Nginx 403，新增首页与 API 双检查。真实验证安装、原镜像回滚、再次安装，以及升级前登录令牌、工作区、确认结果和训练包保留。容器中的生产构建实际通过上传/提帧、删除后刷新、人工框送审。补丁以现有本地镜像加 COPY 层，构建禁止拉取与网络，不传模型。Linux/Windows 7 Chrome 93 的实际客户端与 GPU 推理仍需医院端验收；本机缺失 API 测试和 CPU 容器不能代替它们。

数据位于 `work/e2e-confirm-ux-compat-*`、`work/compat-offline-test/runtime`，日志 `work/compat-*.log`，截图 `output/playwright/compat-*`。离线包生成方式与执行命令见 [Docker 部署说明](DOCKER_DEPLOYMENT.md#2026-09-29-医院-chrome-93-专项离线补丁)。`test_offline_compat_update.py` 使用模拟 Docker，不访问真实服务器；生产页面脚本为 `offline-update-browser.mjs`，默认验证本机隔离容器 18087 端口，可用 `COMPAT_PRODUCTION_PORT` 修改端口，需先生成上述三帧 AVI 夹具。

模型交付的专项验收：2026-09-28 部署测试 18 项通过，完整模型包校验、下载续传与缓存复用、处理器加载及公开 Release 附件检查完成；未运行真实 GPU 推理。固定模型信息以 `model-distribution/manifest.json` 为准。

更早的环境、数量和故障过程集中保存在 [历史验证记录](archive/README.md#历史验证记录)，不作为本次修改自动通过的证明。
