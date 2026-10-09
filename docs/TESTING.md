# 测试与日志排查

测试需要证明目标行为和核心边界；不要为纯文案反复跑 GPU，也不要以构建通过代替交互验证。

## 固定回归入口与发布门禁

修改工作区加载、认证、持久化/重试、公共 API、缓存、依赖或构建时，在专项测试之外必须运行下列固定入口。它从新的隔离目录启动自己的服务，顺序生成各套夹具，执行全部十组浏览器回归，结束后只关闭自己启动的进程。不能手工挑掉失败的套件后宣称完整回归通过。

```bash
npm ci --prefix frontend
npx --prefix frontend playwright install chromium
python3 scripts/run_browser_regression.py --python work/review-venv/bin/python --channel chromium
```

也可用本机 Chrome：`--channel chrome`。CI 使用已构建的 Python 3.12 后端镜像：`python3 scripts/run_browser_regression.py --backend-image annotation-ci-backend:cpu --channel chromium`。镜像模式仍使用独立 `work/e2e-confirm-ux-regression-*` 数据和单后端进程。`--critical-only` 仅用于定位问题，报告明确标记为子集，不能用于完整验收。

固定套件：`critical-workflows`、`workflow-transitions`、`annotation-ux`、`tracking-recovery`、`review-failure`、`confirmation-failure`、`confirmation`、`training-export`、`workbench-consistency`、`independent-integrity`。每组前按依赖重建夹具；不得并行消费或重置共享夹具。执行记录、版本/未提交文件清单、各组退出状态、耗时与日志保存在该次隔离目录；失败会非零退出并记录尚未执行的范围，不能把旧报告当成本次结果。CI 保留服务/套件日志及截图子目录；夹具 JSON 可能包含 token，禁止上传。

`independent-integrity` 保留独立审查中的行为断言：坏结构的成功响应、完整结果撤销后的 SPA 重读、跨素材迟到失败/成功、真实编码 4K 与 50,000 合成对象的读入/保存/刷新。另验证匿名/无效凭据不能读视频，HTTP 下真实 H.264 原生播放和 Range 正常，退出后不能读；真实保存已提交但响应丢失，再遇 `/auth/me` 500/网络失败/401，检查原请求保留、同账号重放与恢复后再次保存。首次新上传还从真实 Tracking/异常检测进入两个对象暂停，逐个正常确认后继续发布结果；只有模型预测被模拟。

该合成负载设置宽松 CI 停滞门槛：全量结果读取 <8 s、首次恢复 <15 s、同步人工缓存 <64 KiB、最长主线程任务 <1.5 s，并保留实测耗时。这是隔离回归预算，不是医院 GPU、GB 视频或内网延迟的 SLA。慢盘测试以可释放的哈希阻塞注入证明同事件循环的健康请求与其他媒体保存仍在 2 s 内完成；不能将它称为真实医院慢盘实测。

后端 `test_independent_integrity_audit.py` 保留独立审查用例；`test_integrity_recovery.py` 补 seed 临时写/替换失败、坏历史来源、合法点与像素边界、同事件循环慢盘上传时读取/另一视频写入、查重途中删除的竞态、长期反馈回执体积及重置后的准确重放。前端 `tracking-result.test.mjs` 通过 `npm run test:annotation` 校验结构故障组合，不能只靠静态源码断言。规范 ID 校验后，路径探针直接构造不安全目标；原子 seed 修复后，磁盘故障注入同时覆盖目标和临时文件，保留所有旧数据与错误断言。

`critical-workflows-browser.mjs` 不导入前端 store，也不预先生成工作区、AI 结果或 A/B/C 任务：真实登录 → 上传原视频 → 画框保存/刷新 → 保存成功但响应丢失 → 刷新沿用原键/原正文重放 → 再次画框保存/刷新 → 删除重传 → 取消/覆盖重标 → 追踪 → 送审 → B 改框、逐帧提交、显式完成 → C 选择、显式完成 → 下载真实 ZIP → 重新确认关闭导出。夹具只产生输入视频。CI 还在实际生产前端镜像上运行同一用例的 CPU 模式，并启用旧浏览器 API 兼容检查，避免只验证 Vite 开发页面。

完整回归中的模型输出由仅存在于 `backend/tests/browser_server.py` 的测试引擎模拟；视频解码、追踪编排、任务状态、结果发布与其余 API/数据库均走真实代码。测试入口拒绝业务数据路径，生产服务不导入它。CPU 镜像用例验证“AI 未启用后仍可人工完成流程”。两者都不代表真实 GPU 或模型准确率验收。医院 GPU 验收须另列，不能混入这些通过数量。

### 用例设计必须检查的状态组合

| 变化点 | 必须覆盖的正常路径 | 成对故障与恢复路径 |
| --- | --- | --- |
| 认证/首次进入 | 真实登录、空服务器、默认本地图片、首次上传可操作 | 账号变化后的锁只保护需要重新读取的服务器数据，不能永久锁住本地图片和导入 |
| AI 结果读取 | 尚未生成、有效零对象、有对象、人工与 AI 合并 | 断网、超时、404 媒体缺失、500、200 坏 JSON、已生成文件丢失；保留数据，恢复后可再次操作 |
| 列表/详情/原始结果下载 | 文件/标记四种组合，列表不读取全量 JSONL；旧文件首次读取补记 | marker 存在而结果丢失均明确异常；恢复真实文件后各端点恢复；坏格式/几何不能下载为成功结果 |
| 删除/覆盖 | 删除后重传、覆盖后刷新和新标注保存、取消保留旧数据 | 拒绝已送审覆盖、版本冲突、提交成功但响应丢失的原键重放 |
| 工作流转换 | 草稿→逐帧提交→显式完成 B→逐项选择→显式完成 C→真实导出 | 保存失败不增进度、不离页；100% 不自动完成；重审/重新确认立即关闭旧导出 |

- 新增读取失败保护时，必须同时证明健康空态不会被锁；新增重试时，既测试持续故障，也测试恢复后再次完成实际操作。
- 回归缺陷先补行为测试，并记录修复前的预期失败；不能用搜索某段源码存在代替行为验证。性能夹具预置结果可以用于测量，但不能替代全新上传的验收。
- 断言检查实际画框/保存/刷新后的数据和用户下一步能否完成，不能止于“接口返回 200”“页面显示空列表”“错误条消失”。不能为了通过而删除保护断言；业务契约改变时同时解释并更新正常/失败两侧用例。
- CI 的浏览器步骤与后端/单元测试同为发布条件，不使用 `continue-on-error`。发布/交付说明分别列出实际通过、失败、未执行及模拟范围。分支保护是否将工作流设为必需检查属于仓库设置，不能仅凭本地 YAML 声称已经启用。
- 子套件打印 PASS 但运行器整体退出非零时，只能记录该子套件通过及进程/清理故障，不能判定完整验收通过；修正执行环境后重跑取得实际退出码。`--critical-only` 也不能代替十组固定回归。

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
| 删除范围、人工正常反馈、重试 | `test:annotation`（含写入队列重放/账号隔离/版本冲突）；`test_annotation_controls.py`、`test_anomaly_detector.py`、`test_tracking_adaptive_feedback.py` | `annotation-delete-feedback-browser.mjs`（67；反馈/删除真后端，GPU模拟） |
| Tracking 调用/完成定位 | seed、rewind、timing、manual baseline、frontend tracking contract pytest | `annotation-tracking-ui.mjs`（14，模拟响应，无真实 GPU） |
| 4K 准备、保存锁与失败恢复 | `test_tracking_performance.py`、`test_video_timing.py`、`test_tracking_adaptive_feedback.py`；真实 4K 连续窗口/坐标、慢模型期间读写、启动重放、预览缓存和原子发布失败 | `tracking-recovery-browser.mjs`（25；真实 4K 视频/保存/抽帧，模拟模型与网络故障，查询恢复/原键重放/冲突备份） |
| 工作区初始状态、结果生命周期、删除重传 | `test_tracking_result_lifecycle.py`；尚未生成/零对象/损坏/丢失、重置回滚、缺失结果不能当空帧送审 | 固定十组回归；`critical-workflows-browser.mjs` 必须从真实登录和原视频上传开始，不预置结果 |
| 帧解码、追踪内存、大量修改项性能 | `test_video_frames.py`（逐帧身份/随机访问/失败 seek/缓存限量与原子失败/真实 processor 输入一致/掩码几何一致/首次模型 dtype）、`test_confirmation_performance.py`（5,000 项/紧凑回执/升级前重放/精确进度） | `performance-browser.mjs`（27；外部真实 4K 副本、50,000 AI 对象、初次读取失败/坏 JPEG 原位重试、C 同帧图像复用/增量回复/并发版本跳变） |
| Chrome 93 / HTTP、素材删除 | `test:compatibility`、`test_media_deletion.py` | `browser-compat-media.mjs`；下述旧 API 回归模式 |
| 送审 | `test_review_completion.py` | `review-ingress-browser.mjs`（6） |
| 撤回、重复视频覆盖、本帧重审 | `test_workflow_transitions.py`、`test_media_reimport.py`、`test_tracking_adaptive_feedback.py`；撤回/领取竞争、回滚/重放、导出门禁和审计继承 | `workflow-transitions-browser.mjs`（37；真实源视频/接口，覆盖成功响应丢失、旧窗口待重试草稿、新轮次人工记录/seed，两尺寸浅深主题） |
| B 草稿/提交/恢复 | `test_review_workflow.py`、`test:review` | `review-browser.mjs`（27）、`review-failure-browser.mjs`（19） |
| C 选择/恢复/最终版本 | `test_confirmation_workflow.py`、`test:confirmation` | `confirmation-browser.mjs`（40）、`confirmation-failure-browser.mjs`（34） |
| 训练导出/门禁/版本 | `test_training_export.py`、`test_quality_audit.py`、`test_export_audit_script.py` | `training-export-browser.mjs`（29） |
| 全窗口专注、视频进度 | 上述对应页面回归、构建 | `workspace-layout-browser.mjs`（49） |
| 三页工作台一致性、紧凑对象导航 | 上述对应页面回归、构建 | `workbench-consistency-browser.mjs`（124）；包含两尺寸浅深主题、跨页收起、专注、原生折叠、对象/修改项导航与保存 |
| 数据库查询/升级、Docker 发布 | `test_annotation_result_dedupe.py`、`test_deployment_preflight.py`、`test_docker_deployment_contract.py`、`test_model_distribution.py` | 下方容器验收；不是只运行前端构建 |
| 完整离线包、旧版识别、备份升级与回滚 | `test_offline_update.py`、`scripts/test_build_offline.ps1` | 下方离线升级验收；模拟 Docker 不代替真实导入和数据恢复 |
| 核心操作优先级、用户说明 | 上述 B/C/导出回归、构建 | `workspace-priority-browser.mjs`（36） |

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

先生成 C 夹具建立固定测试用户 ID，再生成其他夹具。UX 脚本要求路径含 `/work/e2e-confirm-ux`，生成横/竖两个 60 帧真实 AVI，并**重置测试媒体工作区**。C 夹具生成 main、zero、failure、many、portrait 五个独立任务；many 含 45 个真实修改项，用于分页导航，portrait 是 450×800 真实竖向视频，用于检查全幅主图比例和可见边界。B 夹具生成一个三帧任务。token 在 `work/*fixture.json`，不要输出到报告。

撤回/覆盖/本帧重审另运行 `work/review-venv/bin/python backend/tests/workflow_transitions_browser_fixture.py`，随后 `CONFIRMATION_ORIGIN=http://127.0.0.1:5373 node frontend/tests/workflow-transitions-browser.mjs`。使用上述同一隔离环境；夹具生成 free/pending/started/confirm 四个独立视频，每批内容含唯一标记，防止误撞上次夹具的真实 SHA。浏览器用例仅让这四个素材进入列表，消费本批任务，覆盖失败响应的原键重试会真实提交；重复运行应先生成新夹具。测试后端设 `SAM3_ENABLED=false`：只模拟 rewind 返回以检查新轮次输入，人工记录/seed 真实保存，Tracking 在 CPU 禁用边界返回 503，不执行模型。截图及 37 项结果保存在 `output/playwright/`。

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

这个脚本同时使用 B/C/UX 夹具，检查横竖素材、纵向增益、原图比例、退出恢复、手势取消、窗口缩高、离页清理、顶部进度、100% 与完成、零修改。它会改变 B 与 C 状态，跑其他状态测试前重新生成相应夹具。

`node frontend/tests/workspace-priority-browser.mjs` 同样需要 B/C/UX 夹具；检查 1366×768、1180×760 的顶部进度和右上提交可见性、C 首屏操作与完成后导出顺序、说明章节/搜索/深色主题/Markdown 原文下载，以及说明窗口中的键盘隔离、焦点循环、退出滚动恢复和章节滚动重置。它会完成 C main，运行其他 C 正常测试前需重新生成夹具。

`node frontend/tests/workbench-consistency-browser.mjs` 使用同一套 B/C/UX 夹具；`CONFIRMATION_ORIGIN` 可指定前端地址。检查三页在 1366×768、1180×760 及浅深主题下的共用区域、核心操作、列表独立滚动、跨页视频库收起和专注全宽、A/B 折叠详情的键盘行为、B 筛选定位与真实草稿、C 分组导航/回访、长文件名、45 项分页定位、说明隔离及零修改显式完成。C 完整帧还检查横竖源图四角与等比例显示、主图及局部图首屏大小、图上鼠标/键盘选择联动且不产生决定、未修改对象非交互、弹窗内图像读取和位置保存失败的分别重试。会写 B 草稿、消费 C main/zero，并浏览 many/portrait；后续 B/C 正常或故障脚本前必须重新生成对应夹具，不能并行重置同一批任务。

其他脚本按上表选择 `node frontend/tests/<脚本>.mjs`：

- `annotation-ux-browser` 前重置 UX；外观、模拟 Tracking 不需要新 C 任务，但需要可用账号。
- `tracking-recovery-browser` 前在同一隔离环境运行 `backend/tests/tracking_recovery_fixture.py`（要求 `/work/e2e-confirm-ux*`，先用 C 夹具建立用户）；测试后端开启 SAM3_ENABLED 以走真实 rewind/seed，但模型响应由浏览器模拟，不加载权重。运行 `CONFIRMATION_ORIGIN=http://127.0.0.1:5373 node frontend/tests/tracking-recovery-browser.mjs`。脚本只修改本批隔离媒体，需重跑先重新生成夹具；HTTP 30 秒计时器缩短为 300ms 复现同一取消路径，不是等待真实 30 秒。GPU 输出模拟原始帧 JSONL，保存、抽帧、冲突和原键重放使用真后端。截图/失败诊断位于 `output/playwright/tracking-recovery-*`，不能将其称为真实模型 4K 效果验收。

### 外部真实视频性能测量

保持上述三个 APP 路径隔离到 `work/`，素材会复制到测试媒体目录，原文件只读。`real_video_benchmark.py` 测量当前精确帧冷/热读取，并以独立顺序解码的 JPEG 校验原始帧身份。预处理模式需要已准备的本地模型配置及锁定的 Transformers/Torchvision，使用真实 processor，但不加载模型权重；每次内存测量用独立进程，RSS 包含 Python/Torch/processor，不含模型推理。

```bash
work/review-venv/bin/python backend/tests/real_video_benchmark.py --source-dir "/实际视频目录"
work/review-venv/bin/python backend/tests/real_video_benchmark.py --source-dir "/实际视频目录" --processor --count 120 --video "某个视频.mp4"
work/review-venv/bin/python backend/tests/real_video_benchmark.py --source-dir "/实际视频目录" --pipeline --count 120
```

`--phase before --processor --count 8` 为旧整窗口预处理的同样本对照；勿用长高分辨率窗口冒险占满测试机内存。`--pipeline` 验证真实原视频解码、标准预处理、seed、原始帧号、框坐标及 JSONL 发布，但推理输出模拟，不能用作模型精度/GPU 耗时证明。结果写入忽略的 `work/performance-*.json`。

真实素材浏览器用例先生成 C/UX/B 基础夹具，再在同一 `/work/e2e-confirm-ux-*` 环境运行 `backend/tests/real_4k_browser_fixture.py --source-dir "/实际视频目录"`；生成的测试标注不代表该视频真实目标。随后运行 `CONFIRMATION_ORIGIN=http://127.0.0.1:5373 node frontend/tests/performance-browser.mjs`，会消费本批 C 决定；重新运行先生成新任务。截图/指标在 `output/playwright/performance-results.json` 和 `real-4k-performance.png`。
- `annotation-delete-feedback-browser` 前在同一隔离环境运行 `backend/tests/annotation_controls_fixture.py`，只重置六个 `controls-*` 媒体，凭证留在 `work/annotation-controls-browser-fixture.json`。环境路径支持 `/work/e2e-confirm-ux*` 或单独 `/work/e2e-annotation-controls*`；前端地址用 `ANNOTATION_ORIGIN`。覆盖当前帧/全视频删除、统计取消/确认、跨帧撤销、刷新、正常/仅本次/实际修框、尺寸误报无须重画的人工参照保存与恢复、校准重置、两类写入成功但响应丢失的原键重放、帮助下载一致及小窗口首屏。其后续 GPU job 响应模拟，保存/反馈/seed/rewind与原视频帧走真实后端；不能称真实模型验收。
- `test_tracking_adaptive_feedback.py` 使用模拟推理输出与真实反馈持久化，复现高度 0.59 倍的逐帧暂停，验证正常确认后不重画也可继续、原形状往返、重启读取、回退帧界限，以及新尺寸异常/丢失/重叠仍暂停；这些结果不代表真实 GPU 推理。
- `review-browser` 和 `review-failure-browser` 每组前重新生成 B 夹具。
- C 正常用 main/zero，C 故障用 failure，二者可依次运行；整组重跑需新 C 夹具。
- `training-export-browser` 前重新生成 C 夹具，因为它消费 main/zero。
- 测试执行时不修改前端文件，避免 HMR 改变正在运行的实例。

Vite 调试脚本若需要访问 workspace，须导入浏览器实际已加载的模块 URL（包含可能的 `?t=`），不要无条件二次 `import('/src/stores/workspace.ts')` 建立独立实例。

结束后只停止自己启动的测试服务器；业务数据不清理。截图/控制台保存在 `output/playwright/`，命令日志放 `work/`。

### Chrome 93 / HTTP 专项回归

`browser-compat-media.mjs` 覆盖素材列表仅保留删除、本地图片删除范围提示、取消与刷新不复活，并使用真实上传的短 AVI、送审接口和删除接口；拒绝非 `work/` 夹具路径。沿用以上 B/C/UX 夹具，推荐独立数据根 `work/e2e-confirm-ux-compat-*`。启动后端 3307；前端用 `__VITE_ADDITIONAL_SERVER_ALLOWED_HOSTS=hospital-test.local API_PROXY_TARGET=http://127.0.0.1:3307 npm run dev --prefix frontend -- --host 127.0.0.1 --port 5377 --strictPort`。

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

### 完整离线升级验收

服务端工具回归使用模拟 Docker 和临时目录，制作端回归使用 PowerShell 与模拟 Git/Docker。它们不访问医院服务器、不使用业务数据；在仓库根目录运行：

```bash
APP_DATA_DIR="$PWD/work/offline-update-test-data" \
APP_DB_FILE="$PWD/work/offline-update-test-data/app.db" \
APP_STORAGE_DIR="$PWD/work/offline-update-test-storage" \
PYTHONPATH=backend work/review-venv/bin/python -m pytest backend/tests/test_offline_update.py -q
bash -n update-offline.sh
python3 scripts/check_repository.py
```

```powershell
.\scripts\test_build_offline.ps1
```

集成验收必须另建隔离 Compose 项目，沿用上方 `scripts/docker_smoke.py` 的新测试数据目录，使用已知基线镜像创建账号、素材、工作区、B/C/F 和训练包。新包按 [离线升级流程](DOCKER_DEPLOYMENT.md#医院内网可复用的离线升级流程) 构建；不能直接以可变本地源码替代指定提交快照。先执行 `--check` 并确认旧服务仍可用，再执行真实升级和 `--verify-restart`，核对镜像、版本状态与现有登录令牌/数据保留。必须另验证重复安装、坏包/未知旧版/运行配置变化拒绝、升级失败恢复、中断后 `--recover`，以及完成升级后的 `--rollback <编号> --restore-data`。回滚后核对恢复数据及回滚前数据副本；不得只凭 HTTP 200 判定保留了业务状态。

测试记录应明确区分模拟 Docker、真实 CPU 容器和真实 GPU 服务器。Windows PowerShell 脚本通过不代表 Windows Docker 已构建出 GPU 包；CPU 镜像通过不代表 Linux amd64 / NVIDIA L20 上的推理通过。GitHub Actions 结果以实际运行记录为准。

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
| `audit.captured`、`audit.export_failed/uncovered/extracted` | 自动固定快照、只读提取失败、旧包覆盖范围与提取数量；CLI 诊断在 stderr，不混入 ZIP |
| `annotation.frame_slow/frame_failed/prefetch_failed` | 媒体、目标帧、耗时；慢阈值 250ms |
| `annotation.workspace_save_failed/tracking_load_failed` | 媒体、保存序号与实际读取异常 |
| `annotation.tracking_read_failed/tracking_results_missing/completion_results_missing`、`tracking.expected_result_missing` | 区分损坏、已有结果丢失、送审被阻止及任务报告完成却无结果；正常尚未追踪不记录为故障 |
| `media.deleted/delete_failed/delete_rejected`、`annotation.media_deleted/media_delete_failed/media_list_failed` | 删除结果、媒体 ID、操作者、文件系统异常；前端网络失败不得当删除成功 |
| `annotation.local_save_failed/selection_save_failed`、`ui.preference_save_failed` | 本机存储不可用 |
| `annotation.feedback_saved`、`tracking.anomaly_pause/completed` | 正常确认原因、尺寸参照帧/来源、运动样本及实际续追起止帧；区分清除当前暂停与后续检测是否读取确认参照 |
| `tracking.rewind/decoded/task_started/task_finished/task_failed/start_replay` | JSONL 回退耗时、本轮原始尺寸/帧数/RGB 字节、模型任务与耗时、启动重放及失败堆栈；与前端 `annotation.tracking_failed` 的阶段/帧号/taskId 对齐 |
| `tracking.preview_started/preview_completed/preview_failed` | 按需整段预览编码及耗时，区分预览失败与追踪或保存失败 |
| `source.lock_slow` | 来源锁等待 ≥500ms 或占用 ≥1s 时记录操作名与 wait_ms/work_ms；用来识别其他上传/保存是否仍在阻塞，不能只看客户端 timeout 猜模型耗时 |

日志不记录 JWT/密码；不要记录每个鼠标移动。浏览器 route 注入的 503 不会出现在服务端日志；数据库触发器故障测试验证真实事务回滚与日志。

## 最近验证记录

2026-10-09 推送前验收：隔离后端全套 **430 项通过、1 项跳过**（需外部统计仓库的联调），前端单元 **37 项通过**，类型检查/生产构建通过；固定 **十组浏览器回归全部通过**，运行器退出码为 0。完整用户流程、正常与故障恢复、认证/播放/Range、迟到请求、4K 编码帧及 50,000 合成对象均按固定入口验证，模型预测使用测试引擎模拟。此轮使用 macOS CPU 与本机 Chrome，未验证医院 CUDA 或实际 Chrome 93 引擎，也不等于新提交远程 Actions 已通过。日志在 `work/push-1009/`，完整范围在 `work/e2e-confirm-ux-regression-511dd565bc/run.json`，测试服务已由运行器清理。

此前生命周期、独立审查、4K 性能、部署及审计联调的过程和数量统一保存在 [历史验证记录](archive/README.md#历史验证记录)，不作为当前提交通过的证明。

### 自动审计与统计仓库联调

新增审计、脚本与离线升级用例：

```bash
AUDIT_INTEROP_DIR="$PWD/work/audit-tests/interop" \
APP_DATA_DIR="$PWD/work/audit-tests/data" APP_DB_FILE="$PWD/work/audit-tests/data/app.db" \
APP_STORAGE_DIR="$PWD/work/audit-tests/storage" PYTHONPATH=backend \
work/review-venv/bin/python -m pytest backend/tests/test_quality_audit.py backend/tests/test_export_audit_script.py backend/tests/test_training_export.py backend/tests/test_offline_update.py -q
```

interop 夹具生成真实 YOLO/COCO/双格式、多视频及重新确认前后的完整训练包和一份运维审计包。独立统计仓库检出到 `标注统计/` 时，将 `ANNOTATION_AUDIT_INTEROP_DIR` 指向该绝对目录，在统计仓库的 PYTHONPATH 下运行它的 pytest；两个进程不共用名为 app 的模块。没有设置联调路径时用例明确跳过。统计真实浏览器入口及运行方式见该仓库当前说明。

容器 smoke 可设置 `SMOKE_STATE_FILE` 指向本次隔离 work 目录，避免覆盖其他测试的令牌文件。CPU 容器生成训练集后，用 `export-audit.sh` 实际提取，再让统计 CLI 导入；测试包含只读连接、输出拒绝覆盖、失败无半包，以及升级/恢复时运维脚本一起还原。
