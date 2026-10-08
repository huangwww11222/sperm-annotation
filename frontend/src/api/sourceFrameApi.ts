import { FrameCache } from '../annotation/frameCache'
import { withRequestTimeout } from '../utils/browserCompat'
import { tokenStore } from './http'

const cache = new FrameCache(8, 32 * 1024 * 1024)
let actor = ''
export const invalidateSourceFrame = (mediaId: string, frameIndex: number) => cache.invalidate(`${mediaId}:${frameIndex}`)
/** Shared by review and confirmation; only native JPEG blobs, no business state. */
export function sourceFrameBlob(mediaId: string, frameIndex: number) {
  const token = tokenStore.get() || ''
  if (actor !== token) { actor = token; cache.clear() }
  return cache.get(`${mediaId}:${frameIndex}`, () => withRequestTimeout(20000, async signal => {
    const response = await fetch(`/api/track/frame/${encodeURIComponent(mediaId)}/${frameIndex}`, {
      signal, headers: { Authorization: `Bearer ${token}` },
    })
    if (!response.ok) throw { status: response.status, message: '当前帧图像加载失败，请重试' }
    const blob = await response.blob()
    if (!blob.size) throw new Error('当前帧图像为空，请重试')
    return blob
  }))
}
