import { ref, watch } from 'vue'

const preferenceKey = 'workbench-library-collapsed'
const readCollapsed = () => {
  try { return localStorage.getItem(preferenceKey) === 'true' }
  catch { return false }
}
const libraryCollapsed = ref(readCollapsed())
watch(libraryCollapsed, value => {
  try { localStorage.setItem(preferenceKey, String(value)) }
  catch (error) { console.warn('[ui.preference_save_failed]', { preference: preferenceKey, error }) }
})

// All three workflow stages share this preference; collapsing a list must not
// reset its selection, editing state, or unsaved work.
export const useWorkbench = () => ({ libraryCollapsed })
