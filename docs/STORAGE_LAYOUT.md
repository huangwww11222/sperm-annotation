# 存储、快照与迁移边界

路径来自 [config.py](../backend/app/config.py)。

| 配置 | 默认 | 内容 |
| --- | --- | --- |
| `APP_DATA_DIR` | `backend/data` | 默认 DB 目录、`logs/review.log` |
| `APP_DB_FILE` | `$APP_DATA_DIR/app.db` | SQLite 账号、人工记录、A/B/C/F 和导出任务 |
| `APP_STORAGE_DIR` | `backend/storage` | `media/`、`datasets/` |
| `SAM3_MODEL_ID` | `backend/track_modul/facebook--sam3/snapshots/master` | 模型配置与权重 |

Docker 显式设定 `APP_DATA_DIR=/data/database`，业务日志位于宿主机 `APP_DATA_ROOT/database/logs`，与数据库一同持久化。源码默认路径不变。

修改 DB 路径不自动修改日志路径；修改变量不自动搬迁已有数据。不要将测试目录与业务目录混用。

## 文件与工作区

```text
storage/
  media/<media-id>/
    原视频（上传时不转码）
    media.json                   # 原始宽高、FPS、帧数等
    annotations_frame_*.json     # 每次 Tracking seed
    tracker_results.json         # JSONL：逐行原始视频帧
    tracker_overlay.mp4          # 带框预览
    workspace_state.json         # 可恢复人工/删除/基准/异常/当前位置与设置
    .frame_cache/                # 逐帧 JPEG 缓存
  datasets/
    train_<id>.zip               # 当前训练导出生成包，认证接口下载
    train_<id>.zip.partial       # 打包临时文件
    .work/                      # 临时提帧与标签
```

`workspace_state.json` 临时文件写入后原子替换。AI 逐帧结果不复制进去，加载时与 `tracker_results.json` 合并，人工修正优先，删除标记不能因重新加载复活。

旧媒体没有工作区文件时，只从 seed 的 `source=manual` 恢复人工记录/基准；AI、warning、anomaly 不当人工基准。首次编辑/追踪可写回恢复状态。文件原子替换与 SQLite 事务不是同一跨资源事务；不要假设一个能回滚另一个。

## SQLite 数据关系

完整 DDL 在 [review_schema.py](../backend/app/review_schema.py)、[review_workflow.py](../backend/app/review_workflow.py)、[confirmation_workflow.py](../backend/app/confirmation_workflow.py)、[training_export.py](../backend/app/training_export.py)。启动时增量建表，保留历史数据。

| 数据层 | 表/作用 |
| --- | --- |
| 媒体与 A | `media_revisions`、`annotation_baselines`、`baseline_frames` 固定来源、完整帧及对象 |
| B 编辑 | `review_sessions`、`review_frames.patch_json/state` 保存草稿状态，`submission_id` 保留上次成功引用；提交正文另存不可变表 |
| 不可变帧提交 | `frame_submissions` + `review_submission_payloads` 存成功提交及净差量 |
| 固定 B | `review_versions` + `review_version_frames` 完整逐帧对象；`review_changes` 存相对 A 的净变化 |
| C 决定 | `confirmation_sessions`、`decision_events`、`decision_heads`；事件保留、head 表示当前选择 |
| C 并发/撤销 | `confirmation_change_versions`、`confirmation_actions` |
| F | `final_versions`、`final_version_frames`、`confirmation_final_records` 存完整帧、决定快照、前一版本 |
| 历史/兼容 | `confirmation_reopen_events` 留重新确认记录；`final_frame_objects` 兼容旧对象读取方，不能取代完整帧表 |
| 游标 | `review_bookmarks`、`confirmation_bookmarks`，按用户与任务隔离、版本独立 |
| 幂等 | `review_write_receipts` 与业务变化同一事务；`training_exports` 有操作者+请求键唯一约束 |
| 导出 | `training_exports` 存任务设置、进度、manifest、错误、完成状态 |

A/B/F 不随浏览、缩放、草稿、重新确认而改写。F 的对象集合和类别继承 A，只选择几何来源。旧问题标记表或状态仍可能存在，不表示当前产品启用阻塞/退回流程。

## 备份、兼容和清理

- 备份至少覆盖实际 DB、storage、仍使用的 `backend/track_data` 及部署配置/模型版本。停写后备份或使用 SQLite 一致性备份，不能在写入中随意复制不完整状态。
- 当前部署是单进程，不共享一个 DB 给多后端进程；不要将 SQLite 放到 SMB/NFS 供多个实例直接读写。
- 旧 `backend/track_data/<media-id>` 保留兼容读取；新上传写入 storage/media。受支持的编辑流程仍可能对已定位到的旧媒体目录写入工作区，不能将“兼容读取”误解为整个目录被文件系统锁定只读。
- 迁移前停后端，备份，然后逐个移动旧素材子目录到 media；不要套入一层 track_data 或覆盖同名 ID。
- 缺少完整 A/B/空帧的历史任务保持只读并解释原因；不能猜测补帧或冒充可导出 F，应按完整来源重新送审。
- `.frame_cache` 可重建；训练任务正常完成/失败会清理临时目录。异常进程终止留下的 `.work` / `.partial` 需停服务后核对再清理。
- 原视频、seed、Tracking、工作区、DB 和历史版本不是缓存。不要因文档整理或测试删除它们。已完成 ZIP 暂无自动过期策略，清理需考虑任务记录及下载关系。
