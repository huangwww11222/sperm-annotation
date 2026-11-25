// One clock for every playing film strip, so a page of them moves in step and costs one timer.
import { onMounted, onUnmounted, ref } from 'vue'

export const tick = ref(0)
let users = 0, timer: ReturnType<typeof setInterval> | null = null
export function useTicker() {
  onMounted(() => { if (!users++) timer = setInterval(() => { if (!document.hidden) tick.value++ }, 230) })
  onUnmounted(() => { if (!--users && timer) { clearInterval(timer); timer = null } })
}
