# Unified FastAPI Backend

现在整个后端只有 **一个 FastAPI 进程、一个端口 3000**。

```text
Vue + Vite
   │ /api/*
   ▼
FastAPI :3000
   ├─ 登录 / 注册 / JWT
   ├─ 人工标注 SQLite
   ├─ MP4 上传与视频播放
   └─ SAM3 Tracking
        └─ 本地 SAM3 + PyTorch + GPU
```

不再启动 Node，也不再启动第二个 uvicorn Worker。

## 启动

第一次安装：

```powershell
cd backend
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
```

确保本地 SAM3 位于：

```text
backend/track_modul/facebook--sam3/snapshots/master
```

模型目录不同可以设置：

```powershell
$env:SAM3_MODEL_ID="E:\add_login\backend\track_modul\facebook--sam3\snapshots\master"
```

GPU 默认：

```text
SAM3_DEVICE=cuda
SAM3_DTYPE=bfloat16
```

启动唯一后端：

```powershell
cd backend
python -m uvicorn app.main:app --host 127.0.0.1 --port 3000
```

或者 Windows：

```text
backend/start-backend.bat
```

然后再启动前端：

```powershell
cd frontend
npm run dev
```

## Tracking

点击“AI Tracking”后：

1. FastAPI 创建任务；
2. 单线程 GPU 队列执行，避免 4GB 显存同时跑多个 SAM3；
3. SAM3 第一次 Tracking 时加载模型，后续复用同一个模型实例；
4. 每次从当前人工标注帧向后处理 `SAM3_TRACK_FRAMES` 帧（默认 5 帧）；
5. 结果合并到对应视频目录的 `tracker_results.json`（逐行 JSONL，结构与 01_test 的 `tracker_results.json` 一致）；
6. 前端继续按 `frameIndex` 显示对应 bbox。

Tracking 进行期间，前端人工标注工具会锁定，同时后端也会拒绝新的人工标注写入，避免 GPU 推理与人工修改产生竞争条件。登录、视频播放等非标注操作仍可用。

## Tracking 帧数配置
默认每次处理 5 帧，统一由后端配置：

```text
backend/app/config.py
TRACK_FRAMES = int(os.getenv("SAM3_TRACK_FRAMES", "5"))
```

也可以在 Windows PowerShell 启动前临时修改：

```powershell
$env:SAM3_TRACK_FRAMES="10"
python -m uvicorn app.main:app --host 127.0.0.1 --port 3000
```

前端点击 Tracking 时不再写死帧数，由后端返回本次任务实际使用的帧数。

## 同名视频
如果重复导入相同文件名，例如两次导入 `test.mp4`，后端不会覆盖之前的数据目录：

```text
track_data/test/
track_data/test_001/
track_data/test_002/
```

每个目录都有自己独立的 MP4、人工标注 JSON 和 `tracker_results.json`（逐行 JSONL，结构与 01_test 的 `tracker_results.json` 一致）。


## SAM3 Tracking Pipeline

The FastAPI Tracking endpoint reuses the SAM3 engine/session/propagation/decoding path used by the validated tracker test, but production tracking runs in **raw-frame mode**: no FPS down-sampling, no sampled-frame index, and no browser-side interpolation.

The original video FPS and frame numbers are used everywhere. `SAM3_TRACK_FRAMES` controls how many consecutive source frames one Tracking request processes (default `5`).

The SAM3 model/processor stay cached for the whole FastAPI process. Video preprocessing/storage are kept on CPU to make the setup practical for a 4 GB GPU, while model inference uses the configured CUDA device.

## Tracking 输出

每个视频目录会产生：

```text
tracker_results.json    # JSONL：每行一个原始视频 frame，frame_index == source_frame_index
tracker_overlay.mp4     # 与原视频保持相同 FPS/帧序的带框 MP4
```

浏览器继续通过 `/api/track/result/{mediaId}` 获取按原视频 `source_frame_index` 对齐的帧结果；也可以通过 `/api/track/result-file/{mediaId}` 获取原始 JSONL，通过 `/api/track/overlay/{mediaId}` 播放处理后 MP4。


## AI Tracking 新流程：帧差规划 → SAM3

现在点击前端 **AI Tracking** 后，不再直接使用固定帧数调用 SAM3。一次点击按以下顺序执行：

```text
当前页面正在显示的 frame N
        │
        ├─ ① 读取当前帧人工/AI bbox
        │
        ▼
annotations_frame_NNNNNN.json
        │
        ├─ ② MP4 + JSON → Frame Difference Planner
        │       ├─ 跟踪已知 seed
        │       ├─ 排除已知目标附近运动
        │       ├─ ROI/面积/尺寸/长宽比过滤
        │       └─ 连续帧确认新运动目标
        │
        ▼
recommendedTrackFrames
        │
        ├─ ③ 原 SAM3 tracker
        │       从 frame N 开始处理 recommendedTrackFrames 帧
        │
        ▼
如果找到 newObjectFrame
        │
        └─ 前端自动定位到该帧，人工确认/补框
```

帧差模块不会把候选框直接当成“新精子”。它只负责回答：

> 从当前 seed frame 开始，SAM3 本轮应该向后处理多少原始视频帧，以及是否存在一个持续的运动候选。

### 新接口

```text
POST /api/track/plan
```

请求：

```json
{
  "mediaId": "sample",
  "mediaName": "sample.mp4",
  "startFrame": 120,
  "seedFilename": "annotations_frame_000120.json"
}
```

返回：

```json
{
  "mediaId": "sample",
  "startFrame": 120,
  "status": "new_object_found",
  "newObjectFrame": 137,
  "frameOffset": 17,
  "recommendedTrackFrames": 18,
  "searchFrames": 17,
  "knownBoxCount": 13,
  "bbox": [401, 175, 430, 198],
  "score": 0.72,
  "message": "持续帧差候选已确认，建议 SAM3 追踪到该帧。",
  "seedFilename": "annotations_frame_000120.json",
  "willReachNewObject": true
}
```

`recommendedTrackFrames = frameOffset + 1`，因为 SAM3 的结果需要包含 seed frame 本身。

如果帧差在搜索窗口内没有确认新目标，接口仍会返回一个搜索窗口对应的追踪帧数；这样一次点击不会因为“没找到候选”而直接跳过 SAM3。

### 环境变量

```powershell
$env:FRAME_DIFF_MAX_SEARCH_FRAMES="120"
$env:FRAME_DIFF_CONFIRM_FRAMES="3"
$env:FRAME_DIFF_MAX_CONFIRM_MISS="2"
$env:FRAME_DIFF_THRESHOLD="12"
$env:FRAME_DIFF_KNOWN_MARGIN="25"
$env:FRAME_DIFF_MIN_AREA="15"
$env:FRAME_DIFF_MIN_AREA_RATIO="0.55"
$env:FRAME_DIFF_TEMPLATE_THRESHOLD="0.35"
```

入口 ROI 可以通过：

```powershell
$env:FRAME_DIFF_ROI_RECT="0,96,640,260"
```

设置；留空时使用全画面。由于不同视频分辨率不同，默认不写死 ROI。

### 当前帧 JSON 的文件生命周期

每次点击 Tracking，后端都会先生成：

```text
track_data/
└── <mediaId>/
    ├── sample.mp4
    ├── media.json
    ├── annotations_frame_000120.json   # 本次 seed
    ├── tracker_results.json             # SAM3 JSONL
    └── tracker_overlay.mp4              # SAM3 可视化
```

因此同一个视频可以在不同帧建立不同的 seed：

```text
annotations_frame_000000.json
annotations_frame_000137.json
annotations_frame_000254.json
...
```

这也支持“发现新目标 → 人工补框 → 再点击 AI Tracking”的循环工作流。

### SAM3 与帧差的职责

| 模块 | 职责 |
|---|---|
| 前端当前帧 | 决定本轮 seed frame |
| seed JSON | 保存当前帧已知 bbox |
| Frame Difference | 决定“需要向后看多少帧” |
| SAM3 | 从 seed frame 开始，对这些帧中的已知 seed 对象进行精确 tracking |
| 人工标注 | 在新目标首次出现帧确认/补标 |
| 下一轮 Tracking | 把新补标目标纳入新的 seed JSON |

