# 运行数据目录

应用代码、模型和用户生成的数据已分离。默认持久化目录为 `backend/storage/`；可在后端 `.env` 设置 `APP_STORAGE_DIR` 指向数据盘或共享盘。

```text
backend/
├─ app/                         # FastAPI 业务代码
├─ data/
│  └─ app.db                    # 用户和人工标注索引（SQLite）
├─ track_modul/                 # 本地 SAM3 模型；不提交到版本库
└─ storage/
   ├─ media/
   │  └─ <media-id>/
   │     ├─ <原始上传视频>       # 原始 AVI/MP4 等，从不在上传时转码
   │     ├─ media.json           # FPS、分辨率、总帧数及上传元数据
   │     ├─ annotations_frame_*.json
   │     ├─ workspace_state.json   # 人工框、人工基准、异常/显示等可恢复工作区状态
   │     ├─ tracker_results.json # 逐行 JSON，AI Tracking 结果
   │     ├─ tracker_overlay.mp4  # 可视化预览
   │     └─ .frame_cache/        # 可安全删除的逐帧 JPEG 缓存
   └─ datasets/
      ├─ <素材名>_dataset_*.zip # 用户导出的 COCO/YOLO 训练数据集
      └─ .work/                  # 导出时的短暂工作目录；可在服务停止后清理
```

## 迁移已有部署

旧版本使用 `backend/track_data/`。升级后该目录保持只读兼容：已有视频、追踪结果和 API 地址继续可用；新上传的素材会写入 `backend/storage/media/`。

确认升级正常后，可在停掉后端服务的情况下，将旧目录中的**素材子目录**移动到 `backend/storage/media/`。不要移动 `track_data` 根目录本身，也不要覆盖同名 `<media-id>` 目录。迁移前备份 `backend/data/app.db`、旧 `track_data/` 和新 `storage/`。

训练集 ZIP 位于 `storage/datasets/`，原视频位于对应的 `storage/media/<media-id>/`。部署、备份或清理时按这两个目录分别处理即可。

## 工作区恢复与去重

`workspace_state.json` 是每个视频唯一的可恢复编辑状态文件，采用临时文件写入后原子替换。它保存人工框、每个 `object_id` 最近一次人工基准、用户隐藏的 Tracking 对象、异常标记/暂停信息、当前帧以及显示和编辑器设置。AI 逐帧结果不复制到该文件，仍以 `tracker_results.json` 为唯一来源，加载时再与人工框合并，避免同一框出现两份。

旧素材目录没有 `workspace_state.json` 时，后端会从 `annotations_frame_*.json` 中只提取 `source=manual` 的记录生成兼容状态；AI、warning 和 anomaly 框不会被误当成人工基准。第一次编辑或 AI Tracking 前会把迁移后的状态写回当前素材目录。

标注结果按“文件、帧、标注人”聚合；同一 `object_id` 的重复保存会在数据库写入时替换旧记录，读取时仍会兼容清理历史版本产生的重复行。
