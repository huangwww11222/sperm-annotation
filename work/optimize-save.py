from pathlib import Path
p=Path('frontend/src/stores/workspace.ts');s=p.read_text()
s=s.replace("  const statusMessage = ref('就绪')", "  const statusMessage = ref('就绪')\n  const saveState = ref<'idle'|'saving'|'saved'|'error'>('idle')\n  const saveError = ref('')\n  const saveQueues = new Map<string, Promise<unknown>>()\n  const saveTickets = new Map<string, number>()")
s=s.replace('    await trackApi.saveWorkspaceState(media.serverMediaId, state)', '''    const ticket = (saveTickets.get(mediaId) || 0) + 1
    saveTickets.set(mediaId, ticket)
    if (mediaId === currentMediaId.value) { saveState.value = 'saving'; saveError.value = '' }
    const prior = saveQueues.get(mediaId) || Promise.resolve()
    const task = prior.catch(() => {}).then(() => trackApi.saveWorkspaceState(media.serverMediaId!, state))
    saveQueues.set(mediaId, task)
    try {
      await task
      if (mediaId === currentMediaId.value && ticket === saveTickets.get(mediaId)) saveState.value = 'saved'
    } catch (error) {
      console.error('[annotation.workspace_save_failed]', { mediaId, ticket, error })
      if (mediaId === currentMediaId.value && ticket === saveTickets.get(mediaId)) {
        saveState.value = 'error'; saveError.value = error instanceof Error ? error.message : '保存失败，请重试'
      }
      throw error
    } finally { if (saveQueues.get(mediaId) === task) saveQueues.delete(mediaId) }''')
s=s.replace('    const previous = workspaceSaveTimers.get(mediaId)', "    if (mediaId === currentMediaId.value) saveState.value = 'saving'\n    const previous = workspaceSaveTimers.get(mediaId)")
s=s.replace("    workspaceRestoring.value = true\n    revokeExactFrameUrl()", "    workspaceRestoring.value = true\n    saveState.value = 'idle'; saveError.value = ''\n    revokeExactFrameUrl()")
s=s.replace('  const videoFps = ref(30)', '  const videoFps = ref(30)\n  const playbackRate = ref(1)')
s=s.replace('const delay = Math.max(10, Math.round(1000 / fps))', 'const delay = Math.max(10, Math.round(1000 / (fps * playbackRate.value)))')
s=s.replace('        await videoRef.value.play()', '        videoRef.value.playbackRate = playbackRate.value\n        await videoRef.value.play()')
s=s.replace('  const onVideoEnded = async () => {', '''  const pausePlayback = () => {
    videoRef.value?.pause(); stopFallbackPlayback()
    if (videoFrameCallbackId !== null && videoRef.value) videoRef.value.cancelVideoFrameCallback?.(videoFrameCallbackId)
    videoFrameCallbackId = null
  }
  watch(playbackRate, value => { if (videoRef.value) videoRef.value.playbackRate = value })
  const onVideoEnded = async () => {''')
s=s.replace('    persistWorkspaceState, displayObjects,', '    persistWorkspaceState, saveState, saveError, playbackRate, pausePlayback, displayObjects,')
p.write_text(s)
p=Path('frontend/src/api/trackApi.ts');s=p.read_text().replace('async getFrameBlob(mediaId: string, frameIndex: number): Promise<Blob> {', 'async getFrameBlob(mediaId: string, frameIndex: number): Promise<Blob> {').replace('`/api/track/frame/${encodeURIComponent(mediaId)}/${encodeURIComponent(frameIndex)}`, {\n      headers:', '`/api/track/frame/${encodeURIComponent(mediaId)}/${encodeURIComponent(frameIndex)}`, {\n      signal: AbortSignal.timeout(20000),\n      headers:');p.write_text(s)
