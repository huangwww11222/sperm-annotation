# AI Tracking 业务流程：当前帧 JSON → 帧差 → SAM3


### AI Tracking 断点循环（当前版本）

每次点击 **AI Tracking** 都以页面当前 `currentFrame` 作为新的 `startFrame`，不会固定从 0 帧开始。业务循环严格为：

```text
currentFrame = N
  ↓
保存 N 帧完整 seed JSON
  ↓
MP4 + N 帧 JSON 做帧差
  ↓
找到新目标首次确认帧 M
  ↓
SAM3 只追踪 N → M
  ↓
页面自动跳到 M
  ↓
用户在 M 帧人工补标新物体
  ↓
再次点击 AI Tracking
  ↓
currentFrame = M，再执行下一轮 M → K
```

如果这一轮帧差没有找到可确认的新目标，则本轮不会启动 SAM3，页面停留在当前起始帧，等待人工检查后再次执行。`recommendedTrackFrames = M - N + 1`，包含起始 seed 帧。


## 1. 用户动作

用户把视频播放/定位到当前帧：

```text
currentFrame = N
```

然后在当前帧完成 bbox 人工标注。

点击：

```text
AI Tracking
```

## 2. 第一阶段：生成当前帧 JSON

前端从：

```text
annotationsByMedia[mediaId]
```

筛选：

```text
frameIndex == currentFrame
```

把百分比 bbox 转成像素：

```text
[x1, y1, x2, y2]
```

并调用：

```http
POST /api/track/annotations
```

保存：

```text
track_data/<mediaId>/annotations_frame_NNNNNN.json
```

JSON 基本结构：

```json
{
  "media": {
    "id": "sample",
    "name": "sample.mp4",
    "type": "video",
    "width": 640,
    "height": 432
  },
  "frame": {
    "frameIndex": 120,
    "timestampMs": 6000
  },
  "coordinateSystem": {
    "source": "frontend-pixel",
    "target": "pixel",
    "bbox": "[x1, y1, x2, y2]"
  },
  "annotations": [
    {
      "id": "manual-001",
      "object_id": 1,
      "name": "rare sperm",
      "source": "manual",
      "frameIndex": 120,
      "bbox": [25, 184, 56, 208]
    }
  ]
}
```

## 3. 第二阶段：帧差规划

前端拿刚保存的 JSON 文件名调用：

```http
POST /api/track/plan
```

后端打开：

```text
原始 MP4
+
当前帧 seed JSON
```

从：

```text
N + 1
```

开始逐帧分析。

### 3.1 跟踪已知对象

seed JSON 中的目标作为 known objects。

先用局部 template matching 跟踪它们，形成：

```text
known boxes @ frame N+1
known boxes @ frame N+2
...
```

### 3.2 相邻帧差

使用：

```python
cv2.absdiff(previous, current)
```

再：

```text
threshold
→ morphology open
→ morphology close
→ ROI mask
```

### 3.3 排除已知对象

对 known boxes 扩大：

```text
FRAME_DIFF_KNOWN_MARGIN
```

把已知对象运动导致的边缘响应去掉。

### 3.4 候选过滤

候选需要满足：

```text
面积 ≥ min_area
矩形面积 ≥ min_area_ratio × known median area
宽高 ≥ 相对阈值
长宽比 ≤ max_aspect
```

### 3.5 连续确认

候选不是出现一帧就确认。

默认：

```text
confirm_frames = 3
max_confirm_miss = 2
```

最终得到：

```text
newObjectFrame = F
frameOffset = F - N
recommendedTrackFrames = F - N + 1
```

这里 `+1` 是为了让 SAM3 结果包含 seed frame。

## 4. 第三阶段：SAM3

前端把：

```json
{
  "startFrame": N,
  "maxFrames": F - N + 1,
  "annotations": [...]
}
```

发送到：

```http
POST /api/track
```

这里仍然走原来的：

```text
ThreadPoolExecutor(max_workers=1)
        ↓
track_video()
        ↓
get_sam3_engine()
        ↓
make_tracker_session()
        ↓
add_manual_boxes()
        ↓
propagate_manual()
```

所以 SAM3 部分没有被帧差模块替代。

## 5. 第四阶段：回到新目标帧

SAM3 完成后，如果：

```text
willReachNewObject == true
```

前端定位：

```text
currentFrame = newObjectFrame
```

此时用户看到：

```text
帧差发现新运动候选
+
SAM3 已经把原 seed 对象追踪到这里
```

用户人工检查这个位置并给新目标补框。

## 6. 形成循环

```text
seed frame N
   ↓
帧差发现 F
   ↓
SAM3 N → F
   ↓
人工在 F 补新对象
   ↓
seed frame F
   ↓
帧差继续寻找下一个目标
   ↓
SAM3 F → G
   ↓
...
```

这就是最终的“逐个发现新目标”工作流。

## 7. 为什么帧差不直接代替 SAM3

帧差解决的是：

```text
“什么时候出现新的持续运动区域？”
```

SAM3 解决的是：

```text
“seed 对象在后续帧具体在哪里？”
```

两者职责不同。

因此当前设计保留：

```text
Frame Difference = temporal planner
SAM3 = object tracker
Human = new-object confirmation
```

## 8. 一个重要限制

帧差检测到的是“持续运动候选”，不是类别分类器。

因此：

```text
灰尘
气泡
杂质
其他细胞
背景变化
```

都有可能成为候选。

所以系统在 `newObjectFrame` 停下来给人工确认，而不是自动把候选写成“新精子”真值。


## 本次 UI 更新

- 已移除“AI 检测 / 分割”按钮，AI Tracking 保留。
- 新增“选择框”工具，位于“点标注”左侧，快捷键 `V`；点击已有 bbox 即可选中，选择框模式不创建/拖动 bbox。
- 帧差规划默认开启后端逐帧日志，可由 `FRAME_DIFF_VERBOSE_LOG=0` 关闭。
