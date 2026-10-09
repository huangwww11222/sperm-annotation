/**
 * 后端 HTTP 客户端（基于 fetch，不引入 axios 等新依赖）
 *
 * 所有请求走 /api 前缀，由 vite.config.ts 的 proxy 转发到 http://localhost:3000，
 * 所以前端不用写死后端地址，也没有跨域问题。
 */

import { withRequestTimeout } from '../utils/browserCompat'
const BASE = '/api'
export const TOKEN_KEY = 'rare-sperm-token'

/** token 存取 */
export const tokenStore = {
  get: (): string => localStorage.getItem(TOKEN_KEY) ?? '',
  set: (t: string) => localStorage.setItem(TOKEN_KEY, t),
  clear: () => localStorage.removeItem(TOKEN_KEY),
}

/** Clear stale credentials and let the auth/router layer return to login. */
export const notifyAuthExpired = () => {
  tokenStore.clear()
  if (typeof window !== 'undefined') {
    window.dispatchEvent(new CustomEvent('auth:expired'))
  }
}

export interface ApiError {
  message: string
  status: number
}

async function request<T>(path: string, options: RequestInit = {}): Promise<T> {
  const token = tokenStore.get()
  try {
    return await withRequestTimeout(30000, async signal => {
      const res = await fetch(`${BASE}${path}`, {
        ...options,
        signal,
        headers: {
          'Content-Type': 'application/json',
          ...(token ? { Authorization: `Bearer ${token}` } : {}),
          ...(options.headers ?? {}),
        },
      })
      const data = await res.json().catch(() => null)
      if (!res.ok) {
        if (res.status === 401) notifyAuthExpired()
        const detail = (data as any)?.detail
        const detailMessage = Array.isArray(detail)
          ? detail.map((item: any) => item?.msg || item?.message || JSON.stringify(item)).join('; ')
          : typeof detail === 'string' ? detail : undefined
        throw { message: (data as any)?.message ?? detailMessage ?? `请求失败 (${res.status})`, status: res.status } as ApiError
      }
      if (data === null) throw { message: '服务器响应无法读取，请重试', status: 0 } as ApiError
      return data as T
    }, options.signal)
  } catch (error) {
    if (typeof (error as ApiError)?.status === 'number') throw error
    throw { message: '连接后端失败或请求超时，请稍后重试', status: 0 } as ApiError
  }
}

export const http = {
  get: <T>(path: string) => request<T>(path),
  post: <T>(path: string, body?: unknown) =>
    request<T>(path, { method: 'POST', body: body === undefined ? undefined : JSON.stringify(body) }),
  put: <T>(path: string, body?: unknown) =>
    request<T>(path, { method: 'PUT', body: body === undefined ? undefined : JSON.stringify(body) }),
  del: <T>(path: string) => request<T>(path, { method: 'DELETE' }),

  /** 下载二进制文件（zip / 图片等） */
  postBlob: async (path: string, body?: unknown): Promise<Blob> => {
    const token = tokenStore.get()
    let res: Response
    try {
      res = await fetch(`${BASE}${path}`, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          ...(token ? { Authorization: `Bearer ${token}` } : {}),
        },
        body: body === undefined ? undefined : JSON.stringify(body),
      })
    } catch {
      throw { message: '无法连接后端，请确认 FastAPI 已启动', status: 0 } as ApiError
    }
    if (!res.ok) {
      const data = await res.json().catch(() => ({}))
      if (res.status === 401) notifyAuthExpired()
      throw { message: (data as any)?.message ?? `请求失败 (${res.status})`, status: res.status } as ApiError
    }
    return res.blob()
  },
}
