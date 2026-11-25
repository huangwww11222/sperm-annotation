import { onMounted, onUnmounted, ref, type Ref } from 'vue'

/** Inner size of an element, kept current. */
export function useSize(element: Ref<HTMLElement | null>) {
  const width = ref(0), height = ref(0)
  let observer: ResizeObserver | null = null
  const read = () => { const el = element.value; if (el) { width.value = el.clientWidth; height.value = el.clientHeight } }
  onMounted(() => { read(); observer = new ResizeObserver(read); if (element.value) observer.observe(element.value) })
  onUnmounted(() => observer?.disconnect())
  return { width, height }
}
