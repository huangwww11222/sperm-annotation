# AI 开发入口

本项目是 Vue 3 / TypeScript + 单进程 FastAPI / SQLite 的视频标注工具。本文面向接手代码的 AI agent；按任务阅读下列当前文档，不必遍历历史材料。

## 开始工作

1. 读 [当前流程与不可变规则](docs/WORKFLOW.md) 和 [代码地图](docs/PROJECT_STRUCTURE.md)。先检查 `git status`，保留已有未提交改动。
2. 标注、追踪、画布或快捷键任务读 [ANNOTATION.md](docs/ANNOTATION.md)。接口或数据库任务读 [API.md](docs/API.md) 与 [STORAGE_LAYOUT.md](docs/STORAGE_LAYOUT.md)。
3. 修改前确定 [TESTING.md](docs/TESTING.md) 中与任务对应的测试；在隔离数据上复现，按日志定位失败，再做修复和回归。
   修改工作区加载、认证、读写/重试、公共 API、缓存或依赖/构建时，必须执行 TESTING.md 的固定浏览器回归入口；不能只挑本次功能的专项用例。缺陷先补能在旧行为下失败的行为测试，正常状态与故障恢复成对验证。数量多、构建通过、静态字符串断言均不能替代用户完整流程。
4. 用户当前明确要求优先。代码和当前文档不一致时调查原因，不能从旧原型、旧报告或旧注释推断现有规则。

## 关键边界

- 当前流程（2026-11-01）：标注 → 审查 → 导出，只有两个页面。首尾双向追踪默认识别边缘入画。审查是 `AnnotatePage` 的 `mode="review"`：同一画布、同一工作区，审查员直接改标注，没有 A/B 副本；系统生成清单（未标注帧、追踪疑点、计数突变、整帧漏标检查、随机抽检，抽检错多时加抽），全部检查后显式“审查通过”，记录修改与审查人。规则见 WORKFLOW。
- 只有审查通过且内容未再改动的视频才能导出（服务端 `annotation_review.require_passed`）。任何改动使通过失效，重新审查只查改动部分。浏览、勾选清单都不等于通过。
- **允许同一账号标注并审查**，分别记录。
- 旧对比确认页、旧审查页、送审/撤回/领取及其接口已删除；A/B/C/F 表与数据只读保留给统计/审计导出，不能再锁住工作区，也不能恢复这些页面。说明见 `docs/archive/旧审查与对比确认流程_2026-11-01前.md`。
- 真实原始帧号；API 从 0 开始，页面从 1 开始。人工标注工作区坐标为 0–100 百分比；B/C/seed 是原图像素 xyxy；不要混用。
- `objectId` 稳定且独立于数组顺序。未知帧不能当空帧，缺失媒体不能用占位图导出。
- 保留版本校验、幂等键、事务回滚、失败待重试状态和日志。不要把网络失败当成功或空数据。
- 内网客户端兼容 Chrome 93 / HTTP；超时和 UUID 使用 `frontend/src/utils/browserCompat.ts`，不要直接依赖新版 API。素材列表仅保留经确认的删除，不提供“关闭素材”；服务器视频与本地图片的删除范围须明确，被历史审查基准引用的源视频不可删。
- 当前部署只支持一个后端进程/worker。测试设置独立 `APP_DATA_DIR`、`APP_DB_FILE`、`APP_STORAGE_DIR`，禁止向用户业务目录生成夹具。

## 文档维护

人工标注与审查共用画面优先外壳 `WorkbenchShell.vue` 与 `workbench/shell.css`（上下文进站点顶栏、抽屉式视频列表、底部帧条/导航、可收起检查面板，见 WORKFLOW.md）。以后新增、删除或调整功能，遵守 [工作台布局与扩展规则](docs/WORKFLOW.md#工作台布局与扩展规则)：顶部状态、左侧视频、中间图像、图下导航、右上主操作、右下对象或修改项；次要详情按需展开。不要另起一套页面布局或重复常驻统计。用户可见调整同时维护使用说明，并验证三页在小窗口和浅深主题中的位置、可见性及原有流程。

入口与阅读顺序见 [docs/README.md](docs/README.md)。改变稳定规则、接口、运行方式时直接更新对应当前文档；测试复现命令集中在 TESTING.md。不要每次改动都新增“本次实现/最终审查/优化总结”文档。

用户可见流程或快捷键改变时，同步维护 [用户使用说明](frontend/src/help/user-guide.md)。它是前端说明窗口与 Markdown 下载的共同来源；不要复制成另一份长期维护的手册。

`docs/archive/` 只用于追溯，`docs/*.html` 只作为历史视觉参考；不是当前实现契约。日志、截图、临时分析放在 `work/`、`output/playwright/`，不把测试 token 或业务数据写入文档。

## 第三方交付

根 README 面向安装者，`deploy.sh` / `deploy.ps1` 初始化配置并启动；首次默认 GPU + AI，CPU 仅人工模式须显式选择；已有 .env 不自动切换。CPU 基础模式与 GPU 覆盖配置分开。部署修改须读 DOCKER_DEPLOYMENT.md，使用隔离 Compose 项目验证；不能把 CPU 容器测试当成真实 GPU 验收。提交前运行 `python3 scripts/check_repository.py`，运行数据和大型 SAM3 权重不纳入 Git，也不进入 Docker build context。用户已明确要求开发者拉取即获得自训练检测器：backend/app/model_assets/head-detector.pt（约19.4 MiB）是唯一随普通 Git 交付的小模型例外，附固定清单和许可，随 app 进入镜像；不能扩展为上传其他权重或业务数据。SAM3 随仓库 Release 提供，Git 保留 model-distribution/ 的固定清单与许可；部署先运行独立 model-setup 容器，不把公开模型包误当用户业务数据清理。`db.py` 的查询接口必须保留可选 user_id/media_id/source 过滤，同名视频按 media_id 区分，不能在合并时恢复重复函数。

医院已有部署通过 `build-offline.ps1` 制作固定提交的完整镜像包，再用 `update-offline.sh` 离线升级。不能只更换脚本或沿用旧 images.tar；不能解压到新目录后以相对 runtime 路径重建一套空数据。升级器保留实际运行配置、校验已知旧后端或托管版本身份、停写备份后切换；`.offline/` 是运维状态和备份，不纳入 Git，也不自动清理。`scripts/offline_legacy.json` 的 `contract` 是存储/运行兼容承诺，新增破坏性数据库、环境或模型变更必须实现迁移并调整契约，不能把未知版本加入白名单绕过检查。回滚必须明确恢复同批数据，并保存回滚时的新数据副本。
