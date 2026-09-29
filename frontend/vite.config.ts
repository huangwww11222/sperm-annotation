import { defineConfig, loadEnv } from 'vite'
import vue from '@vitejs/plugin-vue'

/**
 * 前端开发服务器。所有 /api 请求统一代理到唯一 FastAPI 后端 :3000。
 */
export default defineConfig(({mode}) => ({
  plugins: [vue()],
  // Hospital clients include Windows 7 with Chrome 93. Runtime APIs also need
  // explicit compatibility helpers; transpilation alone cannot polyfill them.
  build: { target: 'chrome93', cssTarget: 'chrome93' },
  server: {
    port: 5173,
    // ↓ 新增：所有 /api 请求转发到后端，前端不用写死地址，也没有跨域问题
    //   改完必须重启 dev server 才生效
    proxy: {
      '/api': {
        target: loadEnv(mode, '.', '').API_PROXY_TARGET || 'http://localhost:3000',
        changeOrigin: true,
      },
    },
  },
}))
