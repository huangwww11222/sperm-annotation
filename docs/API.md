# 当前接口与数据契约

本页是 API 地图与不变量，不复制全部类型定义。精确请求验证以已注册路由的 Pydantic 模型为准；开发时可读本地 FastAPI `/openapi.json`。旧原型中的示例接口不是活动接口。

## 通用协议

活动业务接口使用 `Authorization: Bearer <token>`。B/C/送审/训练导出的业务写请求还要求 `X-Review-Contract: 2` 与 `Idempotency-Key`。同一次未知结果/网络失败重试使用**相同 key、相同请求体**；用户新的业务意图才生成新 key。

服务端以 `BEGIN IMMEDIATE`、期望版本及成功回执保护写入。回执重放先于版本校验；业务数据与回执同时提交/回滚。帧、视频、决定和游标各自有版本；游标更新不改变业务完成状态。

错误包含 `message`、`code`、`requestId`（亦可从 `X-Request-ID` 获取）。401 处理登录；409 处理冲突并保留意图；422 不合法参数；428 契约需要更新。不要统一吞掉错误返回空数据。具体错误以路由为准。

`GET /api/health` 提供进程健康和 `sam3.enabled/modelLoaded`。Docker 默认安装启用 GPU + AI；显式 CPU 基础模式设置 `SAM3_ENABLED=false`，`POST /api/track` 和 `/api/track/rewind` 返回明确 503（不会先删除已有追踪分支）；本机开发默认仍启用。健康正常不等于 GPU/权重推理已验证。

## 媒体、标注与追踪

实现：[main.py](../backend/app/main.py)、[trackApi.ts](../frontend/src/api/trackApi.ts)、[httpAnnotationApi.ts](../frontend/src/api/httpAnnotationApi.ts)。

| 路径 | 用途 |
| --- | --- |
| `POST /api/auth/register`、`/api/auth/login`；`GET /api/auth/me` | 真实账号与 JWT |
| `POST /api/track/upload`；`GET /api/track/media` | 原视频上传、可用媒体 |
| `DELETE /api/track/media/{mediaId}` | 删除未送审视频目录；需要登录。成功返回 `deleted: true, mediaId`；AI 运行、基准引用、新旧存储同名目录返回 409；文件系统失败返回 500 并记录日志，不能伪报成功 |
| `GET /api/track/video/{mediaId}`、`/api/track/frame/{mediaId}/{fi}` | 原视频、真实指定帧 |
| `GET / PUT /api/track/workspace/{mediaId}` | 可恢复工作区 |
| `GET /api/track/deletion-preview/{mediaId}/{objectId}` | 整视频删除预览，按稳定 ID 合并人工和 AI 同帧框，返回真实有效数量 |
| `POST /api/track/feedback/{mediaId}` | 显式正常/已修正/重置判断；版本校验和原键重试 |
| `POST /api/annotation/annotations/manual` | 保存人工对象记录 |
| `GET /api/annotation/projects/{projectId}/results` | 人工标注记录查询 |
| `POST /api/track/annotations`、`/api/track/rewind`、`/api/track` | seed、回退、发起追踪 |
| `GET /api/track/status/{taskId}`、`/api/track/result/{mediaId}` | 追踪状态与对象 |
| `GET /api/track/result-file/{mediaId}`、`/api/track/overlay/{mediaId}` | JSONL 与带框视频 |

`AnnotationObject` 定义在 [annotation.ts](../frontend/src/types/annotation.ts)：界面 `id` 与稳定 `objectId` 不等价，`frameIndex` 为实际 0 起帧号；工作区 bbox 为百分比 `{x,y,width,height}`。seed / Tracking / B/C 使用原图像素 xyxy，转换不能重复进行。追踪结果文件虽后缀 `.json`，实际逐行 JSONL。

工作区 GET 返回 `revision`（旧工作区首次为 0）；新版 PUT 携带 `expectedRevision` 和 `Idempotency-Key`，返回新的 `revision`。未知保存结果必须沿用原键和原正文，成功回执重放先于版本校验。无版本的旧客户端仍可保存普通编辑，但不能变更新删除规则，也不能覆盖服务端反馈、样本和回执。新前端始终使用版本和幂等键。

删除规则是 `deletedObjectIds:number[]`（整视频）和 `deletedFrameObjects:[{objectId,frameIndex}]`（单帧）；`deletedTrackingIds` 仅保留历史兼容。规则同时作用于人工/AI、seed、追踪读取、人工记录查询与送审。单帧删除不关闭后续该对象的追踪；整视频删除不允许该对象进入后续 seed。原追踪文件不物理抹除，撤销通过恢复工作区及移除对应删除标记实现。A/B/F 不随删除或撤销改变。

删除预览前必须保存当前工作区。响应为 `{mediaId,objectId,name,revision,totalCount,frameCount,manualCount,aiCount,firstFrame,lastFrame}`；同对象同帧人工覆盖 AI 只算一框；帧号 0 起，无框时首末帧为 null。确认删除时仍用当前工作区版本，不能把预览数量当无需校验的删除许可。

反馈请求为 `{expectedRevision,objectId,frameIndex,decision:'normal'|'corrected'|'reset',calibrate:boolean}`，使用 `Idempotency-Key`，与工作区共享版本。`reset` 不要求帧号，清该对象活动运动样本并使已确认尺寸参照失效，保留审计。`normal` 只在明确勾选校准且持久化追踪结果包含位移依据时生成 `normalMotionSamples`；每条为 `{objectId,frameIndex,reason:'motion',decision:'normal',calibrate:true,features:{motionNormalized}}`。数值取自服务端追踪数据，不接受客户提供阈值。`corrected` 要求当前帧已保存的人工框实际不同于异常原框，且不生成正常样本。 对尺寸/形状原因的 `normal`，服务端从实际追踪行取有效像素框，在该条 `trackingFeedbackEvents` 中保存 `geometryReference:{objectId,frameIndex,bbox,source:"confirmed-normal"}`；无须重画，且不依赖运动校准勾选。不接受客户端提供参照，不能将该框改写为人工标注。该字段为兼容旧事件的可选扩展。

反馈响应包含 `{ok,mediaId,revision,normalMotionSamples,trackingFeedbackEvents,pausedAnomalies,lastPausedContext}`。正常/已修正请求必须匹配当前持久化暂停帧及未解决对象，并核验真实追踪行；新键不能再次确认已解决的历史异常。每次只移除选定对象及已被删除的暂停项；仍有待处理对象时保留暂停上下文。运动样本按媒体、对象和位移原因隔离；尺寸参照单独按确认事件恢复，不豁免重叠或丢失检测。普通工作区 PUT 不能注入校准样本。Tracking 状态另返回 `warningSummary`，用于合并轻提示，不把每次轻微抖动升级为阻塞弹窗。

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

## 审计传输契约

现有创建 API 和按钮不增加请求字段；任务与不可变审计同事务落库。训练 `manifest.json` 的 `schemaVersion` 为 **2**：顶层 `sourceSystemId`、`exportId`、`auditReference {auditId,sha256,schemaVersion:1,capturedAtUtc}`；`sources` 固定 mediaRevisionId/mediaId/sourceSha256/宽高/帧数/A/B/C/F/snapshotHash；`samples` 增加 sampleId、mediaRevisionId、imageSha256、objects[]。objects 项将稳定 objectId 对应到 1 基 yoloLine 或 cocoAnnotationId，空帧保留空列表。文件名包含 exportId；标签仍是标准格式。

完整证据通过服务端 `python -m app.audit_export` 只读提取，或使用根 `export-audit.sh`。运维 ZIP 顶层 `format:'annotation-quality-audit',schemaVersion:1,extractedAtUtc,datasetCount,files[{name,sha256,size}]`；`datasets.jsonl` 含生成状态、创建时间、提取时 currentFinalAtRead、auditReference、snapshotFile、manifestFile。snapshots schemaVersion:1 保存 sourceSystemId/exportId/auditId/capturedAtUtc/sourceAppRevision、dataset 设置、sources、tables、completeness、坐标/责任约定。该 ZIP 的版本与训练 manifest 版本分别维护。

提取记录“ready”只表示生成成功，不表示已下载或训练。currentFinalAtRead 只表示提取时的当前状态；导出的历史 F 选择取 F 的 decisionEventId 和冻结决定，不能用最新 head 替代。SHA 用于传输校验，发布方身份仍依赖可信交付渠道。读取/提取故障返回非零且不发布半个包。
