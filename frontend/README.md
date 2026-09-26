# Vue 前端

从项目根目录运行 `npm ci --prefix frontend`，然后 `npm run dev --prefix frontend`。默认 Vite 5173 代理到 FastAPI 3000；可用 `API_PROXY_TARGET` 覆盖。

- [AI 开发入口](../AGENTS.md)
- [代码地图与配置](../docs/PROJECT_STRUCTURE.md)
- [标注交互与快捷键](../docs/ANNOTATION.md)
- [当前接口](../docs/API.md)
- [测试与日志](../docs/TESTING.md)

生产构建：`npm run build --prefix frontend`，包含 Vue / TypeScript 类型检查。
