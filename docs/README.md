# 当前文档索引

维护基线：2026-09-27。读者主要是后续 AI 编程工具。这里描述当前实现和必须保持的行为，不再按开发轮次组织需求。

## 按任务阅读

| 文档 | 唯一职责 | 何时阅读 |
| --- | --- | --- |
| [WORKFLOW.md](WORKFLOW.md) | A → B → C → F → 训练集；状态、权限、数据流、验收边界 | 所有业务修改前 |
| [PROJECT_STRUCTURE.md](PROJECT_STRUCTURE.md) | 实际代码地图、启动、配置入口和兼容层 | 接手项目、找实现 |
| [ANNOTATION.md](ANNOTATION.md) | 人工标注、稳定 ID、追踪回退、画布、快捷键与性能 | 改标注体验或追踪 |
| [API.md](API.md) | 当前路由、结构、并发/幂等/错误规则及源码入口 | 改接口、数据结构 |
| [STORAGE_LAYOUT.md](STORAGE_LAYOUT.md) | 数据落点、A/B/F 表、备份、迁移与清理边界 | 改持久化、部署、排查数据 |
| [TESTING.md](TESTING.md) | 测试选择、隔离夹具、运行顺序、日志排查与最近验证 | 实现与交付 |
| [DEPLOYMENT_WINDOWS.md](DEPLOYMENT_WINDOWS.md) | Windows 本机运行、生产部署与验收 | Windows 环境 |
| [DOCKER_DEPLOYMENT.md](DOCKER_DEPLOYMENT.md) | 单后端容器、GPU、卷、更新和恢复 | Docker 环境 |

根目录 [AGENTS.md](../AGENTS.md) 是模型默认入口；前后端 README 只提供所在目录的入口链接。

## 给工具使用者的说明

[用户使用说明](../frontend/src/help/user-guide.md) 面向标注员、审查员及确认人员，覆盖完整流程、保存与续做、快捷键、训练集导出及常见问题。前端顶部“使用说明”支持按当前页面打开章节、搜索和下载同一份 Markdown。源文件放在前端内以随部署一起打包，避免维护两份内容；它不替代上表中的开发契约。

## 本次整合决策

| 原材料 | 处理 | 当前去向 |
| --- | --- | --- |
| 审查功能说明、后端目标设计、最终开发交接 | 提取仍有效规则；原文归档 | WORKFLOW、API、STORAGE_LAYOUT |
| B / C / 训练导出 / 标注体验的“实现与测试” | 合并现有实现、恢复规则、日志及复现方式；阶段数字留在历史 | WORKFLOW、ANNOTATION、TESTING |
| 三轮 review_demo 审查报告 | 全部归档，不再当待办清单 | [历史目录](archive/README.md) |
| ID 稳定性、追踪回退两篇短文 | 合并并对照代码纠正回退边界（保留当前 N 帧） | ANNOTATION |
| Windows venv 启动说明 | 合入 Windows 部署；旧正文归档 | DEPLOYMENT_WINDOWS |
| 前端旧接口契约、模块化说明、前后端旧 README | 归档过时 Mock、固定第一帧和默认 5 帧说明；README 改成入口 | API、PROJECT_STRUCTURE |
| 项目结构、存储、Windows、Docker | 保留文件名，更新当前事实与交叉引用 | 同名当前文档 |
| HTML 原型 | 保留，不当运行代码或最新视觉规范 | [审查原型](review_demo.html)、[确认原型](annotation-confirm-demo.html) |

原资料保留在 `archive/`，每篇标有“历史归档，非当前开发依据”。已失效的规则包括“禁止自审”“登录只是 Mock”“所有视频只标第 0 帧”“原始标注直接导出训练集”“Tracking 默认 5 帧”。历史报告的“未实现”不表示当前仍未实现。

## 以后怎样维护

- 用户确认的新要求更新到对应当前文档，必要时补测试；不不断追加新交接文档。
- 只有重大方案的取舍需要独立决策记录。日志、调试过程、截图和临时测试输出不进入默认阅读清单。
- 记录一次验证必须注明范围与限制，不能把曾经通过的数量当作以后每次提交的通过结果。
- 文档移动后检查相对链接；命令引用必须指向仍存在的脚本。模型仅在追溯具体原因时读取历史目录。
