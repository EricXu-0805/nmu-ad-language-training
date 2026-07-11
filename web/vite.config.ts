import { writeFileSync } from 'node:fs'
import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// 构建指纹:注入 bundle(__BUILD_ID__)+ 落 dist/build-id.txt,前端定时比对。
// 反复踩过的坑:重建后旧标签页静默跑旧 JS,点击行为与新版对不上——现在会弹"界面已更新"。
const buildId = String(Date.now())

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
  plugins: [react(), {
    name: 'write-build-id',
    apply: 'build',   // dev server 关闭时也会走 closeBundle,不加会拿 dev 指纹覆写生产 dist
    closeBundle() { writeFileSync('dist/build-id.txt', buildId) },
  }],
  define: { __BUILD_ID__: JSON.stringify(buildId) },
  server: { proxy },
  build: { outDir: 'dist' },
})
