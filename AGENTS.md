# AI 开发入口

本项目是 Vue 3 / TypeScript + 单进程 FastAPI / SQLite 的视频标注工具。本文面向接手代码的 AI agent；按任务阅读下列当前文档，不必遍历历史材料。

## 开始工作

1. 读 [当前流程与不可变规则](docs/WORKFLOW.md) 和 [代码地图](docs/PROJECT_STRUCTURE.md)。先检查 `git status`，保留已有未提交改动。
2. 标注、追踪、画布或快捷键任务读 [ANNOTATION.md](docs/ANNOTATION.md)。接口或数据库任务读 [API.md](docs/API.md) 与 [STORAGE_LAYOUT.md](docs/STORAGE_LAYOUT.md)。
3. 修改前确定 [TESTING.md](docs/TESTING.md) 中与任务对应的测试；在隔离数据上复现，按日志定位失败，再做修复和回归。
4. 用户当前明确要求优先。代码和当前文档不一致时调查原因，不能从旧原型、旧报告或旧注释推断现有规则。

## 关键边界

- A 原始快照永久保留；B 仅移动/缩放已有框，逐帧显式提交，再显式完成视频；C 仅选择 A/B，再显式完成确认。浏览、草稿保存、进度 100% 都不等于完成视频。
- **允许同一账号标注、自审、自确认。** 别人已领取的任务仍不能编辑。不恢复旧的“三人必须不同”限制。
- 训练数据集只能从当前已审查、已确认的最终 F 导出。人工标注页和旧接口不能绕过；重新确认时旧训练包停止下载。
- 真实原始帧号；API 从 0 开始，页面从 1 开始。人工标注工作区坐标为 0–100 百分比；B/C/seed 是原图像素 xyxy；不要混用。
- `objectId` 稳定且独立于数组顺序。未知帧不能当空帧，缺失媒体不能用占位图导出。
- 保留版本校验、幂等键、事务回滚、失败待重试状态和日志。不要把网络失败当成功或空数据。
- 当前部署只支持一个后端进程/worker。测试设置独立 `APP_DATA_DIR`、`APP_DB_FILE`、`APP_STORAGE_DIR`，禁止向用户业务目录生成夹具。

## 文档维护

入口与阅读顺序见 [docs/README.md](docs/README.md)。改变稳定规则、接口、运行方式时直接更新对应当前文档；测试复现命令集中在 TESTING.md。不要每次改动都新增“本次实现/最终审查/优化总结”文档。

用户可见流程或快捷键改变时，同步维护 [用户使用说明](frontend/src/help/user-guide.md)。它是前端说明窗口与 Markdown 下载的共同来源；不要复制成另一份长期维护的手册。

`docs/archive/` 只用于追溯，`docs/*.html` 只作为历史视觉参考；不是当前实现契约。日志、截图、临时分析放在 `work/`、`output/playwright/`，不把测试 token 或业务数据写入文档。

## 第三方交付

根 README 面向安装者，`deploy.sh` / `deploy.ps1` 初始化配置并启动；首次默认 GPU + AI，CPU 仅人工模式须显式选择；已有 .env 不自动切换。CPU 基础模式与 GPU 覆盖配置分开。部署修改须读 DOCKER_DEPLOYMENT.md，使用隔离 Compose 项目验证；不能把 CPU 容器测试当成真实 GPU 验收。提交前运行 `python3 scripts/check_repository.py`，运行数据和模型不纳入 Git，也不进入 Docker build context。`db.py` 的查询接口必须保留可选 user_id/media_id/source 过滤，同名视频按 media_id 区分，不能在合并时恢复重复函数。
