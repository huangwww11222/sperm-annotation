# 项目结构：帧差规划 + SAM3 Tracking

```text
new_object_detection_integrated/
├── README_AI_TRACKING_INTEGRATION.md
├── docs/
│   ├── PROJECT_STRUCTURE.md
│   ├── AI_TRACKING_PIPELINE.md
│   └── STARTUP_WINDOWS_VENV.md
│
├── frontend/
│   ├── src/
│   │   ├── api/
│   │   │   └── trackApi.ts                 # 新增 /track/plan 调用
│   │   ├── stores/
│   │   │   └── workspace.ts                 # AI Tracking 三阶段编排
│   │   └── pages/
│   │       └── AnnotatePage.vue             # AI Tracking 文案/状态
│   ├── docs/
│   │   └── API_CONTRACT.md                  # 更新视频 Tracking 契约
│   ├── package.json
│   └── vite.config.ts
│
└── backend/
    ├── app/
    │   ├── main.py                          # /api/track/plan + 原 Tracking API
    │   ├── schemas.py                       # TrackPlanRequest/Response
    │   ├── config.py                        # 帧差/SAM3 配置
    │   ├── tracker.py                       # 原 SAM3 Tracking 主逻辑
    │   └── services/
    │       ├── annotation_seed.py           # 读取 seed JSON
    │       ├── frame_difference.py           # 帧差规划器（本次新增）
    │       ├── sam3_engine.py               # 原 SAM3 模型/session
    │       ├── anomaly_detector.py          # 原异常暂停逻辑
    │       └── visualization.py             # 原可视化
    ├── tests/
    │   ├── test_anomaly_detector.py
    │   └── test_frame_difference.py          # 本次新增测试
    ├── requirements.txt
    └── start-backend.bat
```

## 新旧职责关系

```text
旧项目：
前端 → /api/track → tracker.py → SAM3

现在：
前端
  │
  ├── /api/track/annotations
  │       ↓
  │   保存当前帧 seed JSON
  │
  ├── /api/track/plan
  │       ↓
  │   frame_difference.py
  │       ↓
  │   recommendedTrackFrames
  │
  └── /api/track
          ↓
      tracker.py
          ↓
      sam3_engine.py
```

没有把 SAM3 代码复制成第二套 tracker，也没有在前端运行 OpenCV。


## 本次 UI 更新

- 已移除“AI 检测 / 分割”按钮，AI Tracking 保留。
- 新增“选择框”工具，位于“点标注”左侧，快捷键 `V`；点击已有 bbox 即可选中，选择框模式不创建/拖动 bbox。
- 帧差规划默认开启后端逐帧日志，可由 `FRAME_DIFF_VERBOSE_LOG=0` 关闭。
