
<script setup lang="ts">
import { computed, ref } from 'vue'
import { useRouter } from '../router'
import { useAuth } from '../stores/auth'
import { useAppearance } from '../stores/appearance'
import AppIcon from '../components/AppIcon.vue'
const { theme, toggleTheme } = useAppearance()

const router = useRouter()
const { login, register } = useAuth()

const isRegisterMode = ref(false)
const username = ref('')
const password = ref('')
const confirmPassword = ref('')
const loading = ref(false)
const error = ref('')

const canSubmit = computed(() => {
  if (!username.value.trim() || !password.value) return false
  if (isRegisterMode.value && password.value !== confirmPassword.value) return false
  return true
})

const toggleMode = () => {
  isRegisterMode.value = !isRegisterMode.value
  error.value = ''
  confirmPassword.value = ''
}

const submit = async () => {
  error.value = ''

  if (!username.value.trim() || !password.value) {
    error.value = '请输入账号和密码'
    return
  }
  if (isRegisterMode.value) {
    if (password.value.length < 6) {
      error.value = '密码至少 6 位'
      return
    }
    if (password.value !== confirmPassword.value) {
      error.value = '两次输入的密码不一致'
      return
    }
  }

  loading.value = true
  try {
    if (isRegisterMode.value) {
      await register(username.value, password.value)
    } else {
      await login(username.value, password.value)
    }
    router.replace('/annotate')
  } catch (err) {
    error.value = (err as any)?.message || '操作失败'
  } finally {
    loading.value = false
  }
}
</script>

<template>
  <div class="login-page">
    <button class="quiet-button login-theme" @click="toggleTheme"><AppIcon :name="theme==='light'?'moon':'sun'"/>{{ theme==='light'?'深色模式':'浅色模式' }}</button>
    <main class="login-shell">
      <header class="login-intro">
        <span class="login-mark"><AppIcon name="layers" :size="29" /></span>
        <div><h1>微流控标注工作台</h1><p class="login-subject">稀有精子识别与提取</p></div>
        <p class="login-purpose">逐帧标注与 AI 辅助追踪，<br>审查确认后导出训练数据。</p>
        <ol class="login-flow" aria-label="数据制作流程"><li>人工标注</li><li>审查模式</li><li>对比确认</li></ol>
      </header>
      <section class="panel login-card" aria-labelledby="login-title">
        <div class="login-card-heading">
          <h2 id="login-title">{{ isRegisterMode ? '注册账号' : '登录系统' }}</h2>
          <p>
            {{ isRegisterMode ? '注册后自动登录，进入稀有精子识别与标注工作台。' : '登录后进入稀有精子识别与标注工作台。' }}
          </p>
        </div>
        <form class="login-form" @submit.prevent="submit">
          <label>
            <span>账号</span>
            <input v-model="username" class="input w-full" autocomplete="username" placeholder="请输入账号" />
          </label>
          <label>
            <span>密码</span>
            <input v-model="password" class="input w-full" type="password" :autocomplete="isRegisterMode ? 'new-password' : 'current-password'" placeholder="请输入密码" />
          </label>
          <label v-if="isRegisterMode">
            <span>确认密码</span>
            <input v-model="confirmPassword" class="input w-full" type="password" autocomplete="new-password" placeholder="请再次输入密码" />
          </label>
          <p v-if="error" class="error-banner login-error" role="alert">{{ error }}</p>
          <button class="btn-primary w-full" :disabled="loading || !canSubmit">
            {{ loading ? '处理中…' : isRegisterMode ? '注册并登录' : '登录' }}
          </button>
        </form>

        <div class="login-mode">
          <span>{{ isRegisterMode ? '已有账号？' : '还没有账号？' }}</span>
          <button type="button" @click="toggleMode">
            {{ isRegisterMode ? '去登录' : '立即注册' }}
          </button>
        </div>

        <p class="login-support">标注记录会关联当前账号。忘记密码请联系管理员。</p>
      </section>
    </main>
  </div>
</template>

<style scoped>
.login-page{min-height:100vh;padding:80px 28px 40px;background:var(--app-bg);color:var(--text)}
.login-shell{min-height:calc(100vh - 120px);width:100%;max-width:920px;margin:auto;display:grid;grid-template-columns:minmax(0,1fr) minmax(320px,400px);align-items:center;gap:72px}
.login-intro{display:grid;grid-template-columns:50px minmax(0,1fr);align-items:center;gap:14px;min-width:0}
.login-mark{display:grid;place-items:center;width:50px;height:50px;border:1px solid var(--accent);border-radius:12px;background:var(--accent-soft);color:var(--accent)}
.login-intro h1{font-size:24px;font-weight:650;line-height:1.4;letter-spacing:-.025em;margin:0}
.login-subject{margin:5px 0 0;font-size:13px;line-height:1.6;color:var(--muted)}
.login-purpose{grid-column:1/-1;font-size:20px;line-height:1.8;margin:28px 0 12px;letter-spacing:.01em}
.login-flow{grid-column:1/-1;display:flex;gap:0;margin:0;padding:20px 0 0;list-style:none;border-top:1px solid var(--line);counter-reset:login-step}
.login-flow li{flex:1;display:flex;flex-direction:column;gap:8px;font-size:13px;color:var(--muted);counter-increment:login-step}
.login-flow li::before{content:counter(login-step,decimal-leading-zero);font-size:12px;color:var(--accent);font-variant-numeric:tabular-nums}
.login-card{width:100%;padding:30px;border-radius:14px;box-shadow:var(--shadow);border-top:3px solid var(--accent)}
.login-card-heading{margin-bottom:26px}.login-card-heading h2{font-size:22px;line-height:1.4;font-weight:650;margin:0}
.login-card-heading p{margin:10px 0 0;font-size:13px;line-height:1.7;color:var(--muted)}
.login-form{display:flex;flex-direction:column;gap:18px}.login-form label{display:flex;flex-direction:column;gap:8px;min-width:0;font-size:13px;color:var(--text)}
.login-form .input{min-height:42px;font-size:14px}.login-form>.btn-primary{min-height:42px;font-size:14px;margin-top:4px}
.login-error{margin:0;overflow-wrap:anywhere;font-size:13px;line-height:1.6}
.login-mode{display:flex;align-items:center;justify-content:center;flex-wrap:wrap;gap:5px;margin-top:24px;font-size:13px;color:var(--muted)}
.login-mode button{color:var(--accent);padding:4px}.login-mode button:hover{text-decoration:underline}
.login-support{border-top:1px solid var(--line);padding-top:18px;margin:18px 0 0;color:var(--muted);font-size:12px;line-height:1.7}
.login-theme{position:absolute;right:24px;top:20px}
@media(max-width:860px){.login-shell{gap:36px;max-width:760px}.login-intro h1{font-size:21px}.login-purpose{font-size:18px}.login-card{padding:26px}}
@media(max-width:680px){.login-page{padding:76px 20px 28px}.login-shell{display:flex;flex-direction:column;gap:26px;min-height:0;max-width:400px;align-items:stretch}.login-intro{grid-template-columns:42px minmax(0,1fr);gap:12px}.login-mark{width:42px;height:42px;border-radius:10px}.login-intro h1{font-size:20px}.login-subject{font-size:12px;margin-top:2px}.login-purpose,.login-flow{display:none}.login-card{padding:24px}.login-theme{right:14px;top:16px}}
</style>
