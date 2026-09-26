<script setup lang="ts">
defineProps<{ label: string; percent: number; summary: string; detail: string; state: string; completed: boolean; variant?: 'card' }>()
</script>

<template>
  <section class="workflow-progress" :aria-label="label" :class="{completed, 'progress-card':variant==='card'}" data-testid="video-progress">
    <div class="workflow-progress-title"><h2>{{ label }}</h2><strong>{{ Number(percent.toFixed(1)) }}<small>%</small></strong></div>
    <div class="workflow-progress-meter">
      <div class="workflow-progress-counts" role="status"><b>{{ summary }}</b><span>{{ detail }}</span></div>
      <progress :value="percent" max="100" :aria-label="label" />
    </div>
    <span class="workflow-progress-state" role="status">{{ state }}</span>
    <div class="workflow-progress-actions"><slot /></div>
  </section>
</template>

<style scoped>
.workflow-progress{position:sticky;top:72px;z-index:30;display:flex;align-items:center;gap:20px;flex-shrink:0;padding:10px 16px;border:1px solid var(--line);border-radius:10px;background:var(--surface);box-shadow:var(--shadow);min-width:0}
.workflow-progress-title{display:flex;gap:14px;align-items:center;flex-shrink:0}
.workflow-progress-title h2{font-size:13px;font-weight:600;margin:0;color:var(--text)}
.workflow-progress-title strong{font-size:26px;line-height:1.2;letter-spacing:-.04em;color:var(--accent);font-variant-numeric:tabular-nums}
.workflow-progress-title small{font-size:12px;color:inherit;margin-left:2px}
.workflow-progress-meter{flex:1;min-width:0}
.workflow-progress-counts{display:flex;justify-content:space-between;gap:10px;flex-wrap:wrap;font-size:11px;margin-bottom:7px;color:var(--muted)}
.workflow-progress-counts b{font-weight:600;color:var(--text)}
.workflow-progress progress{appearance:none;width:100%;height:6px;border:0;display:block;margin:0;border-radius:5px;background:var(--line)}
.workflow-progress progress::-webkit-progress-bar{background:var(--line);border-radius:5px}
.workflow-progress progress::-webkit-progress-value{background:var(--accent);border-radius:5px}
.workflow-progress progress::-moz-progress-bar{background:var(--accent);border-radius:5px}
.workflow-progress-state{font-size:11px;color:var(--muted);max-width:175px}
.workflow-progress.completed .workflow-progress-state{color:var(--success)}
.workflow-progress-actions{display:flex;gap:8px;flex-shrink:0}
@media(max-width:1200px){.workflow-progress{gap:12px;padding:9px 12px}.workflow-progress-title{display:block}.workflow-progress-title h2{font-size:11px}.workflow-progress-title strong{font-size:22px}.workflow-progress-counts{gap:3px 10px}.workflow-progress-state{max-width:115px}}
/* B keeps video progress beside the canvas, above the frame submission controls. */
.workflow-progress.progress-card{position:static;display:flex;flex-direction:column;align-items:stretch;gap:7px;padding:12px 14px;border:1px solid var(--accent);border-top:4px solid var(--accent);background:var(--accent-soft);box-shadow:var(--shadow)}
.progress-card .workflow-progress-title{display:flex;justify-content:space-between;gap:8px;align-items:center}
.progress-card .workflow-progress-title h2{font-size:14px;font-weight:700}
.progress-card .workflow-progress-title strong{font-size:36px;font-weight:750;line-height:1.1}
.progress-card .workflow-progress-title small{font-size:15px}
.progress-card .workflow-progress-counts{display:flex;flex-direction:column;gap:4px;font-size:11px}
.progress-card .workflow-progress-counts b{font-size:12px}
.progress-card progress{height:8px;background:var(--surface)}
.progress-card progress::-webkit-progress-bar{background:var(--surface)}
.progress-card .workflow-progress-state{max-width:none;font-size:11px}
.progress-card .workflow-progress-actions{display:block}
.progress-card .workflow-progress-actions :slotted(button){width:100%;font-size:12px}
</style>
