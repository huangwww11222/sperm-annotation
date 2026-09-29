<script setup lang="ts">
defineProps<{ title: string; description?: string }>()
</script>

<template>
  <header class="workbench-heading" data-testid="workbench-heading">
    <div class="workbench-heading-text"><h2>{{ title }}</h2><p v-if="description" :title="description">{{ description }}</p></div>
    <div class="workbench-heading-actions"><slot /></div>
  </header>
</template>

<style scoped>
.workbench-heading{display:flex;align-items:center;justify-content:space-between;gap:16px;min-height:40px;flex-shrink:0}
.workbench-heading-text{display:flex;align-items:baseline;gap:14px;min-width:0}
.workbench-heading h2{margin:0;font-size:19px;font-weight:650;letter-spacing:-.02em;color:var(--text);white-space:nowrap;flex-shrink:0}
.workbench-heading p{margin:0;color:var(--muted);font-size:11px;line-height:1.5;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.workbench-heading-actions{display:flex;align-items:center;gap:12px;flex-shrink:0}
@media(max-width:1100px){.workbench-heading-text{gap:8px}.workbench-heading p{font-size:10px}}
</style>
