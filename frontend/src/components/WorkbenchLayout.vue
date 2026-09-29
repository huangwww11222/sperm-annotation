<script setup lang="ts">
import AppIcon from './AppIcon.vue'
import { useWorkbench } from '../stores/workbench'
import '../workbench/workbench.css'

withDefaults(defineProps<{ libraryLabel?: string; immersive?: boolean }>(), { libraryLabel: '视频列表', immersive: false })
const { libraryCollapsed } = useWorkbench()
</script>

<template>
  <div class="workbench-layout" :class="{'library-collapsed':libraryCollapsed, immersive}" data-testid="workbench-layout">
    <div v-show="!immersive" class="workbench-library" data-workbench-region="library">
      <button class="workbench-library-toggle" data-testid="toggle-workbench-library" :aria-expanded="!libraryCollapsed" :aria-label="libraryCollapsed?'展开视频列表':'收起视频列表'" :title="libraryCollapsed?'展开视频列表':'收起视频列表'" @click="libraryCollapsed=!libraryCollapsed">
        <AppIcon name="folder" :size="15"/><span>{{ libraryCollapsed?'视频':libraryLabel }}</span><AppIcon name="chevron" :size="13" class="workbench-library-arrow" />
      </button>
      <div v-show="!libraryCollapsed" class="workbench-library-content"><slot name="library" /></div>
    </div>
    <div class="workbench-canvas" data-workbench-region="canvas"><slot name="canvas" /></div>
    <div v-show="!immersive" class="workbench-inspector" data-workbench-region="inspector"><slot name="inspector" /></div>
  </div>
</template>
