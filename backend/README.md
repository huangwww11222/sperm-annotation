# FastAPI 后端

在 `backend` 创建 Python 虚拟环境并安装 `requirements.txt`，运行 `python -m uvicorn app.main:app --host 127.0.0.1 --port 3000`。保持单进程 / 单 worker。

- [AI 开发入口](../AGENTS.md)
- [代码地图与配置](../docs/PROJECT_STRUCTURE.md)
- [业务状态与不变量](../docs/WORKFLOW.md)
- [接口](../docs/API.md)、[存储](../docs/STORAGE_LAYOUT.md)、[测试与日志](../docs/TESTING.md)
- [Windows](../docs/DEPLOYMENT_WINDOWS.md)、[Docker](../docs/DOCKER_DEPLOYMENT.md)

SAM3 模型在首次真实 Tracking 时加载；UI、审查、确认和导出测试不等同于 GPU 推理验收。
