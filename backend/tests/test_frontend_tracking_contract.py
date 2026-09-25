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
    store = (ROOT / "frontend/src/stores/workspace.ts").read_text(encoding="utf-8")
    assert "closeAnomalyPanel" in page
    assert "item.displayName" in page
    assert "最近人工基准" in page
    assert "该错误可能在触发暂停前已经逐渐出现" in store
    assert "anomalyPanelVisible.value = false" in store
    close_panel_block = store[store.index("const closeAnomalyPanel"):store.index("const lastPausedContext")]
    assert "anomalyFrames.value = []" not in close_panel_block
    assert "anomalyPanelVisible.value = true" in store


def test_direct_retry_promotes_only_paused_objects_to_manual_baselines() -> None:
    store = (ROOT / "frontend/src/stores/workspace.ts").read_text(encoding="utf-8")
    page = (ROOT / "frontend/src/pages/AnnotatePage.vue").read_text(encoding="utf-8")
    assert "lastPausedContext.value?.mediaId === mediaId" in store
    assert "pausedAnomalies.value.map((item) => item.objectId)" in store
    assert "confirmedPausedIds.has(obj.objectId)" in store
    assert "source: 'manual' as const" in store
    assert "本帧异常框会被确认为新的人工基准" in page


def test_track_api_401_expires_session_and_login_route_is_available() -> None:
    track_api = (ROOT / "frontend/src/api/trackApi.ts").read_text(encoding="utf-8")
    http = (ROOT / "frontend/src/api/http.ts").read_text(encoding="utf-8")
    auth = (ROOT / "frontend/src/stores/auth.ts").read_text(encoding="utf-8")
    app = (ROOT / "frontend/src/App.vue").read_text(encoding="utf-8")
    assert "if (res.status === 401) notifyAuthExpired()" in track_api
    assert "window.dispatchEvent(new CustomEvent('auth:expired'))" in http
    assert "window.addEventListener('auth:expired'" in auth
    assert "if (!isAuthenticated.value) return '/login'" in app


def test_loading_backend_annotation_folder_reuses_media_id_and_switches_existing_video() -> None:
    store = (ROOT / "frontend/src/stores/workspace.ts").read_text(encoding="utf-8")
    api = (ROOT / "frontend/src/api/trackApi.ts").read_text(encoding="utf-8")
    backend = (ROOT / "backend/app/main.py").read_text(encoding="utf-8")

    assert "openExistingBackendMedia" in store
    assert "item.serverMediaId === serverItem.mediaId" in store
    assert "if (sourceFolderName && await openExistingBackendMedia(sourceFolderName, videoFile)) return" in store
    load_folder = store[store.index("const loadTrackerFolder"):store.index("const openAnnotationFolderPicker")]
    assert load_folder.index("openExistingBackendMedia(sourceFolderName, videoFile)") < load_folder.index("trackApi.uploadVideo(videoFile)")
    assert "该视频已经打开，已切换到" in store
    assert '"directoryName": entry.name' in backend
    assert '"sourceVideoName": base_name' in backend
    assert "directoryName: string" in api
    assert "sourceVideoName: string" in api


def test_media_list_close_never_deletes_backend_storage() -> None:
    store = (ROOT / "frontend/src/stores/workspace.ts").read_text(encoding="utf-8")
    page = (ROOT / "frontend/src/pages/AnnotatePage.vue").read_text(encoding="utf-8")
    track_api = (ROOT / "frontend/src/api/trackApi.ts").read_text(encoding="utf-8")
    close_block = store[store.index("const closeMedia"):store.index("const toastMessage")]

    assert "trackApi.deleteMedia" not in close_block
    assert "async deleteMedia" not in track_api
    assert "delete annotationsByMedia" not in close_block
    assert "closedMediaFrontendIds[media.serverMediaId] = media.id" in close_block
    assert "closedMediaFrontendIds[serverItem.mediaId] || `server-${serverItem.mediaId}`" in store
    assert "后端文件和标注记录均已保留" in close_block
    assert "closeMedia(media.id)" in page
    assert "不会删除后端文件和标注" in page


def test_workspace_state_is_restored_before_tracking_results_and_uses_canonical_media_id() -> None:
    store = (ROOT / "frontend/src/stores/workspace.ts").read_text(encoding="utf-8")
    api = (ROOT / "frontend/src/api/trackApi.ts").read_text(encoding="utf-8")
    backend = (ROOT / "backend/app/main.py").read_text(encoding="utf-8")
    tracker = (ROOT / "backend/app/tracker.py").read_text(encoding="utf-8")

    reset_block = store[store.index("const resetAnnotationViewForMedia"):store.index("const loadServerMedia")]
    assert reset_block.index("restoreWorkspaceState(mediaId)") < reset_block.index("loadTrackingResult(mediaId, true)")
    assert "media.serverMediaId || mediaId" in store
    assert "getWorkspaceState" in api and "saveWorkspaceState" in api
    assert 'WORKSPACE_STATE_FILE_NAME = "workspace_state.json"' in tracker
    assert '@app.put("/api/track/workspace/{media_id}")' in backend
    assert "temporary.replace(path)" in backend


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
