#!/usr/bin/env python3
"""本机 Chrome 验收：新场次指定从第 2 题开始，且跳过不伪造回答。"""
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

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from harness.caregiver_browser_acceptance import (  # noqa: E402
    HARNESS_MARKER_HEADER, HARNESS_MARKER_VALUE, INSTANCE_MARKER_HEADER,
    BrowserAcceptanceError, _redacted_error, _same_acceptance_origin,
    _stable_error_code, _wait_for, resolve_browser_config,
)
from harness.caregiver_recovery_chains import (  # noqa: E402
    ADMIN_PASSWORD_ENV, PATIENT_ID, _AdminApi, _admin_credentials,
)

RECEIPT_NAME = "browser-start-position-result.json"
SCHEMA = "caregiver-browser-start-position.v1"


@dataclass(frozen=True)
class StartPositionResult:
    session_id: str
    first_item_id: str
    second_item_id: str
    command_key: str
    actor_id: str


def _write_receipt(config, result: StartPositionResult) -> None:
    fd = os.open(config.harness_root / RECEIPT_NAME, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump({"schema": SCHEMA, **result.__dict__}, handle, sort_keys=True)


def _read_receipt(root: Path) -> StartPositionResult:
    fd = os.open(root / RECEIPT_NAME, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    with os.fdopen(fd, "r", encoding="utf-8") as handle:
        facts = os.fstat(handle.fileno())
        if (not stat.S_ISREG(facts.st_mode) or stat.S_IMODE(facts.st_mode) != 0o600
                or facts.st_uid != os.getuid() or facts.st_nlink != 1
                or not 0 < facts.st_size <= 8_192):
            raise BrowserAcceptanceError("起点走查收据不是本用户的私密普通文件")
        value = json.load(handle)
    if not isinstance(value, dict) or value.get("schema") != SCHEMA:
        raise BrowserAcceptanceError("起点走查收据格式无效")
    fields = {name: value.get(name) for name in StartPositionResult.__dataclass_fields__}
    if any(not isinstance(item, str) or not item for item in fields.values()):
        raise BrowserAcceptanceError("起点走查收据缺少字段")
    return StartPositionResult(**fields)


def run_start_position(config) -> None:
    from playwright.sync_api import sync_playwright

    username, password = _admin_credentials()
    obs = {"session_id": None, "starts": [], "commands": {}, "tts_started": set(), "paused": False}
    violations: list[str] = []

    def route_request(route) -> None:
        if _same_acceptance_origin(route.request.url, config.origin):
            route.continue_()
        else:
            violations.append("浏览器请求了非本次本机地址")
            route.abort("blockedbyclient")

    def observe(response) -> None:
        request = response.request
        path = urlsplit(response.url).path
        headers = response.all_headers()
        if (headers.get(INSTANCE_MARKER_HEADER) != config.instance_marker
                or headers.get(HARNESS_MARKER_HEADER) != HARNESS_MARKER_VALUE):
            violations.append("响应不属于本次临时演练环境")
        if response.status >= 400:
            code = _stable_error_code(response)
            expected = (request.method == "GET" and path == "/auth/me" and response.status == 401)
            expected = expected or (path.endswith("/autopilot/next") and response.status == 409
                                    and code in {"autopilot_not_active", "autopilot_runtime_inactive"})
            if not expected:
                violations.append(f"非预期 HTTP {response.status} {request.method} {path} ({code})")
            return
        if response.status != 200:
            return
        if request.method == "POST" and path.startswith("/caregiver/visit-plans/") and path.endswith("/start"):
            obs["session_id"] = response.json()["session"]["session_id"]
        if request.method == "POST" and path.endswith("/autopilot/start"):
            obs["starts"].append((request.post_data_json, response.json()))
        if path.endswith("/autopilot/next"):
            payload = response.json()
            if isinstance(payload, dict) and isinstance(payload.get("command_key"), str):
                obs["commands"][payload["command_key"]] = payload
        if request.method == "POST" and path.endswith("/acks"):
            if request.post_data_json.get("ack_type") == "tts_started":
                obs["tts_started"].add(path.split("/")[-2])

    def check() -> None:
        if violations:
            raise BrowserAcceptanceError("；".join(violations[:8]))

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(channel="chrome", headless=True, args=[
            "--autoplay-policy=no-user-gesture-required", "--disable-background-networking",
            "--disable-component-update", "--disable-default-apps", "--disable-sync",
        ])
        admin_api = _AdminApi(playwright, config, username, password)
        try:
            contexts = [browser.new_context(base_url=config.origin, service_workers="block") for _ in range(3)]
            for context in contexts:
                context.route("**/*", route_request)
                context.on("response", observe)
            caregiver, admin, patient = [context.new_page() for context in contexts]
            caregiver.goto(config.origin + "/console")
            caregiver.get_by_label("用户名").fill(config.username)
            caregiver.get_by_label("密码").fill(config.password)
            caregiver.get_by_role("button", name="登录", exact=True).click()
            caregiver.get_by_role("heading", name="今天要做的练习", exact=True).wait_for(timeout=20_000)
            caregiver.get_by_role("listitem").filter(has_text=PATIENT_ID).get_by_role(
                "button", name="开始本次", exact=True).click()
            caregiver.get_by_text("老人画面还没打开", exact=True).wait_for(timeout=20_000)
            sid = obs["session_id"]
            if not isinstance(sid, str):
                raise BrowserAcceptanceError("开场回执缺少场次标识")
            # 照护员尚未点开始练习。关闭该页，避免另一个操作入口与研究者争用启动。
            caregiver.close()
            plan = admin_api.get(f"/sessions/{sid}/plan", "读冻结计划")
            items = sorted(plan["items"], key=lambda item: item["presentation_order"])
            if len(items) != 20 or [item["presentation_order"] for item in items[:2]] != [1, 2]:
                raise BrowserAcceptanceError("临时演练计划不是预期的 20 题")
            first, second = (str(item["item_id"]) for item in items[:2])

            patient.goto(config.origin + "/patient")
            pairing = patient.get_by_role("dialog", name="连接这台平板", exact=True)
            pairing.wait_for(timeout=20_000)
            patient.get_by_label("配对码").fill(config.pin)
            patient.get_by_role("button", name="完成配对", exact=True).click()
            pairing.wait_for(state="hidden", timeout=20_000)

            admin.goto(config.origin + "/console")
            admin.get_by_label("用户名").fill(username)
            admin.get_by_label("密码").fill(password)
            admin.get_by_role("button", name="登录", exact=True).click()
            admin.get_by_role("button", name="打开恢复入口", exact=True).click()
            admin.get_by_role("group", name="异常恢复数据分区").get_by_role(
                "button", name=re.compile("^专用模拟")).click()
            admin.locator(".run-picker-card").filter(has_text=PATIENT_ID).get_by_role(
                "button", name="核查既有场次", exact=True).click()
            admin.locator(".resume-row").get_by_role("button", name="继续这场", exact=True).click()
            selector = admin.get_by_label("本场从哪一题开始", exact=False)
            selector.wait_for(timeout=20_000)
            _wait_for(selector.is_enabled, (admin,), timeout_seconds=20, label="新场次起点可选")
            if selector.input_value() != "1" or selector.locator("option").count() != 20:
                raise BrowserAcceptanceError("起点默认值或冻结题目数量不符")
            selector.select_option("2")
            admin.get_by_text("从第 2 题开始；前面的 1 题、1 个环节将记录为跳过。", exact=True).wait_for()
            start = admin.get_by_role("button", name="确认从第 2 题开始", exact=True)
            if start.is_enabled():
                raise BrowserAcceptanceError("未选跳过原因仍可启动")

            # 实际老人端激活 + 同窗激活信号，验证两种入口均不能覆盖已编辑起点。
            patient.get_by_role("button", name="点一下，开始", exact=True).click()
            def activate_and_assert_no_start(label: str) -> None:
                admin.evaluate("sid => window.dispatchEvent(new CustomEvent('nmu:patient-activation', {detail:{sessionId:sid}}))", sid)
                admin.wait_for_timeout(1_000)
                if obs["starts"]:
                    raise BrowserAcceptanceError(label + "意外发出了启动请求")
                check()

            activate_and_assert_no_start("起点选择后")
            reason = admin.get_by_label("跳过前面题目的原因", exact=False)
            reason.select_option("trained_in_prior_sitting")
            admin.get_by_label("跳过备注（可不填，200 字以内）", exact=True).fill("  起点走查  ")
            _wait_for(start.is_enabled, (admin,), timeout_seconds=20, label="具备原因后可确认启动")
            start.click()
            dialog = admin.get_by_role("dialog", name="确认从第 2 题开始训练？", exact=True)
            dialog.wait_for()
            if first not in dialog.inner_text() and "前面的 1 题" not in dialog.inner_text():
                raise BrowserAcceptanceError("确认框没有跳过预览")
            dialog.get_by_role("button", name="取消", exact=True).click()
            activate_and_assert_no_start("取消确认后")
            selector.select_option("1")
            activate_and_assert_no_start("选回第一题但没有显式重置时")
            selector.select_option("2")
            reason.select_option("trained_in_prior_sitting")
            admin.get_by_label("跳过备注（可不填，200 字以内）", exact=True).fill("  起点走查  ")
            start.click()
            dialog.get_by_role("button", name="确认跳过前面题目并开始", exact=True).click()
            _wait_for(lambda: len(obs["starts"]) == 1, (admin, patient), timeout_seconds=20, label="指定起点启动成功")
            posted, receipt = obs["starts"][0]
            expected = {"idempotency_key": f"p0a.start.{sid}", "expected_revision": 0,
                        "start_presentation_order": 2, "skip_reason_code": "trained_in_prior_sitting",
                        "skip_note": "起点走查"}
            if posted != expected or receipt.get("position_item_id") != second:
                raise BrowserAcceptanceError("起点请求或服务器回执与第 2 题不符")
            _wait_for(lambda: bool(obs["tts_started"]), (admin, patient), timeout_seconds=30, label="老人端开始朗读第 2 题")
            key = next(iter(obs["tts_started"]))
            command = obs["commands"].get(key, {})
            if command.get("command_seq") != 1 or command.get("item_ref") != "itm-0002" or command.get("kind") != "tts":
                raise BrowserAcceptanceError("老人端首条朗读命令不是冻结计划第 2 题")
            # 立即安全暂停，使账本核查只覆盖起点与首条朗读，不触发真实采音。
            obs["paused"] = True
            admin_api.post(f"/sessions/{sid}/pause", {}, "收尾安全暂停")
            patient.get_by_text("练习已暂停，请稍候", exact=True).wait_for(timeout=20_000)
            check()
            # 单独的 UI 投影测试：真实暂停回执仅替换新错误码，不写入任何假裁定。
            def saved_receipt(route) -> None:
                response = route.fetch()
                payload = response.json()
                if payload.get("status") != "paused" or payload.get("takeover_ready") is not True:
                    route.fulfill(response=response)
                    return
                payload["last_error_code"] = "autopilot_adjudication_saved"
                # 保存裁定在服务端会追加一版；同版本改原因必须被 UI 拒绝。
                payload["state_revision"] += 1
                route.fulfill(response=response, json=payload)
            contexts[1].route(f"**/sessions/{sid}/autopilot/status", saved_receipt)
            admin.get_by_text("现场决定已保存。AI 服务暂不可用，训练保持暂停；恢复后可继续 AI，或转为人工操作。", exact=False).wait_for(timeout=20_000)
            for label in ("老人已答对", "跳过本题"):
                if admin.get_by_role("button", name=label, exact=True).count():
                    raise BrowserAcceptanceError("已保存裁定的投影仍显示重复裁定按钮")
            for label in ("继续 AI 自动带练", "转为人工操作"):
                if not admin.get_by_role("button", name=label, exact=True).is_enabled():
                    raise BrowserAcceptanceError("已保存裁定的投影没有保留安全继续/人工入口")
            _write_receipt(config, StartPositionResult(sid, first, second, key, username))
        finally:
            admin_api.close()
            browser.close()


def validate_start_position_ledger() -> None:
    from sqlmodel import Session, select
    from app import db
    from app.models import (
        AttemptEvent, AudioAssetRow, AutopilotControlEvent, AutopilotPositionAdjudication,
        ItemEvent, RuntimeCommand, SessionRuntimeState,
    )
    from harness import caregiver_demo_harness

    config = caregiver_demo_harness.resolve_caregiver_config()
    caregiver_demo_harness._assert_imported_engine_binding(config.base, db)  # noqa: SLF001
    result = _read_receipt(config.base.root)
    with Session(db.engine) as session, session.no_autoflush:
        sid = result.session_id
        runtime = session.get(SessionRuntimeState, sid)
        if runtime is None or runtime.status != "paused":
            raise BrowserAcceptanceError("起点验收结束后场次没有安全暂停")
        starts = list(session.exec(select(AutopilotControlEvent).where(
            AutopilotControlEvent.session_id == sid, AutopilotControlEvent.event_type == "start")))
        if len(starts) != 1 or starts[0].idempotency_key != f"p0a.start.{sid}":
            raise BrowserAcceptanceError("启动账本不是恰好一次具备同场幂等键的启动")
        commands = list(session.exec(select(RuntimeCommand).where(
            RuntimeCommand.session_id == sid).order_by(RuntimeCommand.command_seq)))
        if not commands or (commands[0].item_id, commands[0].idempotency_key, commands[0].kind) != (
                result.second_item_id, result.command_key, "tts"):
            raise BrowserAcceptanceError("首条命令没有绑定冻结计划第 2 题")
        rows = list(session.exec(select(AutopilotPositionAdjudication).where(
            AutopilotPositionAdjudication.session_id == sid)))
        if len(rows) != 1 or (rows[0].kind, rows[0].item_id, rows[0].presentation_order, rows[0].turn_seq,
                             rows[0].reason_code, rows[0].note, rows[0].actor_id,
                             rows[0].source_attempt_id, rows[0].turn_event_id) != (
                "skipped", result.first_item_id, 1, 1, "trained_in_prior_sitting", "起点走查",
                result.actor_id, None, None):
            raise BrowserAcceptanceError("第 1 题跳过记录没有保留正确题号、原因、备注与操作人")
        for model in (AttemptEvent, AudioAssetRow, ItemEvent):
            if session.exec(select(model).where(model.session_id == sid)).first() is not None:
                raise BrowserAcceptanceError("仅朗读并跳过的起点走查不应生成回答、录音或题目评分")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="本机 Chrome 指定起始题验收")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--start-position", action="store_true")
    group.add_argument("--verify-ledger", action="store_true")
    parser.add_argument("--origin")
    args = parser.parse_args(argv)
    secrets = tuple(os.environ.get(name) or "" for name in (
        "NMU_CAREGIVER_PASSWORD", "CONSOLE_PIN", "NMU_CAREGIVER_HARNESS_INSTANCE", ADMIN_PASSWORD_ENV))
    try:
        if args.start_position:
            if not args.origin:
                raise BrowserAcceptanceError("起点走查缺少本机地址")
            run_start_position(resolve_browser_config(args.origin))
            print("真实 Chrome 起点走查通过：原因必填、取消不启动、老人端不覆盖选择、第 2 题首条朗读")
        else:
            if args.origin:
                raise BrowserAcceptanceError("账本核验不接受网页地址")
            validate_start_position_ledger()
            print("起点账本核验通过：第 1 题具名跳过，没有虚构回答、录音或评分")
        return 0
    except Exception as exc:
        print(f"起点走查失败：{_redacted_error(exc, secrets)}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
