// 把冻结题库/脚本从 platform/content 同步到 web/public/content(构建前跑,防前端副本漂移)。
// 前端副本是双要素功能线索的唯一可得源;版本三方断言依赖它与后端一致。
import { cpSync, mkdirSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const src = resolve(here, "../../content");
const dst = resolve(here, "../public/content");
mkdirSync(dst, { recursive: true });
for (const f of ["item_bank_v1.json", "week1_script.json"]) {
  cpSync(resolve(src, f), resolve(dst, f));
  console.log(`synced ${f}`);
}
