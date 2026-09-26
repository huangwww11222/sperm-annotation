import { http } from './http'

// ── 类型契约（和后端 review_routes.py 对齐） ──

export interface BaselineFrame {
  frameIndex: number
  coverage: 'objects'
  objects: Array<{ objectId: number; bbox: number[]; classKey?: string }>
}

export interface FreezeBaselineReq {
  mediaId: string
  frames: BaselineFrame[]
}

export interface FreezeBaselineRes {
  baseline: {
    id: string
    mediaId: string
    frameCount: number
    createdAt: string
  }
}

export interface CreateReviewSessionReq {
  baselineId: string
}

export interface ReviewSession {
  id: string
  baselineId: string
  state: 'pending' | 'in_progress' | 'reviewed' | string | 'closed' | string
  revision: number
  frameCount: number
  submittedFrames: number
  changedCount: number
  createdAt: string
  frames: Array<{
    frameIndex: number
    state: 'pending' | 'draft' | 'submitted'
    patchJson?: string | null
  }>
}

export interface SubmitFrameReq {
  patch: Array<{ objectId: number; bbox: number[] }>
}

export interface SubmitFrameRes {
  submissionId: string
  sessionRevision: number
  netChangeCount: number
  autoFrozen?: { confirmationSessionId?: string; error?: string }
}

export interface ReviewChange {
  changeId: string
  frameIndex: number
  objectId: number
  annotationId: string
  beforeBbox: number[]
  afterBbox: number[]
  beforeCenter: number[]
  afterCenter: number[]
  iou: number
  centerShift: number
  dx: number
  dy: number
  dw: number
  dh: number
  reviewerId: number
  decision?: { choice: 'A' | 'B'; decidedAt?: string } | null
}

export interface ConfirmationSession {
  id: string
  reviewVersionId: string
  baselineId: string
  state: 'pending' | 'in_progress' | 'confirmed' | 'blocked' | 'returned' | string
  revision: number
  totalChanges: number
  decidedCount: number
  keptA: number
  adoptedB: number
  createdAt: string
}

export interface DecisionReq {
  choice: 'A' | 'B'
}

export interface FinalizeRes {
  finalVersionId: string
  snapshotHash: string
  frameCount: number
  status: 'confirmed'
}

export interface FinalVersion {
  id: string
  baselineId: string
  reviewVersionId: string
  confirmationId: string
  snapshotHash: string
  frameCount: number
  confirmedBy: number
  createdAt: string
  frames: Array<{
    frameIndex: number
    objects: Array<{
      objectId: number
      bbox: number[]
      resolution: 'adopted_b' | 'kept_a'
      baselineBBox: number[]
      adoptedBBox?: number[] | null
      changeId?: string | null
    }>
  }>
}

// ── API 调用 ──

export const reviewApi = {
  // Baseline
  freezeBaseline: (body: FreezeBaselineReq) =>
    http.post<FreezeBaselineRes>('/review/baselines/freeze', body),

  listBaselines: () =>
    http.get<{ items: Array<{ id: string; mediaId: string; frameCount: number; createdAt: string }> }>('/review/baselines'),

  // Review Session
  createSession: (body: CreateReviewSessionReq) =>
    http.post<{ session: ReviewSession }>('/review/sessions', body),

  listSessions: () =>
    http.get<{ items: ReviewSession[] }>('/review/sessions'),

  getSession: (id: string) =>
    http.get<ReviewSession>(`/review/sessions/${id}`),

  claimSession: (id: string) =>
    http.post<{ ok: boolean }>(`/review/sessions/${id}/claim`),

  submitFrame: (id: string, frameIndex: number, body: SubmitFrameReq) =>
    http.post<SubmitFrameRes>(`/review/sessions/${id}/frames/${frameIndex}/submit`, body),

  reopenFrame: (id: string, frameIndex: number) =>
    http.post<{ ok: boolean }>(`/review/sessions/${id}/frames/${frameIndex}/reopen`),

  freezeSession: (id: string) =>
    http.post<{ reviewVersionId: string; confirmationSessionId: string; totalChanges: number; snapshotHash: string }>(`/review/sessions/${id}/freeze`, {}),

  // Confirmation
  getConfirmation: (id: string) =>
    http.get<ConfirmationSession>(`/confirmation/sessions/${id}`),

  listConfirmations: () =>
    http.get<{ items: ConfirmationSession[] }>('/confirmation/sessions'),

  listChanges: (confirmationId: string) =>
    http.get<{ items: ReviewChange[] }>(`/confirmation/sessions/${confirmationId}/changes`),

  setDecision: (confirmationId: string, changeId: string, choice: 'A' | 'B') =>
    http.put<{ ok: boolean; revision: number }>(`/confirmation/sessions/${confirmationId}/changes/${changeId}/decision`, { choice }),

  finalize: (confirmationId: string) =>
    http.post<FinalizeRes>(`/confirmation/sessions/${confirmationId}/finalize`, {}),

  // Final Version
  listFinalVersions: () =>
    http.get<{ items: FinalVersion[] }>('/final-versions'),

  getFinalVersion: (id: string) =>
    http.get<FinalVersion>(`/final-versions/${id}`),

}
