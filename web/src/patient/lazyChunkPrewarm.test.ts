import assert from "node:assert/strict";
import { readFileSync, statSync } from "node:fs";
import test from "node:test";
import ts from "typescript";

// 换 dist 会删掉上一版的分块。页面在换版前没 import 过的懒加载分块,换版后第一次
// import 就 404:开麦那一步会被记成「麦克风未能安全启动」。动态 import 可以写在老人端
// 静态引入的任何模块里(audio/、sync/、api.ts 都算),所以从 PatientShell 沿依赖走全图,
// 不只看 patient/ 目录。
function resolveRelative(specifier: string, from: URL): URL | null {
  if (!specifier.startsWith(".")) return null;
  for (const suffix of ["", ".ts", ".tsx", "/index.ts", "/index.tsx"]) {
    const candidate = new URL(specifier + suffix, from);
    try {
      if (statSync(candidate).isFile()) return candidate;
    } catch {
      // 换下一个后缀
    }
  }
  throw new Error(`${from.pathname} 引入的 ${specifier} 解析不到`);
}

function moduleEdges(file: URL): { staticDeps: URL[]; dynamicDeps: URL[] } {
  const staticDeps: URL[] = [];
  const dynamicDeps: URL[] = [];
  if (!/\.(ts|tsx)$/.test(file.pathname)) return { staticDeps, dynamicDeps };
  const source = ts.createSourceFile(
    file.pathname, readFileSync(file, "utf8"), ts.ScriptTarget.Latest, false,
    file.pathname.endsWith(".tsx") ? ts.ScriptKind.TSX : ts.ScriptKind.TS);
  const visit = (node: ts.Node): void => {
    // import type / export type 编译后不留;类型位置的 import("./x").T 是 ImportTypeNode,不是调用。
    if (ts.isImportDeclaration(node) && !node.importClause?.isTypeOnly
        && ts.isStringLiteral(node.moduleSpecifier)) {
      const target = resolveRelative(node.moduleSpecifier.text, file);
      if (target) staticDeps.push(target);
    } else if (ts.isExportDeclaration(node) && !node.isTypeOnly && node.moduleSpecifier
        && ts.isStringLiteral(node.moduleSpecifier)) {
      const target = resolveRelative(node.moduleSpecifier.text, file);
      if (target) staticDeps.push(target);
    } else if (ts.isCallExpression(node) && node.expression.kind === ts.SyntaxKind.ImportKeyword) {
      const [specifier] = node.arguments;
      assert.ok(specifier && ts.isStringLiteralLike(specifier),
        `${file.pathname} 有不是字面量的动态 import,这里没法确认它的分块被预取`);
      const target = resolveRelative(specifier.text, file);
      if (target) dynamicDeps.push(target);
    }
    ts.forEachChild(node, visit);
  };
  visit(source);
  return { staticDeps, dynamicDeps };
}

test("PatientShell 挂载时预取老人端能走到的全部懒加载分块", () => {
  const shellUrl = new URL("./PatientShell.tsx", import.meta.url);
  const lazyTargets = new Set<string>();
  const seen = new Set<string>();
  const queue = [shellUrl];
  while (queue.length > 0) {
    const file = queue.shift()!;
    if (seen.has(file.href)) continue;
    seen.add(file.href);
    const { staticDeps, dynamicDeps } = moduleEdges(file);
    queue.push(...staticDeps);
    // 懒加载进来的模块里再写的 import() 也是换版后才会去取的分块。
    for (const target of dynamicDeps) {
      lazyTargets.add(target.pathname);
      queue.push(target);
    }
  }
  // api.ts 是 PatientShell 的静态依赖,和页面一起加载进共享分块,页面一加载就在模块表里。
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
