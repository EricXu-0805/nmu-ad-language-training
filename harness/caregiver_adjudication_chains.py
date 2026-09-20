#!/usr/bin/env python3
"""照护员本机演练:裁定/续弹链的真实 Chrome 走查(收据 260 第一批的浏览器证据)。

收据 260 上线的四件事(题内暂停后续弹、研究者现场裁定、「AI 听到的」面板、题号按
presentation_order)之前只有 HTTP 级与单测证据,这里在真 Chrome 里点一遍:

  链 A  题内暂停后续弹:第一题录完一次(伪麦克风是 330Hz 正弦、ASR 关着 → 判「沉默」),
        AI 下发第 1 级线索;线索话术放到一半照护员点「暂停练习」→ 收麦回执 → 管理员在
        训练台点「继续 AI 自动带练」→ 服务端续弹的是**同一题同一轮**的那条线索
        (purpose=cue / prompt_level=1 / attempt_seq=2),不是 409
        autopilot_resume_position_unresumable;线索放完照常开第二次麦。
  链 B  老人已答对:第二次录完、AI 再下发话术时暂停 → 点「老人已答对」(原因「识别错了，
        其实答对了」+ 备注「走查」)→ 200,题位推进到第 2 题;journal 里 attempt 行的
        AI 判类一字不改,TurnEvent 的 ai_answer_type 就是最后一次 attempt 的判类。
  链 D  「AI 听到的」面板:暂停后卡片里有「识别：没有识别到语音」和一行 AI 判类
        (不是「判分中…」)。
  链 E  题号:训练台页首「当前训练任务 · …（总第 N 题）」的 N 跟着冻结计划的
        presentation_order,第 1 题、第 2 题各看一次。
  链 F  无回答拒「答对」:第 2 题问句放到一半就暂停(一次录音都没有)→ 点「老人已答对」
        → 卡片常驻「裁定未记录：这一题还没有录到老人的回答…」(409
        autopilot_adjudication_attempt_required),标题仍是安全暂停、不折成 uncertain。
  链 C  同一题位再点「跳过本题」(原因「老人不愿意答这题」)→ 200,题位推进到第 3 题;
        账本里是 kind=skipped 的裁定行(没有录音就跳过,不造 TurnEvent)。

与 caregiver_recovery_chains.py 同一套沙箱(临时目录、实例标记、只认本机地址),管理员
HTTP 侧动作直接复用它的 _AdminApi。跑法见 scripts/run-caregiver-demo20.sh
--browser-check adjudication-chains。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import stat
import sys
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

# 启动器用 python -I 跑本文件,同目录不在 sys.path;只把仓库根加进去,复用同目录的沙箱工具。
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from harness.caregiver_browser_acceptance import (  # noqa: E402
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
from harness.caregiver_recovery_chains import (  # noqa: E402
    ADMIN_PASSWORD_ENV,
    PATIENT_ID,
    _AdminApi,
    _admin_credentials,
)

RESULT_RECEIPT_NAME = "browser-adjudication-chains-result.json"
RECEIPT_SCHEMA = "caregiver-browser-adjudication-chains.v1"
CONFIRMED_REASON_LABEL = "识别错了，其实答对了"
NO_ANSWER_REASON_LABEL = "研究者在场判定答对"
SKIP_REASON_LABEL = "老人不愿意答这题"
ADJUDICATION_NOTE = "走查"
NOTE_FIELD_LABEL = "备注（可不填，200 字以内）"
ATTEMPT_REQUIRED_CODE = "autopilot_adjudication_attempt_required"
ATTEMPT_REQUIRED_HINT = "裁定未记录：这一题还没有录到老人的回答"
PAUSED_TITLE = "AI 自动带练已安全暂停"
UNCERTAIN_TITLE = "服务器状态待核实"


@dataclass(frozen=True)
class AdjudicationChainsResult:
    session_id: str
    plan_id: str
    adjudicated_by: str
    first_item_id: str
    second_item_id: str
    third_item_id: str
    first_cue_command_key: str
    resumed_cue_command_key: str
    resume_expected_revision: str
    resume_receipt_status: str
    resumed_next_purpose: str
    resumed_next_prompt_level: str
    resumed_next_attempt_seq: str
    confirmed_correct_revision: str
    confirmed_correct_position_item_id: str
    attempt_required_code: str
    skip_revision: str
    skip_position_item_id: str
    ai_verdict_first_item: str
    heard_panel_line: str
    header_label_first: str
    header_label_second: str


def _write_receipt(config: BrowserAcceptanceConfig, result: AdjudicationChainsResult) -> None:
    target = config.harness_root / RESULT_RECEIPT_NAME
    if target.exists() or target.is_symlink():
        raise BrowserAcceptanceError("裁定链走查结果收据已存在,拒绝覆盖")
    payload = json.dumps({"schema": RECEIPT_SCHEMA, **result.__dict__},
                         sort_keys=True, separators=(",", ":"))
    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(payload)


def _read_receipt(root: Path) -> AdjudicationChainsResult:
    target = root / RESULT_RECEIPT_NAME
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(target, flags)
    except OSError as exc:
        raise BrowserAcceptanceError("缺少本次裁定链走查结果收据") from exc
    try:
        facts = os.fstat(fd)
        if (not stat.S_ISREG(facts.st_mode) or stat.S_IMODE(facts.st_mode) != 0o600
                or facts.st_uid != os.getuid() or facts.st_nlink != 1
                or facts.st_size <= 0 or facts.st_size > 8_192):
            raise BrowserAcceptanceError("裁定链走查结果收据不是本用户的私密普通文件")
        with os.fdopen(fd, "r", encoding="utf-8") as handle:
            fd = -1
            value = json.load(handle)
    finally:
        if fd >= 0:
            os.close(fd)
    if not isinstance(value, dict) or value.get("schema") != RECEIPT_SCHEMA:
        raise BrowserAcceptanceError("裁定链走查结果收据格式无效")
    fields = {name: value.get(name) for name in AdjudicationChainsResult.__dataclass_fields__}
    if any(not isinstance(item, str) or not item for item in fields.values()):
        raise BrowserAcceptanceError("裁定链走查结果收据缺少字段")
    return AdjudicationChainsResult(**fields)


def run_adjudication_chains(config: BrowserAcceptanceConfig) -> AdjudicationChainsResult:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise BrowserAcceptanceError("本机没有可用的 Python Playwright;已停止,不会自动安装") from exc

    admin_username, admin_password = _admin_credentials()
    fake_audio = config.harness_root / "tmp" / "browser-fake-microphone-adjudication.wav"
    fake_audio.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    _write_fake_microphone_wav(fake_audio)
    violations: list[str] = []
    obs: dict[str, object] = {
        "logged_in": {"caregiver": False, "admin": False},
        "session_id": None,
        "plan_id": None,
        "attached_session_ids": [],
        "autopilot_started": False,
        # True=已请求暂停;"resuming"=恢复/裁定已 200、平板还没拿到新命令;False=带练中。
        "runtime_paused": False,
        "expect_attempt_required": False,
        "teardown_started": False,
        "commands": {},
        "command_order": [],
        "ack_types": {},
        "audio_posts": {},
        "audio_uploads": [],
        "resumes": [],
        "adjudications": [],
        "pause_posts": 0,
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

    def json_detail(response) -> dict | None:
        try:
            value = response.json()
        except Exception:
            return None
        if isinstance(value, dict) and isinstance(value.get("detail"), dict):
            return value["detail"]
        return None

    def posted_json(request) -> dict | None:
        try:
            posted = request.post_data_json
        except Exception:
            return None
        return posted if isinstance(posted, dict) else None

    def adjudicate_path() -> str | None:
        session_id = obs["session_id"]
        return f"/sessions/{session_id}/autopilot/adjudicate" if isinstance(session_id, str) else None

    def expected_failure(tag: str, request, path: str, status: int, response) -> bool:
        code = _stable_error_code(response) if status >= 400 else None
        logged_in = obs["logged_in"]
        assert isinstance(logged_in, dict)
        if request.method == "GET" and path == "/auth/me":
            return not logged_in.get(tag) and status == 401 and code == "auth_me_not_logged_in"
        if request.method == "GET" and path.endswith("/autopilot/next"):
            if obs["runtime_paused"]:
                return status == 409 and code == "autopilot_runtime_inactive"
            return not obs["autopilot_started"] and status == 409 and code == "autopilot_not_active"
        if (request.method == "POST" and path == adjudicate_path() and status == 409
                and code == ATTEMPT_REQUIRED_CODE and bool(obs["expect_attempt_required"])):
            # 链 F 唯一允许的一次写前拒绝:服务端明确未写入,卡片应常驻提示。
            obs["expect_attempt_required"] = False
            rows = obs["adjudications"]
            assert isinstance(rows, list)
            rows.append({"request": posted_json(request), "status": 409, "code": code})
            return True
        return False

    def register_command(payload) -> None:
        # 平板拿命令有两条路:GET /autopilot/next(话术、以及旧口径的录音),以及
        # tts_ended ACK 回执体里直接带下来的录音命令(合入平板分支后 tts→record 不再
        # 走 /next)。两处形状同为 NextCommandProjection,这里统一登记一次。
        if not isinstance(payload, dict) or not isinstance(payload.get("command_key"), str):
            return
        commands = obs["commands"]
        order = obs["command_order"]
        assert isinstance(commands, dict) and isinstance(order, list)
        key = payload["command_key"]
        if key in commands:
            return
        inner = payload.get("payload") if isinstance(payload.get("payload"), dict) else {}
        commands[key] = {
            "command_key": key,
            "command_seq": payload.get("command_seq"),
            "kind": payload.get("kind"),
            "item_ref": payload.get("item_ref"),
            "turn_seq": payload.get("turn_seq"),
            "attempt_seq": payload.get("attempt_seq"),
            "prompt_level": payload.get("prompt_level"),
            "purpose": inner.get("purpose"),
        }
        order.append(key)

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
            posted = posted_json(request)
            if posted and isinstance(posted.get("kind"), str):
                kind = f" kind={posted['kind']}"
            violation(f"[{tag}] 非预期 HTTP {response.status} {request.method} {parsed.path} ({code}){kind}")
        logged_in = obs["logged_in"]
        assert isinstance(logged_in, dict)
        if request.method == "POST" and parsed.path == "/auth/login" and response.status == 200:
            logged_in[tag] = True
        if (request.method == "POST" and parsed.path.startswith("/caregiver/visit-plans/")
                and parsed.path.endswith("/start") and response.status == 200):
            try:
                payload = response.json()
                obs["plan_id"] = payload["plan_id"]
                obs["session_id"] = payload["session"]["session_id"]
            except Exception:
                violation("开始本次回执缺少完整计划或场次标识")
        if response.status == 200 and parsed.path in {"/device/pair", "/device/attach"}:
            try:
                attached = obs["attached_session_ids"]
                assert isinstance(attached, list)
                attached.append(response.json()["sessionId"])
            except Exception:
                violation("设备配对/续接回执缺少场次标识")
        if response.status == 200 and parsed.path.endswith("/autopilot/next"):
            try:
                payload = response.json()
                if isinstance(payload, dict) and isinstance(payload.get("command_key"), str):
                    register_command(payload)
                    if obs["runtime_paused"] == "resuming":
                        obs["runtime_paused"] = False
            except Exception:
                violation("下一条命令响应无法校验")
        if request.method == "POST" and parsed.path == "/audio" and response.status == 200:
            posted = posted_json(request)
            raw_id = posted.get("raw_audio_id") if posted else None
            posts = obs["audio_posts"]
            assert isinstance(posts, dict)
            posts[str(raw_id)] = int(posts.get(str(raw_id), 0)) + 1
        if (request.method == "PUT" and parsed.path.startswith("/audio/")
                and parsed.path.endswith("/blob") and response.status == 200):
            uploads = obs["audio_uploads"]
            assert isinstance(uploads, list)
            uploads.append(parsed.path.split("/")[-2])
        if request.method == "POST" and parsed.path.endswith("/acks") and response.status == 200:
            posted = posted_json(request)
            ack_type = posted.get("ack_type") if posted else None
            key = parsed.path.split("/")[-2]
            if isinstance(ack_type, str):
                ack_types = obs["ack_types"]
                assert isinstance(ack_types, dict)
                ack_types.setdefault(key, []).append(ack_type)
            else:
                violation("设备命令 ACK 无法校验")
            # 合入平板分支后 tts_ended 的回执体直接带下来后续录音命令,平板不再为它
            # 调 /autopilot/next;这里同样登记,否则等录音命令会一直等不到。
            try:
                register_command(response.json().get("command"))
            except Exception:
                violation("命令 ACK 回执体里的后续命令无法校验")
        session_id = obs["session_id"]
        if request.method == "POST" and isinstance(session_id, str) and response.status == 200:
            if parsed.path == f"/sessions/{session_id}/autopilot/start":
                obs["autopilot_started"] = True
            elif parsed.path == f"/sessions/{session_id}/pause":
                obs["pause_posts"] = int(obs["pause_posts"]) + 1
            elif parsed.path == f"/sessions/{session_id}/autopilot/resume":
                try:
                    rows = obs["resumes"]
                    assert isinstance(rows, list)
                    rows.append({"request": posted_json(request), "response": response.json()})
                    obs["runtime_paused"] = "resuming"
                except Exception:
                    violation("恢复回执无法校验")
            elif parsed.path == f"/sessions/{session_id}/autopilot/adjudicate":
                try:
                    rows = obs["adjudications"]
                    assert isinstance(rows, list)
                    rows.append({"request": posted_json(request), "status": 200, "response": response.json()})
                    obs["runtime_paused"] = "resuming"
                except Exception:
                    violation("裁定回执无法校验")

    def observe_request_failure(request) -> None:
        if bool(obs["teardown_started"]):
            return
        parsed = urlsplit(request.url)
        violation(f"浏览器网络请求未完成 {request.method} {parsed.path}")

    def attach_page(tag: str, page) -> None:
        page.on("pageerror", lambda _error: violation(f"[{tag}] 页面运行出错"))

        def observe_console(message) -> None:
            if message.type != "error":
                return
            text = message.text
            if text.startswith("Failed to load resource: the server responded with a status of"):
                return
            violation(f"[{tag}] 页面控制台报错: {text[:200]}")

        page.on("console", observe_console)
        page.on("response", lambda response: observe_response(tag, response))
        page.on("requestfailed", observe_request_failure)

    debug_on = bool(os.environ.get("NMU_ADJ_DEBUG"))

    def acks(key: str) -> list[str]:
        ack_types = obs["ack_types"]
        assert isinstance(ack_types, dict)
        return list(ack_types.get(key, []))

    def dump_debug() -> None:
        if not debug_on:
            return
        table = obs["commands"]
        order = obs["command_order"]
        assert isinstance(table, dict) and isinstance(order, list)
        print("--- 命令序列(调试)---", file=sys.stderr)
        for key in order:
            command = table[key]
            print(f"  seq={command['command_seq']} {command['kind']}/{command['purpose']}"
                  f" {command['item_ref']}#{command['turn_seq']} L{command['prompt_level']}"
                  f" A{command['attempt_seq']} acks={acks(key)}", file=sys.stderr)
        print(f"--- resumes={len(obs['resumes'])} adjudications="  # type: ignore[arg-type]
              f"{[(r.get('status'), r.get('code')) for r in obs['adjudications']]} ---",  # type: ignore[union-attr]
              file=sys.stderr)

    def commands() -> list[dict]:
        table = obs["commands"]
        order = obs["command_order"]
        assert isinstance(table, dict) and isinstance(order, list)
        return [table[key] for key in order]

    def started_tts_after(min_seq: int, *, purposes: set[str]) -> dict | None:
        for command in commands():
            if (command["kind"] == "tts" and isinstance(command["command_seq"], int)
                    and command["command_seq"] > min_seq and command["purpose"] in purposes
                    and "tts_started" in acks(command["command_key"])):
                return command
        return None

    def started_record_after(min_seq: int) -> dict | None:
        for command in commands():
            if (command["kind"] == "record" and isinstance(command["command_seq"], int)
                    and command["command_seq"] > min_seq
                    and "record_started" in acks(command["command_key"])):
                return command
        return None

    def raise_if_violated() -> None:
        if violations:
            raise BrowserAcceptanceError(violations[0])

    def same_position(left: dict, right: dict) -> bool:
        return (left["item_ref"], left["turn_seq"]) == (right["item_ref"], right["turn_seq"])

    def describe(command: dict) -> str:
        return (f"{command['kind']}/{command['purpose']} {command['item_ref']}#{command['turn_seq']}"
                f" level={command['prompt_level']} attempt={command['attempt_seq']}")

    browser = None
    contexts: list[object] = []
    admin_api: _AdminApi | None = None
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
            caregiver = caregiver_context.new_page()
            patient = patient_context.new_page()
            admin_page = admin_context.new_page()
            attach_page("caregiver", caregiver)
            attach_page("patient", patient)
            attach_page("admin", admin_page)
            admin_api = _AdminApi(playwright, config, admin_username, admin_password)

            def status_receipt() -> dict:
                return admin_api.get(f"/sessions/{session_id}/autopilot/status", "读自动带练状态")

            def paused_receipt() -> dict | None:
                receipt = status_receipt()
                if (receipt.get("status") == "paused" and receipt.get("mode") == "autonomous"
                        and receipt.get("server_owned") is True and receipt.get("takeover_ready") is True):
                    return receipt
                return None

            def journal() -> dict:
                return admin_api.get(f"/sessions/{session_id}/journal", "读场次日志")

            def attempts_at(item_id: str) -> list[dict]:
                rows = journal().get("attempts")
                assert isinstance(rows, list)
                return sorted((row for row in rows if row.get("item_id") == item_id and row.get("turn_seq") == 1),
                              key=lambda row: int(row["attempt_seq"]))

            def pause_from_caregiver(label: str) -> dict:
                pause_button = caregiver.get_by_role("button", name="暂停练习", exact=True)
                pause_button.wait_for(state="visible", timeout=20_000)
                _wait_for(pause_button.is_enabled, (caregiver,), timeout_seconds=20, label=f"{label}前「暂停练习」可点")
                obs["runtime_paused"] = True
                pause_button.click()
                caregiver.get_by_text("练习已暂停", exact=True).wait_for(state="visible", timeout=20_000)
                patient.get_by_text("练习已暂停，请稍候", exact=True).wait_for(state="visible", timeout=20_000)
                holder: dict[str, dict] = {}

                def settled() -> bool:
                    receipt = paused_receipt()
                    if receipt is None:
                        return False
                    holder["receipt"] = receipt
                    return True
                _wait_for(settled, (caregiver, patient), timeout_seconds=20, label=f"{label}后的收麦回执")
                return holder["receipt"]

            def wait_admin_card_paused(label: str) -> None:
                admin_page.get_by_text(PAUSED_TITLE, exact=True).wait_for(state="visible", timeout=20_000)
                for name in ("老人已答对", "跳过本题", "继续 AI 自动带练"):
                    button = admin_page.get_by_role("button", name=name, exact=True)
                    button.wait_for(state="visible", timeout=20_000)
                    _wait_for(button.is_enabled, (admin_page,), timeout_seconds=20, label=f"{label}后「{name}」可点")

            def heard_panel() -> str:
                panel = admin_page.locator("div[aria-live='polite']").filter(has_text="AI 听到的")
                if panel.count() != 1:
                    return ""
                return panel.inner_text()

            def wait_heard_panel(label: str) -> str:
                holder: dict[str, str] = {}

                def judged() -> bool:
                    text = heard_panel()
                    if not text:
                        return False
                    heard_ok = "识别：没有识别到语音" in text or "识别：「" in text
                    verdict_ok = ("AI 判类：" in text and "判分中" not in text
                                  and "转写中" not in text and "处理失败" not in text)
                    if heard_ok and verdict_ok:
                        holder["text"] = text
                        return True
                    return False
                _wait_for(judged, (admin_page,), timeout_seconds=20, label=f"{label}的「AI 听到的」面板")
                return holder["text"]

            def wait_header_label(order: int, label: str) -> str:
                expected = f"总第 {order} 题"
                kicker = admin_page.locator(".page-kicker").filter(has_text="当前训练任务")
                _wait_for(lambda: kicker.count() == 1 and expected in kicker.inner_text(),
                          (admin_page,), timeout_seconds=20, label=f"{label}页首题号「{expected}」")
                return expected

            def adjudicate(kind_label: str, dialog_title: str, reason_label: str,
                           note: str | None, confirm_label: str) -> None:
                button = admin_page.get_by_role("button", name=kind_label, exact=True)
                button.wait_for(state="visible", timeout=20_000)
                _wait_for(button.is_enabled, (admin_page,), timeout_seconds=20, label=f"「{kind_label}」可点")
                button.click()
                dialog = admin_page.get_by_role("dialog", name=dialog_title, exact=True)
                dialog.wait_for(state="visible", timeout=10_000)
                dialog.get_by_label(reason_label, exact=True).check()
                if note is not None:
                    dialog.get_by_label(NOTE_FIELD_LABEL, exact=True).fill(note)
                dialog.get_by_role("button", name=confirm_label, exact=True).click()
                dialog.wait_for(state="hidden", timeout=10_000)

            def adjudication_rows() -> list[dict]:
                rows = obs["adjudications"]
                assert isinstance(rows, list)
                return list(rows)

            def resume_rows() -> list[dict]:
                rows = obs["resumes"]
                assert isinstance(rows, list)
                return list(rows)

            def uploads() -> list[str]:
                rows = obs["audio_uploads"]
                assert isinstance(rows, list)
                return list(rows)

            # ---- 0. 照护员开场、平板配对、开始练习 ----
            caregiver.goto(f"{config.origin}/console", wait_until="domcontentloaded")
            caregiver.get_by_label("用户名").fill(config.username)
            caregiver.get_by_label("密码").fill(config.password)
            caregiver.get_by_role("button", name="登录", exact=True).click()
            caregiver.get_by_role("heading", name="今天要做的练习").wait_for(state="visible", timeout=20_000)
            caregiver.get_by_role("listitem").filter(has_text=PATIENT_ID).get_by_role(
                "button", name="开始本次", exact=True).click()
            caregiver.get_by_role("heading", name=PATIENT_ID, exact=True).wait_for(state="visible", timeout=20_000)
            caregiver.get_by_text("老人画面还没打开", exact=True).wait_for(state="visible", timeout=20_000)
            _wait_for(lambda: isinstance(obs["session_id"], str), (caregiver,), timeout_seconds=10, label="开场回执")
            session_id = str(obs["session_id"])

            patient.goto(f"{config.origin}/patient", wait_until="domcontentloaded")
            pair_dialog = patient.get_by_role("dialog", name="连接这台平板", exact=True)
            pair_dialog.wait_for(state="visible", timeout=20_000)
            patient.get_by_label("配对码").fill(config.pin)
            patient.get_by_role("button", name="完成配对", exact=True).click()
            pair_dialog.wait_for(state="hidden", timeout=20_000)
            attached = obs["attached_session_ids"]
            assert isinstance(attached, list)
            if attached[:1] != [session_id]:
                raise BrowserAcceptanceError("平板没有配到本场")
            caregiver.get_by_text("老人画面已打开", exact=True).wait_for(state="visible", timeout=25_000)

            plan = admin_api.get(f"/sessions/{session_id}/plan", "读冻结计划")
            plan_items = sorted(
                (item for item in plan.get("items", []) if isinstance(item, dict)),
                key=lambda item: int(item["presentation_order"]))
            if len(plan_items) < 3 or [int(item["presentation_order"]) for item in plan_items[:3]] != [1, 2, 3]:
                raise BrowserAcceptanceError("冻结计划前三题的 presentation_order 不是 1、2、3")
            first_item_id, second_item_id, third_item_id = (str(item["item_id"]) for item in plan_items[:3])

            start_practice = caregiver.get_by_role("button", name="开始练习", exact=True)
            start_practice.wait_for(state="visible", timeout=20_000)
            patient.get_by_role("button", name="点一下，开始", exact=True).click()
            patient.get_by_role("button", name="关闭语音朗读", exact=True).wait_for(state="visible", timeout=10_000)
            start_practice.click()
            caregiver.get_by_text("练习进行中", exact=True).wait_for(state="visible", timeout=20_000)
            patient.get_by_text("正在听您说", exact=True).wait_for(state="visible", timeout=30_000)
            _wait_for(lambda: started_record_after(0) is not None, (patient,), timeout_seconds=20,
                      label="第一题第一次录音的 record_started 回执")
            first_record = started_record_after(0)
            assert first_record is not None
            if first_record["attempt_seq"] != 1:
                raise BrowserAcceptanceError(f"第一题第一次录音命令没有按 attempt_seq=1 开始({describe(first_record)})")
            patient.get_by_role("button", name="说完了可以点这里", exact=True).click()

            # 第一次回答判完,AI 下发第 1 级线索(伪麦克风不是人声 → 沉默 → 进提示阶梯)。
            _wait_for(lambda: "record_stopped" in acks(first_record["command_key"])
                      and started_tts_after(int(first_record["command_seq"]), purposes={"cue"}) is not None,
                      (patient, caregiver), timeout_seconds=45, label="第一次回答判完后的线索话术")
            first_cue = started_tts_after(int(first_record["command_seq"]), purposes={"cue"})
            assert first_cue is not None
            if (first_cue["prompt_level"], first_cue["attempt_seq"]) != (1, 2) or not same_position(first_cue, first_record):
                raise BrowserAcceptanceError(f"第一次回答后的线索不是同一题位的第 1 级提示({describe(first_record)} → {describe(first_cue)})")
            raise_if_violated()

            # ---- 链 A:线索放到一半暂停 → 训练台「继续」→ 续弹同一题位的线索 ----
            receipt_a = pause_from_caregiver("链 A 题内暂停")
            if receipt_a.get("position_item_id") != first_item_id or receipt_a.get("position_turn_seq") != 1:
                raise BrowserAcceptanceError("题内暂停后的权威回执题位不是第 1 题第 1 轮")

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
            row = admin_page.locator(".resume-row").filter(has_text="已暂停")
            row.get_by_role("button", name="继续这场", exact=True).click()
            wait_admin_card_paused("链 A 进训练台")
            header_label_first = wait_header_label(1, "链 E 第 1 题")
            heard_first = wait_heard_panel("链 D 第 1 次回答")

            resume_button = admin_page.get_by_role("button", name="继续 AI 自动带练", exact=True)
            resume_button.click()
            admin_page.get_by_role("button", name="确认继续 AI", exact=True).click()
            _wait_for(lambda: len(resume_rows()) == 1, (admin_page,), timeout_seconds=20, label="恢复请求 200")
            resume_request = resume_rows()[0]["request"] or {}
            resume_receipt = resume_rows()[0]["response"]
            if (resume_receipt.get("status") != "waiting_tts" or resume_receipt.get("mode") != "autonomous"
                    or resume_receipt.get("position_item_id") != first_item_id
                    or resume_receipt.get("position_turn_seq") != 1):
                raise BrowserAcceptanceError("恢复回执没有停在第 1 题第 1 轮的 waiting_tts")
            if resume_request.get("expected_revision") != receipt_a.get("state_revision"):
                raise BrowserAcceptanceError("恢复请求的 expected_revision 不是暂停回执的版本")

            def resumed_cue() -> dict | None:
                command = started_tts_after(int(first_cue["command_seq"]), purposes={"cue", "feedback", "tell_answer"})
                return command if command is not None and command["command_key"] != first_cue["command_key"] else None
            _wait_for(lambda: resumed_cue() is not None, (patient, admin_page), timeout_seconds=45,
                      label="恢复后平板拿到并放出续弹的线索")
            resumed = resumed_cue()
            assert resumed is not None
            if not same_position(resumed, first_cue):
                raise BrowserAcceptanceError(f"恢复后的命令不在同一题同一轮({describe(first_cue)} → {describe(resumed)})")
            if (resumed["purpose"], resumed["prompt_level"], resumed["attempt_seq"]) != ("cue", 1, 2):
                raise BrowserAcceptanceError(
                    f"恢复后续弹的不是第 1 级线索({resumed['purpose']}/{resumed['prompt_level']}/{resumed['attempt_seq']})")
            raise_if_violated()

            # 线索放完照常开第二次麦,第二次回答判完 AI 再下发话术。
            _wait_for(lambda: started_record_after(int(resumed["command_seq"])) is not None,
                      (patient, admin_page), timeout_seconds=45, label="续弹线索后的第二次开麦")
            second_record = started_record_after(int(resumed["command_seq"]))
            assert second_record is not None
            if second_record["attempt_seq"] != 2 or not same_position(second_record, first_cue):
                raise BrowserAcceptanceError(f"第二次录音不是同一题位的 attempt_seq=2({describe(second_record)})")
            patient.get_by_text("正在听您说", exact=True).wait_for(state="visible", timeout=20_000)
            patient.get_by_role("button", name="说完了可以点这里", exact=True).click()
            _wait_for(lambda: "record_stopped" in acks(second_record["command_key"])
                      and len(uploads()) >= 2
                      and started_tts_after(int(second_record["command_seq"]),
                                            purposes={"cue", "feedback", "tell_answer"}) is not None,
                      (patient, admin_page), timeout_seconds=45, label="第二次回答判完后的话术")
            after_second = started_tts_after(int(second_record["command_seq"]), purposes={"cue", "feedback", "tell_answer"})
            assert after_second is not None
            if not same_position(after_second, first_cue):
                raise BrowserAcceptanceError(f"第二次回答后的话术不在同一题位({describe(after_second)})")
            raise_if_violated()

            # ---- 链 B + D:暂停后「老人已答对」;AI 判类原样保留 ----
            receipt_b = pause_from_caregiver("链 B 暂停")
            if receipt_b.get("position_item_id") != first_item_id:
                raise BrowserAcceptanceError("链 B 暂停后的题位不是第 1 题")
            wait_admin_card_paused("链 B 暂停")
            heard_second = wait_heard_panel("链 D 第 2 次回答")
            if "本题第 2 次回答" not in heard_second:
                raise BrowserAcceptanceError("「AI 听到的」没有显示第 2 次回答")
            before = attempts_at(first_item_id)
            if len(before) != 2 or any(row.get("processing_status") != "completed" for row in before):
                raise BrowserAcceptanceError(f"裁定前第 1 题不是两次已判完的回答(n={len(before)})")
            verdict_snapshot = {int(row["id"]): (row.get("operational_answer_type"), row.get("operational_score"),
                                                 row.get("processing_status")) for row in before}
            ai_verdict_first = str(before[-1].get("operational_answer_type") or "")
            if not ai_verdict_first or ai_verdict_first == "正确":
                raise BrowserAcceptanceError("伪麦克风的回答本应被 AI 判为非正确")

            adjudicate("老人已答对", "确认老人已答对？", CONFIRMED_REASON_LABEL, ADJUDICATION_NOTE, "确认老人已答对")
            _wait_for(lambda: len(adjudication_rows()) == 1 and adjudication_rows()[0]["status"] == 200,
                      (admin_page,), timeout_seconds=20, label="「老人已答对」写入 200")
            confirmed = adjudication_rows()[0]
            request_b = confirmed["request"] or {}
            receipt_after_b = confirmed["response"]
            if (request_b.get("kind"), request_b.get("reason_code"), request_b.get("note")) != (
                    "confirmed_correct", "asr_misrecognized", ADJUDICATION_NOTE):
                raise BrowserAcceptanceError("「老人已答对」请求体不是选定的原因与备注")
            if request_b.get("expected_revision") != receipt_b.get("state_revision"):
                raise BrowserAcceptanceError("裁定请求的 expected_revision 不是暂停回执的版本")
            if (receipt_after_b.get("position_item_id") != second_item_id
                    or receipt_after_b.get("status") != "waiting_tts"):
                raise BrowserAcceptanceError("「老人已答对」后题位没有推进到第 2 题")
            after = attempts_at(first_item_id)
            if {int(row["id"]): (row.get("operational_answer_type"), row.get("operational_score"),
                                 row.get("processing_status")) for row in after} != verdict_snapshot:
                raise BrowserAcceptanceError("裁定后 attempt 行的 AI 判类被改动了")
            journal_b = journal()
            item_events = {int(item["id"]): item for item in journal_b.get("items", []) if isinstance(item, dict)}
            turns_first = [turn for turn in journal_b.get("turns", []) if isinstance(turn, dict)
                           and item_events.get(int(turn["item_event_id"]), {}).get("item_id") == first_item_id]
            if (len(turns_first) != 1 or turns_first[0].get("ai_answer_type") != ai_verdict_first
                    or turns_first[0].get("reviewed_score") is not None):
                raise BrowserAcceptanceError("第 1 题没有按最后一次 attempt 的 AI 判类收口,或研究分被写了")
            header_label_second = wait_header_label(2, "链 E 第 2 题")

            # ---- 链 F + C:第 2 题问句放到一半暂停;没录音时「老人已答对」409、「跳过本题」200 ----
            _wait_for(lambda: started_tts_after(int(after_second["command_seq"]), purposes={"question"}) is not None,
                      (patient, admin_page), timeout_seconds=45, label="第 2 题问句开始播放")
            second_question = started_tts_after(int(after_second["command_seq"]), purposes={"question"})
            assert second_question is not None
            if same_position(second_question, first_cue) or second_question["attempt_seq"] != 1:
                raise BrowserAcceptanceError(f"第 2 题问句不是新题位的第一次({describe(second_question)})")
            receipt_f = pause_from_caregiver("链 F 问句中暂停")
            if receipt_f.get("position_item_id") != second_item_id:
                raise BrowserAcceptanceError("链 F 暂停后的题位不是第 2 题")
            if attempts_at(second_item_id):
                raise BrowserAcceptanceError("链 F 的题位不该有任何回答")
            wait_admin_card_paused("链 F 暂停")
            admin_page.get_by_text("AI 听到的：这一题还没有录到老人的回答。", exact=True).wait_for(
                state="visible", timeout=20_000)

            obs["expect_attempt_required"] = True
            adjudicate("老人已答对", "确认老人已答对？", NO_ANSWER_REASON_LABEL, None, "确认老人已答对")
            _wait_for(lambda: len(adjudication_rows()) == 2 and adjudication_rows()[1]["status"] == 409,
                      (admin_page,), timeout_seconds=20, label="没录音时「老人已答对」被 409 拒绝")
            rejected = adjudication_rows()[1]
            if rejected["code"] != ATTEMPT_REQUIRED_CODE:
                raise BrowserAcceptanceError("没录音时的拒绝码不是 attempt_required")
            admin_page.get_by_role("alert").filter(has_text=ATTEMPT_REQUIRED_HINT).wait_for(
                state="visible", timeout=20_000)
            if admin_page.get_by_text(UNCERTAIN_TITLE, exact=False).count() != 0:
                raise BrowserAcceptanceError("写前 409 被折成了 uncertain")
            admin_page.get_by_text(PAUSED_TITLE, exact=True).wait_for(state="visible", timeout=5_000)
            if status_receipt().get("status") != "paused":
                raise BrowserAcceptanceError("被拒的裁定改动了服务端状态")

            adjudicate("跳过本题", "确认跳过本题？", SKIP_REASON_LABEL, None, "确认跳过本题")
            _wait_for(lambda: len(adjudication_rows()) == 3 and adjudication_rows()[2]["status"] == 200,
                      (admin_page,), timeout_seconds=20, label="「跳过本题」写入 200")
            skipped = adjudication_rows()[2]
            request_c = skipped["request"] or {}
            receipt_after_c = skipped["response"]
            if (request_c.get("kind"), request_c.get("reason_code")) != ("skip_item", "participant_declined"):
                raise BrowserAcceptanceError("「跳过本题」请求体不是选定的原因")
            if (receipt_after_c.get("position_item_id") != third_item_id
                    or receipt_after_c.get("status") != "waiting_tts"):
                raise BrowserAcceptanceError("「跳过本题」后题位没有推进到第 3 题")
            _wait_for(lambda: started_tts_after(int(second_question["command_seq"]), purposes={"question"}) is not None,
                      (patient, admin_page), timeout_seconds=45, label="第 3 题问句开始播放")
            raise_if_violated()

            # ---- 收尾:照护员安全暂停 ----
            pause_from_caregiver("收尾")
            raise_if_violated()

            obs["teardown_started"] = True
            for context in contexts:
                context.close()
            contexts = []
            browser.close()
            browser = None
            result = AdjudicationChainsResult(
                session_id=session_id,
                plan_id=str(obs["plan_id"]),
                adjudicated_by=admin_username,
                first_item_id=first_item_id,
                second_item_id=second_item_id,
                third_item_id=third_item_id,
                first_cue_command_key=str(first_cue["command_key"]),
                resumed_cue_command_key=str(resumed["command_key"]),
                resume_expected_revision=str(resume_request.get("expected_revision")),
                resume_receipt_status=str(resume_receipt.get("status")),
                resumed_next_purpose=str(resumed["purpose"]),
                resumed_next_prompt_level=str(resumed["prompt_level"]),
                resumed_next_attempt_seq=str(resumed["attempt_seq"]),
                confirmed_correct_revision=str(receipt_after_b.get("state_revision")),
                confirmed_correct_position_item_id=str(receipt_after_b.get("position_item_id")),
                attempt_required_code=str(rejected["code"]),
                skip_revision=str(receipt_after_c.get("state_revision")),
                skip_position_item_id=str(receipt_after_c.get("position_item_id")),
                ai_verdict_first_item=ai_verdict_first,
                heard_panel_line=next(line for line in heard_first.splitlines() if line.startswith("识别：")),
                header_label_first=header_label_first,
                header_label_second=header_label_second,
            )
            _write_receipt(config, result)
            return result
    except BaseException:
        dump_debug()
        raise
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


def _validate_adjudication_ledger(config, result: AdjudicationChainsResult) -> None:
    from sqlmodel import Session, select

    from app import db
    from app.models import (
        AttemptEvent, AudioAssetRow, AuditLog, AutopilotControlEvent, AutopilotPositionAdjudication,
        ItemEvent, Session as TrainSession, SessionRuntimeState, TurnEvent,
    )
    from harness import caregiver_demo_harness

    caregiver_demo_harness._assert_imported_engine_binding(config.base, db)  # noqa: SLF001
    with Session(db.engine) as session, session.no_autoflush:
        if session.get(TrainSession, result.session_id) is None:
            raise BrowserAcceptanceError("账本没有本场")
        runtime = session.get(SessionRuntimeState, result.session_id)
        if runtime is None or runtime.status != "paused":
            raise BrowserAcceptanceError("收尾后场次 runtime 不是暂停态")

        events = list(session.exec(select(AutopilotControlEvent).where(
            AutopilotControlEvent.session_id == result.session_id).order_by(AutopilotControlEvent.event_seq)))
        types = [event.event_type for event in events]
        resumes = [event for event in events if event.event_type == "resume"]
        # 一次「继续」+ 两次裁定各自走 resume 同一条路径 = 三条具名恢复事实。
        if len(resumes) != 3 or any(event.actor_id != result.adjudicated_by for event in resumes):
            raise BrowserAcceptanceError(f"控制账本没有三条由管理员发起的恢复事实({types})")
        if types.count("pause") < 4 or types.index("pause") > types.index("resume"):
            raise BrowserAcceptanceError(f"控制账本的暂停/恢复次序不对({types})")

        rows = list(session.exec(select(AutopilotPositionAdjudication).where(
            AutopilotPositionAdjudication.session_id == result.session_id
        ).order_by(AutopilotPositionAdjudication.id)))
        if len(rows) != 2:
            raise BrowserAcceptanceError(f"裁定行不是恰好两条(n={len(rows)})")
        confirmed, skipped = rows
        if ((confirmed.kind, confirmed.item_id, confirmed.turn_seq, confirmed.reason_code, confirmed.note,
             confirmed.actor_id, confirmed.is_simulation)
                != ("confirmed_correct", result.first_item_id, 1, "asr_misrecognized", "走查",
                    result.adjudicated_by, True)
                or confirmed.source_attempt_id is None or confirmed.turn_event_id is None):
            raise BrowserAcceptanceError("「老人已答对」的裁定行不是具名带原因、挂在最后一次 attempt 上")
        if ((skipped.kind, skipped.item_id, skipped.turn_seq, skipped.reason_code, skipped.note,
             skipped.actor_id, skipped.source_attempt_id, skipped.turn_event_id)
                != ("skipped", result.second_item_id, 1, "participant_declined", None,
                    result.adjudicated_by, None, None)):
            raise BrowserAcceptanceError("「跳过本题」的裁定行不是无录音的 skipped")

        attempts = list(session.exec(select(AttemptEvent).where(
            AttemptEvent.session_id == result.session_id).order_by(AttemptEvent.attempt_seq)))
        if ([(row.item_id, row.turn_seq, row.attempt_seq, row.processing_status) for row in attempts]
                != [(result.first_item_id, 1, 1, "completed"), (result.first_item_id, 1, 2, "completed")]):
            raise BrowserAcceptanceError("回答账本不是第 1 题的两次已判完回答(第 2、3 题不该有录音)")
        if attempts[-1].id != confirmed.source_attempt_id:
            raise BrowserAcceptanceError("「老人已答对」没有挂在最后一次 attempt 上")
        if attempts[-1].operational_answer_type != result.ai_verdict_first_item:
            raise BrowserAcceptanceError("attempt 行的 AI 判类与走查时看到的不一致")

        items = {item.item_id: item for item in session.exec(select(ItemEvent).where(
            ItemEvent.session_id == result.session_id))}
        first = items.get(result.first_item_id)
        if first is None or first.presentation_order != 1:
            raise BrowserAcceptanceError("第 1 题的 ItemEvent 没有以 presentation_order=1 落账")
        turns = list(session.exec(select(TurnEvent).where(TurnEvent.item_event_id == first.id)))
        if (len(turns) != 1 or turns[0].id != confirmed.turn_event_id
                or turns[0].ai_answer_type != result.ai_verdict_first_item
                or turns[0].reviewed_score is not None or turns[0].score_locked):
            raise BrowserAcceptanceError("第 1 题没有按 AI 原判收口成一条未锁分的 TurnEvent")
        second = items.get(result.second_item_id)
        if second is not None and list(session.exec(select(TurnEvent).where(TurnEvent.item_event_id == second.id))):
            raise BrowserAcceptanceError("无录音跳过的第 2 题不该有 TurnEvent")

        audits = list(session.exec(select(AuditLog).where(AuditLog.action == "autopilot_position_adjudicated")))
        if len(audits) != 2:
            raise BrowserAcceptanceError(f"裁定审计不是恰好两条(n={len(audits)})")
        uploaded = [row for row in session.exec(select(AudioAssetRow).where(
            AudioAssetRow.session_id == result.session_id)) if row.byte_count and row.checksum]
        if len(uploaded) != 2:
            raise BrowserAcceptanceError(f"本场没有两段真实上传的录音(uploaded={len(uploaded)})")


def validate_adjudication_ledger() -> None:
    from harness import caregiver_demo_harness
    from harness.tts_ack_harness import HarnessConfigError

    try:
        config = caregiver_demo_harness.resolve_caregiver_config()
    except HarnessConfigError as exc:
        raise BrowserAcceptanceError("账本核验的临时目录或运行配置无效") from exc
    _validate_adjudication_ledger(config, _read_receipt(config.base.root))


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="照护员真实 Chrome:裁定/续弹链走查")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--adjudication-chains", action="store_true")
    group.add_argument("--verify-ledger", action="store_true")
    parser.add_argument("--origin")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    secrets = tuple(os.environ.get(name) or "" for name in (
        "NMU_CAREGIVER_PASSWORD", "CONSOLE_PIN", "NMU_CAREGIVER_HARNESS_INSTANCE", ADMIN_PASSWORD_ENV))
    try:
        if args.adjudication_chains:
            if not args.origin:
                raise BrowserAcceptanceError("裁定链走查缺少本机地址")
            run_adjudication_chains(resolve_browser_config(args.origin))
            print("真实 Chrome 裁定链已走完:题内暂停→续弹同一线索;老人已答对→进下一题;"
                  "无回答拒答对→跳过本题;「AI 听到的」与「第 N 题」都在训练台上看到")
        else:
            if args.origin:
                raise BrowserAcceptanceError("账本核验不接受网页地址")
            validate_adjudication_ledger()
            print("裁定链账本核验已通过")
        return 0
    except BrowserAcceptanceError as exc:
        print(f"裁定链走查失败:{_redacted_error(exc, secrets)}", file=sys.stderr)
        return 1
    except Exception as exc:  # fail closed without leaking browser call values
        print(f"裁定链走查失败:{_redacted_error(exc, secrets)}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
