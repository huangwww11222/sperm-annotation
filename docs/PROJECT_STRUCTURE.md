# 项目结构

```text
E:\\1/
├─ frontend/                         # Vue 3 + Vite 浏览器应用
│  ├─ src/api/                       # HTTP API 封装、Token 注入
│  ├─ src/pages/                     # 登录、标注、结果页面
│  ├─ src/stores/                    # 前端状态与交互编排
│  ├─ src/types/                     # 共享 TypeScript 类型
│  └─ vite.config.ts                 # 开发期 /api 代理
├─ backend/                          # FastAPI 服务
│  ├─ app/main.py                    # API 入口、认证、媒体、导出端点
│  ├─ app/tracker.py                 # Tracking 编排、结果持久化、可视化
│  ├─ app/services/                  # SAM3、帧差、异常检测等领域服务
│  ├─ tests/                         # 后端回归测试
│  ├─ data/app.db                    # SQLite 业务索引
│  └─ storage/                       # 运行时用户数据（不提交版本库）
│     ├─ media/                      # 原视频与每个视频的追踪资产
│     └─ datasets/                   # 导出的训练数据集 ZIP
└─ docs/                             # 部署、存储和流程文档
```

## 前后端职责

| 层 | 职责 |
|---|---|
| `frontend` | 显示原始/逐帧预览，编辑人工框，发起追踪与导出请求，展示异常诊断。 |
| `backend/app/main.py` | REST API、权限、上传、媒体元数据、任务状态和数据集下载。 |
| `backend/app/tracker.py` | 用原始帧号调用 SAM3，将结果和异常写入媒体目录。 |
| `backend/app/services` | 独立的模型适配、帧差规划、异常检测与视频绘制能力。 |
| `backend/storage` | 与源码、Python 虚拟环境、模型目录分离的用户数据。 |

开发时 Vite 将 `/api` 代理到 FastAPI；部署时由 Nginx/Caddy/IIS 托管 `frontend/dist` 并将 `/api` 反向代理给 FastAPI。详细数据位置见 [STORAGE_LAYOUT.md](STORAGE_LAYOUT.md)，部署见 [DEPLOYMENT_WINDOWS.md](DEPLOYMENT_WINDOWS.md)。
