# 当前接口与数据契约

## 2026-10-29 当前入口

新增 POST /api/datasets/annotations/{media_id}/snapshot：认证 + X-Review-Contract:2 + Idempotency-Key，正文 expectedSourceRevision、confirmComplete、explicitEmptyFrameRanges（原帧从0开始）。返回 finalVersionId（annexp_ 前缀，兼容训练队列选择字段，并非已验收F）。后续现有 exports/preview、exports、status/download 接口复用；快照仅本人可选择，来源变化使旧导出失效。旧 complete 接口保留历史兼容，新标注UI不调用。

本页是 API 地图与不变量，不复制全部类型定义。精确请求验证以已注册路由的 Pydantic 模型为准；开发时可读本地 FastAPI `/openapi.json`。旧原型中的示例接口不是活动接口。

## 集中审查协议

- `POST /api/track` 新增 `combineResults:boolean`（默认 false 兼容旧请求）。新页面的首尾追踪固定 true，并携带人工 terminalAnnotations、reviewMode=true、startFrame=0。缺少端点时422；该字段进入幂等正文，不在重试时补写或切换。
- true 时正向、人工尾帧反向及头部检测器共同生成实际输出；任务成功需同时有 `endpointVerification` 与 `jointTracking.state=checked`。jointTracking 只含状态、policy/version、checkedFrames、changedObjects、recoveredObjects、reviewCount等摘要，不增加画面上的采用理由。前端收到旧后端的仅核验结果必须报错，不能冒充综合完成。
- 检测器依赖/权重、原图读取或数据结构失败：task.status=failed，jointTracking.state=failed，保留正向已发布结果及失败疑点；局部无检测候选是明确空观测，区别于检测失败。`head_click.detect_regions` 仅关联已知追踪身份，不自动新增内部检测。
- 最终 `objects[].bbox` 是唯一综合几何，source 内部可为 joint_tracking、score/mask_area 在修正后清空过期值；读取 API 仍统一映射 source=ai。人工覆盖、删除过滤、stable objectId 和训练集准入不变。reviewEvidence.jointCheck 记录检查状态与不确定对象，审查 evidence 接口新增简短 jointTracking 状态；已修正且有支持的旧位置疑点不继续当作当前错误。
- 全量结果读取 envelope 同样返回 jointTracking 状态：所有帧均有 checked 才表示整段综合完成，部分缺失或失败返回 failed 和实际 checkedFrames。客户端刷新后仍展示未完成提示并保留几何可查看，不把失败成果当成空数据，也不冒充综合成功。成功重试后替换失败标记。

- `POST /api/track` 可选 `terminalAnnotations: object[] | null`：省略/null 保留当前帧单向追踪；数组（含明确空数组）启动人工首尾核验，要求 `startFrame=0`、`reviewMode=true`。对象使用原图像素 bbox、正整数 object_id、source=manual，frameIndex 若提供必须为真实尾帧。首尾快照必须与已保存 manualAnnotations 完整一致；来源/几何/帧/重复身份错误返回422。尾帧ID是独立端点编号，不必等于首帧ID；不得依据相同编号推断对应。参数包含在原请求哈希和幂等重放中。
- 任务状态的 `checking` 覆盖反向/综合阶段；`message` 显示处理进度。完成时可附 `endpointVerification`：state=checked/failed/insufficient、anchorKind、anchorFrame、anchorHash、matches（尾帧局部ID字符串→正向ID）、unmatchedTailIds、checkedFrames、策略与阈值。checked表示执行完比较，不能作为自动验收。旧仅核验请求可保留正向成功与反向失败证据；新综合请求不能把反向执行失败报告成综合成功，审查完成规则不变。
- JSONL `reviewEvidence.endpointCheck` 保存证据，末帧另保存 terminalAnnotations 原始人工快照。审查 evidence 接口可返回 `endpointVerification`；疑点新增 endpoint_disagreement / endpoint_identity_uncertain，供原有片段队列展示。结果显示接口抑制已被人工尾框替代的匹配 AI 框；旧仅核验请求保留正向几何，新综合请求保存唯一综合几何，A 合并时按唯一对应采用人工几何。
- 全量结果 envelope 可带 `terminalIdentity={frameIndex,anchorHash,matches}`。前端严格校验其帧号/哈希/一对一正整数映射后，只对对应尾帧人工框统一身份，保留 endpointOriginalObjectId / endpointAnchorHash；同一哈希按原始尾编号应用当前对应，结果相同不重复转换，明确错配纠正后可更新 canonical ID。该人工工作区修改沿用原保存队列/版本校验，不改原始几何。冻结审查证据保留原始 terminalAnnotations，不依赖后续可变工作区。
- 综合输出可附 `reviewEvidence.boundaryExit={policy:'boundary-exit-v1',exitedObjectIds,findings}`：已确认边界退出的AI身份停止出框，原相关诊断归档于 findings，不继续作为当前活动风险；不写工作区删除或正常确认。历史读取、JSONL下载、训练来源共用该只读投影，缺少有效原图尺寸不推测边界。身份重关联策略记录为 associationPolicy=first-anchor-v2，无新增客户端写入权限或数据库迁移。

- `POST /api/track` 新增 `reviewMode`（默认 false 兼容旧客户端，新页面发送 true）。true 时质量疑点不暂停，按原单段上限自动续跑；状态新增 `checking`。模型/持久化错误仍 failed。
- `POST /api/review/media/{id}/complete` 新增 `concentratedReview`。新页面在建立幂等请求时固定 true；旧持久请求不注入新字段，以保持旧回执哈希。新任务冻结 Tracking 疑点及 sourceRevision；不自动迁移已有三阶段任务。
- `GET /api/review/sessions/{id}/evidence` 返回 groups（对象/原因/范围/原始样本）、实际 checkedFrames/failedFrames、backwardChecked/backwardFailed、sourceHash。reviewed 按涉及帧的有效提交计算，浏览没有写入。
- 新 session 字段 `workflow=concentrated|legacy`、`finalVersionId`。集中审查的 freeze 同事务接受 B 为 F；仍要求完整逐帧显式提交。
- `POST /api/review/sessions/{id}/reopen-review`：`expectedSessionRevision, frameIndex`，原审查员可重新开启已完成的新任务；版本/幂等保护，当前最终指针清空，旧包失效。后续须重新提交该帧并完成视频。
- 新任务的 C 写入接口返回 `REVIEW_ACCEPTANCE_ONLY`，不能通过历史接口绕过审查。

## 通用协议

活动业务接口使用 `Authorization: Bearer <token>`。B/C/送审/训练导出的业务写请求还要求 `X-Review-Contract` 与 `Idempotency-Key`。当前审查增删与退回能力使用版本 `3`；旧 `2` 客户端保留原几何操作兼容，但不能改写含存在性变化的帧或确认任务，需刷新升级。同一次未知结果/网络失败重试使用**相同 key、相同请求体**；用户新的业务意图才生成新 key。

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

`POST /api/track` 新前端另携带 `Idempotency-Key`，同账号原键/输入在当前后端进程内返回同一个 taskId，原键用于不同输入返回 409；覆盖轮次仍校验，不能借回执绕过 generationId。旧无键客户端保持兼容。状态 GET 含 `stage`（queued/decoding/preparing_model/tracking/saving_results/paused/completed）、处理帧数与原始 `lastProcessedFrame`，不暴露请求 hash/key。模型失败在任务状态返回 failed 与具体 message，短 HTTP 超时不等于后台任务已失败。工作区浏览状态可在追踪中保存；改人工框、基准或删除规则返回 409。rewind 成功的 `overlayRegenerated` 为 false，不重编码源视频；结果发布原子完成。普通 rewind 将结果截断与被丢弃未来的身份/关系认可退休一起纳入两文件恢复日志，API 使用真实账号的内部 SQL 回执判断提交；不递增人工工作区 revision，不导致随后正常保存/续追的版本冲突。

删除规则是 `deletedObjectIds:number[]`（整视频）和 `deletedFrameObjects:[{objectId,frameIndex}]`（单帧）；`deletedTrackingIds` 仅保留历史兼容。规则同时作用于人工/AI、seed、追踪读取、人工记录查询与送审。单帧删除不关闭后续该对象的追踪；整视频删除不允许该对象进入后续 seed。原追踪文件不物理抹除，撤销通过恢复工作区及移除对应删除标记实现。A/B/F 不随删除或撤销改变。

删除预览前必须保存当前工作区。响应为 `{mediaId,objectId,name,revision,totalCount,frameCount,manualCount,aiCount,firstFrame,lastFrame}`；同对象同帧人工覆盖 AI 只算一框；帧号 0 起，无框时首末帧为 null。确认删除时仍用当前工作区版本，不能把预览数量当无需校验的删除许可。

反馈请求为 `{expectedRevision,objectId,frameIndex,decision:'normal'|'corrected'|'reset',calibrate:boolean,scope?:'object'|'frame'}`，使用 `Idempotency-Key`，与工作区共享版本。`reset` 不要求帧号，清该对象活动运动样本并使已确认尺寸及身份参照失效，保留审计。`normal` 只在明确勾选校准且持久化追踪结果包含位移依据时生成 `normalMotionSamples`；每条为 `{objectId,frameIndex,reason:'motion',decision:'normal',calibrate:true,features:{motionNormalized}}`。数值取自服务端追踪数据，不接受客户提供阈值。已保存人工修框不学习旧 AI 位移。`corrected` 要求当前帧已保存的人工框实际不同于异常原框，且不生成正常样本。

默认/`scope:"object"` 的“本对象正常”只清除该对象当前暂停，其他项继续待处理；正常事件从当前实际有效框保存 `geometryReference:{objectId,frameIndex,bbox,source:"confirmed-normal",checkpoint:true}`，已保存人工框优先，无须重画且不依赖运动校准。它更新该对象独立的几何检查点，不修改人工标注来源，也不重新选择框内最强光学核来当可信身份。消失或缺少有效框时拒绝正常确认。`pausedAnomalies` 返回尚待处理项，全部成功保存后才可续追；失败或原键重放不清除另一项。

图像风险的逐对象 normal 同样保存上述实际参考；它可用于后续原图光学建档，不能把正常确认改成在线模型训练。主体位移样本在 features 追加 `referenceKind:"head"`，旧样本省略即 bbox；两类独立使用，避免旧框中心阈值放宽主体跳跃。

追踪行的 v2 `identity_evidence` 可追加 `observationVerified`：仅由生产图像核验器在原图、BBox、非空二值Mask及框内占用有效时设置，不接受客户端宣称。该标志与 `status/head/identityReference` 独立，身份缺参考仍为unknown。当前源签名及帧/对象/框/Mask面积验证通过后，普通矩形形变和两框高重叠只作提示；独立图像风险、整体尺度极端与对象消失仍保护。历史Mask面积骤减仅在当前证据严格有效、主体可靠且无明确丢失时降提示；未知、失效或旧证据保留坍缩兜底。新增 `diagonal_ratio`、`geometry_thresholds.manual_diagonal_ratio` 及 `manual_diagonal_ratio=... HARD` 诊断解释实际整体尺度触发，不把同条记录的SOFT宽高指标冒充硬暂停原因。旧行缺字段不自动升级为已核验，没有新增确认权限或API写入字段。

诊断可追加 `mask_area_decision`（可靠当前主体提示或保守坍缩兜底），及coverage.metrics中的尾部光学归属标志；不新增客户端写入权限。`pausedObjects.type` 的尺寸分类包含对角线HARD，发布/持久化/前端说明按真实HARD原因统一；兼容历史错误type但保有raw reasons的记录，不根据SOFT尺寸把运动HARD改写成尺寸。原始框、掩膜面积、参考和暂停帧不因展示修正而重写。

可选 `singleCoreConflict` 仅记录当前唯一可见光核与可信伙伴、真实Mask交叠的核验摘要；对象身份仍可为unknown。持续未恢复时以 `image_shared_core_unresolved` 提示身份未辨明，按同一partner连续计数，不给未知对象新身份或修改单对象正常确认范围。已提供但非字典的head/coverage metrics被明确拒绝并记录，不让损坏历史摘要触发属性访问错误。

反馈响应包含 `{ok,mediaId,revision,normalMotionSamples,trackingFeedbackEvents,pausedAnomalies,lastPausedContext}`。正常/已修正请求必须匹配当前持久化暂停帧及未解决对象，并核验真实追踪行；新键不能再次确认已解决的历史异常。每次只移除选定对象及已被删除的暂停项；仍有待处理对象时保留暂停上下文。运动样本按媒体、对象和位移原因隔离；尺寸参照单独按确认事件恢复，不豁免重叠或丢失检测。普通工作区 PUT 不能注入校准样本。Tracking 状态另返回 `warningSummary`，用于合并轻提示，不把每次轻微抖动升级为阻塞弹窗。

### 正常参照与短时风险观察

新版“本帧所有对象无异常”使用原 feedback API，另带 `scope:"frame"`（仅 `decision:"normal"`）。默认/省略 `scope` 保持以上逐对象旧契约，`corrected/reset` 仍逐对象。帧级确认不按请求 `objectId` 限定范围：服务端读取暂停帧全部有效对象、人工修框优先、应用删除规则；包括正常对象，缺失异常对象的有效框则拒绝全部操作。不能用客户端传来的框、对象列表或参照取代实际数据。

每个有效对象追加一个 normal 事件，带共同 `checkpointId`、`scope:"frame"`、`createdAt`/`userId`；`geometryReference` 带 `checkpoint:true` 和实际像素 bbox/name/frameIndex，可带真实 maskArea。实际相交组合保存 overlapReferences，已人工修框的对象不采纳旧 AI 运动样本。全部事件、所有暂停项的清除和版本/回执一次原子保存，版本只增加一次。失败不部分确认；原键重放返回原响应且不新增检查点。后续从事件恢复最新适用检查点作为固定尺寸基准，reset 或更新人工框按原规则优先；原人工标注和 AI 数据不改写。

尺寸/形状的 `normal` 可在 `geometryReference` 追加正值 `maskArea`（取自服务端模型结果；未知不填），恢复时最多使用每对象最近 3 个完整人工认可框，不拼接不同框的宽高范围。原人工框与来源不改变。续追校验最新适用的正常参考确实已载入；删除、人工修正、重置或分支时序使参照不适用时按各自规则处理，不把缺失参考静默当正常。

重叠认可来自同帧实际有效框，不接受客户端组合或坐标。单项正常只记录本对象的候选邻居快照，不能直接认可整对。双方在同一次暂停、同一双框/人工编辑快照下分别正常确认成功，或显式 `scope:"frame"` 同时检查后，才保存 `overlapReferences:[{objectIds:[a,b],frameIndex,bboxes:[boxA,boxB],source:"confirmed-normal",approvalVersion:2,pauseId,pairSnapshotId,generationId,objectEpochs,confirmedObjectIds,confirmedEventIds}]`。首个事件保持不可变，第二次确认只在自己的新事件中发布有效双边认可；不能拼凑历史暂停的双方决定。修改、删除/撤销删除、重置和新追踪分支使对应旧认可失效；撤销恢复框不能复活已退休的关系认可。仅更名或其他对象的编辑不退休这对认可。认可仅限原对象对的相近状态，不豁免新的伙伴、明显恶化、分离后的新风险或独立异常。双方还须有服务器核验的可靠身份参照，才能附带 `identityReferences`；仅有旧几何认可不提供新的图像可信身份。成功反馈可返回兼容扩展 `confirmedReference:{eventId,geometryReference|null,overlapReferences:[],identityReference?}`，原键重放保留原摘要。

`identityReference` 是与几何检查点分开的可选服务器字段：`{version:2,source:"native-frame-mask-v2",objectId,frameIndex,bbox,originFrameIndex,referenceBBox,referencePoint,point,radius,appearance,status:"reliable",sourceSignature,generationId,objectEpoch}`。仅从当前真实追踪行的新版可靠来源取候选，并在一次当前原视频解码的小区域核验后保存；不接受客户提供可信点、外观、阈值或参考帧。旧 v1 的 `matched` 光学点不能升级为可信身份，缺新版依据时正常确认仍保存几何，身份由后续追踪后台按真实人工来源重新核验。旧身份原点不能通过改帧号伪装成当前可靠来源；无法分辨时保留不确定而不生成可信引用。

普通尺寸越界与新增疑似合并按原始帧观察连续 3 帧，同对象/对象对独立计数；严重几何突变、极端跳跃与异常消失仍及时暂停。初始稳定重叠及低 IoU 的大小框包含仅提示。JSONL 的 `anomaly_details` 提供 `geometry_thresholds`、`geometry_observation_count/required`、`overlap_pairs`（全部组合）、`pause_started_frame` 和 `decision_frame_index`。持续异常暂停定位首次风险帧，观察窗口中的后续帧不发布为正常结果；`lastProcessedFrame`/`processedFrames` 对应实际发布结果，不能用模型分析到的第三帧冒充已保存进度。短时观察跨追踪轮次恢复，确认或新的人工工作结束对应旧观察。

真实原图/Mask 辅助核验的诊断为 `identity_evidence:{version:2,source:"native-frame-mask-v2",sourceSignature,objectId,frameIndex,bbox,maskArea,status,geometryReferenceFrameIndex,geometryReferenceBBox,head,coverage,reasons,thresholds,identityReference?}`，仍兼容读取 v1 诊断。当前对象/源帧/框/Mask 面积、原始身份建立帧/框和最新适用几何检查点分别校验，不能改帧号伪造引用。status 为 supported/risk/unknown/warning/uncertain；head 包含光学匹配点、原身份参考/前次可靠点、半径与 associationStatus，coverage 区分明确丢失、局部 Mask 覆盖不足、邻居污染与暂时粘连。它不是精子分类概率，也不证明完整尾部。

可靠独立主体仍保留时，旋转、甩尾和仅 Mask 混入邻居不直接按宽高硬停；局部细线在框内但 Mask 覆盖不足为提示，明确大段结构跑出框仍保护。短暂粘连使用有限不确定观察并冻结可信身份；不能将粘连质心更新成正常位置，也不能无限放行。缺字段、坏绑定或其他未知证据保留旧保护，已确认Mask骤减仅在可靠当前主体且无明确丢失时降为候选，明确消失仍检查。图像持续风险提供 `image_observation_count/required`、`image_pending` 与首次风险帧；pausedObjects.type 兼容 `coverage_loss` / `identity_loss`。前端只解释实际证据，不推断或决定风险。Mask ROI 和原图不进入 JSON，仍按真实原始像素坐标保存。

暂停工作区可带服务端 `diagnostic` 以刷新恢复完整解释。客户端展示字段不参与后端判定；原始帧号、稳定对象 ID、幂等/版本、删除及送审边界保持原协议。

### 异常暂停后从较早帧重新追踪

`POST /api/track/restart-branch/{mediaId}` 使用 Bearer 和 `Idempotency-Key`，正文为 `{expectedRevision,expectedPausedFrame,startFrame,confirmDiscardFuture:true,generationId?}`，帧号全部为原始 0 起编号。首次执行必须 `startFrame < expectedPausedFrame`、服务器暂停仍未解决、起点有有效框、原视频存在、轮次/工作区版本一致，且未送审、无运行中的 Tracking。暂停帧及之后只能使用逐项异常确认；普通 rewind/track 不得绕过未解决暂停。

成功返回 `{ok,mediaId,revision,pausedAnomalies:[],lastPausedContext:null,cutoffFrame,removedRows,keptRows,deletedFutureSeedFiles:0,overlayRegenerated:false}`。只截断起点之后的旧 AI 行，清除旧分支的当前暂停及未来派生提示；所有人工框、seed/基准、删除规则、历史反馈和正常样本保留，不生成 normal/corrected 事件。被丢弃未来里的身份/关系认可通过内部事件 ID 退休，不删除人工决定、几何历史或旧回执；起点同帧及更早认可仍适用，避免正常续追立即丢掉刚保存的判断。以后新分支走过旧确认帧再启动，也不能恢复旧分支的未来关系认可。操作不自动启动模型；客户端成功核对后保存该早帧 seed，再启动追踪，不重复普通 rewind。

跨文件发布通过持久日志与 SQL `review_write_receipts` 协调，收据重放早于业务版本/暂停校验，但轮次重置后不重放旧轮次操作；同账号原键原正文恢复已完成的重建，不再截断之后新模型的结果。相同 key 用不同意图返回 409。合法收据只证明过去已完成的分支操作；客户端恢复后另查询当前送审状态，已送审时停止 seed/模型启动，查询失败保留原起点待重试，不当作未送审。发布失败和重启按提交证据恢复两份文件，读取不能混用中间状态；临时故障保留原请求、暂停和数据，冲突需明确重读。当前暂停由真实追踪任务持久化，普通工作区 PUT 可以补展示信息和删除/撤销对象，但不能清空或伪造已确认暂停；显式反馈或重建才解除。运行时暂停更新不改变人工工作区 revision。

## A 完成与 B

实现：[review_workflow_routes.py](../backend/app/review_workflow_routes.py)、[reviewWorkflowApi.ts](../frontend/src/api/reviewWorkflowApi.ts)。前缀 `/api/review`。

| 方法与相对路径 | 请求/作用 |
| --- | --- |
| `GET /media/{mediaId}/completion-preview` | 来源 revision、有框/空/未知帧及未知范围 |
| `POST /media/{mediaId}/complete` | `expectedSourceRevision`、`confirmComplete`、`explicitEmptyFrameRanges:[{start,end}]`（0 起，含端点） |
| `GET /sessions`、`/sessions/{sid}` | 任务列表、详情 |
| `POST /sessions/{sid}/claim` | 原子领取，可自审 |
| `GET /sessions/{sid}/objects` | 全视频对象目录，`items:[{objectId,classKey,origin}]`；origin 为 baseline/review |
| `POST /sessions/{sid}/objects` | `expectedSessionRevision`、可选 `classKey`；保留新对象身份，返回 `{session,object}`，原键重放不再分配 |
| `POST /sessions/{sid}/return-annotation` | `expectedSessionRevision,reason`；当前审查员将进行中任务退回人工标注，旧任务只读、历史保留 |
| `GET /media/{mediaId}/submission-status` | `items` 为活动送审任务及 `canWithdraw`，排除已撤回/退回人工任务；`history` 提供退回人工任务及原因 |
| `POST /sessions/{sid}/withdraw` | `expectedSessionRevision`；仅送审者且审查尚未领取/编辑，保留 A、任务置 `withdrawn` |
| `POST /media/{mediaId}/reset-annotations` | `expectedRevision`、`confirmDiscard:true`；覆盖重新标注，已送审活动任务或 AI 运行时拒绝，返回工作区新 revision / generationId |
| `GET /sessions/{sid}/frames/{fi}` | A、当前有效框、草稿、上次成功提交、帧版本/权限 |
| `PUT /sessions/{sid}/frames/{fi}/draft` | `expectedFrameRevision`、`patch` |
| `POST /sessions/{sid}/frames/{fi}/submit` | `expectedFrameRevision`、`patch` |
| `POST /sessions/{sid}/frames/{fi}/discard` | `expectedFrameRevision`，恢复上次有效提交或 A |
| `POST /sessions/{sid}/freeze` | `expectedSessionRevision`，固定 B、changes、C 任务 |
| `PUT /cursors` | `sessionId`、`frameIndex`、`expectedCursorRevision` |

`patch` 是当前帧相对 A 的完整净差量，坐标为原图像素：`{objectId,bbox}` 默认为 update；`{op:"add",objectId,classKey,bbox}` 补当前帧；`{op:"delete",objectId}` 删除本帧。add 只能关联本任务全视频目录中的合法身份，重复身份、非法类别/坐标拒绝。新增 ID 先由 objects 接口分配，不能客户端猜号。唯一视频类别默认继承；多类别必须明确选择已有类别，全空 A 默认项目类别 rare sperm。已有对象类别不可改，不能因补标把 rare sperm 拆成另一类 sperm。A 不改写；净差量消失则无需 C 决定。

Session 关键字段：`id, baselineId, revision, state, reviewerId, media, progress, resume, permissions, readOnlyReason, completedReviewVersionId,submittedBy,withdrawalBlockedReason,returnAnnotation`。`permissions.canReturnAnnotation` 表示当前审查员是否可退回人工；`returnAnnotation` 含 reason/actorId/createdAt。`progress` 含 `submittedFrames/unsubmittedFrames/draftFrames/modifiedFrames/modifiedBoxes/percent`；`resume` 含最后浏览、首个未提交、草稿索引及游标版本。Frame 关键字段：`frameRevision/state/hasDraft/baselineObjects/effectiveObjects/patch/lastSubmission/permissions`。不要用 `lastSubmission != null` 推断当前已提交。

`resume.submittedFrameIndexes` 是由当前帧状态 `submitted` 投影出的只读 0 起索引数组，供审查帧条绘制已提交状态；它不是客户端提交进度的写入口。已提交帧重新编辑为草稿或被退回重审后，应从该数组移除，仍保留其历史 submission；放弃草稿恢复有效提交或重新提交成功后再出现。`resume.draftFrameIndexes` 表示当前草稿，退回标记来自退回记录，不能仅凭历史提交引用、当前浏览帧或帧条颜色判定视频已完成。

上传相同内容返回 409 `DUPLICATE_VIDEO`，正文 `{message,code,duplicate:{mediaId,videoName,canOverwrite,reason,workspaceRevision,media}}`；`media` 是现存源视频元信息，不会保存第二份上传。覆盖通过上表 reset 接口完成，使用原键原正文重试；目录与未撤回 A 的关联在服务端再次校验。已覆盖工作区 GET 带 generationId；旧无版本保存返回 428。该视频之后的人工记录、seed、rewind 和 Tracking 写请求携带当前 generationId，缺失或旧值返回 409，在修改文件/DB或清未来分支之前拒绝。尚未覆盖的旧工作区保持兼容。

Session 另带 `returnRequests:[{frameIndex,reason,confirmationId}]`，仅未解决的本帧重审请求。撤回后再次送审创建新 A/任务，不复用 `withdrawn` 或 `returned_to_annotation`；领取与撤回使用同一 SQL 事务竞争。

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

`Change` 含 `changeId/frameIndex/objectId/annotationId/beforeBbox/afterBbox/changeKind/metrics/decisionRevision/decision`。`changeKind` 为 update/add/delete；beforeBbox 或 afterBbox 可为 JSON null，表示该版本不存在该框，不等于读取失败。增删项的几何指标为 null。C 不上传任意新 bbox 或类别，只选择 A/B 的框或明确不存在状态。`progress` 含 `totalChanges/decided/pending/keptA/adoptedB/percent`，零修改百分比不替代显式完成。

C 权限另带 `canReturn`；返回状态为 `returned`，`returnedReview:{frameIndex,reason,reviewSessionId,nextConfirmationId,reviewState,returnedToAnnotation,annotationReturnReason}|null` 指向原 B 及重审后新 C。旧 C 不再可编辑或完成。B 仅取消被退回帧的有效提交，保留旧 submission 与起始 B 几何；草稿/恢复不能代替重新显式提交。再次 freeze 固定新 B/C，其他帧存在性、类别、几何均未变且已有决定的项以带来源关联的新事件继承，退回帧不继承决定；新 C 保留原确认人。

新版 C 写请求携带 `X-Confirmation-Response: delta`。响应新增 `itemsScope:'changed'`：决定/撤销仅返回受影响项，游标/领取/完成/重新确认/退回返回空 `items`；完整 `session`、选中项、下个待选项仍返回。前端按 changeId 合并而不是替换全列表，检测到其他窗口的业务版本跳变时重新 GET session/changes。无 header 的旧客户端仍返回全量列表。幂等回执保存首次响应，重试返回同一响应形态；新版能重放升级前的全量回执。响应模式不改变原请求业务 hash，不删除历史决定或旧回执。会话统计与下个待选项通过 SQL 计算，不为浏览位置反复解码全视频几何。

`GET /api/final-versions/{id}` 和 `/{id}/download` 提供完整 JSON。每帧存在，包括空 `objects:[]`；对象保留稳定身份、类别、最终框、A/B 框及 `choice/resolution/changeId/decisionEventId`。未改对象 `choice:null`、`resolution:'unchanged'`，来源 A。完整帧表不能用仅有对象的表代替，否则会丢空帧。被确认删除/拒绝补标的对象不进入最终帧 objects，但独立决定记录及统计/训练审计仍保留其事件与两侧状态。

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

含补标/删除的统计包、训练审计与审计传输包声明 `requiredFeatures:["review_presence_v1"]`。消费端必须理解 nullable bbox、对象存在性和完整决定记录；旧统计系统需适配后再分析，不能将 null 几何当 0 或遗漏删除决定。

### 点选头部

`POST /api/track/head-box/{media_id}`（需登录）：`{frameIndex,x,y}` 为原始帧号和原图像素点击坐标；返回 `{frameIndex,width,height,bbox:[x1,y1,x2,y2],confidence}`。只提供候选，不写工作区或建立对象 ID。422 表示坐标/候选无效或歧义，404 表示素材/帧不存在，503 表示模型不可用或忙。日志 `annotation.head_click` 记录素材、帧、框、耗时与失败原因。

服务需安装 `backend/requirements-head-detector.txt` 并配置 `HEAD_DETECTOR_MODEL` 指向可信自训练 YOLO 头部权重（默认 backend/track_modul/head-detector.pt），`HEAD_DETECTOR_DEVICE` 默认 cpu，可显式设 cuda:0。惰性加载、单请求推理，不自动下载模型。权重不纳入 Git；当前 SAM3 Release 不包含此检测权重，不能将已有离线包当作已具备该功能的模型包。

`POST /api/track` 可选 `edgeDiscovery:boolean`（兼容接口默认 false，新前端固定 true），包含在启动幂等正文中。开启需安装 `backend/requirements-head-detector.txt` 并配置与点选头部共用的 `HEAD_DETECTOR_MODEL` / `HEAD_DETECTOR_DEVICE`。任务推理阶段发现边缘新对象后仍以既有 Tracking objects 返回，不改变frame/objectId坐标契约；检测器不可用时任务failed，不静默降级为识别成功。现有SAM3 Release模型包不包含头部检测权重，离线部署须另行准备。

首尾结果读取采用 head-centre-v1 统一端点投影，返回 terminalIdentity.matches 合并已验证对应和必要的未匹配尾ID消歧（内部仍分开保存）。后者只定义端点身份，不证明跨视频时序匹配。JSONL 与导出来源应用同一规则；原结果文件不改写。旧快照不变，新预览的 sourceRevision 包含投影策略。
