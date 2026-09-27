# 当前接口与数据契约

基线：2026-09-27。本页是 API 地图与不变量，不复制全部类型定义。精确请求验证以已注册路由的 Pydantic 模型为准；开发时可读本地 FastAPI `/openapi.json`。旧原型中的示例接口不是活动接口。

## 通用协议

活动业务接口使用 `Authorization: Bearer <token>`。B/C/送审/训练导出的业务写请求还要求 `X-Review-Contract: 2` 与 `Idempotency-Key`。同一次未知结果/网络失败重试使用**相同 key、相同请求体**；用户新的业务意图才生成新 key。

服务端以 `BEGIN IMMEDIATE`、期望版本及成功回执保护写入。回执重放先于版本校验；业务数据与回执同时提交/回滚。帧、视频、决定和游标各自有版本；游标更新不改变业务完成状态。

错误包含 `message`、`code`、`requestId`（亦可从 `X-Request-ID` 获取）。401 处理登录；409 处理冲突并保留意图；422 不合法参数；428 契约需要更新。不要统一吞掉错误返回空数据。具体错误以路由为准。

`GET /api/health` 提供进程健康和 `sam3.enabled/modelLoaded`。Docker CPU 基础模式设置 `SAM3_ENABLED=false`，`POST /api/track` 和 `/api/track/rewind` 返回明确 503（不会先删除已有追踪分支）；本机开发默认仍启用。健康正常不等于 GPU/权重推理已验证。

## 媒体、标注与追踪

实现：[main.py](../backend/app/main.py)、[trackApi.ts](../frontend/src/api/trackApi.ts)、[httpAnnotationApi.ts](../frontend/src/api/httpAnnotationApi.ts)。

| 路径 | 用途 |
| --- | --- |
| `POST /api/auth/register`、`/api/auth/login`；`GET /api/auth/me` | 真实账号与 JWT |
| `POST /api/track/upload`；`GET /api/track/media` | 原视频上传、可用媒体 |
| `GET /api/track/video/{mediaId}`、`/api/track/frame/{mediaId}/{fi}` | 原视频、真实指定帧 |
| `GET / PUT /api/track/workspace/{mediaId}` | 可恢复工作区 |
| `POST /api/annotation/annotations/manual` | 保存人工对象记录 |
| `GET /api/annotation/projects/{projectId}/results` | 人工标注记录查询 |
| `POST /api/track/annotations`、`/api/track/rewind`、`/api/track` | seed、回退、发起追踪 |
| `GET /api/track/status/{taskId}`、`/api/track/result/{mediaId}` | 追踪状态与对象 |
| `GET /api/track/result-file/{mediaId}`、`/api/track/overlay/{mediaId}` | JSONL 与带框视频 |

`AnnotationObject` 定义在 [annotation.ts](../frontend/src/types/annotation.ts)：界面 `id` 与稳定 `objectId` 不等价，`frameIndex` 为实际 0 起帧号；工作区 bbox 为百分比 `{x,y,width,height}`。seed / Tracking / B/C 使用原图像素 xyxy，转换不能重复进行。追踪结果文件虽后缀 `.json`，实际逐行 JSONL。

## A 完成与 B

实现：[review_workflow_routes.py](../backend/app/review_workflow_routes.py)、[reviewWorkflowApi.ts](../frontend/src/api/reviewWorkflowApi.ts)。前缀 `/api/review`。

| 方法与相对路径 | 请求/作用 |
| --- | --- |
| `GET /media/{mediaId}/completion-preview` | 来源 revision、有框/空/未知帧及未知范围 |
| `POST /media/{mediaId}/complete` | `expectedSourceRevision`、`confirmComplete`、`explicitEmptyFrameRanges:[{start,end}]`（0 起，含端点） |
| `GET /sessions`、`/sessions/{sid}` | 任务列表、详情 |
| `POST /sessions/{sid}/claim` | 原子领取，可自审 |
| `GET /sessions/{sid}/frames/{fi}` | A、当前有效框、草稿、上次成功提交、帧版本/权限 |
| `PUT /sessions/{sid}/frames/{fi}/draft` | `expectedFrameRevision`、`patch` |
| `POST /sessions/{sid}/frames/{fi}/submit` | `expectedFrameRevision`、`patch` |
| `POST /sessions/{sid}/frames/{fi}/discard` | `expectedFrameRevision`，恢复上次有效提交或 A |
| `POST /sessions/{sid}/freeze` | `expectedSessionRevision`，固定 B、changes、C 任务 |
| `PUT /cursors` | `sessionId`、`frameIndex`、`expectedCursorRevision` |

`patch` 是当前帧相对 A 的完整净差量列表 `{objectId,bbox}`；不是新增框列表，也不是屏幕坐标。服务端拒绝未知/重复身份、非法坐标和不合法几何。

Session 关键字段：`id, baselineId, revision, state, reviewerId, media, progress, resume, permissions, readOnlyReason, completedReviewVersionId`。`progress` 含 `submittedFrames/unsubmittedFrames/draftFrames/modifiedFrames/modifiedBoxes/percent`；`resume` 含最后浏览、首个未提交、草稿索引及游标版本。Frame 关键字段：`frameRevision/state/hasDraft/baselineObjects/effectiveObjects/patch/lastSubmission/permissions`。不要用 `lastSubmission != null` 推断当前已提交。

旧上传任意 frames 冻结 A 的路由不用于新流程；历史不完整来源只读，不能在前端填充假帧修复。

## C 与完整最终版本

实现：[confirmation_workflow_routes.py](../backend/app/confirmation_workflow_routes.py)、[confirmationApi.ts](../frontend/src/api/confirmationApi.ts)。前缀 `/api/confirmation`。

| 方法与相对路径 | 请求/作用 |
| --- | --- |
| `GET /sessions`、`/sessions/{sid}` | 任务、进度、权限、恢复/撤销信息 |
| `GET /sessions/{sid}/changes` | 净变化及当前决定 |
| `GET /sessions/{sid}/frames/{fi}` | 固定完整 A/B 帧对象 |
| `POST /sessions/{sid}/claim` | 原子领取，可自确认 |
| `PUT /sessions/{sid}/changes/{changeId}/decision` | `choice:'A'|'B'`、`expectedDecisionRevision` |
| `POST /sessions/{sid}/undo` | `actionId`、`expectedSessionRevision` |
| `POST /sessions/{sid}/finalize`、`/reopen` | `expectedSessionRevision` |
| `PUT /sessions/{sid}/cursor` | `changeId`、`expectedCursorRevision` |

`Change` 含 `changeId/frameIndex/objectId/annotationId/beforeBbox/afterBbox/metrics/decisionRevision/decision`。C 不能上传任意新 bbox 或改变类别/对象集合。`progress` 含 `totalChanges/decided/pending/keptA/adoptedB/percent`，零修改百分比不替代显式完成。

`GET /api/final-versions/{id}` 和 `/{id}/download` 提供完整 JSON。每帧存在，包括空 `objects:[]`；对象保留稳定身份、类别、最终框、A/B 框及 `choice/resolution/changeId/decisionEventId`。未改对象 `choice:null`、`resolution:'unchanged'`，来源 A。完整帧表不能用仅有对象的表代替，否则会丢空帧。

## 训练导出

实现：[training_export_routes.py](../backend/app/training_export_routes.py)、[trainingExportApi.ts](../frontend/src/api/trainingExportApi.ts)。

| 方法与路径 | 作用 |
| --- | --- |
| `POST /api/datasets/exports/preview` | `finalVersionIds`，只读校验/统计 |
| `POST /api/datasets/exports` | `finalVersionIds`、`format:'yolo'|'coco'|'both'`、`splitRatio`（默认 0.8） |
| `GET /api/datasets/exports/{id}` | 本人任务阶段、帧数、错误和下载资格 |
| `GET /api/datasets/exports/{id}/download` | 校验当前资格后返回真实 ZIP |
| `POST /api/export/dataset` | 旧入口拒绝，410 `FINAL_CONFIRMATION_REQUIRED` |

创建请求幂等；任务生成状态与资格共同决定是否可下载，不得仅按 ZIP 文件存在就返回。资格与产物规则见 [WORKFLOW.md](WORKFLOW.md)。
