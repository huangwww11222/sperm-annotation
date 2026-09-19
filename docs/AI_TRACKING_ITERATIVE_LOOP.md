# AI Tracking 迭代式断点循环（v3）

## 用户操作逻辑

每一次点击 **AI Tracking** 时，按钮点击瞬间页面的 `currentFrame` 都作为本轮 `startFrame`。不会固定从 0 帧开始。

### 第一次

```text
当前帧 = 0
  ↓
保存第 0 帧完整 seed JSON
  ↓
Frame Difference：0 → 向后搜索
  ↓
找到新目标首次确认帧 = N
  ↓
SAM3：0 → N
  ↓
页面跳转到 N
```

### 人工补标后第二次

```text
当前帧 = N
用户在 N 帧补标/修正
  ↓
再次点击 AI Tracking
  ↓
保存第 N 帧完整 seed JSON
  ↓
Frame Difference：N → 向后搜索
  ↓
找到新目标首次确认帧 = M
  ↓
SAM3：N → M
  ↓
页面跳转到 M
```

然后重复：`M → K → ...`。

## Seed JSON 规则

当前帧的所有可用 bbox 都进入 seed JSON，包括：

- 上一轮 SAM3 已经追踪到当前帧的目标
- 用户在当前帧新添加的目标
- 用户在当前帧修正过的目标

因此下一轮帧差知道哪些目标已经存在，可以把它们作为 known objects 排除。

## 帧差与 SAM3 帧数

若帧差从 `N` 找到目标帧 `M`，SAM3 的 `maxFrames` 为：

```text
M - N + 1
```

因为 SAM3 的 `maxFrames` 包含 seed 起始帧。这样本轮不会越过帧差确定的结束帧。

## 没有找到新目标

若当前轮帧差没有找到可确认的新目标：

```text
不启动 SAM3
不改变当前帧
提示用户检查当前标注，然后再次点击
```

这样可以避免没有“下一站”时仍然盲目向后追踪固定窗口。

## 关键代码

前端：`frontend/src/stores/workspace.ts` 的 `runAiTrack()`。

后端：`backend/app/main.py` 的 `/api/track/annotations`、`/api/track/plan`、`/api/track`。

帧差核心：`backend/app/services/frame_difference.py`。
