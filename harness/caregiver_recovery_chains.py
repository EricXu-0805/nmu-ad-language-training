#!/usr/bin/env python3
"""照护员本机演练:两条恢复链的真实 Chrome 走查(收据 257 §五 4)。

收据 257 上线后只有 HTTP 级与单测证据的两条链,这里在真 Chrome 里点一遍:

  链 A  设备故障 → 研究者点「继续 AI 自动带练」→ 平板不刷新自己重新探测
        做法:第二段话术的 TTS 请求被浏览器层拦掉(route.abort)→ 老人端回 tts_failed
        (audio_playback_failed)→ 服务端把场次 runtime 一起暂停(收据 257 §二 1)→ 管理员在
        真实控制台的「AI 自动带练」卡片看到暂停原因、点「继续」→ 老人端页面没有导航,
        自己 GET /autopilot/next 拿到新命令并放出下一段话术。
  链 B  开麦前清旧场次的外来录音 → 服务端 410 作废 → 本机删副本 → 照常开麦
        做法:先让平板配到同一受试者的旧场次 A(中止 A 后再开新场次 B),往平板 IndexedDB
        outbox 里塞一条 A 的、从未登记的 captured 录音;恢复后执行器开麦前清它:
        POST /audio 409 → audioSaved 探测 → 410(墓碑行,收据 257 §二·补二 12)→ 删本机副本
        → 回执 200 → 授权、record_started 照常。

与 caregiver_browser_acceptance.py 同一套沙箱(临时目录、实例标记、只认本机地址),
新流程单独一个入口,不动 start-pause 那条。跑法见 scripts/run-caregiver-demo20.sh
--browser-check recovery-chains。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import stat
import sys
import uuid
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

# 启动器用 python -I 跑本文件,同目录不在 sys.path;只把仓库根加进去,复用同目录的沙箱工具。
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from harness.caregiver_browser_acceptance import (  # noqa: E402
    _NATIVE_MEDIA_OBSERVER_SCRIPT,
    HARNESS_MARKER_HEADER,
    HARNESS_MARKER_VALUE,
    INSTANCE_MARKER_HEADER,
    BrowserAcceptanceConfig,
    BrowserAcceptanceError,
    _redacted_error,
    _same_acceptance_origin,
    _stable_error_code,
    _wait_for,
    _write_fake_microphone_wav,
    resolve_browser_config,
)

ADMIN_USERNAME_ENV = "NMU_HARNESS_ACTOR"
ADMIN_PASSWORD_ENV = "NMU_HARNESS_PASSWORD"
RESULT_RECEIPT_NAME = "browser-recovery-chains-result.json"
PATIENT_ID = "SYN-CG-P001"
DEMO_PROFILE_VERSION = "week2-single20-demo-v1"
# 旧场次那条外来录音的题位引用:同一份演示题库的第一题第一轮,服务端按 A 场次规范化。
FOREIGN_TURN_REF = "itm-0001#1"
_USERNAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
_TTS_PATH = re.compile(r"^/sessions/[^/]+/autopilot/commands/([^/]+)/tts$")


@dataclass(frozen=True)
class RecoveryChainsResult:
    old_session_id: str
    session_id: str
    plan_id: str
    foreign_raw_id: str
    failed_tts_command_key: str
    resumed_tts_command_key: str
    resumed_record_command_key: str
    foreign_register_code: str
    patient_page_epoch: str


def _admin_credentials() -> tuple[str, str]:
    username = os.environ.get(ADMIN_USERNAME_ENV) or ""
    password = os.environ.get(ADMIN_PASSWORD_ENV) or ""
    if not _USERNAME_RE.fullmatch(username) or len(password) < 8:
        raise BrowserAcceptanceError("恢复链走查缺少有效的临时管理员账号")
    return username, password


class _AdminApi:
    """管理员的 HTTP 侧动作(建/审安排、中止旧场次、读 runtime),与页面同一本机地址。"""

    def __init__(self, playwright, config: BrowserAcceptanceConfig, username: str, password: str):
        self.config = config
        self.ctx = playwright.request.new_context(base_url=config.origin)
        login = self.ctx.post("/auth/login", data={"username": username, "password": password})
        self._check(login, "管理员登录")

    def _check(self, response, what: str) -> dict:
        headers = {key.lower(): value for key, value in response.headers.items()}
        if headers.get(INSTANCE_MARKER_HEADER) != self.config.instance_marker:
            raise BrowserAcceptanceError(f"{what}的响应不属于本次启动的临时服务")
        if response.status != 200:
            raise BrowserAcceptanceError(f"{what}失败(HTTP {response.status})")
        try:
            payload = response.json()
        except Exception as exc:
            raise BrowserAcceptanceError(f"{what}的回执不是 JSON") from exc
        return payload if isinstance(payload, dict) else {"value": payload}

    def _csrf(self) -> str:
        for cookie in self.ctx.storage_state().get("cookies", []):
            if cookie.get("name") == "nmu_csrf" and cookie.get("value"):
                return str(cookie["value"])
        raise BrowserAcceptanceError("管理员登录后没有拿到 CSRF 令牌")

    def get(self, path: str, what: str) -> dict:
        return self._check(self.ctx.get(path), what)

    def post(self, path: str, body: dict, what: str) -> dict:
        return self._check(
            self.ctx.post(path, data=body, headers={"X-CSRF-Token": self._csrf()}), what)

    def close(self) -> None:
        try:
            self.ctx.dispose()
        except Exception:
            pass


def _write_receipt(config: BrowserAcceptanceConfig, result: RecoveryChainsResult) -> None:
    target = config.harness_root / RESULT_RECEIPT_NAME
    if target.exists() or target.is_symlink():
        raise BrowserAcceptanceError("恢复链走查结果收据已存在,拒绝覆盖")
    payload = json.dumps({"schema": "caregiver-browser-recovery-chains.v1", **result.__dict__},
                         sort_keys=True, separators=(",", ":"))
    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(payload)


def _read_receipt(root: Path) -> RecoveryChainsResult:
    target = root / RESULT_RECEIPT_NAME
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(target, flags)
    except OSError as exc:
        raise BrowserAcceptanceError("缺少本次恢复链走查结果收据") from exc
    try:
        facts = os.fstat(fd)
        if (not stat.S_ISREG(facts.st_mode) or stat.S_IMODE(facts.st_mode) != 0o600
                or facts.st_uid != os.getuid() or facts.st_nlink != 1
                or facts.st_size <= 0 or facts.st_size > 8_192):
            raise BrowserAcceptanceError("恢复链走查结果收据不是本用户的私密普通文件")
        with os.fdopen(fd, "r", encoding="utf-8") as handle:
            fd = -1
            value = json.load(handle)
    finally:
        if fd >= 0:
            os.close(fd)
    if not isinstance(value, dict) or value.get("schema") != "caregiver-browser-recovery-chains.v1":
        raise BrowserAcceptanceError("恢复链走查结果收据格式无效")
    fields = {name: value.get(name) for name in RecoveryChainsResult.__dataclass_fields__}
    if any(not isinstance(item, str) or not item for item in fields.values()):
        raise BrowserAcceptanceError("恢复链走查结果收据缺少字段")
    return RecoveryChainsResult(**fields)


_INJECT_FOREIGN_OUTBOX = """
async ({ sessionId, turnKey, rawAudioId }) => {
  const db = await new Promise((resolve, reject) => {
    const req = indexedDB.open("nmu-audio", 4);
    req.onsuccess = () => resolve(req.result);
    req.onerror = () => reject(req.error);
    req.onblocked = () => reject(new Error("blocked"));
  });
  if (!db.objectStoreNames.contains("outbox") || !db.objectStoreNames.contains("blobs")) {
    db.close();
    throw new Error("outbox store missing");
  }
  const bytes = new Uint8Array([0x1a, 0x45, 0xdf, 0xa3, 0x9f, 0x42, 0x86, 0x81, 0x01, 0x42, 0xf7, 0x81, 0x01]);
  const blob = new Blob([bytes], { type: "audio/webm" });
  const now = Date.now();
  const entry = {
    schemaVersion: 1, rawAudioId, sessionId, turnKey, containsDirectIdentifier: false,
    durationSeconds: 3, blobBytes: blob.size, mimeType: "audio/webm", phase: "captured",
    autopilotStopReason: "user_done", createdAtMs: now, updatedAtMs: now,
  };
  await new Promise((resolve, reject) => {
    const tx = db.transaction(["blobs", "outbox"], "readwrite");
    tx.objectStore("blobs").add(blob, rawAudioId);
    tx.objectStore("outbox").add(entry);
    tx.oncomplete = () => resolve();
    tx.onabort = () => reject(tx.error || new Error("aborted"));
  });
  db.close();
  return rawAudioId;
}
"""

_OUTBOX_KEYS = """
async () => {
  const db = await new Promise((resolve, reject) => {
    const req = indexedDB.open("nmu-audio", 4);
    req.onsuccess = () => resolve(req.result);
    req.onerror = () => reject(req.error);
  });
  const keys = await new Promise((resolve, reject) => {
    const tx = db.transaction(["outbox"], "readonly");
    const req = tx.objectStore("outbox").getAllKeys();
    req.onsuccess = () => resolve(req.result);
    req.onerror = () => reject(req.error);
  });
  db.close();
  return keys;
}
"""


def run_recovery_chains(config: BrowserAcceptanceConfig) -> RecoveryChainsResult:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise BrowserAcceptanceError("本机没有可用的 Python Playwright;已停止,不会自动安装") from exc

    admin_username, admin_password = _admin_credentials()
    fake_audio = config.harness_root / "tmp" / "browser-fake-microphone-recovery.wav"
    fake_audio.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    _write_fake_microphone_wav(fake_audio)
    violations: list[str] = []
    obs: dict[str, object] = {
        "logged_in": {"caregiver": False, "admin": False},
        "old_session_id": None,
        "session_id": None,
        "plan_id": None,
        "attached_session_ids": [],
        "autopilot_started": False,
        "device_failure_induced": False,
        "aborted_tts_key": None,
        "resumed": False,
        "pause_requested": False,
        "teardown_started": False,
        "next": [],
        "ack_types": {},
        "tts": {},
        "record_authorizations": {},
        "audio_posts": {},
        "audio_uploads": [],
        "audio_saved": [],
        "foreign_raw_id": None,
        "foreign_register_code": None,
        "foreign_disposition": None,
        "foreign_disposal_confirmed": False,
        "resume_posts": 0,
    }

    def violation(message: str) -> None:
        if len(violations) < 20:
            violations.append(message)

    def route_request(route) -> None:
        if _same_acceptance_origin(route.request.url, config.origin):
            route.continue_()
        else:
            violation("已拦截非本次本机地址的网络请求")
            route.abort("blockedbyclient")

    def abort_next_tts(route) -> None:
        # 链 A 的故障注入:第一次命中的话术请求整条掐掉(浏览器层 net::ERR_BLOCKED_BY_CLIENT),
        # 老人端会按 fetch_failed 回 tts_failed;之后的话术照常放行。
        matched = _TTS_PATH.match(urlsplit(route.request.url).path)
        if obs["aborted_tts_key"] is None and matched and obs["device_failure_induced"] is False:
            obs["aborted_tts_key"] = matched.group(1)
            obs["device_failure_induced"] = True
            route.abort("blockedbyclient")
            return
        route.fallback()

    def json_detail(response) -> dict | None:
        try:
            value = response.json()
        except Exception:
            return None
        if isinstance(value, dict) and isinstance(value.get("detail"), dict):
            return value["detail"]
        return None

    def expected_failure(tag: str, request, path: str, status: int, response) -> bool:
        code = _stable_error_code(response) if status >= 400 else None
        logged_in = obs["logged_in"]
        assert isinstance(logged_in, dict)
        if request.method == "GET" and path == "/auth/me":
            return not logged_in.get(tag) and status == 401 and code == "auth_me_not_logged_in"
        in_repair_window = (isinstance(obs["session_id"], str)
                            and obs["session_id"] not in obs["attached_session_ids"])  # type: ignore[operator]
        if (in_repair_window and (
                (request.method == "POST" and path == "/live/patient-heartbeat" and status == 401)
                or (request.method == "GET" and path == "/live/state" and status == 409
                    and code == "device_session_changed"))):
            # 新场次 B 已开、平板还攥着旧场次 A 的凭据:心跳 401 / 实时状态 409 就是
            # 「这台配的是另一场」的信号(哪个先到看时序),紧接着工作人员在屏上重新配对。
            return True
        if request.method == "GET" and path.endswith("/autopilot/next"):
            if obs["device_failure_induced"] and not obs["resumed"]:
                return status == 409 and code == "autopilot_runtime_inactive"
            if obs["pause_requested"]:
                return status == 409 and code == "autopilot_runtime_inactive"
            return not obs["autopilot_started"] and status == 409 and code == "autopilot_not_active"
        foreign = obs["foreign_raw_id"]
        if request.method == "POST" and path == "/audio" and status == 409 and isinstance(foreign, str):
            try:
                posted = request.post_data_json
            except Exception:
                return False
            if isinstance(posted, dict) and posted.get("raw_audio_id") == foreign and code:
                obs["foreign_register_code"] = code
                return True
            return False
        if request.method == "PUT" and path == "/live/state" and status == 410 and isinstance(foreign, str):
            detail = json_detail(response)
            if (detail and detail.get("code") == "audio_terminal_disposition"
                    and detail.get("rawAudioId") == foreign
                    and detail.get("sessionId") == obs["old_session_id"]
                    and detail.get("reason") == "deleted"):
                obs["foreign_disposition"] = detail
                return True
            return False
        return False

    def observe_response(tag: str, response) -> None:
        request = response.request
        parsed = urlsplit(response.url)
        if not _same_acceptance_origin(response.url, config.origin):
            violation("浏览器收到了非本次本机地址的响应")
            return
        try:
            headers = {key.lower(): value for key, value in response.all_headers().items()}
        except Exception:
            violation("浏览器无法核对响应的实例标记")
            return
        if headers.get(INSTANCE_MARKER_HEADER) != config.instance_marker:
            violation("响应不属于本次启动的临时服务")
        if headers.get(HARNESS_MARKER_HEADER) != HARNESS_MARKER_VALUE:
            violation("响应不属于照护员本机演练环境")
        if response.status >= 400 and not expected_failure(tag, request, parsed.path, response.status, response):
            code = _stable_error_code(response) or (json_detail(response) or {}).get("code") or "-"
            kind = ""
            try:
                posted = request.post_data_json
                if isinstance(posted, dict) and isinstance(posted.get("kind"), str):
                    kind = f" kind={posted['kind']}"
            except Exception:
                pass
            violation(f"[{tag}] 非预期 HTTP {response.status} {request.method} {parsed.path} ({code}){kind}")
        logged_in = obs["logged_in"]
        assert isinstance(logged_in, dict)
        if request.method == "POST" and parsed.path == "/auth/login" and response.status == 200:
            logged_in[tag] = True
        if (request.method == "POST" and parsed.path.startswith("/caregiver/visit-plans/")
                and parsed.path.endswith("/start") and response.status == 200):
            try:
                payload = response.json()
                session_id = payload["session"]["session_id"]
                if obs["old_session_id"] is None:
                    obs["old_session_id"] = session_id
                else:
                    obs["plan_id"] = payload["plan_id"]
                    obs["session_id"] = session_id
            except Exception:
                violation("开始本次回执缺少完整计划或场次标识")
        if response.status == 200 and parsed.path in {"/device/pair", "/device/attach"}:
            try:
                attached = obs["attached_session_ids"]
                assert isinstance(attached, list)
                attached.append(response.json()["sessionId"])
            except Exception:
                violation("设备配对/续接回执缺少场次标识")
        if response.status == 200 and parsed.path.endswith("/tts") and request.method == "POST":
            key = parsed.path.split("/")[-2]
            tts = obs["tts"]
            assert isinstance(tts, dict)
            tts[key] = int(tts.get(key, 0)) + 1
        if response.status == 200 and parsed.path.endswith("/autopilot/next"):
            try:
                payload = response.json()
                if isinstance(payload, dict) and isinstance(payload.get("command_key"), str):
                    entries = obs["next"]
                    assert isinstance(entries, list)
                    row = (payload["command_key"], payload.get("command_seq"), payload.get("kind"))
                    if row not in entries:
                        entries.append(row)
            except Exception:
                violation("下一条命令响应无法校验")
        if request.method == "POST" and parsed.path == "/audio" and response.status == 200:
            try:
                posted = request.post_data_json
                raw_id = posted.get("raw_audio_id") if isinstance(posted, dict) else None
                posts = obs["audio_posts"]
                assert isinstance(posts, dict)
                posts[str(raw_id)] = int(posts.get(str(raw_id), 0)) + 1
            except Exception:
                violation("录音登记请求无法校验")
        if (request.method == "PUT" and parsed.path.startswith("/audio/")
                and parsed.path.endswith("/blob") and response.status == 200):
            uploads = obs["audio_uploads"]
            assert isinstance(uploads, list)
            uploads.append(parsed.path.split("/")[-2])
        if request.method == "PUT" and parsed.path == "/live/state" and response.status == 200:
            try:
                posted = request.post_data_json
                kind = posted.get("kind") if isinstance(posted, dict) else None
                if kind == "audioSaved":
                    saved = obs["audio_saved"]
                    assert isinstance(saved, list)
                    saved.append(str((posted.get("payload") or {}).get("rawAudioId")))
                elif kind == "audioDisposalConfirmed":
                    payload = posted.get("payload") or {}
                    if payload.get("rawAudioId") == obs["foreign_raw_id"]:
                        obs["foreign_disposal_confirmed"] = True
            except Exception:
                violation("实时状态写请求无法校验")
        if request.method == "POST" and parsed.path.endswith("/acks") and response.status == 200:
            try:
                posted = request.post_data_json
                ack_type = posted.get("ack_type") if isinstance(posted, dict) else None
                key = parsed.path.split("/")[-2]
                if isinstance(ack_type, str):
                    ack_types = obs["ack_types"]
                    assert isinstance(ack_types, dict)
                    ack_types.setdefault(key, []).append(ack_type)
            except Exception:
                violation("设备命令 ACK 无法校验")
        if (request.method == "POST" and parsed.path.endswith("/recording-authorization")
                and response.status == 200):
            key = parsed.path.split("/")[-2]
            authorizations = obs["record_authorizations"]
            assert isinstance(authorizations, dict)
            authorizations[key] = int(authorizations.get(key, 0)) + 1
        session_id = obs["session_id"]
        if (request.method == "POST" and isinstance(session_id, str) and response.status == 200
                and parsed.path == f"/sessions/{session_id}/autopilot/start"):
            obs["autopilot_started"] = True
        if (request.method == "POST" and isinstance(session_id, str) and response.status == 200
                and parsed.path == f"/sessions/{session_id}/autopilot/resume"):
            obs["resume_posts"] = int(obs["resume_posts"]) + 1
            obs["resumed"] = True

    def observe_request_failure(request) -> None:
        parsed = urlsplit(request.url)
        aborted_key = obs["aborted_tts_key"]
        if (request.method == "POST" and isinstance(aborted_key, str)
                and parsed.path.endswith(f"/commands/{aborted_key}/tts")):
            return  # 链 A 的故障注入本身
        if bool(obs["teardown_started"]):
            return
        violation(f"浏览器网络请求未完成 {request.method} {parsed.path}")

    def attach_page(tag: str, page) -> None:
        page.on("pageerror", lambda _error: violation(f"[{tag}] 页面运行出错"))

        def observe_console(message) -> None:
            if message.type != "error":
                return
            text = message.text
            if text.startswith("Failed to load resource: the server responded with a status of"):
                return
            if "ERR_BLOCKED_BY_CLIENT" in text and obs["device_failure_induced"]:
                return  # 被我们掐掉的那条话术请求
            violation(f"[{tag}] 页面控制台报错: {text[:200]}")

        page.on("console", observe_console)
        page.on("response", lambda response: observe_response(tag, response))
        page.on("requestfailed", observe_request_failure)

    def acks(key: str) -> list[str]:
        ack_types = obs["ack_types"]
        assert isinstance(ack_types, dict)
        return list(ack_types.get(key, []))

    def raise_if_violated() -> None:
        if violations:
            raise BrowserAcceptanceError(violations[0])

    def caregiver_start_today(caregiver) -> None:
        caregiver.get_by_role("heading", name="今天要做的练习").wait_for(state="visible", timeout=20_000)
        caregiver.get_by_role("listitem").filter(has_text=PATIENT_ID).get_by_role(
            "button", name="开始本次", exact=True).click()
        caregiver.get_by_role("heading", name=PATIENT_ID, exact=True).wait_for(state="visible", timeout=20_000)

    browser = None
    contexts: list[object] = []
    admin_api: _AdminApi | None = None
    caregiver = None
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(
                channel="chrome", headless=True,
                args=[
                    "--autoplay-policy=no-user-gesture-required",
                    "--disable-background-networking", "--disable-component-update",
                    "--disable-default-apps", "--disable-sync",
                    "--use-fake-device-for-media-stream", "--use-fake-ui-for-media-stream",
                    f"--use-file-for-fake-audio-capture={fake_audio}",
                ],
            )
            caregiver_context = browser.new_context(base_url=config.origin, permissions=[], service_workers="block")
            patient_context = browser.new_context(base_url=config.origin, permissions=["microphone"], service_workers="block")
            admin_context = browser.new_context(base_url=config.origin, permissions=[], service_workers="block")
            contexts = [patient_context, caregiver_context, admin_context]
            for context in contexts:
                context.route("**/*", route_request)
            patient_context.add_init_script(_NATIVE_MEDIA_OBSERVER_SCRIPT)
            caregiver = caregiver_context.new_page()
            patient = patient_context.new_page()
            admin_page = admin_context.new_page()
            attach_page("caregiver", caregiver)
            attach_page("patient", patient)
            attach_page("admin", admin_page)
            admin_api = _AdminApi(playwright, config, admin_username, admin_password)

            # ---- 0. 旧场次 A:照护员开场、平板配对,再由管理员中止 ----
            caregiver.goto(f"{config.origin}/console", wait_until="domcontentloaded")
            caregiver.get_by_label("用户名").fill(config.username)
            caregiver.get_by_label("密码").fill(config.password)
            caregiver.get_by_role("button", name="登录", exact=True).click()
            caregiver_start_today(caregiver)
            caregiver.get_by_text("老人画面还没打开", exact=True).wait_for(state="visible", timeout=20_000)
            _wait_for(lambda: isinstance(obs["old_session_id"], str), (caregiver,), timeout_seconds=10, label="旧场次开场回执")
            old_session = str(obs["old_session_id"])

            patient.goto(f"{config.origin}/patient", wait_until="domcontentloaded")
            pair_dialog = patient.get_by_role("dialog", name="连接这台平板", exact=True)
            pair_dialog.wait_for(state="visible", timeout=20_000)
            patient.get_by_label("配对码").fill(config.pin)
            patient.get_by_role("button", name="完成配对", exact=True).click()
            pair_dialog.wait_for(state="hidden", timeout=20_000)
            attached = obs["attached_session_ids"]
            assert isinstance(attached, list)
            if attached[:1] != [old_session]:
                raise BrowserAcceptanceError("平板没有先配到旧场次 A")
            caregiver.get_by_text("老人画面已打开", exact=True).wait_for(state="visible", timeout=25_000)
            # 平板页面在整条链里不许刷新:在 window 上放一个标记,重新导航就没了。
            epoch = uuid.uuid4().hex
            patient.evaluate("(epoch) => { window.__nmuRecoveryEpoch = epoch; }", epoch)

            runtime = admin_api.get(f"/sessions/{old_session}/runtime", "读旧场次 runtime")
            admin_api.post(f"/sessions/{old_session}/abort", {
                "reason_code": "technical_failure",
                "expected_revision": int(runtime["revision"]),
                "idempotency_key": f"recovery-chains-abort-{uuid.uuid4().hex[:16]}",
            }, "中止旧场次 A")

            # ---- 1. 新场次 B:管理员建/审第二份安排,照护员开场,平板静默续接到 B ----
            today = admin_api.get("/visit-plans/today", "读研究日期")
            as_of = today.get("as_of_date")
            if not isinstance(as_of, str):
                raise BrowserAcceptanceError("研究日期回执缺少 as_of_date")
            created = admin_api.post("/visit-plans", {
                "idempotency_key": f"recovery-chains-create-{uuid.uuid4().hex[:16]}",
                "patient_id": PATIENT_ID, "scheduled_date": as_of, "session_sitting_no": 2,
                "week_no": 2, "phase_type": "正式训练", "event_line": "正式训练",
                "autopilot_profile_version_id": DEMO_PROFILE_VERSION,
            }, "建第二份安排")
            approved = admin_api.post(f"/visit-plans/{created['plan_id']}/approve", {
                "idempotency_key": f"recovery-chains-approve-{uuid.uuid4().hex[:16]}",
                "expected_revision": int(created["revision"]),
            }, "审核第二份安排")
            if approved.get("status") != "approved":
                raise BrowserAcceptanceError("第二份安排没有进入已审核")

            caregiver.get_by_role("button", name="查看今天任务", exact=True).wait_for(state="visible", timeout=30_000)
            caregiver.get_by_role("button", name="查看今天任务", exact=True).click()
            caregiver_start_today(caregiver)
            _wait_for(lambda: isinstance(obs["session_id"], str), (caregiver,), timeout_seconds=10, label="新场次开场回执")
            session_id = str(obs["session_id"])
            if session_id == old_session:
                raise BrowserAcceptanceError("第二次开场没有产生新场次")
            # 平板还攥着旧场次 A 的凭据,不会静默续接到 B(有凭据就不走 attach 轮询);
            # 屏上常驻的「换一位受试者或重新配对」入口就是给工作人员这时候点的——
            # 9/13 演示里钱凯也是这样把同一台平板换到新场次的。不刷新页面。
            pair_entry = patient.locator("button.patient-pair-entry")
            try:
                pair_entry.first.wait_for(state="visible", timeout=20_000)
                pair_entry.first.click()
            except Exception:
                pass  # 配对框可能已经自己弹出来了
            pair_dialog.wait_for(state="visible", timeout=20_000)
            patient.get_by_label("配对码").fill(config.pin)
            patient.get_by_role("button", name="完成配对", exact=True).click()
            pair_dialog.wait_for(state="hidden", timeout=20_000)
            _wait_for(lambda: session_id in obs["attached_session_ids"], (patient, caregiver),
                      timeout_seconds=20, label="平板换到新场次 B")
            caregiver.get_by_text("老人画面已打开", exact=True).wait_for(state="visible", timeout=30_000)
            if patient.evaluate("() => window.__nmuRecoveryEpoch") != epoch:
                raise BrowserAcceptanceError("换场次时平板页面刷新了(应原页重新配对)")

            # ---- 2. 正常走到第一题录完 ----
            start_practice = caregiver.get_by_role("button", name="开始练习", exact=True)
            start_practice.wait_for(state="visible", timeout=20_000)
            patient.get_by_role("button", name="点一下，开始", exact=True).click()
            tts_toggle = patient.get_by_role("button", name="关闭语音朗读", exact=True)
            tts_toggle.wait_for(state="visible", timeout=10_000)
            start_practice.click()
            caregiver.get_by_text("练习进行中", exact=True).wait_for(state="visible", timeout=20_000)
            patient.get_by_text("正在听您说", exact=True).wait_for(state="visible", timeout=30_000)
            entries = obs["next"]
            assert isinstance(entries, list)
            first_tts_key = entries[0][0]
            # 第一段话术已放完、正在录音:从现在起第一次命中的话术请求(判定后的反馈)被掐掉。
            patient_context.route("**/autopilot/commands/*/tts", abort_next_tts)
            patient.get_by_role("button", name="说完了可以点这里", exact=True).click()

            # ---- 3. 链 A 前半:tts_failed → 场次 runtime 一起暂停 ----
            _wait_for(lambda: isinstance(obs["aborted_tts_key"], str)
                      and acks(str(obs["aborted_tts_key"])) == ["tts_failed"],
                      (patient, caregiver), timeout_seconds=45, label="反馈话术被掐掉后的 tts_failed 回执")
            failed_key = str(obs["aborted_tts_key"])
            patient.get_by_text("练习已暂停，请稍候", exact=True).wait_for(state="visible", timeout=20_000)
            caregiver.get_by_text("练习已暂停", exact=True).wait_for(state="visible", timeout=20_000)
            _wait_for(lambda: admin_api.get(f"/sessions/{session_id}/runtime", "读 B runtime")
                      .get("status") == "paused", (patient,), timeout_seconds=10,
                      label="设备故障后场次 runtime 暂停")
            raise_if_violated()

            # ---- 链 B 布置:往平板塞一条旧场次 A 的、从未登记的录音 ----
            foreign_raw_id = f"aud-{uuid.uuid4()}"
            obs["foreign_raw_id"] = foreign_raw_id
            injected = patient.evaluate(_INJECT_FOREIGN_OUTBOX, {
                "sessionId": old_session, "turnKey": FOREIGN_TURN_REF, "rawAudioId": foreign_raw_id})
            if injected != foreign_raw_id:
                raise BrowserAcceptanceError("外来录音没有写进平板 outbox")

            # ---- 4. 链 A 后半:管理员在真实控制台点「继续」 ----
            admin_page.goto(f"{config.origin}/console", wait_until="domcontentloaded")
            admin_page.get_by_label("用户名").fill(admin_username)
            admin_page.get_by_label("密码").fill(admin_password)
            admin_page.get_by_role("button", name="登录", exact=True).click()
            admin_page.get_by_role("button", name="打开恢复入口", exact=True).wait_for(state="visible", timeout=20_000)
            admin_page.get_by_role("button", name="打开恢复入口", exact=True).click()
            boundary = admin_page.get_by_role("group", name="异常恢复数据分区")
            boundary.wait_for(state="visible", timeout=20_000)
            boundary.get_by_role("button", name=re.compile("^专用模拟")).click()
            card = admin_page.locator(".run-picker-card").filter(has_text=PATIENT_ID)
            card.get_by_role("button", name="核查既有场次", exact=True).click()
            row = admin_page.locator(".resume-row").filter(has_text="已暂停").filter(has_text="同周第 2 次")
            row.get_by_role("button", name="继续这场", exact=True).click()
            admin_page.get_by_text("AI 自动带练已安全暂停", exact=True).wait_for(state="visible", timeout=20_000)
            admin_page.get_by_text(re.compile("老人端放不出这句引导语")).wait_for(state="visible", timeout=20_000)
            resume_button = admin_page.get_by_role("button", name="继续 AI 自动带练", exact=True)
            resume_button.wait_for(state="visible", timeout=20_000)
            _wait_for(resume_button.is_enabled, (admin_page,), timeout_seconds=20, label="「继续 AI 自动带练」可点")
            resume_button.click()
            admin_page.get_by_role("button", name="确认继续 AI", exact=True).click()
            _wait_for(lambda: int(obs["resume_posts"]) == 1, (admin_page,), timeout_seconds=20, label="恢复请求 200")

            # 平板没有导航、自己重新探测:新的话术命令出现并放出来。
            def resumed_tts_started() -> bool:
                for key, _seq, kind in entries:
                    if kind == "tts" and key not in {first_tts_key, failed_key} and "tts_started" in acks(key):
                        return True
                return False
            _wait_for(resumed_tts_started, (patient, admin_page), timeout_seconds=45, label="恢复后平板自己拿到并放出新话术")
            if patient.evaluate("() => window.__nmuRecoveryEpoch") != epoch:
                raise BrowserAcceptanceError("恢复后平板页面刷新了(应靠 session.paused 下降沿自己重新探测)")
            resumed_tts_key = next(key for key, _seq, kind in entries
                                   if kind == "tts" and key not in {first_tts_key, failed_key} and "tts_started" in acks(key))

            # ---- 5. 链 B:开麦前清旧账,然后照常录 ----
            def resumed_record_started() -> bool:
                for key, _seq, kind in entries:
                    if kind == "record" and "record_started" in acks(key) and key != entries[1][0]:
                        return True
                return False
            _wait_for(resumed_record_started, (patient, admin_page), timeout_seconds=60, label="清完旧账后的 record_started")
            record_key = next(key for key, _seq, kind in entries
                              if kind == "record" and "record_started" in acks(key) and key != entries[1][0])
            if obs["foreign_register_code"] is None:
                raise BrowserAcceptanceError("清旧账没有先尝试登记外来录音(应撞 409)")
            if obs["foreign_disposition"] is None:
                raise BrowserAcceptanceError("服务端没有对外来录音给出 410 作废")
            _wait_for(lambda: bool(obs["foreign_disposal_confirmed"]), (patient,), timeout_seconds=10, label="本机删副本后的回执")
            remaining = patient.evaluate(_OUTBOX_KEYS)
            if foreign_raw_id in (remaining or []):
                raise BrowserAcceptanceError("410 之后外来录音仍留在平板 outbox")
            patient.get_by_text("正在听您说", exact=True).wait_for(state="visible", timeout=20_000)
            patient.get_by_role("button", name="说完了可以点这里", exact=True).click()
            _wait_for(lambda: "record_stopped" in acks(record_key)
                      and any(k != foreign_raw_id and n >= 1 for k, n in obs["audio_posts"].items())
                      and len(obs["audio_uploads"]) >= 2,
                      (patient,), timeout_seconds=45, label="恢复后的录音登记与上传")
            raise_if_violated()

            # ---- 6. 收尾:照护员安全暂停 ----
            obs["pause_requested"] = True
            caregiver.get_by_role("button", name="暂停练习", exact=True).click()
            caregiver.get_by_text("练习已暂停", exact=True).wait_for(state="visible", timeout=20_000)
            raise_if_violated()

            obs["teardown_started"] = True
            for context in contexts:
                context.close()
            contexts = []
            browser.close()
            browser = None
            result = RecoveryChainsResult(
                old_session_id=old_session,
                session_id=session_id,
                plan_id=str(obs["plan_id"]),
                foreign_raw_id=foreign_raw_id,
                failed_tts_command_key=failed_key,
                resumed_tts_command_key=resumed_tts_key,
                resumed_record_command_key=record_key,
                foreign_register_code=str(obs["foreign_register_code"]),
                patient_page_epoch=epoch,
            )
            _write_receipt(config, result)
            return result
    finally:
        if admin_api is not None:
            admin_api.close()
        obs["teardown_started"] = True
        for resource in [*contexts, browser]:
            if resource is not None:
                try:
                    resource.close()
                except Exception:
                    pass
        try:
            fake_audio.unlink(missing_ok=True)
        except OSError:
            pass


def _validate_recovery_ledger(config, result: RecoveryChainsResult) -> None:
    from sqlmodel import Session, select

    from app import db
    from app.models import (
        AudioAssetRow, AudioLocalCopyDisposalReceipt, AudioStatus, AuditLog,
        AutopilotControlEvent, Session as TrainSession, SessionRuntimeState,
    )
    from harness import caregiver_demo_harness

    caregiver_demo_harness._assert_imported_engine_binding(config.base, db)  # noqa: SLF001
    with Session(db.engine) as session, session.no_autoflush:
        old = session.get(TrainSession, result.old_session_id)
        new = session.get(TrainSession, result.session_id)
        if old is None or new is None or old.patient_id != new.patient_id:
            raise BrowserAcceptanceError("账本没有同一受试者的新旧两场")
        old_runtime = session.get(SessionRuntimeState, result.old_session_id)
        new_runtime = session.get(SessionRuntimeState, result.session_id)
        if old_runtime is None or old_runtime.status != "aborted":
            raise BrowserAcceptanceError("旧场次 A 不是中止态")
        if new_runtime is None or new_runtime.status != "paused":
            raise BrowserAcceptanceError("新场次 B 收尾后不是暂停态")

        events = list(session.exec(select(AutopilotControlEvent).where(
            AutopilotControlEvent.session_id == result.session_id).order_by(AutopilotControlEvent.event_seq)))
        types = [event.event_type for event in events]
        failure = [event for event in events if event.event_type == "failure" and event.actor_type == "device"]
        if not failure or "audio_playback_failed" not in (failure[0].payload_json or ""):
            raise BrowserAcceptanceError(f"控制账本没有设备侧 audio_playback_failed 失败事实({types})")
        if "resume" not in types[types.index("failure"):]:
            raise BrowserAcceptanceError(f"控制账本在设备故障之后没有恢复事实({types})")

        tombstone = session.get(AudioAssetRow, result.foreign_raw_id)
        if (tombstone is None or tombstone.session_id != result.old_session_id
                or tombstone.status != AudioStatus.deleted or tombstone.delete_gate_passed is not True
                or tombstone.checksum is not None or tombstone.byte_count is not None):
            raise BrowserAcceptanceError("外来录音没有以 deleted 墓碑行落在旧场次 A 上")
        audits = list(session.exec(select(AuditLog).where(AuditLog.action == "audio_capture_superseded")))
        mine = [row for row in audits if result.foreign_raw_id in (row.summary or "")]
        if len(mine) != 1 or "unregistered=1" not in (mine[0].summary or ""):
            raise BrowserAcceptanceError("作废审计不是恰好一条从未登记的记录")
        receipts = list(session.exec(select(AudioLocalCopyDisposalReceipt).where(
            AudioLocalCopyDisposalReceipt.raw_audio_id == result.foreign_raw_id)))
        if len(receipts) != 1 or receipts[0].reason != "deleted":
            raise BrowserAcceptanceError("平板删本机副本的回执没有落账")
        recordings = list(session.exec(select(AudioAssetRow).where(
            AudioAssetRow.session_id == result.session_id)))
        uploaded = [row for row in recordings if row.byte_count and row.checksum]
        if len(uploaded) < 2:
            raise BrowserAcceptanceError(f"新场次 B 没有两段真实上传的录音(uploaded={len(uploaded)})")


def validate_recovery_ledger() -> None:
    from harness import caregiver_demo_harness
    from harness.tts_ack_harness import HarnessConfigError

    try:
        config = caregiver_demo_harness.resolve_caregiver_config()
    except HarnessConfigError as exc:
        raise BrowserAcceptanceError("账本核验的临时目录或运行配置无效") from exc
    _validate_recovery_ledger(config, _read_receipt(config.base.root))


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="照护员真实 Chrome:两条恢复链走查")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--recovery-chains", action="store_true")
    group.add_argument("--verify-ledger", action="store_true")
    parser.add_argument("--origin")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    secrets = tuple(os.environ.get(name) or "" for name in (
        "NMU_CAREGIVER_PASSWORD", "CONSOLE_PIN", "NMU_CAREGIVER_HARNESS_INSTANCE", ADMIN_PASSWORD_ENV))
    try:
        if args.recovery_chains:
            if not args.origin:
                raise BrowserAcceptanceError("恢复链走查缺少本机地址")
            run_recovery_chains(resolve_browser_config(args.origin))
            print("真实 Chrome 恢复链已走完:设备故障→场次暂停→控制台继续→平板自行重探;开麦前清旧账→410→照常录音")
        else:
            if args.origin:
                raise BrowserAcceptanceError("账本核验不接受网页地址")
            validate_recovery_ledger()
            print("恢复链账本核验已通过")
        return 0
    except BrowserAcceptanceError as exc:
        print(f"恢复链走查失败:{_redacted_error(exc, secrets)}", file=sys.stderr)
        return 1
    except Exception as exc:  # fail closed without leaking browser call values
        print(f"恢复链走查失败:{_redacted_error(exc, secrets)}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
