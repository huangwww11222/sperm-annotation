# 历史资料

以下材料仅用于追溯，**不属于当前需求、待办或验收依据**。模型默认从 [当前文档索引](../README.md) 阅读，只有需要了解某项历史决策时才展开这里。

旧文中提到的权限限制、接口、未实现状态、测试数量和截图反映当时版本；不得覆盖用户后来确认的自审、自确认、训练导出门禁及当前界面布局。原文保留，链接按新位置修正。

| 文档 | 追溯价值 |
| --- | --- |
| [AI_TRACKING_REWIND_BRANCH.md](AI_TRACKING_REWIND_BRANCH.md) | 旧追踪约定；当前边界见 ANNOTATION.md |
| [API_CONTRACT.md](API_CONTRACT.md) | 旧模块/API 构想，含已过时说明 |
| [ID_STABILITY_CHECKLIST.md](ID_STABILITY_CHECKLIST.md) | 旧追踪约定；当前边界见 ANNOTATION.md |
| [README_MODULARIZATION.md](README_MODULARIZATION.md) | 旧模块/API 构想，含已过时说明 |
| [STARTUP_WINDOWS_VENV.md](STARTUP_WINDOWS_VENV.md) | 历史方案或运行说明 |
| [backend_README.md](backend_README.md) | 旧模块/API 构想，含已过时说明 |
| [frontend_README.md](frontend_README.md) | 旧模块/API 构想，含已过时说明 |
| [review_demo_最终审查与开发交接.md](review_demo_最终审查与开发交接.md) | 原型演变与当时发现的问题 |
| [review_demo_第二轮审查报告.md](review_demo_第二轮审查报告.md) | 原型演变与当时发现的问题 |
| [review_demo_需求审查报告.md](review_demo_需求审查报告.md) | 原型演变与当时发现的问题 |
| [审查模式_功能交互说明.md](审查模式_功能交互说明.md) | 历史方案或运行说明 |
| [审查模式_后端数据与接口设计.md](审查模式_后端数据与接口设计.md) | 历史方案或运行说明 |
| [审查模式_实现与测试.md](审查模式_实现与测试.md) | 当时的实现范围、故障排查与测试证据 |
| [对比确认_实现与测试.md](对比确认_实现与测试.md) | 当时的实现范围、故障排查与测试证据 |
| [标注体验与统一界面_优化与测试.md](标注体验与统一界面_优化与测试.md) | 当时的实现范围、故障排查与测试证据 |
| [训练数据集导出_迁移与测试.md](训练数据集导出_迁移与测试.md) | 当时的实现范围、故障排查与测试证据 |

历史原型：[审查 HTML](../review_demo.html)、[确认 HTML](../annotation-confirm-demo.html)。原型不随当前开发维护。

## 文档整合来源

| 原材料 | 处理 | 当前去向 |
| --- | --- | --- |
| 审查功能说明、后端目标设计、最终开发交接 | 提取仍有效规则；原文归档 | WORKFLOW、API、STORAGE_LAYOUT |
| B / C / 训练导出 / 标注体验的“实现与测试” | 合并现有实现、恢复规则、日志及复现方式；阶段数字留在历史 | WORKFLOW、ANNOTATION、TESTING |
| 三轮 review_demo 审查报告 | 全部归档，不再当待办清单 | [历史目录](README.md) |
| ID 稳定性、追踪回退两篇短文 | 合并并对照代码纠正回退边界（保留当前 N 帧） | ANNOTATION |
| Windows venv 启动说明 | 合入 Windows 部署；旧正文归档 | DEPLOYMENT_WINDOWS |
| 前端旧接口契约、模块化说明、前后端旧 README | 归档过时 Mock、固定第一帧和默认 5 帧说明；README 改成入口 | API、PROJECT_STRUCTURE |
| 项目结构、存储、Windows、Docker | 保留文件名，更新当前事实与交叉引用 | 同名当前文档 |
| HTML 原型 | 保留，不当运行代码或最新视觉规范 | [审查原型](../review_demo.html)、[确认原型](../annotation-confirm-demo.html) |

原资料保留在本目录，每篇标有“历史归档，非当前开发依据”。已失效的规则包括“禁止自审”“登录只是 Mock”“所有视频只标第 0 帧”“原始标注直接导出训练集”“Tracking 默认 5 帧”。历史报告的“未实现”不表示当前仍未实现。

## 历史验证记录

以下为各次开发当时的验证结果与限制；当前运行方法及最新结果见 [TESTING.md](../TESTING.md)。

2026-09-28 模型随仓库交付：固定官方 facebook/sam3 快照，权重 SHA256 `6d06f0a5f84e435071fe6603e61d0b4cc7b40e0d39d487cfd4d67d8cc11cc14a`，所有随包配置/许可与官方 Git blob 一致。模型以仓库 Release 附件提供，Git 仅记录清单、完整许可和安装工具。部署相关 **18 项 pytest 通过**，覆盖分块续传、SHA256 失败不发布模型、完整安装与缓存复用、自备模型不覆盖、关闭下载、路径防护及模型失败停止部署；PowerShell 7.4 模拟 Docker 默认/GPU/CPU 三组通过。真实 3.44 GB 权重经本机 HTTP 分块下载/组装、完整 SHA256 和再次零下载复用验证；实际 model-setup Compose 容器通过缓存校验。生产 CPU 镜像能从该包加载 Sam3TrackerVideoProcessor，safetensors 1797 个张量头可读取。日志 `work/model-distribution-*.log`、`work/model-full-restore.log`、`work/model-setup-container.log`、`work/model-processor-check.log`。15 个公开模型 Release 附件的远端大小和 SHA256 与本地包一致；匿名下载已核对清单、许可、处理器配置及四个权重块开头，确认无需模型站凭据。Windows CI 发现模拟失败留下 LASTEXITCODE=42，虽然断言全通过，runner 仍按失败退出；测试清理阶段重置该模拟退出码，保留部署脚本对真实失败的检查。未运行真实 GPU 推理。

2026-09-27 默认 AI 部署调整：首次无参数 Bash/PowerShell 安装及 `.env.docker.example` 默认选择 GPU + AI；显式 CPU 及已有配置保持原行为。部署相关 pytest **14 通过**，含真实 Compose 解析默认模板/GPU/CPU 三种配置；PowerShell 7.4 容器中默认/GPU/CPU 三组模拟 Docker 安装测试通过，验证重复执行保留密钥与模式冲突保护。日志 `work/ai-default-tests.log`、`work/ai-default-powershell.log`。本轮未运行真实 GPU 推理。

2026-09-27 的第三方 Docker 交付整理：先复现了本地 `list_annotations(..., source=...)` 的 TypeError，再修复查询、同名视频身份与旧库补列/建索引顺序。本机后端 **146 通过**；实际 Python 3.12 Linux ARM64 CPU 镜像中 **145 通过、1 跳过**（镜像内不安装 Docker CLI，Compose 解析项在宿主机已通过）。生产前后端镜像构建通过，后端 `pip check` 通过，SAM3 Tracker Model/Processor 可导入；实际版本为 Python 3.12.14、NumPy 2.5.3、torch 2.14.0+cpu。

实际隔离 Compose 从空库完成 SPA/健康检查、注册、真实 AVI 上传、人工记录保存/查询、工作区保存、B/C 显式完成、YOLO 真实图片和标签导出；删除并重建容器后，原登录令牌、工作区、确认版本与 ZIP 均保留。Bash CPU/GPU 初始化保护由 pytest 覆盖；PowerShell 7.4 容器内通过 CPU/GPU 两组模拟 Docker 的安装配置测试，**不是 Windows/WSL2 实机验收**。仓库检查、155 个本地文档链接检查通过；从 Git 索引移除的 164 个运行文件仍全部保留在本地。

测试项目 `annotation-deploy-audit`，数据位于 `work/docker-deploy-release-check`；日志 `work/deploy-*.log`。本机 Docker Hub 令牌端点连接重置，使用 Docker Official Images 的 ECR Public 副本取得基础镜像后完成构建，没有修改用户 Docker 镜像源或业务配置。此次未运行真实 CUDA/模型权重推理，未在 GitHub 触发 CI，也未发布镜像；新增 CI 发布流程需要推送后执行。测试期间未使用生产 DB/storage。

2026-09-27 的操作优先级与用户说明调整：新增检查 **35**、专注/布局 **49**、B 正常 **27**、C 正常 **39**、C 故障恢复 **30**、训练导出 **29**、外观/记录 **14** 均通过，浏览器本轮合计 **223 项**。后端全量 **133 通过**，前端几何/缓存 **5+6+4 通过**，类型检查和生产构建通过；当前和归档文档的 **148 个本地链接**无失效。

使用 `work/e2e-confirm-ux-priority-*` 独立数据；命令日志 `work/priority-*.log`，新增截图 `output/playwright/priority-*`。依据浏览器断言与滚动位置记录修复了说明窗口的 Tab 焦点循环、Esc 关闭时滚动锁清理，以及切换章节时旧键盘滚动动画延续的问题。训练回归实际生成并检查 YOLO/COCO ZIP，验证重新确认期间和旧版本的下载门禁。未运行真实 GPU 推理或 Docker/Windows 部署。

同日较早的专注/进度调整：新增布局浏览器检查 **49 通过**；后端全量 **133 通过**；前端几何/缓存 **5+6+4 通过**；类型检查及生产构建通过。人工标注 **48**、外观/记录 **14**、B 正常 **27**、C 正常 **39**、C 故障恢复 **30**、训练导出 **29** 均通过，该轮浏览器合计 **236 项**。

较早一轮测试用 `work/e2e-confirm-ux-layout-*` 独立数据。命令日志 `work/layout-*.log`，截图 `output/playwright/layout-*`（同名布局脚本重跑会更新截图）。该轮浏览器回归发现了布局调整后已完成区域被多余模板层隐藏的问题；依据截图与未发出下载请求的日志修复后，重新验证了完整 JSON 下载、训练 ZIP、重新确认及旧包禁用。也未运行真实 GPU 推理或 Docker/Windows 部署，不能据此声称这些环境验收完成。此前每阶段的验证保留在 [历史目录](#历史资料)。
