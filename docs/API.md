# 当前接口与数据契约

本页是 API 地图与不变量，不复制全部类型定义。精确请求验证以已注册路由的 Pydantic 模型为准；开发时可读本地 FastAPI `/openapi.json`。旧原型中的示例接口不是活动接口。

## 通用协议

活动业务接口使用 `Authorization: Bearer <token>`。B/C/送审/训练导出的业务写请求还要求 `X-Review-Contract: 2` 与 `Idempotency-Key`。同一次未知结果/网络失败重试使用**相同 key、相同请求体**；用户新的业务意图才生成新 key。

原视频也必须登录。为支持原生 `<video>` 与 Range 请求，登录/注册/`GET /api/auth/me` 同时签发仅用于 `/api/track/video/` 的 HttpOnly、SameSite=Strict 播放 Cookie；HTTPS 使用 Secure，医院 HTTP 环境仍可播放。它不能作为工作区或其他业务接口凭据，也不放入 URL。显式无效 Bearer 不被旧 Cookie 掩盖。`POST /api/auth/logout` 清除播放 Cookie；启动先校验身份再挂载工作区。401 才清除失效身份，超时/500 保留账号及待重试请求并允许重新验证。

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
| `POST /api/track/restart-branch/{mediaId}` | 异常暂停后，从更早帧修正并显式重建分支；版本与原键恢复，成功后才续追 |
| `GET /api/track/status/{taskId}`、`/api/track/result/{mediaId}` | 追踪状态与对象 |
| `GET /api/track/result-file/{mediaId}`、`/api/track/overlay/{mediaId}` | JSONL 与按需生成的完整带框视频；预览缓存以当前视频/追踪/删除规则校验，生成时输入变化返回 409，失败 503 不影响已保存 JSONL |

`AnnotationObject` 定义在 [annotation.ts](../frontend/src/types/annotation.ts)：界面 `id` 与稳定 `objectId` 不等价，`frameIndex` 为实际 0 起帧号；工作区 bbox 为百分比 `{x,y,width,height}`。seed / Tracking / B/C 使用原图像素 xyxy，转换不能重复进行。追踪结果文件虽后缀 `.json`，实际逐行 JSONL。

`GET /api/track/result/{mediaId}` 区分结果生命周期：源视频存在但尚未生成结果时返回 200 `{format:"sam3-tracking-results-jsonl",state:"not_generated",frames:[],count:0}`，不生成文件、不声明空帧；全量结果存在时为 `state:"available"`，有效零对象帧仍在 frames 中。可选 `required=true` 用于追踪任务成功/暂停后的读取，此时缺失返回 409 `TRACKING_RESULTS_MISSING`。服务端记录已发布/成功读取过的结果，之后文件丢失同样返回 409；送审预览及送审也拒绝这个状态，不能用显式空帧确认绕过。媒体缺失返回 404 `MEDIA_NOT_FOUND`。格式/对象几何损坏返回 500 并记录日志，不忽略坏框后伪造空结果。客户端不能将所有 404 或所有异常统一忽略。

素材列表增加 `trackingResultState:'not_generated'|'present'|'missing'`；`hasTrackingResult` 分别为 false / true / null。null 明确表示已知结果丢失，不能当作尚未追踪。列表只查询文件/标记状态，不为列出素材读取全部 JSONL；`present` 表示文件存在，不代表内容已验证。详情和原始下载再校验格式、对象身份及几何。

`GET /api/track/result-file/{mediaId}` 在健康未追踪时返回 404 `TRACKING_RESULTS_NOT_GENERATED`（没有可下载文件），媒体缺失为 404 `MEDIA_NOT_FOUND`，已知结果丢失为 409 `TRACKING_RESULTS_MISSING`；可选 `required=true` 时尚未生成也为 409。损坏结果返回 500，不能下载坏框充当正常 JSONL；有效零对象结果仍可下载。列表、详情和下载遵守同一结果生命周期，404 不是可以统一忽略的成功响应。

工作区 GET 返回 `revision`（旧工作区首次为 0）；新版 PUT 携带 `expectedRevision` 和 `Idempotency-Key`，返回新的 `revision`。未知保存结果必须沿用原键和原正文，成功回执重放先于版本校验。无版本的旧客户端仍可保存普通编辑，但不能变更新删除规则，也不能覆盖服务端反馈、样本和回执。新前端始终使用版本和幂等键。

工作区写入在提交回执前校验人工对象：正整数稳定 ID、同帧身份唯一、原始帧号在视频范围内、百分比框及点为有限数值并在 0–100 内；人工基准使用原图像素 xyxy。合法点标注仍允许。非法输入返回 422，不改变文件、版本或回执。seed 同样校验规范 mediaId、仍存在的原视频及帧/像素框，以临时文件原子替换；删除后的迟到 seed 返回 404，不重建孤立目录，写入/替换失败保留旧来源并记录日志。

历史 Tracking 的 `object_id` / `objectId` / `sam3_object_id` 统一转换为稳定 `object_id`；存在真实 `source_frame_index` 时它优先于模型内部 `frame_index`。详情、下载、删除及送审采用同一转换，重复或矛盾的稳定身份不能静默接受。客户端也校验成功响应的数组、计数、帧、身份与几何结构，HTTP 200 不等于数据有效。完整重读替换 AI 集合，保留人工修改，不保留已撤销分支的旧 AI 框。

旧无版本请求携带的 `deletedTrackingIds` 只能是当前已有历史删除标记的子集（也可省略）；服务端保留完整当前集合，不删除既有标记。尝试新增历史删除标记同样返回 428，不能利用旧字段绕过版本校验。带版本及幂等键的当前客户端可正常删除和撤销。

`POST /api/track` 新前端另携带 `Idempotency-Key`，同账号原键/输入在当前后端进程内返回同一个 taskId，原键用于不同输入返回 409；覆盖轮次仍校验，不能借回执绕过 generationId。旧无键客户端保持兼容。状态 GET 含 `stage`（queued/decoding/preparing_model/tracking/saving_results/paused/completed）、处理帧数与原始 `lastProcessedFrame`，不暴露请求 hash/key。模型失败在任务状态返回 failed 与具体 message，短 HTTP 超时不等于后台任务已失败。工作区浏览状态可在追踪中保存；改人工框、基准或删除规则返回 409。rewind 成功的 `overlayRegenerated` 为 false，不重编码源视频；结果发布原子完成。

删除规则是 `deletedObjectIds:number[]`（整视频）和 `deletedFrameObjects:[{objectId,frameIndex}]`（单帧）；`deletedTrackingIds` 仅保留历史兼容。规则同时作用于人工/AI、seed、追踪读取、人工记录查询与送审。单帧删除不关闭后续该对象的追踪；整视频删除不允许该对象进入后续 seed。原追踪文件不物理抹除，撤销通过恢复工作区及移除对应删除标记实现。A/B/F 不随删除或撤销改变。

删除预览前必须保存当前工作区。响应为 `{mediaId,objectId,name,revision,totalCount,frameCount,manualCount,aiCount,firstFrame,lastFrame}`；同对象同帧人工覆盖 AI 只算一框；帧号 0 起，无框时首末帧为 null。确认删除时仍用当前工作区版本，不能把预览数量当无需校验的删除许可。

反馈请求为 `{expectedRevision,objectId,frameIndex,decision:'normal'|'corrected'|'reset',calibrate:boolean}`，使用 `Idempotency-Key`，与工作区共享版本。`reset` 不要求帧号，清该对象活动运动样本并使已确认尺寸参照失效，保留审计。`normal` 只在明确勾选校准且持久化追踪结果包含位移依据时生成 `normalMotionSamples`；每条为 `{objectId,frameIndex,reason:'motion',decision:'normal',calibrate:true,features:{motionNormalized}}`。数值取自服务端追踪数据，不接受客户提供阈值。`corrected` 要求当前帧已保存的人工框实际不同于异常原框，且不生成正常样本。 对尺寸/形状原因的 `normal`，服务端从实际追踪行取有效像素框，在该条 `trackingFeedbackEvents` 中保存 `geometryReference:{objectId,frameIndex,bbox,source:"confirmed-normal"}`；无须重画，且不依赖运动校准勾选。不接受客户端提供参照，不能将该框改写为人工标注。该字段为兼容旧事件的可选扩展。

反馈响应包含 `{ok,mediaId,revision,normalMotionSamples,trackingFeedbackEvents,pausedAnomalies,lastPausedContext}`。正常/已修正请求必须匹配当前持久化暂停帧及未解决对象，并核验真实追踪行；新键不能再次确认已解决的历史异常。每次只移除选定对象及已被删除的暂停项；仍有待处理对象时保留暂停上下文。运动样本按媒体、对象和位移原因隔离；尺寸参照单独按确认事件恢复，不豁免重叠或丢失检测。普通工作区 PUT 不能注入校准样本。Tracking 状态另返回 `warningSummary`，用于合并轻提示，不把每次轻微抖动升级为阻塞弹窗。

### 异常暂停后从较早帧重新追踪

`POST /api/track/restart-branch/{mediaId}` 使用 Bearer 和 `Idempotency-Key`，正文为 `{expectedRevision,expectedPausedFrame,startFrame,confirmDiscardFuture:true,generationId?}`，帧号全部为原始 0 起编号。首次执行必须 `startFrame < expectedPausedFrame`、服务器暂停仍未解决、起点有有效框、原视频存在、轮次/工作区版本一致，且未送审、无运行中的 Tracking。暂停帧及之后只能使用逐项异常确认；普通 rewind/track 不得绕过未解决暂停。

成功返回 `{ok,mediaId,revision,pausedAnomalies:[],lastPausedContext:null,cutoffFrame,removedRows,keptRows,deletedFutureSeedFiles:0,overlayRegenerated:false}`。只截断起点之后的旧 AI 行，清除旧分支的当前暂停及未来派生提示；所有人工框、seed/基准、删除规则、历史反馈和正常样本保留，不生成 normal/corrected 事件。操作不自动启动模型；客户端成功核对后保存该早帧 seed，再启动追踪，不重复普通 rewind。

跨文件发布通过持久日志与 SQL `review_write_receipts` 协调，收据重放早于业务版本/暂停校验，但轮次重置后不重放旧轮次操作；同账号原键原正文恢复已完成的重建，不再截断之后新模型的结果。相同 key 用不同意图返回 409。合法收据只证明过去已完成的分支操作；客户端恢复后另查询当前送审状态，已送审时停止 seed/模型启动，查询失败保留原起点待重试，不当作未送审。发布失败和重启按提交证据恢复两份文件，读取不能混用中间状态；临时故障保留原请求、暂停和数据，冲突需明确重读。当前暂停由真实追踪任务持久化，普通工作区 PUT 可以补展示信息和删除/撤销对象，但不能清空或伪造已确认暂停；显式反馈或重建才解除。运行时暂停更新不改变人工工作区 revision。

## A 完成与 B

实现：[review_workflow_routes.py](../backend/app/review_workflow_routes.py)、[reviewWorkflowApi.ts](../frontend/src/api/reviewWorkflowApi.ts)。前缀 `/api/review`。

| 方法与相对路径 | 请求/作用 |
| --- | --- |
| `GET /media/{mediaId}/completion-preview` | 来源 revision、有框/空/未知帧及未知范围 |
| `POST /media/{mediaId}/complete` | `expectedSourceRevision`、`confirmComplete`、`explicitEmptyFrameRanges:[{start,end}]`（0 起，含端点） |
| `GET /sessions`、`/sessions/{sid}` | 任务列表、详情 |
| `POST /sessions/{sid}/claim` | 原子领取，可自审 |
| `GET /media/{mediaId}/submission-status` | 活动送审任务及 `canWithdraw`，不包含已撤回任务 |
| `POST /sessions/{sid}/withdraw` | `expectedSessionRevision`；仅送审者且审查尚未领取/编辑，保留 A、任务置 `withdrawn` |
| `POST /media/{mediaId}/reset-annotations` | `expectedRevision`、`confirmDiscard:true`；覆盖重新标注，已送审活动任务或 AI 运行时拒绝，返回工作区新 revision / generationId |
| `GET /sessions/{sid}/frames/{fi}` | A、当前有效框、草稿、上次成功提交、帧版本/权限 |
| `PUT /sessions/{sid}/frames/{fi}/draft` | `expectedFrameRevision`、`patch` |
| `POST /sessions/{sid}/frames/{fi}/submit` | `expectedFrameRevision`、`patch` |
| `POST /sessions/{sid}/frames/{fi}/discard` | `expectedFrameRevision`，恢复上次有效提交或 A |
| `POST /sessions/{sid}/freeze` | `expectedSessionRevision`，固定 B、changes、C 任务 |
| `PUT /cursors` | `sessionId`、`frameIndex`、`expectedCursorRevision` |

`patch` 是当前帧相对 A 的完整净差量列表 `{objectId,bbox}`；不是新增框列表，也不是屏幕坐标。服务端拒绝未知/重复身份、非法坐标和不合法几何。

Session 关键字段：`id, baselineId, revision, state, reviewerId, media, progress, resume, permissions, readOnlyReason, completedReviewVersionId`。`progress` 含 `submittedFrames/unsubmittedFrames/draftFrames/modifiedFrames/modifiedBoxes/percent`；`resume` 含最后浏览、首个未提交、草稿索引及游标版本。Frame 关键字段：`frameRevision/state/hasDraft/baselineObjects/effectiveObjects/patch/lastSubmission/permissions`。不要用 `lastSubmission != null` 推断当前已提交。

上传相同内容返回 409 `DUPLICATE_VIDEO`，正文 `{message,code,duplicate:{mediaId,videoName,canOverwrite,reason,workspaceRevision,media}}`；`media` 是现存源视频元信息，不会保存第二份上传。覆盖通过上表 reset 接口完成，使用原键原正文重试；目录与未撤回 A 的关联在服务端再次校验。已覆盖工作区 GET 带 generationId；旧无版本保存返回 428。该视频之后的人工记录、seed、rewind 和 Tracking 写请求携带当前 generationId，缺失或旧值返回 409，在修改文件/DB或清未来分支之前拒绝。尚未覆盖的旧工作区保持兼容。

Session 另带 `returnRequests:[{frameIndex,reason,confirmationId}]`，仅未解决的本帧重审请求。撤回后再次送审创建新 A/任务，不复用 `withdrawn`；领取与撤回使用同一 SQL 事务竞争。

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
| `POST /sessions/{sid}/return` | `expectedSessionRevision`、`changeId`、`reason`（1–1000 字）；当前确认人将该项所在整帧退回原 B 重审 |
| `PUT /sessions/{sid}/cursor` | `changeId`、`expectedCursorRevision` |

`Change` 含 `changeId/frameIndex/objectId/annotationId/beforeBbox/afterBbox/metrics/decisionRevision/decision`。C 不能上传任意新 bbox 或改变类别/对象集合。`progress` 含 `totalChanges/decided/pending/keptA/adoptedB/percent`，零修改百分比不替代显式完成。

C 权限另带 `canReturn`；返回状态为 `returned`，`returnedReview:{frameIndex,reason,reviewSessionId,nextConfirmationId}|null` 指向原 B 及重审后新 C。旧 C 不再可编辑或完成。B 仅取消被退回帧的有效提交，保留旧 submission 与起始 B 几何；草稿/恢复不能代替重新显式提交。再次 freeze 固定新 B/C，其他帧几何未变且已有决定的项以带来源关联的新事件继承，退回帧不继承决定；新 C 保留原确认人。

新版 C 写请求携带 `X-Confirmation-Response: delta`。响应新增 `itemsScope:'changed'`：决定/撤销仅返回受影响项，游标/领取/完成/重新确认/退回返回空 `items`；完整 `session`、选中项、下个待选项仍返回。前端按 changeId 合并而不是替换全列表，检测到其他窗口的业务版本跳变时重新 GET session/changes。无 header 的旧客户端仍返回全量列表。幂等回执保存首次响应，重试返回同一响应形态；新版能重放升级前的全量回执。响应模式不改变原请求业务 hash，不删除历史决定或旧回执。会话统计与下个待选项通过 SQL 计算，不为浏览位置反复解码全视频几何。

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

## 全量标注流程统计导出

入口在标注记录页；实现为 `statistics_export.py` / `statistics_export_jobs.py` / `statistics_export_routes.py`，前端 `StatisticsDataExport.vue` 与 `statisticsExportApi.ts`。按外部脚本的 annotation-confirmation-statistics-v1 格式导出全实例，包含未完成和历史 A/B/C/F、人员与流程记录，不受页面筛选影响。现有登录账号可申请全量包；当前没有额外管理员角色。生成的任务和下载仅对创建账号开放，匿名拒绝、其他账号不能读取该任务。

| 方法与路径 | 作用 |
| --- | --- |
| `POST /api/statistics/exports` | 空 JSON `{}`，必需 `Idempotency-Key`（1–128 字符）；202 返回后台任务，当前进程的有效回执内同账号原键返回同任务，不要求 X-Review-Contract |
| `GET /api/statistics/exports/{id}` | 本人任务状态 `queued/running/ready/failed`、阶段、表数/行数进度、文件名、大小、错误、到期时间 |
| `GET /api/statistics/exports/{id}/download` | ready 时鉴权下载 ZIP，不返回数据库文件；未就绪 409，生成失败 409，输出丢失 410 |

全实例同时最多生成一个包，新请求遇到已有运行任务返回 409 `STATISTICS_EXPORT_BUSY`；有效回执的原键恢复优先。临时输出保留 24 小时、最多 20 份，任务/回执有上限。已取得任务 ID 的查询在服务重启或任务过期后返回 410 `STATISTICS_EXPORT_EXPIRED`，用户可明确新建。输出文件丢失为 `STATISTICS_EXPORT_FILE_MISSING`，需重新生成。瞬时创建/查询/下载错误保留可重试状态；前端按账号保存创建键和任务 ID，未知创建结果沿用原键。回执是进程内临时状态：若创建响应未返回 ID 且服务随后重启，或回执 24 小时到期，旧键的手动重试可能生成新只读包，不承诺跨重启/过期仍是同任务；这不会重复修改业务数据。请求日志不记录 token。

ZIP 包含 15 个必需 CSV/JSONL、2 个可选历史表（存在时）和 manifest，逐文件校验和与行数用于传输校验。users 仅 id/username/created_at；密码、JWT、原始视频、原 SQLite 文件均排除。`sourceSystemId` 优先读取已有 `audit_source_identity` 部署身份，旧库无身份时 CLI 须显式提供或配置 STATISTICS_SOURCE_SYSTEM_ID，不能默认把多个医院当同一个来源。`final_versions.is_current` 表示读取时已确认会话的最新 F，不代表历史 F 可训练导出；空帧和旧版本保留。

该包与下节 annotation-quality-audit 包分别维护。消费者必须支持此全量格式的导入和展示；本地统计项目和远程仓库可能使用不同前端入口，联调必须核对实际部署版本。相同来源的新全量包会包含旧记录，消费者需要按稳定身份处理重复或更新，不能把主键冲突当正常增量行为，也不能将首包导入成功当成反复导入验收。本功能不改变训练集准入、快照或流程状态。

## 审计传输契约

现有创建 API 和按钮不增加请求字段；任务与不可变审计同事务落库。训练 `manifest.json` 的 `schemaVersion` 为 **2**：顶层 `sourceSystemId`、`exportId`、`auditReference {auditId,sha256,schemaVersion:1,capturedAtUtc}`；`sources` 固定 mediaRevisionId/mediaId/sourceSha256/宽高/帧数/A/B/C/F/snapshotHash；`samples` 增加 sampleId、mediaRevisionId、imageSha256、objects[]。objects 项将稳定 objectId 对应到 1 基 yoloLine 或 cocoAnnotationId，空帧保留空列表。文件名包含 exportId；标签仍是标准格式。

完整证据通过服务端 `python -m app.audit_export` 只读提取，或使用根 `export-audit.sh`。运维 ZIP 顶层 `format:'annotation-quality-audit',schemaVersion:1,extractedAtUtc,datasetCount,files[{name,sha256,size}]`；`datasets.jsonl` 含生成状态、创建时间、提取时 currentFinalAtRead、auditReference、snapshotFile、manifestFile。snapshots schemaVersion:1 保存 sourceSystemId/exportId/auditId/capturedAtUtc/sourceAppRevision、dataset 设置、sources、tables、completeness、坐标/责任约定。该 ZIP 的版本与训练 manifest 版本分别维护。

提取记录“ready”只表示生成成功，不表示已下载或训练。currentFinalAtRead 只表示提取时的当前状态；导出的历史 F 选择取 F 的 decisionEventId 和冻结决定，不能用最新 head 替代。SHA 用于传输校验，发布方身份仍依赖可信交付渠道。读取/提取故障返回非零且不发布半个包。
