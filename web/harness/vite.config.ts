import { fileURLToPath } from 'node:url'
import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// 只给 harness/webkit_handoff.py 构建本地合成走查页：入口是本目录的 index.html，
// 输出目录由调用方 --outDir 指到仓库外的私有临时目录。不进 dist、不进 tsc -b、不改正式构建配置。
export default defineConfig({
  root: fileURLToPath(new URL('.', import.meta.url)),
  publicDir: false,
  plugins: [react()],
})
