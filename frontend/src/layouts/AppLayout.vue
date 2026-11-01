<script setup lang="ts">
import { computed } from 'vue'
import { useRouter } from '../router'
import { useAuth } from '../stores/auth'
import { useAppearance } from '../stores/appearance'
import AppIcon from '../components/AppIcon.vue'
import UserGuide from '../components/UserGuide.vue'
const router = useRouter(), routePath = router.path
const { user, logout } = useAuth()
const { theme, toggleTheme } = useAppearance()
// Workbench pages fill the window edge to edge; their context goes into the header.
const workbenchRoute = computed(() => ['/annotate', '/review'].includes(routePath.value))
async function signOut() { if (await router.canLeave()) { await logout(); await router.replace('/login') } }
</script>
<template>
  <div class="app-shell">
    <header class="app-header">
      <div class="app-brand" title="微流控标注工作台 · 稀有精子识别与提取"><span class="brand-mark"><AppIcon name="layers" :size="18" /></span><h1>标注工作台</h1></div>
      <nav class="workflow-nav" aria-label="数据制作流程">
        <button aria-label="人工标注" :class="{active:routePath==='/annotate'}" :aria-current="routePath==='/annotate'?'step':undefined" @click="router.push('/annotate')"><span>1</span>标注</button>
        <button aria-label="审查模式" :class="{active:routePath==='/review'}" :aria-current="routePath==='/review'?'step':undefined" @click="router.push('/review')"><span>2</span>审查</button>
      </nav>
      <div id="app-header-context" class="app-header-context" />
      <div class="app-account">
        <UserGuide :page="routePath" />
        <button class="icon-button records-nav" :class="{active:routePath==='/results'}" title="标注记录与统计导出" aria-label="标注记录" @click="router.push('/results')"><AppIcon name="list" :size="16" /></button>
        <button class="icon-button" :aria-label="theme === 'light' ? '切换深色主题' : '切换浅色主题'" :title="theme === 'light' ? '深色主题' : '浅色主题'" @click="toggleTheme"><AppIcon :name="theme==='light'?'moon':'sun'" /></button>
        <span class="user-name" :title="user?.name">{{ user?.name }}</span><button class="quiet-button" @click="signOut">退出</button>
      </div>
    </header>
    <main class="app-main" :class="{flush:workbenchRoute}"><slot /></main>
  </div>
</template>
