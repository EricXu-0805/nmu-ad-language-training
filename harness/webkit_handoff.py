#!/usr/bin/env python3
"""本机 WebKit 合成交接走查：手动准备页卸载、旧录音恢复被取消、自动带练接手开麦。

2026-10-23 那次是在审核报告目录里临时跑的；这里收进仓库，只在本机跑，不进 CI
（CI 分钟数要 Eric 另定）。用一个装了 Playwright 的解释器跑，例如::

    /opt/homebrew/bin/python3 -I harness/webkit_handoff.py \
        --receipt /path/to/private-0700-dir/webkit-handoff.json

前提：web/ 下已 ``npm ci``，本机已有 Playwright 的 WebKit。步骤：用
``web/harness/vite.config.ts`` 把 ``web/harness/`` 构建到仓库外的 0700 临时目录，
在 127.0.0.1:4179 起静态服务；先在非持久上下文跑一次 IndexedDB 存储探针（只记观察，
10-23 首个非持久会话就是在这里 Blob 报 UnknownError），再在 0700 私有持久 profile 里
跑探针和完整交接。存储探针不影响退出码。

退出码：0 交接通过；1 交接失败，或走查页对当前 ``web/src`` 构建不过（代码没跟上，
不是环境问题）；2 环境不满足，或跑完了但回执没写成。

结论范围：桌面 WebKit 的合成走查，麦克风是振荡器流，网络全是桩。它不证明 iPad Safari、
微信内置浏览器或真麦克风权限的行为，回执里写死这几项为 false。
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import threading
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / "web"
HOST = "127.0.0.1"
PORT = 4179
ORIGIN = f"http://{HOST}:{PORT}"
SCHEMA = "webkit-handoff-receipt.v1"
SYNTHETIC_SERVER_SEQ = 77
HANDOFF_DEADLINE_MS = 20_000
# 顺序本身就是合同：旧恢复先被取消，再对旧条目问 audioSaved（404 之后不许直接放弃），
# 然后才取流、授权、开录。少了 foreign-refused-kept 就是外场清理退回了 10-23 之前的版本。
EXPECTED_EVENTS = (
    "manual-recovery-waiting",
    "manual-recovery-aborted",
    "foreign-refused-kept",
    "native-microphone-prepared",
    "exact-authorized",
    "native-recording-started",
)


class HandoffHarnessError(RuntimeError):
    """环境不满足（缺依赖、端口被占），不是交接本身失败。"""


class HandoffPageBuildError(RuntimeError):
    """走查页对当前 web/src 构建不过：web/harness 不进 tsc -b，web/src 改了导出它最先断在这里。"""


def handoff_failures(result: Any) -> list[str]:
    if not isinstance(result, dict):
        return ["handoff_result_missing"]
    failures = []
    if result.get("syntheticOnly") is not True:
        failures.append("synthetic_only_flag_missing")
    if tuple(result.get("events") or ()) != EXPECTED_EVENTS:
        failures.append("event_sequence_mismatch")
    bytes_ = result.get("nativeBytes")
    if not isinstance(bytes_, int) or isinstance(bytes_, bool) or bytes_ <= 0:
        failures.append("new_recording_has_no_bytes")
    if result.get("receipt") != SYNTHETIC_SERVER_SEQ:
        failures.append("audio_receipt_server_seq_missing")
    if result.get("foreignBytesAndMetadataUnchanged") is not True:
        failures.append("old_entry_changed")
    if result.get("remainingEntries") != 1:
        failures.append("outbox_entry_count_mismatch")
    if result.get("tracksEnded") is not True:
        failures.append("tracks_not_ended")
    handoff_ms = result.get("handoffMs")
    if not isinstance(handoff_ms, (int, float)) or not 0 <= handoff_ms < HANDOFF_DEADLINE_MS:
        failures.append("handoff_too_slow")
    return failures


def build_receipt(*, handoff: Any, handoff_error: str | None,
                  nonpersistent_probe: Any, persistent_probe: Any,
                  browser_version: str | None, playwright_version: str | None,
                  source: dict[str, Any], page_errors: list[str]) -> dict[str, Any]:
    failures = [f"handoff_threw:{handoff_error}"] if handoff_error else handoff_failures(handoff)
    return {
        "schema_version": SCHEMA,
        "status": "failed" if failures else "passed",
        "recorded_utc": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "synthetic_only": True,
        "actual_iOS_or_WeChat_device_test": False,
        "real_microphone_permission_or_hardware_test": False,
        "production_training_test": False,
        "origin": ORIGIN,
        "browser": {"engine": "webkit", "version": browser_version, "playwright": playwright_version},
        "source": source,
        "persistent_profile": "private_0700_temporary_directory",
        # 存储探针只是观察，不进 status：10-23 的非持久失败无法唯一归因到产品或环境。
        "nonpersistent_storage_observation": nonpersistent_probe,
        "persistent_storage_observation": persistent_probe,
        "persistent_full_handoff": handoff,
        "page_errors": page_errors,
        "failures": failures,
    }


def write_receipt(path: Path, receipt: dict[str, Any]) -> Path:
    parent = path.parent
    mode = parent.stat().st_mode
    if parent.is_symlink() or not stat.S_ISDIR(mode) or stat.S_IMODE(mode) & 0o077:
        raise HandoffHarnessError("回执目录必须是已存在、权限 0700 的私有目录")
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(json.dumps(receipt, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
    return path


def _source_identity() -> dict[str, Any]:
    def git(*args: str) -> str:
        return subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True,
                              check=True, timeout=30).stdout.strip()
    try:
        return {
            "git_revision": git("rev-parse", "HEAD"),
            "web_src_or_harness_dirty": bool(git(
                "status", "--porcelain", "--", "web/src", "web/harness", "harness/webkit_handoff.py")),
        }
    except (OSError, subprocess.SubprocessError):
        return {"git_revision": None, "web_src_or_harness_dirty": None}


def _build_site(site: Path) -> None:
    vite = WEB / "node_modules" / ".bin" / "vite"
    if not vite.exists():
        raise HandoffHarnessError("web/node_modules 不全：先在 web/ 跑 npm ci")
    built = subprocess.run(
        [str(vite), "build", "--config", "harness/vite.config.ts",
         "--outDir", str(site), "--emptyOutDir", "--logLevel", "error"],
        cwd=WEB, capture_output=True, text=True, timeout=300)
    if built.returncode != 0 or not (site / "index.html").is_file():
        raise HandoffPageBuildError((built.stderr or built.stdout)[:2000])


class _QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, *_args: Any) -> None:
        pass


def run(root: Path) -> dict[str, Any]:
    try:
        from playwright.sync_api import Error as PlaywrightError, sync_playwright
        from importlib.metadata import version
    except ImportError as exc:
        raise HandoffHarnessError("这个解释器没有 Python Playwright；不会自动安装") from exc

    site = root / "site"
    _build_site(site)
    try:
        server = ThreadingHTTPServer((HOST, PORT), partial(_QuietHandler, directory=str(site)))
    except OSError as exc:
        raise HandoffHarnessError(f"{HOST}:{PORT} 起不了服务（被占用？）") from exc
    threading.Thread(target=server.serve_forever, daemon=True).start()
    page_errors: list[str] = []
    handoff = handoff_error = None
    try:
        with sync_playwright() as playwright:
            browser = playwright.webkit.launch(headless=True)
            try:
                browser_version = browser.version
                context = browser.new_context()
                page = context.new_page()
                page.goto(ORIGIN + "/")
                nonpersistent_probe = page.evaluate("() => window.storageProbe()")
                context.close()
            finally:
                browser.close()

            profile = root / "webkit-profile"
            profile.mkdir(mode=0o700)
            context = playwright.webkit.launch_persistent_context(str(profile), headless=True)
            try:
                page = context.pages[0] if context.pages else context.new_page()
                page.on("pageerror", lambda error: page_errors.append(str(error)[:300]))
                page.goto(ORIGIN + "/")
                persistent_probe = page.evaluate("() => window.storageProbe()")
                try:
                    handoff = page.evaluate("() => window.runHandoff()")
                except PlaywrightError as exc:
                    handoff_error = str(exc).splitlines()[0][:300]
            finally:
                context.close()
    except PlaywrightError as exc:
        raise HandoffHarnessError(
            "Playwright/WebKit 没跑起来：" + str(exc).splitlines()[0][:300]) from exc
    finally:
        server.shutdown()
        server.server_close()
    return build_receipt(
        handoff=handoff, handoff_error=handoff_error,
        nonpersistent_probe=nonpersistent_probe, persistent_probe=persistent_probe,
        browser_version=browser_version, playwright_version=version("playwright"),
        source=_source_identity(), page_errors=page_errors)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="本机 WebKit 合成交接走查（不代表 iPad Safari / 微信真机）")
    parser.add_argument("--receipt", help="把回执写进已存在的 0700 私有目录（0600、不覆盖）")
    args = parser.parse_args(argv)
    try:
        with tempfile.TemporaryDirectory(prefix="nmu-webkit-handoff-") as raw_root:
            root = Path(raw_root).resolve()
            root.chmod(0o700)
            if root.is_relative_to(ROOT):
                raise HandoffHarnessError("临时目录落进了仓库")
            receipt = run(root)
    except HandoffPageBuildError as exc:
        print(f"走查页对当前 web/src 构建不过，不是环境问题（多半是 web/src 改了导出或接口）：\n{exc}",
              file=sys.stderr)
        return 1
    except (HandoffHarnessError, OSError) as exc:
        print(f"环境不满足：{exc}", file=sys.stderr)
        return 2
    print(json.dumps(receipt, ensure_ascii=False, indent=2, sort_keys=True))
    if args.receipt:
        try:
            write_receipt(Path(args.receipt), receipt)
        except (HandoffHarnessError, OSError) as exc:
            print(f"走查已跑完（结果见标准输出），回执没写成：{exc}", file=sys.stderr)
            return 2
    return 0 if receipt["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
