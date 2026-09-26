# 测试与日志排查

基线：2026-09-27。测试需要证明目标行为和核心边界；不要为纯文案反复跑 GPU，也不要以构建通过代替交互验证。

## 先隔离数据

后端导入/启动会初始化 DB 和表。运行 pytest、夹具、调试服务器前同时指定 `APP_DATA_DIR`、`APP_DB_FILE`、`APP_STORAGE_DIR`，不要使用业务默认目录。夹具只允许 `work/` 下规定的路径。旧 `backend/track_data` 仍可能被兼容读取，浏览器标注测试应筛选测试媒体，不能随便点用户素材。

下列命令在项目根目录执行。`work/review-venv/bin/python` 是本机已有测试解释器；其他机器替换为安装了后端依赖的 Python，例如 `backend/.venv/bin/python`；Windows 见 [部署说明](DEPLOYMENT_WINDOWS.md)。测试脚本额外使用 `httpx`（FastAPI TestClient）。

```bash
APP_DATA_DIR="$PWD/work/agent-test-data" \
APP_DB_FILE="$PWD/work/agent-test-data/app.db" \
APP_STORAGE_DIR="$PWD/work/agent-test-storage" \
PYTHONPATH=backend work/review-venv/bin/python -m pytest backend/tests -q

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
| Tracking 调用/完成定位 | seed、rewind、timing、manual baseline、frontend tracking contract pytest | `annotation-tracking-ui.mjs`（8，模拟响应，无真实 GPU） |
| 送审 | `test_review_completion.py` | `review-ingress-browser.mjs`（6） |
| B 草稿/提交/恢复 | `test_review_workflow.py`、`test:review` | `review-browser.mjs`（27）、`review-failure-browser.mjs`（17） |
| C 选择/恢复/最终版本 | `test_confirmation_workflow.py`、`test:confirmation` | `confirmation-browser.mjs`（39）、`confirmation-failure-browser.mjs`（30） |
| 训练导出/门禁/版本 | `test_training_export.py` | `training-export-browser.mjs`（29） |
| 全窗口专注、视频进度 | 上述对应页面回归、构建 | `workspace-layout-browser.mjs`（49） |
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
- `review-browser` 和 `review-failure-browser` 每组前重新生成 B 夹具。
- C 正常用 main/zero，C 故障用 failure，二者可依次运行；整组重跑需新 C 夹具。
- `training-export-browser` 前重新生成 C 夹具，因为它消费 main/zero。
- 测试执行时不修改前端文件，避免 HMR 改变正在运行的实例。

Vite 调试脚本若需要访问 workspace，须导入浏览器实际已加载的模块 URL（包含可能的 `?t=`），不要无条件二次 `import('/src/stores/workspace.ts')` 建立独立实例。

结束后只停止自己启动的测试服务器；业务数据不清理。截图/控制台保存在 `output/playwright/`，命令日志放 `work/`。

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
| `annotation.local_save_failed/selection_save_failed`、`ui.preference_save_failed` | 本机存储不可用 |

日志不记录 JWT/密码；不要记录每个鼠标移动。浏览器 route 注入的 503 不会出现在服务端日志；数据库触发器故障测试验证真实事务回滚与日志。

## 最近验证记录

2026-09-27 的操作优先级与用户说明调整：新增检查 **35**、专注/布局 **49**、B 正常 **27**、C 正常 **39**、C 故障恢复 **30**、训练导出 **29**、外观/记录 **14** 均通过，浏览器本轮合计 **223 项**。后端全量 **133 通过**，前端几何/缓存 **5+6+4 通过**，类型检查和生产构建通过；当前和归档文档的 **148 个本地链接**无失效。

使用 `work/e2e-confirm-ux-priority-*` 独立数据；命令日志 `work/priority-*.log`，新增截图 `output/playwright/priority-*`。依据浏览器断言与滚动位置记录修复了说明窗口的 Tab 焦点循环、Esc 关闭时滚动锁清理，以及切换章节时旧键盘滚动动画延续的问题。训练回归实际生成并检查 YOLO/COCO ZIP，验证重新确认期间和旧版本的下载门禁。未运行真实 GPU 推理或 Docker/Windows 部署。

同日较早的专注/进度调整：新增布局浏览器检查 **49 通过**；后端全量 **133 通过**；前端几何/缓存 **5+6+4 通过**；类型检查及生产构建通过。人工标注 **48**、外观/记录 **14**、B 正常 **27**、C 正常 **39**、C 故障恢复 **30**、训练导出 **29** 均通过，该轮浏览器合计 **236 项**。

较早一轮测试用 `work/e2e-confirm-ux-layout-*` 独立数据。命令日志 `work/layout-*.log`，截图 `output/playwright/layout-*`（同名布局脚本重跑会更新截图）。该轮浏览器回归发现了布局调整后已完成区域被多余模板层隐藏的问题；依据截图与未发出下载请求的日志修复后，重新验证了完整 JSON 下载、训练 ZIP、重新确认及旧包禁用。也未运行真实 GPU 推理或 Docker/Windows 部署，不能据此声称这些环境验收完成。此前每阶段的验证保留在 [历史目录](archive/README.md)。
