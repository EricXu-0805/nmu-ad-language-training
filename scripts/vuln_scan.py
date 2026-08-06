#!/usr/bin/env python3
"""拿 SBOM 去 OSV 查一遍已知漏洞。

出网说明（写在这儿是为了不用去猜）：只发包名和版本号，全部来自公开的 PyPI /
npm / Alpine 索引，本身就是公开信息。不发仓库内容、不发任何患者字段、不带
凭据。目标是 https://api.osv.dev（Google 运营的公开漏洞库，无需鉴权）。
拿 --offline 跑就一个字节都不出网。

判据：只要有一条没被豁免的漏洞命中钉住的版本就退 1。不按分数分档——
GHSA 的 severity 字段时有时无，按一个半数缺失的字段设阈值等于假装有标准。
要放行就写进豁免文件，说清理由和到期日；过期的豁免按未豁免算，逼人重看一次。

扫不到的东西（说清楚比假装覆盖强）：
  - 两层底座镜像里的系统包。OSV 认不了 OCI digest，那要在构建出的镜像上跑
    trivy / grype 之类。CI 里那一步是单独的，别把这里的 PASS 当成镜像干净。
  - 还没进 OSV 的漏洞。任何漏洞库都只是已知的那部分。

用法：
  vuln_scan.py                                  # 查线上依赖，走网
  vuln_scan.py --save security/osv-response.json
  vuln_scan.py --offline security/osv-response.json     # 复现，不出网
"""
from __future__ import annotations

import argparse
from datetime import date
import json
from pathlib import Path
import sys
import urllib.error
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SBOM = ROOT / "sbom.cdx.json"
DEFAULT_WAIVERS = ROOT / "security" / "vuln-waivers.json"
OSV_BATCH = "https://api.osv.dev/v1/querybatch"
OSV_VULN = "https://api.osv.dev/v1/vulns/"

# purl 前缀 → OSV 生态名。认不出的前缀会被点名，不会被默默丢掉。
ECOSYSTEMS = {"pkg:pypi/": "PyPI", "pkg:npm/": "npm"}
UNSCANNABLE = {"pkg:oci/": "底座镜像（要在构建产物上跑容器扫描器）",
               "pkg:apk/": "Alpine 系统包（版本号随底座走，同上）"}


class Coordinate:
    def __init__(self, ecosystem: str, name: str, version: str, purl: str) -> None:
        self.ecosystem = ecosystem
        self.name = name
        self.version = version
        self.purl = purl


def coordinates(sbom: dict) -> tuple[list[Coordinate], dict[str, int]]:
    """把 SBOM 拆成可查的坐标 + 一份"查不了的"计数。"""
    found: list[Coordinate] = []
    skipped: dict[str, int] = {}
    for component in sbom.get("components", []):
        purl = component.get("purl", "")
        for prefix, ecosystem in ECOSYSTEMS.items():
            if purl.startswith(prefix):
                found.append(Coordinate(ecosystem, component["name"],
                                        component["version"], purl))
                break
        else:
            label = next((why for prefix, why in UNSCANNABLE.items()
                          if purl.startswith(prefix)), f"未知的 purl 前缀：{purl}")
            skipped[label] = skipped.get(label, 0) + 1
    return found, skipped


def _post(url: str, payload: dict, timeout: int) -> dict:
    request = urllib.request.Request(
        url, data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def _get(url: str, timeout: int) -> dict:
    with urllib.request.urlopen(url, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def query_osv(targets: list[Coordinate], timeout: int = 60,
              batch_size: int = 500) -> dict:
    """查 OSV，返回一份可以原样存盘、之后离线复现的结构。"""
    hits: dict[str, list[str]] = {}
    for start in range(0, len(targets), batch_size):
        chunk = targets[start:start + batch_size]
        answer = _post(OSV_BATCH, {"queries": [
            {"package": {"name": c.name, "ecosystem": c.ecosystem},
             "version": c.version} for c in chunk]}, timeout)
        for coordinate, result in zip(chunk, answer.get("results", [])):
            ids = [v["id"] for v in result.get("vulns", [])]
            if ids:
                hits[coordinate.purl] = ids

    details: dict[str, dict] = {}
    for identifier in sorted({i for ids in hits.values() for i in ids}):
        details[identifier] = _get(OSV_VULN + identifier, timeout)
    return {"hits": hits, "details": details}


def load_waivers(path: Path, today: date) -> tuple[dict[str, dict], list[str]]:
    """返回 (仍然有效的豁免, 已过期的 id)。过期的按没豁免算。"""
    if not path.exists():
        return {}, []
    raw = json.loads(path.read_text(encoding="utf-8"))
    live: dict[str, dict] = {}
    expired: list[str] = []
    for item in raw.get("waivers", []):
        for field in ("id", "reason", "expires"):
            if not item.get(field):
                raise ValueError(f"豁免条目缺 {field}：{item}")
        if date.fromisoformat(item["expires"]) < today:
            expired.append(item["id"])
        else:
            live[item["id"]] = item
    return live, expired


def _severity(record: dict) -> str:
    specific = record.get("database_specific", {}).get("severity")
    return str(specific).upper() if specific else "UNKNOWN"


class Finding:
    def __init__(self, purl: str, identifier: str, severity: str,
                 summary: str, waived: dict | None) -> None:
        self.purl = purl
        self.identifier = identifier
        self.severity = severity
        self.summary = summary
        self.waived = waived

    def line(self) -> str:
        mark = "WAIVED" if self.waived else "FAIL"
        tail = f"（豁免至 {self.waived['expires']}：{self.waived['reason']}）" if self.waived else ""
        return f"  [{mark}] {self.purl} — {self.identifier} {self.severity} {self.summary}{tail}"


def evaluate(response: dict, waivers: dict[str, dict]) -> list[Finding]:
    findings: list[Finding] = []
    for purl in sorted(response.get("hits", {})):
        for identifier in sorted(response["hits"][purl]):
            record = response.get("details", {}).get(identifier, {})
            if record.get("withdrawn"):
                continue                                    # 已撤回的条目不算数
            findings.append(Finding(
                purl, identifier, _severity(record),
                (record.get("summary") or record.get("details", "")).split("\n")[0][:120],
                waivers.get(identifier)))
    return findings


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--sbom", type=Path, default=DEFAULT_SBOM)
    parser.add_argument("--waivers", type=Path, default=DEFAULT_WAIVERS)
    parser.add_argument("--offline", type=Path, default=None,
                        help="用存好的 OSV 应答，一个字节都不出网")
    parser.add_argument("--save", type=Path, default=None, help="把 OSV 原始应答存盘")
    parser.add_argument("--today", default=None, help="覆盖今天（判豁免是否过期）")
    parser.add_argument("--timeout", type=int, default=60)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    sbom = json.loads(args.sbom.read_text(encoding="utf-8"))
    targets, skipped = coordinates(sbom)

    if args.offline:
        response = json.loads(args.offline.read_text(encoding="utf-8"))
        source = f"离线：{args.offline}"
    else:
        if not args.json:
            print(f"向 {OSV_BATCH} 发送 {len(targets)} 个包名+版本号（不含其它任何内容）…")
        try:
            response = query_osv(targets, timeout=args.timeout)
        except (urllib.error.URLError, TimeoutError) as error:
            print(f"[FAIL] 查不通 OSV：{error}。查不动就是没查过，不当作通过。",
                  file=sys.stderr)
            return 2
        source = OSV_BATCH

    today = date.fromisoformat(args.today) if args.today else date.today()
    waivers, expired = load_waivers(args.waivers, today)
    findings = evaluate(response, waivers)
    blocking = [f for f in findings if not f.waived]

    if args.save:
        args.save.parent.mkdir(parents=True, exist_ok=True)
        args.save.write_text(json.dumps(response, ensure_ascii=False, indent=2,
                                        sort_keys=True) + "\n", encoding="utf-8")

    if args.json:
        print(json.dumps({
            "ok": not blocking,
            "source": source,
            "scanned": len(targets),
            "not_scanned": skipped,
            "expired_waivers": expired,
            "findings": [{"purl": f.purl, "id": f.identifier, "severity": f.severity,
                          "summary": f.summary, "waived": bool(f.waived)}
                         for f in findings],
        }, ensure_ascii=False, indent=2))
        return 1 if blocking else 0

    print(f"来源 {source}：查了 {len(targets)} 个包")
    for label, count in sorted(skipped.items()):
        print(f"  [未扫] {count} 个 — {label}")
    for identifier in expired:
        print(f"  [过期] 豁免 {identifier} 已过期，按未豁免算")
    for finding in findings:
        print(finding.line())
    if blocking:
        print(f"\n[FAIL] {len(blocking)} 条未豁免的已知漏洞。")
        return 1
    print(f"\n[PASS] {len(targets)} 个包在 OSV 里没有未豁免的已知漏洞"
          f"（另有 {sum(skipped.values())} 个不在本工具能力内，见上）。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
