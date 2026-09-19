# 帧差法日志

`POST /api/track/plan` 调用帧差规划器时，默认向 FastAPI 终端打印逐帧日志。

示例：

```text
[frame-diff] START video=sperm2.mp4 seed_frame=120 total_frames=900 max_search=120 known_boxes=13 diff_threshold=12 confirm_frames=3 max_miss=2 roi=(0,96,640,260)
[frame-diff] frame=121 offset=1 diff_pixels=1832 diff_ratio=0.006615 tracked_known=13 candidates=0 raw_candidates=0 relaxed=None pending=0 top=None
[frame-diff] frame=122 offset=2 diff_pixels=1910 diff_ratio=0.006901 tracked_known=13 candidates=1 raw_candidates=1 relaxed=None pending=0 top=[401, 175, 430, 198, 0.73]
[frame-diff] frame=123 offset=3 diff_pixels=1988 diff_ratio=0.007182 tracked_known=13 pending=1 pending_hits=1 pending_miss=0 step_ok=True template_corr=0.81
[frame-diff] FOUND first_frame=122 offset=2 confirm_hits=3 bbox=(401, 175, 430, 198) searched_frames=4
```

字段含义：`frame` 当前帧；`offset=frame-startFrame`；`diff_pixels/diff_ratio` 当前帧和前一帧超过阈值的变化量；`tracked_known` 已知目标数量；`candidates` 过滤后的候选数；`pending_hits/pending_miss` 连续确认状态；`template_corr` 候选模板匹配相关性。

关闭日志：

```env
FRAME_DIFF_VERBOSE_LOG=0
```
