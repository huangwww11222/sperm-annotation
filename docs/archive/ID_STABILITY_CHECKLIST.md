> **历史归档，非当前开发依据。** 本文保留当时的设计或验收记录，可能包含已撤销规则。当前入口：[文档索引](../README.md)；业务规则：[WORKFLOW.md](../WORKFLOW.md)。归档日期：2026-09-27。

# Object ID 稳定性检查清单

本版把 `objectId` 当作业务实体的稳定主键，而不是数组位置或当前帧顺序。

## 一次 AI Tracking 的完整链路

1. 当前页面帧 N 的全部 bbox 必须都有唯一正整数 `objectId`。
2. 前端生成 `annotations_frame_NNNNNN.json` 时同时写入 `object_id` / `objectId`。
3. `/api/track/annotations` 再次校验并拒绝缺失、重复或跨帧的 ID。
4. `/api/track` 读取该 seed JSON，并在持续追踪时保留稳定 `object_id`。
5. `/api/track` 继续使用同一个 seed；后端不会按数组顺序重新编号。
6. SAM3 使用这些 `object_id` 作为对象 ID。
7. `tracker_results.json` 每帧继续写相同的 `object_id`。
8. 前端根据 `(frameIndex, objectId)` 合并 Tracking 结果。

## 禁止事项

- 禁止 `object_id = annotation_index + 1`。
- 禁止因 annotation 数组排序改变而重新生成 object ID。
- 禁止缺少 object ID 时静默 fallback 到数组序号。
- 禁止同一 seed frame 出现两个相同的 object ID。

## 断点续追

`0 -> N -> M -> ...` 每次从当前停留帧继续；用户在 N/M 帧新建的对象会获得新 ID，并在下一轮 seed 中原样传递。例如新增对象为 15，后续帧仍必须是 15，而不是因为排序变成 8。
