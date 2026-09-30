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

工作区当前还保存 `revision`、稳定 ID 的 `deletedObjectIds/deletedFrameObjects`、服务端生成的 `normalMotionSamples` 和 `trackingFeedbackEvents`。内部 `_writeReceipts` 与状态同一次原子替换，GET 不暴露回执；响应丢失后原键重放不会重复递增版本或新增反馈。文件替换失败保留原状态和版本，日志记录 `annotation.workspace_save_failed`。这些状态受单进程来源锁保护，不属于 SQLite 跨资源事务。

删除是持久化过滤规则，原始 Tracking JSONL 和旧 seed 保留以便撤销；所有活动读入、续追 seed 和送审均应用规则。仅删除某帧不删除模型在后续帧的身份，重新追踪也不能让该帧复活。整视频删除同时排除人工记录、AI 框与人工基准。服务端 `deletedAnnotationFrames` 记录原先有人工框、后因删除变空的帧，使送审仍能区分已知空帧和未知帧。此字段与校准样本一样由服务端维护。

正常运动样本取自真实追踪行的位移测量，按视频、对象、原因保存。尺寸/形状经正常确认的事件另带可选 `geometryReference`（实际像素框、帧、对象、confirmed-normal 来源）；检测恢复参照时校验帧界限、删除及新人工修正，不改写 manualBaselines。旧事件无该字段时仍按原人工基准判断，不推断用户曾认可未知几何。原有 AI 预测不会自行成为正常样本或尺寸参照。重置清活动运动样本并使旧尺寸参照失效，但保留反馈事件，已修正决定独立记录。旧客户端遗漏新字段时，服务端保留删除规则与反馈，避免旧缓存覆盖新控制状态。

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
| 审计身份/证据 | `audit_source_identity` 保存部署身份；`training_export_audits` 按 export_id 唯一保存规范 JSON、采集时间和 SHA；触发器禁止更新/删除 |

A/B/F 不随浏览、缩放、草稿、重新确认而改写。F 的对象集合和类别继承 A，只选择几何来源。旧问题标记表或状态仍可能存在，不表示当前产品启用阻塞/退回流程。

## 备份、兼容和清理

- 备份至少覆盖实际 DB、storage、仍使用的 `backend/track_data` 及部署配置/模型版本。停写后备份或使用 SQLite 一致性备份，不能在写入中随意复制不完整状态。
- 当前部署是单进程，不共享一个 DB 给多后端进程；不要将 SQLite 放到 SMB/NFS 供多个实例直接读写。
- 旧 `backend/track_data/<media-id>` 保留兼容读取；新上传写入 storage/media。受支持的编辑流程仍可能对已定位到的旧媒体目录写入工作区，不能将“兼容读取”误解为整个目录被文件系统锁定只读。
- 迁移前停后端，备份，然后逐个移动旧素材子目录到 media；不要套入一层 track_data 或覆盖同名 ID。
- 缺少完整 A/B/空帧的历史任务保持只读并解释原因；不能猜测补帧或冒充可导出 F，应按完整来源重新送审。
- `.frame_cache` 可重建；训练任务正常完成/失败会清理临时目录。异常进程终止留下的 `.work` / `.partial` 需停服务后核对再清理。
- 原视频、seed、Tracking、工作区、DB 和历史版本不是缓存。不要因文档整理或测试删除它们。已完成 ZIP 暂无自动过期策略，清理需考虑任务记录及下载关系。

## 数据集质量审计

`quality_audit.py` 在创建导出任务的事务内固定所选 F 的相关业务历史：媒体/A 全帧、B 有效帧与全部成功 submission/payload、B 版本与净修改、C 决定/动作/撤销/重新确认、F 完整帧与决定快照。用户仅导出 id/username，不含密码、JWT、游标或写请求收据。部署身份随数据库备份/升级保留。每份导出独立快照，重新确认不修改旧证据。

旧数据集没有快照时明确未覆盖；只读提取不会初始化数据库、补造历史或把未知观看时长写成零。A 的作者字段表示送审责任，不能代表每个框的手工作者。失败任务可保留尝试快照，但不算成功交付。

完整快照保存在 DB，训练 ZIP 仅带轻量索引和原有 provenance。新增表/触发器是增量迁移，存储运行契约仍为 1；回滚按完整离线更新器恢复同批 DB/storage。
