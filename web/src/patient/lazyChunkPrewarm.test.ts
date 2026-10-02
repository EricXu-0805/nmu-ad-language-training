import assert from "node:assert/strict";
import { readdirSync, readFileSync, statSync } from "node:fs";
import test from "node:test";

// 换 dist 会删掉上一版的分块。页面在换版前没 import 过的懒加载分块,换版后第一次
// import 就 404:开麦那一步会被记成「麦克风未能安全启动」。
function sourceFiles(directory: URL): URL[] {
  return readdirSync(directory).flatMap((name) => {
    const entry = new URL(name, directory);
    if (statSync(entry).isDirectory()) return sourceFiles(new URL(`${name}/`, directory));
    return /\.(ts|tsx)$/.test(name) && !/\.test\.(ts|tsx)$/.test(name) ? [entry] : [];
  });
}

test("PatientShell 挂载时预取老人端全部懒加载分块", () => {
  const shellUrl = new URL("./PatientShell.tsx", import.meta.url);
  const lazyTargets = new Set<string>();
  for (const file of sourceFiles(new URL("./", import.meta.url))) {
    if (file.pathname === shellUrl.pathname) continue;
    for (const match of readFileSync(file, "utf8").matchAll(/import\("(\.[^"]+)"\)/g)) {
      lazyTargets.add(new URL(match[1], file).pathname);
    }
  }
  // api.ts 是 PatientShell 的静态依赖,页面一加载就在模块表里。
  lazyTargets.delete(new URL("../api.ts", import.meta.url).pathname);
  assert.ok(lazyTargets.size >= 3);

  const shell = readFileSync(shellUrl, "utf8");
  const start = shell.indexOf("useEffect(() => {\n    void import(");
  assert.ok(start >= 0, "PatientShell 缺少挂载时的预取 effect");
  const effect = shell.slice(start, shell.indexOf("}, []);", start));
  const prewarmed = [...effect.matchAll(/void import\("(\.[^"]+)"\)\.catch\(\(\) => \{[^}]*\}\);/g)]
    .map((match) => new URL(match[1], shellUrl).pathname);
  assert.deepEqual(prewarmed.sort(), [...lazyTargets].sort());
});
