import { ref } from 'vue'
import { useAuth } from '../stores/auth'

export type RoutePath = '/login' | '/annotate' | '/results' | '/review' | '/confirm'

const normalizePath = (path: string): RoutePath => {
  if (path === '/login' || path === '/results' || path === '/review' || path === '/confirm') return path
  return '/annotate'
}

const path = ref<RoutePath>(typeof window !== 'undefined' ? normalizePath(window.location.pathname) : '/login')

const leaveGuards = new Set<() => Promise<boolean>>()
export function addLeaveGuard(guard: () => Promise<boolean>) { leaveGuards.add(guard); return () => { leaveGuards.delete(guard) } }
const canLeave = async () => { for (const guard of leaveGuards) if (!await guard()) return false; return true }
const navigate = async (to: RoutePath, replace = false) => {
  if (to !== path.value && !await canLeave()) return false
  const { isAuthenticated } = useAuth()
  const target = to === '/login' || isAuthenticated.value ? to : '/login'
  if (typeof window !== 'undefined') {
    const method = replace ? 'replaceState' : 'pushState'
    window.history[method]({}, '', target)
  }
  path.value = target
}

if (typeof window !== 'undefined') {
  window.addEventListener('popstate', async () => {
    const requested = normalizePath(window.location.pathname)
    const previous = path.value
    if (!await canLeave()) { window.history.pushState({}, '', previous); return }
    const { isAuthenticated } = useAuth()
    path.value = requested !== '/login' && !isAuthenticated.value ? '/login' : requested
  })
}

export const useRouter = () => ({
  path,
  canLeave,
  push: (to: RoutePath) => navigate(to),
  replace: (to: RoutePath) => navigate(to, true),
})
