#!/usr/bin/env python3
"""出一份 CycloneDX 1.6 的物料清单，把这套系统真正装进去的东西全列出来。

覆盖四条来源，缺一条这份清单就骗人：
  requirements-deploy.lock.txt   运行期 Python 包（容器与裸机共用同一份锁）
  web/package-lock.json          前端。dev 依赖照收——vite/tsc 在构建期跑，
                                 一个被投毒的构建工具照样能改掉发给老人的 dist，
                                 "运行期用不到"不等于"不在供应链里"。
  Dockerfile 的 FROM … @sha256:  两层底座镜像，按 digest 记，不按浮动 tag
  Dockerfile 的 apk add          底座上手工加的系统包；没钉版本的会被点名

输出是确定性的：不带时间戳，serialNumber 由内容摘要推出来。同样的输入必然产出
逐字节相同的文件，因此可以提交进仓库，再让 CI 断言"重新生成后无 diff"——
这样 SBOM 就从一份文档变成一道门禁，忘记更新会被挡下来。

哈希放 properties 不放 hashes：一个 PyPI 版本对应几十个分发包（sdist + 各平台
wheel），锁把它们全钉住了。CycloneDX 的 hashes 字段语义是"这一个构件的摘要"，
塞几十条进去是含糊其辞。properties 里一条一行，不丢信息也不假装精确。

用法：
  generate_sbom.py -o sbom.cdx.json
  generate_sbom.py --check sbom.cdx.json      # 只比对，不写；不一致退 1
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
import sys
from urllib.parse import quote

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUT = ROOT / "sbom.cdx.json"

_FROM = re.compile(r"^FROM\s+(?P<ref>(?P<name>[^\s:@]+)(?::(?P<tag>[^\s@]+))?"
                   r"@(?P<digest>sha256:[0-9a-f]{64}))(?:\s+AS\s+(?P<stage>\S+))?",
                   re.IGNORECASE)
_APK = re.compile(r"apk\s+add\s+(?P<args>[^\n&|;]+)")


def _purl_name(name: str) -> str:
    """purl 里 @scope/pkg 的斜杠要保留，@ 要转义。"""
    return "/".join(quote(part, safe="") for part in name.split("/"))


class Component:
    def __init__(self, kind: str, name: str, version: str, purl: str,
                 properties: dict[str, list[str]] | None = None,
                 scope: str | None = None) -> None:
        self.kind = kind
        self.name = name
        self.version = version
        self.purl = purl
        self.properties = properties or {}
        self.scope = scope

    def as_json(self) -> dict:
        out: dict = {"type": self.kind, "name": self.name, "version": self.version,
                     "purl": self.purl, "bom-ref": self.purl}
        if self.scope:
            out["scope"] = self.scope
        if self.properties:
            out["properties"] = [
                {"name": key, "value": value}
                for key in sorted(self.properties)
                for value in self.properties[key]]
        return out


def python_components(lock_text: str) -> list[Component]:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import supply_chain_check as sc

    entries, _ = sc.parse_lock(lock_text)
    components = []
    for entry in entries:
        properties = {"nmu:pypi:sha256": sorted(entry.hashes)}
        if entry.marker:
            properties["nmu:marker"] = [entry.marker]
        components.append(Component(
            "library", entry.raw_name, entry.version,
            f"pkg:pypi/{_purl_name(sc.canonical(entry.raw_name))}@{entry.version}",
            properties))
    return components


def npm_components(lock: dict) -> list[Component]:
    components = []
    for path, entry in lock.get("packages", {}).items():
        if not path:
            continue                                        # "" 是工程本身，不是依赖
        name = entry.get("name") or path.split("node_modules/", 1)[-1]
        version = entry.get("version")
        if not version:
            continue                                        # link/workspace 条目没有版本
        properties: dict[str, list[str]] = {}
        if entry.get("integrity"):
            properties["nmu:npm:integrity"] = [entry["integrity"]]
        if entry.get("resolved"):
            properties["nmu:npm:resolved"] = [entry["resolved"]]
        properties["nmu:npm:dev"] = ["true" if entry.get("dev") else "false"]
        components.append(Component(
            "library", name, version,
            f"pkg:npm/{_purl_name(name)}@{version}", properties,
            scope="optional" if entry.get("dev") else "required"))
    return components


def dockerfile_components(text: str) -> tuple[list[Component], list[str]]:
    """底座镜像 + apk 装的系统包。第二个返回值是没钉住版本的东西，给调用方报警。"""
    components: list[Component] = []
    unpinned: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        matched = _FROM.match(stripped)
        if matched:
            name, digest = matched.group("name"), matched.group("digest")
            properties = {"nmu:oci:digest": [digest]}
            if matched.group("tag"):
                properties["nmu:oci:tag"] = [matched.group("tag")]
            if matched.group("stage"):
                properties["nmu:docker:stage"] = [matched.group("stage")]
            components.append(Component(
                "container", name, digest,
                f"pkg:oci/{_purl_name(name.rsplit('/', 1)[-1])}@{digest}",
                properties))
            continue
        found = _APK.search(stripped)
        if not found:
            continue
        for token in found.group("args").split():
            if token.startswith("-") or token.endswith("\\"):
                continue
            if "=" in token:
                name, version = token.split("=", 1)
                components.append(Component(
                    "operating-system", name, version,
                    f"pkg:apk/alpine/{_purl_name(name)}@{version}"))
            else:
                unpinned.append(token)
    return components, unpinned


def _deduplicate(components: list[Component]) -> list[Component]:
    """同一个 purl 只留一条。bom-ref 撞车的 CycloneDX 是非法文档。

    npm 同名包被提升不了时会在嵌套 node_modules 里再出现一次，路径不同、purl 相同。
    合并时属性取并集；scope 取更严的那个——只要有一处是运行期依赖，它就是运行期依赖。
    """
    merged: dict[str, Component] = {}
    for component in components:
        seen = merged.get(component.purl)
        if seen is None:
            merged[component.purl] = component
            continue
        for key, values in component.properties.items():
            seen.properties[key] = sorted(set(seen.properties.get(key, [])) | set(values))
        if component.scope == "required":
            seen.scope = "required"
            seen.properties["nmu:npm:dev"] = ["false"]
    return list(merged.values())


def build_sbom(*, lock_text: str, npm_lock: dict, dockerfile: str,
               application_version: str) -> tuple[dict, list[str]]:
    docker, unpinned = dockerfile_components(dockerfile)
    components = python_components(lock_text) + npm_components(npm_lock) + docker
    components.sort(key=lambda c: (c.kind, c.purl))
    body = [component.as_json() for component in _deduplicate(components)]

    # serialNumber 从内容推：同样的依赖必然同样的编号，diff 里不会出现每次都变的
    # 一行噪声，"重新生成后无 diff"才可能成为一道 CI 门禁。
    digest = hashlib.sha256(
        json.dumps(body, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()
    serial = (f"urn:uuid:{digest[0:8]}-{digest[8:12]}-{digest[12:16]}"
              f"-{digest[16:20]}-{digest[20:32]}")

    return {
        "bomFormat": "CycloneDX",
        "specVersion": "1.6",
        "serialNumber": serial,
        "version": 1,
        "metadata": {
            "component": {
                "type": "application",
                "name": "nmu-ad-language-training",
                "version": application_version,
                "bom-ref": f"nmu-ad-language-training@{application_version}",
            },
            "properties": [
                {"name": "nmu:sbom:sources",
                 "value": "requirements-deploy.lock.txt, web/package-lock.json, Dockerfile"},
                {"name": "nmu:sbom:deterministic",
                 "value": "无时间戳；serialNumber = 组件列表的 sha256 前缀"},
            ],
        },
        "components": body,
    }, unpinned


def render(sbom: dict) -> str:
    return json.dumps(sbom, ensure_ascii=False, indent=2, sort_keys=False) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--lock", type=Path, default=ROOT / "requirements-deploy.lock.txt")
    parser.add_argument("--npm-lock", type=Path, default=ROOT / "web" / "package-lock.json")
    parser.add_argument("--dockerfile", type=Path, default=ROOT / "Dockerfile")
    parser.add_argument("--app-version", default="0.0.0",
                        help="放进 metadata 的应用版本。仓库里那份基线一律用默认值——"
                             "把提交号写进被提交的文件是个死循环，写进去就改了内容、"
                             "也就改了提交号。发布产物要带提交号时由 CI 显式传。")
    parser.add_argument("-o", "--output", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--check", action="store_true",
                        help="不写文件，只断言 --output 与重新生成的结果逐字节一致")
    args = parser.parse_args(argv)

    sbom, unpinned = build_sbom(
        lock_text=args.lock.read_text(encoding="utf-8"),
        npm_lock=json.loads(args.npm_lock.read_text(encoding="utf-8")),
        dockerfile=args.dockerfile.read_text(encoding="utf-8"),
        application_version=args.app_version)
    text = render(sbom)

    for token in unpinned:
        print(f"[WARN] Dockerfile 里 apk 装的 {token} 没钉版本，"
              "底座一升就会静默换掉", file=sys.stderr)

    if args.check:
        if not args.output.exists():
            print(f"[FAIL] {args.output} 不存在，先生成一份提交进来。", file=sys.stderr)
            return 1
        if args.output.read_text(encoding="utf-8") != text:
            print(f"[FAIL] {args.output} 与当前依赖不一致，"
                  "跑一次 scripts/generate_sbom.py 重出并提交。", file=sys.stderr)
            return 1
        counts: dict[str, int] = {}
        for component in sbom["components"]:
            counts[component["type"]] = counts.get(component["type"], 0) + 1
        print("[PASS] SBOM 与当前依赖一致："
              + "，".join(f"{k} {v}" for k, v in sorted(counts.items())))
        return 0

    args.output.write_text(text, encoding="utf-8")
    print(f"{args.output} 已写入 {len(sbom['components'])} 个组件")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
