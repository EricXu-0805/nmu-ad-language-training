import { existsSync, lstatSync, readdirSync, writeFileSync } from 'node:fs'
import { basename, resolve } from 'node:path'
import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import { computeBuildIdentity } from './scripts/build-fingerprint.mjs'
import {
  assertNoSensitiveContentInDist,
  protectedBrowserModuleGraph,
  writeBrowserBuildEvidence,
} from './scripts/build-integrity.mjs'

// 构建指纹:对排序后的前端源码/配置/锁和冻结内容做 SHA-256；
// 不读 Git、环境变量、密钥或绝对路径；它只给相同枚举输入相同的“输入身份”，
// 不声称跨 Node/操作系统输出逐字节相同，也不替代 provenance/SBOM/签名。
// 反复踩过的坑:重建后旧标签页静默跑旧 JS,点击行为与新版对不上——现在会弹"界面已更新"。
const { fingerprint: buildFingerprint, buildId } = computeBuildIdentity()

// 本地/离线优先:无外部 CDN;dev 把后端各路径前缀代理到本地 FastAPI(8000)。
// 生产构建为静态 dist/,由 FastAPI StaticFiles 同源托管(见 app/main.py),届时相对路径直达后端。
const backend = 'http://127.0.0.1:8000'
// 完整题库/脚本只走下列精确的工作人员账号路由；不代理整个 /content，
// 也不把任何答案定义复制进 Vite public/dist。
const proxyOptions = { target: backend, changeOrigin: true }
const prefixProxyPaths = [
  '/health', '/auth', '/patients', '/sessions', '/cloud-processing',
  '/score', '/judge', '/audio', '/items', '/turns', '/asr', '/live', '/tts',
  '/audit', '/ai', '/visit-plans', '/assessment-events', '/assessment-instances',
  '/exports', '/governance',
]
const exactContentProxyPaths = [
  '/content/item-bank',
  '/content/item-bank-bundle',
  '/content/week1-script',
  '/content/autopilot-protocol',
  '/content/scale-protocol',
]
const proxy = Object.fromEntries(
  [
    ...prefixProxyPaths.map((path) => [path, proxyOptions]),
    // '/device' 不能用普通前缀键:它会把前端路由 /device-check 一并吞给后端,
    // dev 下整页空白(生产同源静态托管不经过这份代理,不受影响)。
    ['^/device(?:[/?].*)?$', proxyOptions],
    // Vite treats ordinary proxy keys as prefixes.  Regex contexts keep an
    // invented suffix (or a historical JSON filename) out of the backend.
    ...exactContentProxyPaths.map((path) => [
      `^${path}(?:\\?.*)?$`, proxyOptions,
    ]),
  ],
)

const forbiddenStaticBundleNames = new Set([
  'item_bank_v1.json',
  'week1_script.json',
  'autopilot_protocol_v1.json',
])

function assertNoAnswerBearingStaticBundles(directory = 'dist') {
  const root = resolve(directory)
  if (!existsSync(root)) throw new Error('Vite build did not create dist')
  const visit = (current: string, insideProtectedRoot = false) => {
    for (const name of readdirSync(current)) {
      const path = resolve(current, name)
      const stat = lstatSync(path)
      const childInsideProtectedRoot = insideProtectedRoot
        || (current === root && ['content', 'img'].includes(name.toLowerCase()))
      if (stat.isSymbolicLink()) {
        throw new Error(`symlink is forbidden in browser build output: ${path}`)
      }
      if (stat.isDirectory()) visit(path, childInsideProtectedRoot)
      else if (
        forbiddenStaticBundleNames.has(basename(path).toLowerCase())
        || childInsideProtectedRoot
      ) {
        throw new Error(`answer-bearing content entered browser build output: ${path}`)
      }
    }
  }
  visit(root)
}

export default defineConfig({
  publicDir: false,
  plugins: [protectedBrowserModuleGraph(), react(), {
    name: 'write-build-id',
    apply: 'build',   // dev server 关闭时也会走 closeBundle,不加会拿 dev 指纹覆写生产 dist
    closeBundle() {
      assertNoAnswerBearingStaticBundles()
      writeFileSync('dist/build-id.txt', `${buildId}\n`)
      writeFileSync('dist/build-fingerprint.sha256', `${buildFingerprint}\n`)
      assertNoSensitiveContentInDist()
      writeBrowserBuildEvidence({ buildFingerprint, buildId })
    },
  }],
  define: { __BUILD_ID__: JSON.stringify(buildId) },
  server: { proxy },
  // Never base64-inline imported assets: every emitted byte must remain a
  // separately inspectable file for the final raw-byte DLP pass.
  build: { outDir: 'dist', assetsInlineLimit: 0 },
})
