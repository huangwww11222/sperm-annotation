<script setup lang="ts">
import { useRouter } from '../router'
import { useAuth } from '../stores/auth'
import { useAppearance } from '../stores/appearance'
import AppIcon from '../components/AppIcon.vue'
import UserGuide from '../components/UserGuide.vue'
const router = useRouter(), routePath = router.path
const { user, logout } = useAuth()
const { theme, toggleTheme } = useAppearance()
async function signOut() { if (await router.canLeave()) { logout(); await router.replace('/login') } }
</script>
<template>
  <div class="app-shell">
    <header class="app-header">
      <div class="app-brand"><span class="brand-mark"><AppIcon name="layers" :size="22" /></span><div><h1>微流控标注工作台</h1><small>稀有精子识别与提取</small></div></div>
      <nav class="workflow-nav" aria-label="数据制作流程">
        <button aria-label="人工标注" :class="{active:routePath==='/annotate'}" @click="router.push('/annotate')"><span>01</span>人工标注</button><AppIcon name="chevron" :size="13" />
        <button aria-label="审查模式" :class="{active:routePath==='/review'}" @click="router.push('/review')"><span>02</span>审查模式</button><AppIcon name="chevron" :size="13" />
        <button aria-label="对比确认" :class="{active:routePath==='/confirm'}" @click="router.push('/confirm')"><span>03</span>对比确认</button>
      </nav>
      <div class="app-account">
        <UserGuide :page="routePath" />
        <button class="quiet-button records-nav" :class="{active:routePath==='/results'}" @click="router.push('/results')"><AppIcon name="list" :size="16" />标注记录</button>
        <button class="icon-button" :aria-label="theme === 'light' ? '切换深色主题' : '切换浅色主题'" :title="theme === 'light' ? '深色主题' : '浅色主题'" @click="toggleTheme"><AppIcon :name="theme==='light'?'moon':'sun'" /></button>
        <span class="user-name">{{ user?.name }}</span><button class="quiet-button" @click="signOut">退出</button>
      </div>
    </header>
    <main class="app-main"><slot /></main>
  </div>
</template>
