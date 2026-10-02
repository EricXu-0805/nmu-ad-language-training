#!/usr/bin/env python3
"""把 check_nmu_probe 装进 OpenClaw 的 heartbeat.py,装完单独调它一次。

异地探针只在 NMU 状态变化时说话;它自己停了(cron 被删、脚本报错、机器换了)不会有
任何消息。夜里 22:00 CT 的 Heartbeat 多这一行,停了最晚第二天早上看得见。

真正插进 heartbeat.py 的只有 BEGIN/END 两行注释之间那一段(含这两行)。函数用到的
名字(json、datetime、timezone、HOME、CheckResult、OK/WARN/ERROR)heartbeat.py
本来就有,不新增 import;本文件开头那几样同名定义只为在仓库里能单独测这个函数。

  heartbeat_check.py --insert HB --out NEW   两处精确字符串各命中一次才插,结果写到 NEW
  heartbeat_check.py --call HB               按原样导入 HB,核对 CHECKS 顺序,只调这一项

两种模式都不联网、不碰 OpenClaw 命令行。--call 只调这一项而不跑整份 --print-only:
整份里有几项要调 OpenClaw 命令行,撞上 watchdog 的 :00/:05 探测槽会把它拖过超时线。
"""
import argparse
from dataclasses import dataclass
from datetime import datetime, timezone
import importlib.machinery
import importlib.util
import json
import os
from pathlib import Path
import stat
import sys

HOME = Path("/home/eric")
OK, WARN, ERROR, INFO = "OK", "WARN", "ERROR", "INFO"


@dataclass
class CheckResult:
    status: str
    label: str
    detail: str


BEGIN = "# --- nmu-offbox-probe: check_nmu_probe BEGIN ---"
END = "# --- nmu-offbox-probe: check_nmu_probe END ---"
FUNCTION_ANCHOR = "\n\nCHECKS = [\n"
ENTRY_ANCHOR = "    check_watchdog,\n"
ENTRY = "    check_nmu_probe,\n"


# --- nmu-offbox-probe: check_nmu_probe BEGIN ---
def check_nmu_probe() -> CheckResult:
    """NMU 异地探针(~/.openclaw/workspace/nmu-probe,user crontab 4-59/10)。
    探针只在 NMU 状态变化时发 Discord;它自己停了没有任何消息,只能靠这一行。"""
    label = "NMU 异地探针"
    state_path = HOME / ".openclaw" / "workspace" / "nmu-probe" / "state.json"
    try:
        if not state_path.exists():
            return CheckResult(WARN, label, "无 state.json — 4-59/10 cron 没在跑?")
        st = json.loads(state_path.read_text(encoding="utf-8", errors="replace"))
        last = st.get("last_run")
        try:
            ran = datetime.strptime(last, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        except (ValueError, TypeError):
            return CheckResult(WARN, label, f"last_run 解析失败: {last!r}")
        age_m = max(0, int((datetime.now(timezone.utc) - ran).total_seconds() // 60))
        if age_m > 25:
            return CheckResult(WARN, label, f"stale · last run {age_m}m ago(cron 停了?)")
        if st.get("status") == "down":
            reason = st.get("down_reason") or st.get("last_reason")
            return CheckResult(ERROR, label, f"DOWN since {st.get('down_since')} · {reason}"
                                             f" · last {age_m}m ago")
        summary = st.get("summary_24h") or {}
        runs = summary.get("runs", 0)
        owed = st.get("owed") or []
        if owed:
            return CheckResult(WARN, label, f"{len(owed)} 条 Discord 消息没发出去,下一轮重发"
                                            f" · last {age_m}m ago")
        if summary.get("fails"):
            return CheckResult(WARN, label, f"24h {runs} 轮里 {summary['fails']} 轮失败"
                                            f"(没到告警线)· last {age_m}m ago")
        return CheckResult(OK, label, f"healthy · 24h {runs} 轮 · p95 {summary.get('p95_ms')} ms"
                                      f" · last {age_m}m ago")
    except Exception as e:
        return CheckResult(WARN, label, f"internal: {type(e).__name__}")
# --- nmu-offbox-probe: check_nmu_probe END ---


class InsertError(Exception):
    pass


def block() -> str:
    """本文件里 BEGIN 行到 END 行(含)那一段,不带结尾换行。"""
    text = Path(__file__).read_text(encoding="utf-8")
    # 找行首的标记:上面 BEGIN = "…" 那一行也含这串字,但不在行首。
    start = text.index("\n" + BEGIN + "\n") + 1
    end = text.index("\n" + END + "\n", start) + 1 + len(END)
    return text[start:end]


def insert(source: str, snippet: str) -> str:
    if BEGIN in source or "def check_nmu_probe" in source:
        raise InsertError("heartbeat.py 里已经有 check_nmu_probe,不重复插")
    for anchor in (FUNCTION_ANCHOR, ENTRY_ANCHOR):
        hits = source.count(anchor)
        if hits != 1:
            raise InsertError(f"锚点 {anchor!r} 命中 {hits} 次,应当正好 1 次")
    source = source.replace(ENTRY_ANCHOR, ENTRY_ANCHOR + ENTRY, 1)
    return source.replace(FUNCTION_ANCHOR, f"\n\n{snippet}\n\n\nCHECKS = [\n", 1)


def call(path: Path) -> int:
    # 显式给 loader:暂存文件叫 heartbeat.py.nmu-probe.new,按后缀推断会认不出来。
    loader = importlib.machinery.SourceFileLoader("heartbeat_nmu_probe_call", str(path))
    spec = importlib.util.spec_from_file_location(loader.name, path, loader=loader)
    module = importlib.util.module_from_spec(spec)
    # heartbeat.py 里有 @dataclass,解析注解时要在 sys.modules 里找得到自己。
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    names = [check.__name__ for check in module.CHECKS]
    if names.count("check_nmu_probe") != 1 or "check_watchdog" not in names \
            or names.index("check_nmu_probe") != names.index("check_watchdog") + 1:
        print(f"CHECKS 里 check_nmu_probe 不在 check_watchdog 后面正好一次:{names}",
              file=sys.stderr)
        return 1
    result = module.check_nmu_probe()
    print(f"{result.status} {result.label}: {result.detail}")
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="把 check_nmu_probe 装进 heartbeat.py")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--insert", type=Path, metavar="HEARTBEAT_PY")
    mode.add_argument("--call", type=Path, metavar="HEARTBEAT_PY")
    parser.add_argument("--out", type=Path, help="--insert 的结果写到这里(不能是原文件)")
    args = parser.parse_args(argv)
    if args.call is not None:
        return call(args.call)
    if args.out is None or args.out.resolve() == args.insert.resolve():
        parser.error("--insert 要配一个不同于原文件的 --out")
    source = args.insert.read_text(encoding="utf-8")
    try:
        updated = insert(source, block())
    except InsertError as error:
        print(error, file=sys.stderr)
        return 1
    compile(updated, str(args.insert), "exec")
    args.out.write_text(updated, encoding="utf-8")
    os.chmod(args.out, stat.S_IMODE(args.insert.stat().st_mode))
    print(f"inserted: check_nmu_probe 在 CHECKS 之前,CHECKS 里排在 check_watchdog 之后"
          f"(+{len(updated) - len(source)} 字符)→ {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
