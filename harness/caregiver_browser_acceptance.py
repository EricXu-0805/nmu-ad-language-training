"""HARNESS ONLY: real-Chrome caregiver ``start-pause`` acceptance.

The browser half deliberately has no application imports, so it can run from a
machine-level Python that already provides Playwright.  The ledger half imports
the application only when ``--verify-ledger`` is selected, so it runs from the
project virtual environment against the exact temporary database used by the
launcher.

Credentials are read only from the launcher's scrubbed child environment.  They
are never accepted on the command line and never included in output or failure
diagnostics.
"""
from __future__ import annotations

import argparse
import hashlib
import math
import os
import re
import stat
import sys
import json
import time
import wave
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping
from urllib.parse import urlsplit


CAREGIVER_USERNAME_ENV = "NMU_CAREGIVER_USERNAME"
CAREGIVER_PASSWORD_ENV = "NMU_CAREGIVER_PASSWORD"
CONSOLE_PIN_ENV = "CONSOLE_PIN"
INSTANCE_MARKER_ENV = "NMU_CAREGIVER_HARNESS_INSTANCE"
HARNESS_ROOT_ENV = "NMU_HARNESS_ROOT"
INSTANCE_MARKER_HEADER = "x-nmu-caregiver-harness-instance"
HARNESS_MARKER_HEADER = "x-nmu-test-harness"
HARNESS_MARKER_VALUE = "demo20-profile-tts-ack-v1"
RESULT_RECEIPT_NAME = "browser-start-pause-result.json"

_INSTANCE_RE = re.compile(r"[A-Za-z0-9_-]{32,128}")
_USERNAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")


class BrowserAcceptanceError(RuntimeError):
    """Fail-closed acceptance error safe for a non-technical launcher."""


@dataclass(frozen=True)
class BrowserAcceptanceConfig:
    origin: str
    username: str
    password: str
    pin: str
    instance_marker: str
    harness_root: Path

    @property
    def secrets(self) -> tuple[str, ...]:
        return self.password, self.pin, self.instance_marker


@dataclass(frozen=True)
class BrowserResult:
    plan_id: str
    session_id: str
    first_tts_command_key: str
    record_command_key: str
    next_command_key: str
    next_command_seq: int
    pause_runtime_revision: int
    drain_command_key: str
    drain_state_revision: int
    native_audio_pause_observed: bool


def _validated_origin(raw: str) -> str:
    parsed = urlsplit(raw)
    try:
        port = parsed.port
    except ValueError as exc:
        raise BrowserAcceptanceError("浏览器验收地址的端口无效") from exc
    if (
        parsed.scheme != "http"
        or parsed.hostname != "127.0.0.1"
        or port is None
        or not 1024 <= port <= 65535
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise BrowserAcceptanceError(
            "浏览器验收只允许 http://127.0.0.1:端口")
    return f"http://127.0.0.1:{port}"


def resolve_browser_config(
    origin: str,
    env: Mapping[str, str] | None = None,
) -> BrowserAcceptanceConfig:
    source = os.environ if env is None else env
    username = source.get(CAREGIVER_USERNAME_ENV) or ""
    password = source.get(CAREGIVER_PASSWORD_ENV) or ""
    pin = source.get(CONSOLE_PIN_ENV) or ""
    marker = source.get(INSTANCE_MARKER_ENV) or ""
    root_raw = source.get(HARNESS_ROOT_ENV) or ""
    if not _USERNAME_RE.fullmatch(username):
        raise BrowserAcceptanceError("浏览器验收缺少有效的临时照护员账号")
    if len(password) < 8:
        raise BrowserAcceptanceError("浏览器验收缺少有效的临时口令")
    if not re.fullmatch(r"[0-9]{6,12}", pin):
        raise BrowserAcceptanceError("浏览器验收缺少有效的临时配对 PIN")
    if not _INSTANCE_RE.fullmatch(marker):
        raise BrowserAcceptanceError("浏览器验收缺少本次启动的实例标记")
    if not root_raw:
        raise BrowserAcceptanceError("浏览器验收缺少专用临时目录")
    root = Path(root_raw)
    try:
        resolved = root.resolve(strict=True)
    except OSError as exc:
        raise BrowserAcceptanceError("浏览器验收临时目录不存在") from exc
    try:
        root_facts = resolved.stat()
    except OSError as exc:
        raise BrowserAcceptanceError("浏览器验收临时目录无法核对") from exc
    if (
        root.is_symlink()
        or not resolved.is_dir()
        or not resolved.name.startswith("nmu-caregiver-demo20.")
        or stat.S_IMODE(root_facts.st_mode) != 0o700
        or root_facts.st_uid != os.getuid()
    ):
        raise BrowserAcceptanceError("浏览器验收临时目录越界")
    return BrowserAcceptanceConfig(
        origin=_validated_origin(origin),
        username=username,
        password=password,
        pin=pin,
        instance_marker=marker,
        harness_root=resolved,
    )


def _same_acceptance_origin(url: str, origin: str) -> bool:
    if url in {"about:blank", "about:srcdoc"} or url.startswith("data:"):
        return True
    if url.startswith(f"blob:{origin}/"):
        return True
    parsed = urlsplit(url)
    base = urlsplit(origin)
    try:
        port = parsed.port
    except ValueError:
        return False
    return (
        parsed.scheme == base.scheme
        and parsed.hostname == base.hostname
        and port == base.port
        and parsed.username is None
        and parsed.password is None
    )


def _stable_error_code(response) -> str | None:
    try:
        value = response.json()
    except Exception:
        return None
    if not isinstance(value, dict) or set(value) != {"detail"}:
        return None
    if value.get("detail") == "未登录":
        return "auth_me_not_logged_in"
    detail = value.get("detail")
    if (
        isinstance(detail, dict)
        and set(detail) == {"code", "message"}
        and isinstance(detail.get("code"), str)
        and re.fullmatch(r"[a-z][a-z0-9_]{2,127}", detail["code"])
        and isinstance(detail.get("message"), str)
        and 0 < len(detail["message"].strip()) <= 300
        and not any(ord(character) < 32 for character in detail["message"])
    ):
        return detail["code"]
    return None


def _expected_http_failure(
    method: str,
    path: str,
    status: int,
    code: str | None,
    *,
    logged_in: bool,
    autopilot_started: bool,
    pause_requested: bool,
) -> bool:
    if method == "GET" and path == "/auth/me":
        return (not logged_in and status == 401
                and code == "auth_me_not_logged_in")
    if method == "GET" and path.endswith("/autopilot/next"):
        if pause_requested:
            return status == 409 and code == "autopilot_runtime_inactive"
        return (not autopilot_started
                and status == 409 and code == "autopilot_not_active")
    return False


def _write_result_receipt(config: BrowserAcceptanceConfig, result: BrowserResult) -> None:
    target = config.harness_root / RESULT_RECEIPT_NAME
    if target.exists() or target.is_symlink():
        raise BrowserAcceptanceError("浏览器验收结果收据已存在，拒绝覆盖")
    payload = json.dumps({
        "schema": "caregiver-browser-start-pause.v1",
        "plan_id": result.plan_id,
        "session_id": result.session_id,
        "first_tts_command_key": result.first_tts_command_key,
        "record_command_key": result.record_command_key,
        "next_command_key": result.next_command_key,
        "next_command_seq": result.next_command_seq,
        "pause_runtime_revision": result.pause_runtime_revision,
        "drain_command_key": result.drain_command_key,
        "drain_state_revision": result.drain_state_revision,
        "native_audio_pause_observed": result.native_audio_pause_observed,
    }, sort_keys=True, separators=(",", ":"))
    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(payload)


def _read_result_receipt(root: Path) -> BrowserResult:
    target = root / RESULT_RECEIPT_NAME
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(target, flags)
    except OSError as exc:
        raise BrowserAcceptanceError("缺少本次浏览器验收结果收据") from exc
    try:
        facts = os.fstat(fd)
        if (
            not stat.S_ISREG(facts.st_mode)
            or stat.S_IMODE(facts.st_mode) != 0o600
            or facts.st_uid != os.getuid()
            or facts.st_nlink != 1
            or facts.st_size <= 0
            or facts.st_size > 8_192
        ):
            raise BrowserAcceptanceError("浏览器验收结果收据不是本用户的私密普通文件")
        with os.fdopen(fd, "r", encoding="utf-8") as handle:
            fd = -1
            try:
                value = json.load(handle)
            except (UnicodeError, json.JSONDecodeError) as exc:
                raise BrowserAcceptanceError("浏览器验收结果收据格式无效") from exc
    finally:
        if fd >= 0:
            os.close(fd)
    if not isinstance(value, dict) or set(value) != {
        "schema", "plan_id", "session_id", "first_tts_command_key",
        "record_command_key", "next_command_key", "next_command_seq",
        "pause_runtime_revision", "drain_command_key", "drain_state_revision",
        "native_audio_pause_observed",
    } or value.get("schema") != "caregiver-browser-start-pause.v1":
        raise BrowserAcceptanceError("浏览器验收结果收据格式无效")
    text_fields = (
        value.get("plan_id"), value.get("session_id"),
        value.get("first_tts_command_key"), value.get("record_command_key"),
        value.get("next_command_key"), value.get("drain_command_key"),
    )
    if any(not isinstance(item, str) or not item or len(item) > 200 for item in text_fields):
        raise BrowserAcceptanceError("浏览器验收结果收据标识无效")
    sequence = value.get("next_command_seq")
    pause_revision = value.get("pause_runtime_revision")
    drain_revision = value.get("drain_state_revision")
    if not isinstance(sequence, int) or isinstance(sequence, bool) or sequence < 3:
        raise BrowserAcceptanceError("浏览器验收结果收据命令序号无效")
    if (not isinstance(pause_revision, int) or isinstance(pause_revision, bool)
            or pause_revision < 1):
        raise BrowserAcceptanceError("浏览器验收结果收据暂停修订无效")
    if (not isinstance(drain_revision, int) or isinstance(drain_revision, bool)
            or drain_revision < 1):
        raise BrowserAcceptanceError("浏览器验收结果收据收麦修订无效")
    if value.get("native_audio_pause_observed") is not True:
        raise BrowserAcceptanceError("浏览器验收结果缺少原生音频暂停证据")
    return BrowserResult(
        plan_id=text_fields[0],
        session_id=text_fields[1],
        first_tts_command_key=text_fields[2],
        record_command_key=text_fields[3],
        next_command_key=text_fields[4],
        next_command_seq=sequence,
        pause_runtime_revision=pause_revision,
        drain_command_key=text_fields[5],
        drain_state_revision=drain_revision,
        native_audio_pause_observed=True,
    )


def _write_fake_microphone_wav(path: Path) -> None:
    rate = 16_000
    frames = bytearray()
    for index in range(rate * 3):
        sample = int(6_000 * math.sin(2 * math.pi * 330 * index / rate))
        frames += sample.to_bytes(2, "little", signed=True)
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except OSError as exc:
        raise BrowserAcceptanceError("伪麦克风音频文件不是本次新建") from exc
    with os.fdopen(fd, "wb") as raw_output:
        with wave.open(raw_output, "wb") as output:
            output.setnchannels(1)
            output.setsampwidth(2)
            output.setframerate(rate)
            output.writeframes(frames)
    path.chmod(0o600)


_NATIVE_MEDIA_OBSERVER_SCRIPT = r"""
(() => {
  const journal = [];
  const ids = new WeakMap();
  const observed = new WeakSet();
  let nextId = 1;
  const idFor = (element) => {
    let id = ids.get(element);
    if (id === undefined) {
      id = nextId++;
      ids.set(element, id);
    }
    return id;
  };
  const push = (event, element, extra = {}) => {
    journal.push({ event, id: idFor(element), ...extra });
  };
  Object.defineProperty(window, "__nmuNativeMediaJournal", {
    value: journal,
    configurable: false,
    enumerable: false,
    writable: false,
  });
  const originalPlay = HTMLMediaElement.prototype.play;
  const originalPause = HTMLMediaElement.prototype.pause;
  HTMLMediaElement.prototype.play = function (...args) {
    if (!observed.has(this)) {
      observed.add(this);
      for (const event of ["playing", "ended", "error"]) {
        this.addEventListener(event, () => push(event, this));
      }
    }
    push("play_called", this, { pausedBefore: this.paused });
    return originalPlay.apply(this, args);
  };
  HTMLMediaElement.prototype.pause = function (...args) {
    const pausedBefore = this.paused;
    const result = originalPause.apply(this, args);
    push("pause_called", this, {
      pausedBefore,
      pausedAfter: this.paused,
      endedAfter: this.ended,
    });
    return result;
  };
})();
"""


def _wait_for(predicate, pages: tuple[object, ...], *, timeout_seconds: float, label: str) -> None:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if predicate():
            return
        for page in pages:
            page.wait_for_timeout(100)
    raise BrowserAcceptanceError(f"等待{label}超时")


def run_start_pause(config: BrowserAcceptanceConfig) -> BrowserResult:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise BrowserAcceptanceError(
            "本机没有可用的 Python Playwright；已停止，不会自动安装") from exc

    fake_audio = config.harness_root / "tmp" / "browser-fake-microphone.wav"
    fake_audio.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    _write_fake_microphone_wav(fake_audio)
    violations: list[str] = []
    observations: dict[str, object] = {
        "pause_requested": False,
        "teardown_started": False,
        "logged_in": False,
        "autopilot_started": False,
        "plan_id": None,
        "session_id": None,
        "pair_session_id": None,
        "tts": {},
        "next": [],
        "ack_types": {},
        "record_authorizations": {},
        "audio_posts": 0,
        "audio_uploads": 0,
        "audio_saved": 0,
        "drain_target_key": None,
        "drain_target_revision": None,
        "drain_ack_key": None,
        "drain_ack_revision": None,
        "drain_ack_replayed": None,
        "pause_runtime_revision": None,
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

    def observe_response(response) -> None:
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
        code = _stable_error_code(response) if response.status >= 400 else None
        if response.status >= 400 and not _expected_http_failure(
            request.method,
            parsed.path,
            response.status,
            code,
            logged_in=bool(observations["logged_in"]),
            autopilot_started=bool(observations["autopilot_started"]),
            pause_requested=bool(observations["pause_requested"]),
        ):
            violation(f"本机流程出现非预期 HTTP {response.status}")
        session_id = observations["session_id"]
        if (isinstance(session_id, str) and parsed.path.startswith("/sessions/")
                and not parsed.path.startswith(f"/sessions/{session_id}/")):
            violation("浏览器访问了本次场次以外的训练接口")
        if parsed.path == "/auth/me" and response.status == 401 \
                and bool(observations["logged_in"]):
            violation("照护员登录后又丢失了账号会话")
        if request.method == "POST" and parsed.path == "/auth/login" \
                and response.status == 200:
            observations["logged_in"] = True
        if request.method == "POST" and parsed.path.startswith(
            "/caregiver/visit-plans/"
        ) and parsed.path.endswith("/start") and response.status == 200:
            try:
                payload = response.json()
                path_plan_id = parsed.path.split("/")[-2]
                if payload.get("plan_id") != path_plan_id:
                    raise ValueError("plan id mismatch")
                observations["plan_id"] = payload["plan_id"]
                observations["session_id"] = payload["session"]["session_id"]
            except Exception:
                violation("开始本次回执缺少完整计划或场次标识")
        if request.method == "POST" and parsed.path == "/device/pair" \
                and response.status == 200:
            try:
                observations["pair_session_id"] = response.json()["sessionId"]
            except Exception:
                violation("设备配对回执缺少场次标识")
        if response.status == 200 and parsed.path.endswith("/tts"):
            content_type = headers.get("content-type", "").split(";", 1)[0]
            if content_type not in {"audio/wav", "audio/x-wav", "audio/mpeg"}:
                violation("真实 TTS 响应不是可播放音频")
            try:
                body = response.body()
                if body[:4] != b"RIFF" or body[8:12] != b"WAVE":
                    violation("真实 TTS 音频缺少 RIFF/WAVE 字节证据")
            except Exception:
                violation("无法读取真实 TTS 音频字节")
            key = parsed.path.split("/")[-2]
            tts = observations["tts"]
            assert isinstance(tts, dict)
            tts[key] = int(tts.get(key, 0)) + 1
        if response.status == 200 and parsed.path.endswith("/autopilot/next"):
            try:
                payload = response.json()
                if isinstance(payload, dict):
                    key = payload.get("command_key")
                    sequence = payload.get("command_seq")
                    kind = payload.get("kind")
                    if isinstance(key, str) and isinstance(sequence, int) \
                            and isinstance(kind, str):
                        entries = observations["next"]
                        assert isinstance(entries, list)
                        if (key, sequence, kind) not in entries:
                            entries.append((key, sequence, kind))
            except Exception:
                violation("下一条命令响应无法校验")
        if response.status == 200 and request.method == "POST" \
                and parsed.path == "/audio":
            observations["audio_posts"] = int(observations["audio_posts"]) + 1
        if response.status == 200 and request.method == "PUT" \
                and parsed.path.startswith("/audio/") and parsed.path.endswith("/blob"):
            observations["audio_uploads"] = int(observations["audio_uploads"]) + 1
        if response.status == 200 and request.method == "PUT" \
                and parsed.path == "/live/state":
            try:
                posted = request.post_data_json
                if isinstance(posted, dict) and posted.get("kind") == "audioSaved":
                    observations["audio_saved"] = int(observations["audio_saved"]) + 1
            except Exception:
                violation("录音保存回执请求无法校验")
        if response.status == 200 and request.method == "POST" \
                and parsed.path.endswith("/acks"):
            try:
                posted = request.post_data_json
                ack_type = posted.get("ack_type") if isinstance(posted, dict) else None
                key = parsed.path.split("/")[-2]
                if isinstance(ack_type, str):
                    ack_types = observations["ack_types"]
                    assert isinstance(ack_types, dict)
                    ack_types.setdefault(key, []).append(ack_type)
            except Exception:
                violation("设备命令 ACK 无法校验")
        if response.status == 200 and request.method == "POST" \
                and parsed.path.endswith("/recording-authorization"):
            key = parsed.path.split("/")[-2]
            authorizations = observations["record_authorizations"]
            assert isinstance(authorizations, dict)
            authorizations[key] = int(authorizations.get(key, 0)) + 1
        if (response.status == 200 and request.method == "POST"
                and isinstance(session_id, str)
                and parsed.path == f"/sessions/{session_id}/autopilot/start"):
            observations["autopilot_started"] = True
        if (response.status == 200 and request.method == "POST"
                and isinstance(session_id, str)
                and parsed.path == f"/sessions/{session_id}/pause"):
            try:
                payload = response.json()
                if payload.get("session_id") != session_id \
                        or payload.get("runtime_status") != "paused":
                    raise ValueError("pause receipt mismatch")
                revision = payload.get("runtime_revision")
                if not isinstance(revision, int) or isinstance(revision, bool) \
                        or revision < 1:
                    raise ValueError("pause revision invalid")
                observations["pause_runtime_revision"] = revision
            except Exception:
                violation("安全暂停回执无法校验")
        if response.status == 200 and request.method == "GET" \
                and parsed.path.endswith("/autopilot/drain-target"):
            try:
                payload = response.json()
                observations["drain_target_key"] = payload["command_key"]
                observations["drain_target_revision"] = payload["state_revision"]
            except Exception:
                violation("安全收麦目标回执无法校验")
        if response.status == 200 and request.method == "POST" \
                and parsed.path.endswith("/drain-ack"):
            try:
                payload = response.json()
                observations["drain_ack_key"] = parsed.path.split("/")[-2]
                observations["drain_ack_revision"] = payload["state_revision"]
                observations["drain_ack_replayed"] = payload["replayed"]
            except Exception:
                violation("安全收麦 ACK 回执无法校验")

    def observe_request_failure(request) -> None:
        parsed = urlsplit(request.url)
        session_id = observations["session_id"]
        expected_abort = bool(observations["teardown_started"]) and (
            (request.method == "GET" and parsed.path == "/live/state")
            or (request.method == "POST"
                and parsed.path == "/live/patient-heartbeat")
            or (isinstance(session_id, str) and request.method == "GET"
                and parsed.path == f"/caregiver/sessions/{session_id}/status")
        )
        if not expected_abort:
            violation("浏览器网络请求未完成")

    def attach_page(page) -> None:
        page.on("pageerror", lambda _error: violation("页面运行出错"))
        def observe_console(message) -> None:
            if message.type != "error":
                return
            text = message.text
            # Chromium mirrors every HTTP 4xx into console.error without the
            # response body.  The response observer above validates the exact
            # method/path/status/code tuple, so only that browser-generated
            # diagnostic is ignored here.
            if text.startswith("Failed to load resource: the server responded with a status of"):
                return
            violation("页面控制台报错")

        page.on("console", observe_console)
        page.on("response", observe_response)
        page.on("requestfailed", observe_request_failure)

    browser = None
    caregiver_context = None
    patient_context = None
    caregiver = None
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(
                channel="chrome",
                headless=True,
                args=[
                    "--autoplay-policy=no-user-gesture-required",
                    "--disable-background-networking",
                    "--disable-component-update",
                    "--disable-default-apps",
                    "--disable-sync",
                    "--use-fake-device-for-media-stream",
                    "--use-fake-ui-for-media-stream",
                    f"--use-file-for-fake-audio-capture={fake_audio}",
                ],
            )
            caregiver_context = browser.new_context(
                base_url=config.origin,
                permissions=[],
                service_workers="block",
            )
            patient_context = browser.new_context(
                base_url=config.origin,
                permissions=["microphone"],
                service_workers="block",
            )
            caregiver_context.route("**/*", route_request)
            patient_context.route("**/*", route_request)
            patient_context.add_init_script(_NATIVE_MEDIA_OBSERVER_SCRIPT)
            caregiver = caregiver_context.new_page()
            patient = patient_context.new_page()
            attach_page(caregiver)
            attach_page(patient)

            caregiver.goto(f"{config.origin}/console", wait_until="domcontentloaded")
            caregiver.get_by_label("用户名").fill(config.username)
            caregiver.get_by_label("密码").fill(config.password)
            caregiver.get_by_role("button", name="登录", exact=True).click()
            caregiver.get_by_role("heading", name="今天要做的练习").wait_for(
                state="visible", timeout=20_000)
            caregiver.get_by_role("listitem").filter(
                has_text="SYN-CG-P001",
            ).get_by_role("button", name="开始本次", exact=True).click()
            caregiver.get_by_role("heading", name="SYN-CG-P001", exact=True).wait_for(
                state="visible", timeout=20_000)
            caregiver.get_by_text("等待老人画面", exact=True).wait_for(
                state="visible", timeout=20_000)
            caregiver.get_by_text("老人画面还没打开", exact=True).wait_for(
                state="visible", timeout=20_000)

            patient.goto(f"{config.origin}/patient", wait_until="domcontentloaded")
            pair_dialog = patient.get_by_role(
                "dialog", name="配对受试者端", exact=True)
            pair_dialog.wait_for(
                state="visible", timeout=20_000)
            patient.get_by_label("设备 PIN").fill(config.pin)
            patient.get_by_role("button", name="完成配对", exact=True).click()
            pair_dialog.wait_for(
                state="hidden", timeout=20_000)
            if (not isinstance(observations["plan_id"], str)
                    or not isinstance(observations["session_id"], str)
                    or not isinstance(observations["pair_session_id"], str)
                    or observations["session_id"] != observations["pair_session_id"]):
                raise BrowserAcceptanceError("开始本次与设备配对不是同一场次")
            caregiver.get_by_text("老人画面已打开", exact=True).wait_for(
                state="visible", timeout=25_000)
            start_practice = caregiver.get_by_role(
                "button", name="开始练习", exact=True)
            start_practice.wait_for(state="visible", timeout=20_000)
            if not start_practice.is_enabled():
                raise BrowserAcceptanceError("老人画面连接后开始练习仍未开放")
            patient.get_by_role(
                "button", name="点一下，开始并准备声音播放", exact=True
            ).click()
            tts_toggle = patient.get_by_role("button", name="关闭语音朗读", exact=True)
            tts_toggle.wait_for(state="visible", timeout=10_000)
            if tts_toggle.get_attribute("aria-pressed") != "true":
                raise BrowserAcceptanceError("老人画面没有确认已开启朗读")
            start_practice.click()
            caregiver.get_by_text("练习进行中", exact=True).wait_for(
                state="visible", timeout=20_000)

            patient.get_by_text("正在听您说", exact=True).wait_for(
                state="visible", timeout=30_000)
            patient.get_by_role("button", name="说完了可以点这里", exact=True).click()

            def next_command_is_visible() -> bool:
                entries = observations["next"]
                if not isinstance(entries, list) or len(entries) < 3:
                    return False
                first_tts, record, next_command = entries[0], entries[1], entries[2]
                ack_types = observations["ack_types"]
                authorizations = observations["record_authorizations"]
                tts = observations["tts"]
                assert isinstance(ack_types, dict)
                assert isinstance(authorizations, dict)
                assert isinstance(tts, dict)
                return (
                    first_tts[2] == "tts" and record[2] == "record"
                    and next_command[2] == "tts"
                    and len({first_tts[0], record[0], next_command[0]}) == 3
                    and first_tts[1] < record[1] < next_command[1]
                    and ack_types.get(first_tts[0]) == ["tts_started", "tts_ended"]
                    and ack_types.get(record[0]) == ["record_started", "record_stopped"]
                    and ack_types.get(next_command[0]) == ["tts_started"]
                    and authorizations.get(record[0]) == 2
                    and tts.get(first_tts[0]) == 1 and tts.get(next_command[0]) == 1
                    and int(observations["audio_posts"]) == 1
                    and int(observations["audio_uploads"]) == 1
                    and int(observations["audio_saved"]) == 1
                )

            _wait_for(
                next_command_is_visible,
                (caregiver, patient),
                timeout_seconds=45,
                label="真实录音上传、ASR/判定与下一条命令",
            )
            if violations:
                raise BrowserAcceptanceError(violations[0])

            observations["pause_requested"] = True
            caregiver.get_by_role("button", name="暂停练习", exact=True).click()
            caregiver.get_by_text("练习已暂停", exact=True).wait_for(
                state="visible", timeout=20_000)
            patient.get_by_text("练习已暂停，请稍候", exact=True).wait_for(
                state="visible", timeout=20_000)
            caregiver.get_by_text("暂停后不会自动重新开始", exact=True).wait_for(
                state="visible", timeout=20_000)
            for label in ("请求协助", "结束本次"):
                action = caregiver.get_by_role("button", name=label, exact=True)
                action.wait_for(state="visible", timeout=20_000)
                if not action.is_enabled():
                    raise BrowserAcceptanceError(f"安全暂停后“{label}”未开放")
            if caregiver.get_by_role("button", name="开始练习", exact=True).count() != 0 \
                    or caregiver.get_by_role("button", name="暂停练习", exact=True).count() != 0:
                raise BrowserAcceptanceError("安全暂停后仍留有开始或暂停入口")
            _wait_for(
                lambda: observations["drain_target_key"] is not None
                and observations["drain_target_key"] == observations["drain_ack_key"]
                and isinstance(observations["drain_target_revision"], int)
                and observations["drain_ack_revision"]
                    == int(observations["drain_target_revision"]) + 1
                and observations["drain_ack_replayed"] is False,
                (caregiver, patient),
                timeout_seconds=20,
                label="同一命令的物理收麦回执",
            )
            takeover = caregiver.get_by_role("button", name="接管练习", exact=True)
            takeover.wait_for(state="visible", timeout=20_000)
            _wait_for(
                takeover.is_enabled,
                (caregiver, patient),
                timeout_seconds=20,
                label="设备安全收尾后的接管入口",
            )
            entries = observations["next"]
            if not isinstance(entries, list) or len(entries) != 3:
                raise BrowserAcceptanceError("安全暂停前已出现未预期的后续命令")
            if (observations["drain_target_key"] != entries[2][0]
                    or observations["drain_ack_key"] != entries[2][0]):
                raise BrowserAcceptanceError("原生音频暂停与安全收麦不是同一条反馈命令")
            try:
                media_journal = patient.evaluate(
                    "() => window.__nmuNativeMediaJournal"
                    ".map((entry) => ({ ...entry }))")
            except Exception as exc:
                raise BrowserAcceptanceError("无法读取原生音频播放与暂停证据") from exc
            if not isinstance(media_journal, list):
                raise BrowserAcceptanceError("原生音频证据格式无效")
            playing = [entry for entry in media_journal
                       if isinstance(entry, dict) and entry.get("event") == "playing"]
            if len(playing) != 2 or playing[0].get("id") == playing[1].get("id"):
                raise BrowserAcceptanceError("浏览器没有精确播放首次问题和判定后反馈")
            first_media_id = playing[0].get("id")
            feedback_media_id = playing[1].get("id")
            first_ended = any(
                isinstance(entry, dict)
                and entry.get("event") == "ended"
                and entry.get("id") == first_media_id
                for entry in media_journal
            )
            feedback_ended = any(
                isinstance(entry, dict)
                and entry.get("event") == "ended"
                and entry.get("id") == feedback_media_id
                for entry in media_journal
            )
            feedback_paused = any(
                isinstance(entry, dict)
                and entry.get("event") == "pause_called"
                and entry.get("id") == feedback_media_id
                and entry.get("pausedBefore") is False
                and entry.get("pausedAfter") is True
                and entry.get("endedAfter") is False
                for entry in media_journal
            )
            if not first_ended or feedback_ended or not feedback_paused:
                raise BrowserAcceptanceError(
                    "浏览器没有证明第二段原生音频在结束前被安全暂停")
            if violations:
                raise BrowserAcceptanceError(violations[0])
            if not isinstance(observations["pause_runtime_revision"], int):
                raise BrowserAcceptanceError("安全暂停回执没有进入浏览器证据")
            observations["teardown_started"] = True
            patient_context.close()
            caregiver_context.close()
            browser.close()
            patient_context = None
            caregiver_context = None
            browser = None
            if violations:
                raise BrowserAcceptanceError(violations[0])
            result = BrowserResult(
                plan_id=str(observations["plan_id"]),
                session_id=str(observations["session_id"]),
                first_tts_command_key=entries[0][0],
                record_command_key=entries[1][0],
                next_command_key=entries[2][0],
                next_command_seq=entries[2][1],
                pause_runtime_revision=int(observations["pause_runtime_revision"]),
                drain_command_key=str(observations["drain_ack_key"]),
                drain_state_revision=int(observations["drain_ack_revision"]),
                native_audio_pause_observed=True,
            )
            _write_result_receipt(config, result)
            return result
    finally:
        if (caregiver is not None and bool(observations["autopilot_started"])
                and not bool(observations["pause_requested"])):
            try:
                pause = caregiver.get_by_role("button", name="暂停练习", exact=True)
                if pause.count() == 1 and pause.is_enabled():
                    observations["pause_requested"] = True
                    with caregiver.expect_response(
                        lambda response: (
                            response.request.method == "POST"
                            and urlsplit(response.url).path.endswith("/pause")
                        ),
                        timeout=3_000,
                    ):
                        pause.click()
            except Exception:
                pass
        observations["teardown_started"] = True
        for resource in (patient_context, caregiver_context, browser):
            if resource is not None:
                try:
                    resource.close()
                except Exception:
                    pass
        try:
            fake_audio.unlink(missing_ok=True)
        except OSError:
            pass


def _one(rows: list[object], message: str):
    if len(rows) != 1:
        raise BrowserAcceptanceError(message)
    return rows[0]


def _json_object(raw: str | None, message: str) -> dict:
    try:
        value = json.loads(raw or "")
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise BrowserAcceptanceError(message) from exc
    if not isinstance(value, dict):
        raise BrowserAcceptanceError(message)
    return value


def _ledger_harness_config():
    """Reapply the launcher's complete private-root contract before app import."""
    from harness import caregiver_demo_harness
    from harness.tts_ack_harness import HarnessConfigError

    try:
        return caregiver_demo_harness.resolve_caregiver_config()
    except HarnessConfigError as exc:
        raise BrowserAcceptanceError("账本核验的临时目录或运行配置无效") from exc


def read_ledger_snapshot() -> BrowserResult:
    """Read only the browser-produced anchor; database proof is separate below."""
    config = _ledger_harness_config()
    return _read_result_receipt(config.base.root)


def _validate_start_pause_database(config, result: BrowserResult) -> None:
    from sqlmodel import Session, select

    from app import (
        audio_store,
        autopilot_ledger,
        autopilot_service,
        db,
        provider_readiness,
        visit_plan_service,
    )
    from app.models import (
        AttemptCaptureProcessing,
        AttemptEvent,
        AudioAssetRow,
        AudioCaptureReceipt,
        AutopilotControlEvent,
        ItemEvent,
        LiveState,
        PatientDeviceCapability,
        ProviderReadinessProbe,
        ResearchUser,
        RuntimeCommand,
        RuntimeCommandAck,
        Session as TrainSession,
        SessionAutopilotState,
        SessionRuntimeState,
        TtsServeEvidence,
        TurnEvent,
        VisitPlan,
        VisitPlanCommand,
    )
    from harness import caregiver_demo_harness
    from harness.tts_ack_harness import (
        DeterministicHarnessAsrEngine,
        DeterministicHarnessTtsEngine,
    )

    try:
        caregiver_demo_harness._assert_imported_engine_binding(config.base, db)
        expected_provider = provider_readiness.capture_configuration(
            tts_engine=DeterministicHarnessTtsEngine(),
            asr_engine=DeterministicHarnessAsrEngine(),
        )
        expected_tts_version = expected_provider.tts_engine_version
    except Exception as exc:
        raise BrowserAcceptanceError("账本核验的临时数据库或合成语音档位不一致") from exc

    with Session(db.engine) as session, session.no_autoflush:
        caregiver = session.get(ResearchUser, config.caregiver_username)
        admin = session.get(ResearchUser, config.base.actor_username)
        if (
            caregiver is None
            or caregiver.role != "caregiver_operator"
            or caregiver.disabled is not False
            or not (caregiver.display_id or "").strip()
            or admin is None
            or admin.role != "admin"
            or admin.disabled is not False
            or not (admin.display_id or "").strip()
        ):
            raise BrowserAcceptanceError("账本没有证明本次两个临时账号的角色与状态")

        plan = session.get(VisitPlan, result.plan_id)
        train_session = session.get(TrainSession, result.session_id)
        if (
            plan is None
            or plan.status != "started"
            or plan.revision != 3
            or plan.started_by != caregiver.display_id
            or plan.started_at is None
            or plan.is_simulation is not True
            or plan.data_classification != "simulation"
            or train_session is None
            or train_session.visit_plan_id != plan.plan_id
            or train_session.patient_id != plan.patient_id
            or train_session.trainer_id != caregiver.display_id
            or train_session.is_simulation is not True
            or train_session.data_classification != "simulation"
        ):
            raise BrowserAcceptanceError("账本没有证明照护员已从该安排建立本次模拟场次")

        try:
            plan_receipt = visit_plan_service.receipt_for(session, plan)
            visit_plan_service.assert_started_profile_command_chain(
                session, plan, train_session)
        except visit_plan_service.VisitPlanError as exc:
            raise BrowserAcceptanceError(
                "账本无法重新证明计划的冻结定义与创建、审批、开场摘要") from exc
        if (
            plan_receipt.plan_id != result.plan_id
            or plan_receipt.session_id != result.session_id
            or plan_receipt.started_by != caregiver.display_id
        ):
            raise BrowserAcceptanceError("计划权威收据与浏览器开场锺点不一致")

        plan_commands = list(session.exec(
            select(VisitPlanCommand)
            .where(VisitPlanCommand.plan_id == plan.plan_id)
            .order_by(VisitPlanCommand.event_seq)
        ))
        if (
            [row.command_type for row in plan_commands] != ["create", "approve", "start"]
            or [row.event_seq for row in plan_commands] != [1, 2, 3]
            or [row.expected_revision for row in plan_commands] != [0, 1, 2]
            or [row.resulting_revision for row in plan_commands] != [1, 2, 3]
            or [row.actor_id for row in plan_commands]
            != [admin.display_id, admin.display_id, caregiver.display_id]
            or plan_commands[-1].created_at != plan.started_at
        ):
            raise BrowserAcceptanceError("账本没有证明该安排按创建、批准、照护员开场顺序推进")

        probe = session.exec(
            select(ProviderReadinessProbe)
            .where(ProviderReadinessProbe.checked_at <= plan.started_at)
            .order_by(
                ProviderReadinessProbe.checked_at.desc(),
                ProviderReadinessProbe.probe_id.desc(),
            )
        ).first()
        if (
            probe is None
            or probe.schema_version != provider_readiness.SCHEMA_VERSION
            or probe.runtime_contract != expected_provider.runtime_contract
            or probe.config_fingerprint != expected_provider.fingerprint
            or probe.actor_display_id != admin.display_id
            or probe.expires_at <= plan.started_at
            or probe.tts_engine_version != expected_tts_version
            or probe.asr_engine_version != expected_provider.asr_engine_version
            or probe.llm_engine_version != expected_provider.llm_engine_version
            or probe.tts_required is not True
            or probe.tts_success is not True
            or probe.tts_failure_code is not None
            or probe.asr_required is not True
            or probe.asr_success is not True
            or probe.asr_failure_code is not None
            or probe.llm_required is not False
            or probe.llm_configured is not False
            or probe.llm_success is not False
            or probe.llm_failure_code != "llm_not_required_not_configured"
            or probe.required_capabilities_ready is not True
            or probe.all_configured_capabilities_ready is not True
            or probe.probe_failure_code is not None
        ):
            raise BrowserAcceptanceError("账本没有证明开场时离线语音与识别能力已真实检查通过")

        capabilities = list(session.exec(select(PatientDeviceCapability).where(
            PatientDeviceCapability.session_id == result.session_id,
            PatientDeviceCapability.active_session_key == result.session_id,
            PatientDeviceCapability.revoked_at.is_(None),
        )))
        capability = _one(
            capabilities, "账本没有证明本场次只有一个活跃的老人端")
        if capability.recovery_only_at is not None:
            raise BrowserAcceptanceError("账本中的老人端已不是活跃设备")

        def command_by_key(key: str, label: str):
            rows = list(session.exec(select(RuntimeCommand).where(
                RuntimeCommand.session_id == result.session_id,
                RuntimeCommand.idempotency_key == key,
            )))
            return _one(rows, f"账本缺少本次{label}命令")

        first_tts = command_by_key(result.first_tts_command_key, "首次朗读")
        record = command_by_key(result.record_command_key, "录音")
        feedback = command_by_key(result.next_command_key, "判定后反馈")
        drain_command = command_by_key(result.drain_command_key, "安全收麦")

        issued_identity = (
            "session_id", "item_id", "turn_seq", "turn_key", "attempt_seq",
            "prompt_level", "scope_key", "control_generation", "runner_generation",
        )
        try:
            first_payload = autopilot_service.TtsCommandPayload.model_validate_json(
                first_tts.payload_json)
            feedback_payload = autopilot_service.TtsCommandPayload.model_validate_json(
                feedback.payload_json)
        except (TypeError, ValueError) as exc:
            raise BrowserAcceptanceError("账本中的朗读或反馈命令格式无效") from exc
        if (
            first_tts.kind != "tts"
            or first_tts.command_seq != 1
            or first_tts.state != "succeeded"
            or first_tts.succeeded_at is None
            or first_payload.purpose != "question"
            or record.kind != "record"
            or record.command_seq != 2
            or record.state != "succeeded"
            or record.succeeded_at is None
            or record.predecessor_command_id != first_tts.id
            or feedback.kind != "tts"
            or feedback.command_seq != result.next_command_seq
            or feedback.command_seq != record.command_seq + 1
            or feedback_payload.purpose != "feedback"
            or feedback.state not in {"pending", "started", "succeeded"}
            or result.drain_command_key != result.next_command_key
            or drain_command.id != feedback.id
            or any(getattr(feedback, name) != getattr(record, name)
                   for name in issued_identity)
            or drain_command.scope_key != first_tts.scope_key
            or drain_command.control_generation != first_tts.control_generation
            or drain_command.runner_generation != first_tts.runner_generation
            or any(getattr(first_tts, name) != getattr(record, name)
                   for name in issued_identity)
            or any(getattr(command, "issued_capability_token_hash") != capability.token_hash
                   or getattr(command, "issued_device_id_hash") != capability.device_id_hash
                   for command in (first_tts, record, feedback, drain_command))
        ):
            raise BrowserAcceptanceError("账本的首次朗读、录音和判定后反馈顺序不完整")

        controls = list(session.exec(
            select(AutopilotControlEvent)
            .where(AutopilotControlEvent.session_id == result.session_id)
            .order_by(AutopilotControlEvent.event_seq)
        ))
        start = _one(
            [row for row in controls if row.event_type == "start"],
            "账本缺少本次照护员启动事件",
        )
        pause = _one(
            [row for row in controls if row.event_type == "pause"],
            "账本缺少本次照护员安全暂停事件",
        )
        drain = _one(
            [row for row in controls if row.event_type == "drain_complete"],
            "账本缺少本次老人端物理收麦事件",
        )
        if any(row.event_type == "failure" for row in controls):
            raise BrowserAcceptanceError("本次自动流程账本中出现了失败事件")
        if (
            start.command_id != first_tts.id
            or start.actor_type != "researcher"
            or start.actor_id != caregiver.display_id
            or start.from_mode != "disabled"
            or start.to_mode != "autonomous"
            or start.from_status != "idle"
            or start.to_status != "waiting_tts"
            or start.payload_json != autopilot_ledger.encode_control_event_payload(
                "start", {"source": autopilot_service.P0A_SOURCE})
            or pause.command_id != drain_command.id
            or pause.actor_type != "researcher"
            or pause.actor_id != caregiver.display_id
            or pause.reason_code != "researcher_requested_pause"
            or pause.to_mode != "autonomous"
            or pause.to_status != "paused"
            or pause.payload_json != autopilot_ledger.encode_control_event_payload(
                "pause", {
                    "reason_code": "researcher_requested_pause",
                    "source": "account_pause_endpoint",
                })
            or drain.command_id != drain_command.id
            or drain.actor_type != "device"
            or drain.actor_id != capability.device_id_hash
            or drain.reason_code != "device_media_drained"
            or drain.from_status != "paused"
            or drain.to_status != "paused"
            or drain.payload_json != autopilot_ledger.encode_control_event_payload(
                "drain_complete", {"drained_command_id": drain_command.id})
            or not (start.event_seq < pause.event_seq < drain.event_seq)
            or not (
                plan.started_at <= capability.created_at <= first_tts.issued_at
                <= start.created_at <= feedback.issued_at
                <= drain_command.issued_at <= pause.created_at
                <= drain.created_at < capability.expires_at
            )
            or controls[-1].id != drain.id
        ):
            raise BrowserAcceptanceError("账本的启动、暂停与收麦控制链不完整")

        def exact_ack_types(command, expected: list[str], label: str):
            rows = list(session.exec(
                select(RuntimeCommandAck)
                .where(RuntimeCommandAck.command_id == command.id)
                .order_by(RuntimeCommandAck.device_event_seq)
            ))
            if [row.ack_type for row in rows] != expected:
                raise BrowserAcceptanceError(f"账本的{label}设备回执不完整")
            if any(
                row.session_id != result.session_id
                or row.capability_token_hash != capability.token_hash
                or row.device_id_hash != capability.device_id_hash
                for row in rows
            ):
                raise BrowserAcceptanceError(f"账本的{label}回执不属于本次老人端")
            return rows

        first_tts_acks = exact_ack_types(
            first_tts, ["tts_started", "tts_ended"], "首次朗读")
        record_acks = exact_ack_types(
            record, ["record_started", "record_stopped"], "录音")
        feedback_acks = exact_ack_types(
            feedback, ["tts_started"], "判定后反馈开始播放")
        if not (
            first_tts_acks[0].device_event_seq
            < first_tts_acks[1].device_event_seq
            < record_acks[0].device_event_seq
            < record_acks[1].device_event_seq
            < feedback_acks[0].device_event_seq
        ):
            raise BrowserAcceptanceError("账本中朗读、录音与反馈的设备顺序不完整")

        def exact_tts_serve(command, label: str):
            rows = list(session.exec(select(TtsServeEvidence).where(
                TtsServeEvidence.session_id == result.session_id,
                TtsServeEvidence.command_id == command.id,
            )))
            if not rows or any(
                row.source != "autopilot_command"
                or row.result != "served"
                or row.engine_version != expected_tts_version
                or row.byte_count is None
                or row.byte_count <= 44
                or row.is_simulation is not True
                or row.text_sha256 != hashlib.sha256(
                    autopilot_service.TtsCommandPayload.model_validate_json(
                        command.payload_json).speech_text.encode("utf-8")
                ).hexdigest()
                for row in rows
            ):
                raise BrowserAcceptanceError(f"账本的{label}音频使用证据不完整")
            return rows

        exact_tts_serve(first_tts, "首次朗读")
        exact_tts_serve(feedback, "判定后反馈")

        try:
            proof = autopilot_ledger.verify_terminal_record_capture(
                session, record.id)
        except autopilot_ledger.AutopilotProofError as exc:
            raise BrowserAcceptanceError("账本无法重新证明录音命令、回执与收据的完整链") from exc

        stopped = record_acks[-1]
        receipt = session.get(AudioCaptureReceipt, proof.receipt_server_seq)
        asset = session.get(AudioAssetRow, proof.raw_audio_id)
        if (
            receipt is None
            or asset is None
            or stopped.receipt_server_seq != receipt.server_seq
            or stopped.raw_audio_id != receipt.raw_audio_id
            or stopped.checksum != receipt.checksum
            or stopped.byte_count != receipt.byte_count
            or stopped.duration_seconds != receipt.duration_seconds
            or receipt.session_id != result.session_id
            or receipt.turn_key != record.turn_key
            or receipt.data_classification != "simulation"
            or receipt.is_simulation is not True
            or asset.session_id != result.session_id
            or asset.turn_key != record.turn_key
            or asset.checksum != receipt.checksum
            or asset.byte_count != receipt.byte_count
            or asset.uploaded_at is None
            or asset.is_simulation is not True
            or asset.data_classification != "simulation"
            or asset.withdrawn is not False
            or asset.delete_gate_passed is not False
            or str(getattr(asset.status, "value", asset.status)) != "recorded"
        ):
            raise BrowserAcceptanceError("账本的录音资产、上传收据与停止回执不一致")
        try:
            blob = audio_store.blob_facts(
                proof.raw_audio_id, max_bytes=audio_store.MAX_AUDIO_BLOB_BYTES)
            blob_path = blob.path.resolve(strict=True) if blob is not None else None
        except (OSError, ValueError, audio_store.AudioStoreIntegrityError) as exc:
            raise BrowserAcceptanceError("本次录音原件的私有存储完整性异常") from exc
        if (
            blob is None
            or blob_path is None
            or blob_path.parent != config.base.audio_dir
            or not blob_path.is_relative_to(config.base.root)
            or blob.checksum != receipt.checksum
            or blob.byte_count != receipt.byte_count
        ):
            raise BrowserAcceptanceError("本次录音原件与上传账本不一致")

        captures = list(session.exec(select(AttemptCaptureProcessing).where(
            AttemptCaptureProcessing.record_command_id == record.id,
        )))
        capture = _one(captures, "账本缺少该录音唯一的识别处理链")
        attempt = (
            session.get(AttemptEvent, capture.final_attempt_id)
            if capture.final_attempt_id is not None else None
        )
        if (
            capture.session_id != result.session_id
            or capture.predecessor_command_id != first_tts.id
            or capture.receipt_server_seq != receipt.server_seq
            or capture.raw_audio_id != receipt.raw_audio_id
            or capture.item_id != record.item_id
            or capture.turn_seq != record.turn_seq
            or capture.proof_attempt_seq != record.attempt_seq
            or capture.proof_prompt_level != record.prompt_level
            or capture.processing_status != "asr_completed"
            or capture.disposition != "answer_candidate"
            or capture.processed_at is None
            or capture.error_code is not None
            or capture.processing_owner is not None
            or capture.processing_lease_expires_at is not None
            or attempt is None
            or attempt.session_id != result.session_id
            or attempt.raw_audio_id != receipt.raw_audio_id
            or attempt.item_id != record.item_id
            or attempt.turn_seq != record.turn_seq
            or attempt.attempt_seq != record.attempt_seq
            or attempt.prompt_level != record.prompt_level
            or attempt.processing_status != "completed"
            or attempt.processed_at is None
            or attempt.error_code is not None
            or not (attempt.asr_text or "").strip()
            or attempt.asr_engine_version != DeterministicHarnessAsrEngine.version
            or capture.asr_engine_version != attempt.asr_engine_version
            or capture.asr_confidence != attempt.asr_confidence
            or not (attempt.judge_engine_version or "").strip()
            or attempt.is_simulation is not True
            or attempt.judge_portrait_used is not False
            or attempt.asr_confidence != 1.0
            or attempt.contains_target is not True
            or attempt.operational_answer_type != "正确"
            or attempt.operational_score != 1.0
            or attempt.operational_needs_review is not False
            or capture.repeat_admission_semantics != "repeat_bound"
            or capture.repeat_protocol_version_id
                != train_session.repeat_protocol_version_id
            or capture.repeat_protocol_definition_digest
                != train_session.repeat_protocol_definition_digest
            or capture.repeat_protocol_version_id != record.repeat_protocol_version_id
            or capture.repeat_protocol_definition_digest
                != record.repeat_protocol_definition_digest
            or capture.repeat_request_id is not None
        ):
            raise BrowserAcceptanceError("账本没有证明该录音已完成识别与自动判定")
        try:
            expected_attempt = autopilot_service.legacy_expected_attempt_facts(
                session, record=record, capture=capture)
        except autopilot_service.AutopilotServiceError as exc:
            raise BrowserAcceptanceError("无法从冻结计划重新推导该次回答的权威事实") from exc
        if (
            not autopilot_service.legacy_attempt_matches_expected_facts(
                attempt, expected_attempt)
            or not autopilot_service.legacy_attempt_is_successfully_judged(
                session, attempt)
        ):
            raise BrowserAcceptanceError("账本的接收、识别与判定交互证据不合法")

        items = list(session.exec(select(ItemEvent).where(
            ItemEvent.session_id == result.session_id,
            ItemEvent.item_id == attempt.item_id,
        )))
        item = _one(items, "账本缺少该判定对应的唯一题位证据")
        turns = list(session.exec(select(TurnEvent).where(
            TurnEvent.item_event_id == item.id,
            TurnEvent.turn_seq == attempt.turn_seq,
        )))
        turn = _one(turns, "账本缺少该判定对应的唯一环节证据")
        if (
            turn.source_attempt_id != attempt.id
            or turn.raw_audio_id != attempt.raw_audio_id
            or turn.asr_text != attempt.asr_text
            or turn.asr_confidence != attempt.asr_confidence
            or turn.duration_seconds != attempt.duration_seconds
            or turn.prompt_level != attempt.prompt_level
            or turn.response_role != attempt.response_role
            or turn.ai_answer_type != attempt.operational_answer_type
            or turn.ai_score != attempt.operational_score
            or turn.ai_needs_review != attempt.operational_needs_review
            or turn.ai_judge_mode != attempt.judge_mode
            or turn.confirmed_response_text is not None
            or turn.reviewer_id is not None
            or turn.score_locked is not False
        ):
            raise BrowserAcceptanceError("账本的题位、环节与该次自动判定不一致")

        autopilot = session.get(SessionAutopilotState, result.session_id)
        runtime = session.get(SessionRuntimeState, result.session_id)
        live = session.get(LiveState, 1)
        session_live = _json_object(
            live.session_json if live is not None else None,
            "床旁实时状态缺少本场次暂停投影",
        )
        audio_live = _json_object(
            live.audio_json if live is not None else None,
            "床旁实时状态缺少本次录音上传回报",
        )
        try:
            status_receipt = autopilot_service.get_autopilot_status(
                session, session_id=result.session_id)
        except autopilot_service.AutopilotServiceError as exc:
            raise BrowserAcceptanceError("无法重新投影安全暂停后的控制权收据") from exc
        if (
            autopilot is None
            or autopilot.scope_key != autopilot_service.P0A_SCOPE_KEY
            or autopilot.mode != "autonomous"
            or autopilot.status != "paused"
            or autopilot.control_generation != drain_command.control_generation
            or autopilot.runner_generation != drain_command.runner_generation
            or autopilot.current_command_id is not None
            or autopilot.next_command_seq != drain_command.command_seq + 1
            or autopilot.lease_owner is not None
            or autopilot.lease_acquired_at is not None
            or autopilot.lease_expires_at is not None
            or autopilot.last_error_code is not None
            or autopilot.revision != result.drain_state_revision
            or runtime is None
            or runtime.status != "paused"
            or runtime.paused_at is None
            or runtime.revision != result.pause_runtime_revision
            or runtime.intervention_completed_at is not None
            or runtime.intervention_ended_by is not None
            or runtime.completed_at is not None
            or runtime.aborted_at is not None
            or runtime.ended_by is not None
            or runtime.end_reason is not None
            or capability.revoked_at is not None
            or capability.recovery_only_at is not None
            or live is None
            or session_live.get("sessionId") != result.session_id
            or session_live.get("paused") is not True
            or audio_live.get("sessionId") != result.session_id
            or audio_live.get("rawAudioId") != receipt.raw_audio_id
            or audio_live.get("turnKey") != receipt.turn_key
            or audio_live.get("byteCount") != receipt.byte_count
            or str(audio_live.get("checksum") or "").lower() != receipt.checksum.lower()
            or audio_live.get("durationSeconds") != receipt.duration_seconds
            or status_receipt.scope_key != autopilot_service.P0A_SCOPE_KEY
            or status_receipt.mode != "autonomous"
            or status_receipt.status != "paused"
            or status_receipt.state_revision != result.drain_state_revision
            or status_receipt.server_owned is not True
            or status_receipt.takeover_ready is not True
            or status_receipt.current_command_kind is not None
            or status_receipt.last_error_code is not None
        ):
            raise BrowserAcceptanceError("账本没有同时证明自动流程、场次和老人画面已安全暂停")


def validate_start_pause_ledger(snapshot: BrowserResult | None = None) -> None:
    config = _ledger_harness_config()
    result = snapshot or _read_result_receipt(config.base.root)
    _validate_start_pause_database(config, result)


def _redacted_error(error: BaseException, secrets: tuple[str, ...]) -> str:
    message = str(error).strip() or error.__class__.__name__
    for secret in secrets:
        if secret:
            message = message.replace(secret, "[已隐藏]")
    return message[:500]


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="照护员真实 Chrome 本机验收")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--start-pause", action="store_true")
    group.add_argument("--verify-ledger", action="store_true")
    parser.add_argument("--origin")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    secrets = tuple(
        os.environ.get(name) or ""
        for name in (CAREGIVER_PASSWORD_ENV, CONSOLE_PIN_ENV, INSTANCE_MARKER_ENV)
    )
    try:
        if args.start_pause:
            if not args.origin:
                raise BrowserAcceptanceError("真实 Chrome 验收缺少本机地址")
            config = resolve_browser_config(args.origin)
            run_start_pause(config)
            print("真实 Chrome 流程已完成：开始、配对、朗读、录音、上传、ASR/判定、下一命令、安全暂停")
        else:
            if args.origin:
                raise BrowserAcceptanceError("账本核验不接受网页地址")
            validate_start_pause_ledger(read_ledger_snapshot())
            print("同一临时库账本核验已通过")
        return 0
    except BrowserAcceptanceError as exc:
        print(f"真实 Chrome 验收失败：{_redacted_error(exc, secrets)}", file=sys.stderr)
        return 1
    except Exception as exc:  # fail closed without leaking browser call values
        print(f"真实 Chrome 验收失败：{_redacted_error(exc, secrets)}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
