import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// 本地/离线优先:无外部 CDN;dev 把后端各路径前缀代理到本地 FastAPI(8000)。
// 生产构建为静态 dist/,由 FastAPI StaticFiles 同源托管(见 app/main.py),届时相对路径直达后端。
const backend = 'http://127.0.0.1:8000'
// 注意:只代理 /content/item-bank(后端路由),不代理整个 /content——
// 否则会劫持 Vite 从 public/ 提供的静态 /content/*.json(题库/脚本冻结副本)。
const proxy = Object.fromEntries(
  ['/health', '/patients', '/sessions', '/content/item-bank', '/score', '/judge', '/audio', '/items', '/turns', '/asr', '/live']
    .map((p) => [p, { target: backend, changeOrigin: true }]),
)

export default defineConfig({
  plugins: [react()],
  server: { proxy },
  build: { outDir: 'dist' },
})
