from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def test_frontend_no_longer_calls_frame_difference_plan() -> None:
    api = (ROOT / "frontend/src/api/trackApi.ts").read_text(encoding="utf-8")
    store = (ROOT / "frontend/src/stores/workspace.ts").read_text(encoding="utf-8")
    assert "/track/plan" not in api
    assert "trackApi.plan" not in store
    assert "lastProcessedFrame" in store


def test_anomaly_panel_contract_keeps_markers_and_uses_real_names() -> None:
    page = (ROOT / "frontend/src/pages/AnnotatePage.vue").read_text(encoding="utf-8")
    panel = (ROOT / "frontend/src/components/AnnotationTrackingFeedback.vue").read_text(encoding="utf-8")
    store = (ROOT / "frontend/src/stores/workspace.ts").read_text(encoding="utf-8")
    assert "AnnotationTrackingFeedback" in page
    assert "item.displayName" in panel and "最近人工标注" in panel and "尺寸 / 形状判断参考" in panel
    assert "该错误可能在触发暂停前已经逐渐出现" in store
    close_panel_block = store[store.index("const closeAnomalyPanel"):store.index("const lastPausedContext")]
    assert "anomalyFrames.value = []" not in close_panel_block
    assert "const trackingPausedFrame = computed" in store
    assert "pausedFrame + 1" in panel
    assert "查看前几帧是否已经偏移" in panel
    assert "返回暂停帧再确认" in panel


def test_tracking_requires_explicit_per_object_feedback_before_resume() -> None:
    store = (ROOT / "frontend/src/stores/workspace.ts").read_text(encoding="utf-8")
    panel = (ROOT / "frontend/src/components/AnnotationTrackingFeedback.vue").read_text(encoding="utf-8")
    api = (ROOT / "frontend/src/api/trackApi.ts").read_text(encoding="utf-8")
    run = store[store.index("const performTracking"):store.index("const buildSam3AnnotationsJson")]
    assert "confirmedPausedIds" not in run
    assert "请先逐项确认暂停对象" in run
    assert "const prepareTrackingFeedback" in store
    assert "const retryTrackingAnomalyFeedback" in store
    assert "submitFeedback" in api and "Idempotency-Key" in api
    assert "所有对象确认后才继续追踪" in panel
    assert "取消勾选不调整运动范围" in panel
    assert "tracking-geometry-acceptance" in panel and "无需重画" in panel


def test_track_api_401_expires_session_and_login_route_is_available() -> None:
    track_api = (ROOT / "frontend/src/api/trackApi.ts").read_text(encoding="utf-8")
    http = (ROOT / "frontend/src/api/http.ts").read_text(encoding="utf-8")
    auth = (ROOT / "frontend/src/stores/auth.ts").read_text(encoding="utf-8")
    app = (ROOT / "frontend/src/App.vue").read_text(encoding="utf-8")
    assert "if (res.status === 401) notifyAuthExpired()" in track_api
    assert "window.dispatchEvent(new CustomEvent('auth:expired'))" in http
    assert "window.addEventListener('auth:expired'" in auth
    assert "if (!isAuthenticated.value) return '/login'" in app


def test_removed_annotation_folder_entry_retains_server_media_restore() -> None:
    store = (ROOT / "frontend/src/stores/workspace.ts").read_text(encoding="utf-8")
    api = (ROOT / "frontend/src/api/trackApi.ts").read_text(encoding="utf-8")
    backend = (ROOT / "backend/app/main.py").read_text(encoding="utf-8")
    page = (ROOT / "frontend/src/pages/AnnotatePage.vue").read_text(encoding="utf-8")
    # The user removed the manual folder importer. Existing server workspaces
    # must still reconnect through the normal media list and stable IDs.
    assert "加载标注" not in page
    assert "annotationFolderInputRef" not in page
    assert "openAnnotationFolderPicker" not in store
    assert "loadTrackerFolder" not in store
    assert "loadServerMedia" in store
    assert "const existing = byServerId.get(item.mediaId)" in store
    assert "restoreWorkspaceState(mediaId, isCurrent)" in store
    assert '"directoryName": entry.name' in backend
    assert '"sourceVideoName": base_name' in backend
    assert "directoryName: string" in api
    assert "sourceVideoName: string" in api


def test_media_deletion_requires_confirmation_and_retains_legacy_identity() -> None:
    store = (ROOT / "frontend/src/stores/workspace.ts").read_text(encoding="utf-8")
    page = (ROOT / "frontend/src/pages/AnnotatePage.vue").read_text(encoding="utf-8")
    delete_block = store[store.index("const deleteMedia"):store.index("// Switching assets")]
    assert "closeMedia" not in store
    assert "asset-close" not in page
    assert "deleteMedia(media.id)" in page
    assert delete_block.index("window.confirm") < delete_block.index("trackApi.deleteMedia")
    assert delete_block.index("!result.deleted") < delete_block.index("mediaAssets.value =")
    assert "if (media.serverMediaId)" in delete_block
    assert "电脑原文件和已保存到服务器的标注记录会保留" in delete_block
    # Older clients may have closed a media item under a different frontend ID.
    # Keep that mapping readable so its saved annotations still reconnect.
    assert "closedMediaFrontendIds[item.mediaId] || `server-${item.mediaId}`" in store


def test_workspace_state_is_restored_before_tracking_results_and_uses_canonical_media_id() -> None:
    store = (ROOT / "frontend/src/stores/workspace.ts").read_text(encoding="utf-8")
    api = (ROOT / "frontend/src/api/trackApi.ts").read_text(encoding="utf-8")
    backend = (ROOT / "backend/app/main.py").read_text(encoding="utf-8")
    tracker = (ROOT / "backend/app/tracker.py").read_text(encoding="utf-8")

    reset_block = store[store.index("const resetAnnotationViewForMedia"):store.index("const loadServerMedia")]
    assert reset_block.index("restoreWorkspaceState(mediaId, isCurrent)") < reset_block.index("loadTrackingResult(mediaId, true, true)")
    assert "media.serverMediaId || mediaId" in store
    assert "getWorkspaceState" in api and "saveWorkspaceState" in api
    assert 'WORKSPACE_STATE_FILE_NAME = "workspace_state.json"' in tracker
    assert '@app.put("/api/track/workspace/{media_id}")' in backend
    state_writer = (ROOT / "backend/app/annotation_state.py").read_text(encoding="utf-8")
    assert "annotation_state.write_state" in backend
    assert "temporary.replace(directory / FILE_NAME)" in state_writer


def test_results_page_groups_same_file_frame_and_user_instead_of_batches() -> None:
    page = (ROOT / "frontend/src/pages/ResultsPage.vue").read_text(encoding="utf-8")
    db = (ROOT / "backend/app/db.py").read_text(encoding="utf-8")

    assert "const mediaIdentity" in page
    assert "`${mediaIdentity(row)}:${row.frame_index}:${row.user_id}`" in page
    assert "row.batch_id ||" not in page
    assert "media_identity" in db
    assert "if key in seen" in db
    assert "DELETE FROM annotations" in db


def test_workspace_switch_and_rename_preserve_complete_video_state() -> None:
    store = (ROOT / "frontend/src/stores/workspace.ts").read_text(encoding="utf-8")

    rename_block = store[store.index("const renameObject"):store.index("const copyPreviousFrame")]
    assert "const allObjects = annotationsByMedia.value[mediaId]" in rename_block
    assert "currentObjects.value.map" not in rename_block
    assert "persistWorkspaceState(previousId, true)" in store
    assert "state.editor?.activeTool" in store
