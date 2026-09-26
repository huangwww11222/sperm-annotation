> **历史归档，非当前开发依据。** 本文保留当时的设计或验收记录，可能包含已撤销规则。当前入口：[文档索引](../README.md)；业务规则：[WORKFLOW.md](../WORKFLOW.md)。归档日期：2026-09-27。

# AI Tracking 回退分支逻辑

当一轮 Tracking 已经从 `A` 追踪到 `B`，用户在中间帧 `N`（A <= N <= B）发现框错误，并在 N 帧人工修改/新增后再次点击 AI Tracking：

1. 当前 N 帧人工标注先自动写入数据库。
2. 后端执行 `/api/track/rewind`，删除 `tracker_results.json` 中 `source_frame_index >= N` 的旧 AI tracking 行；`N` 本身也删除，因为新的人工 seed 将成为 N 帧的新权威起点。
3. 删除 `annotations_frame_*.json` 中所有 `frame > N` 的旧未来 seed 文件。
4. `N` 以前的 tracking 结果保持不变。
5. 前端同步删除内存中的 `source=ai` 且 `frameIndex >= N` 的旧框。
6. 用 N 帧最新人工框生成新的 seed JSON。
7. SAM3 从 N 向后持续追踪，直到异常、单轮帧数上限或视频末尾。
8. SAM3 从 N 开始写入新的 tracking 分支。

因此不会出现“旧的 N 后未来轨迹 + 新的 N 后轨迹”同时存在的问题。

## 例子

旧结果：`0 → 15`

用户在 `frame=10` 修正/新增对象，再点 AI Tracking：

- 保留 `0~9`
- 删除旧 `10~15`
- 用新的人工 `frame=10` 建 seed
- 重新运行 `10 → M`
- 最终结果成为 `0~9 + 新的 10~M`

如果再次在 `frame=K` 回退，则重复同样规则。
