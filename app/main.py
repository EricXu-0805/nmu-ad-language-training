"""南医大 AI 语言沟通训练平台的 FastAPI 服务。

已接建档/场次、冻结计划、逐环节采集、云或降级 ASR、受限 AI 初评、固定话术 TTS、
音频治理、去标识导出、审计、账号/PIN 与跨设备运行态。研究可用性仍由内容、伦理、
设备和数据质量门禁共同决定；接口存在不代表已获准真实入组。
"""
from __future__ import annotations

from contextlib import ExitStack, asynccontextmanager, contextmanager
import hashlib
import ipaddress
import json as _json
import math
import secrets
import threading
from pathlib import Path
from typing import Callable, Literal, NamedTuple, NoReturn
from urllib.parse import unquote

from fastapi import BackgroundTasks, Depends, FastAPI, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field as PydanticField, model_validator
from sqlalchemy import func, or_
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session as DBSession, select

import os
from datetime import date, datetime, timedelta, timezone

from fastapi import Request
from fastapi.responses import FileResponse, JSONResponse

from fastapi.responses import Response as PlainResponse

from . import (access_policy, assessment_bundles, assessment_contract,
               assessment_definitions, assessment_service, asr,
               audio_capture, audio_gate, audio_store, audit, auth,
               autopilot_contract, autopilot_orchestration,
               autopilot_positions, autopilot_service,
               cloud_processing, content, db, export_security, governance_lock,
               device_capability, evidence_ledger, export, http_security, judging,
               ai_quality_service, llm_judge, rule_judge, runtime, scoring,
               session_completion,
               resource_limits, patient_asset, patient_presentation,
               provider_readiness, repeat_evidence, repeat_intent,
               scale_protocol,
               session_admission, session_closeout, tts,
               visit_plan_contract, visit_plan_service)
from .autopilot_ledger import utc_now_naive
from .auth import COOKIE_NAME, CSRF_COOKIE_NAME
from .db import get_session, init_db
from .enums import AudioStatus, ConsentType
from .models import (AbnormalEvent, AssessmentEvent, AssessmentInstance,
                     AssessmentRecordingAuthorization,
                     AttemptCaptureProcessing,
                     AttemptEvent, AuditLog, AudioAssetRow,
                     AudioCaptureReceipt, AudioLocalCopyDisposalReceipt,
                     ExportBatch, InteractionEvent,
                     InteractionPresentationReceipt, ItemEvent, LiveState,
                     Patient, PatientDeviceCapability, PatientWithdrawalEvent,
                     RuntimeCommand, ScaleResult,
                     SessionAutopilotState,
                     SessionCloseoutReport, SessionOutcomeSummary,
                     SessionRuntimeState, TechnicalPauseReceipt,
                     TtsServeEvidence, TurnConfirmationRevision, TurnEvent,
                     VisitPlan)
from .models import Session as TrainSession

from fastapi import Response


def _install_assessment_bundles_at_startup() -> None:
    """装载 content/assessment_definitions/ 下的正式量表定义包(收据 150 S1)。

    索引缺失=生产尚无注册包(现状,不是错误)。索引存在但装载失败=内容缺陷:
    评估域保持空注册表逐请求 fail-closed(assessment_definitions_not_ready),
    训练域不受牵连;缺陷大声记日志,由部署检查而非静默吞掉。
    已安装(如测试进程内多次建 app)时跳过,不覆盖。
    """
    index_path = (content.CONTENT_DIR / assessment_bundles.ASSESSMENT_BUNDLE_DIR
                  / assessment_bundles.ASSESSMENT_BUNDLE_INDEX_FILE)
    if not index_path.exists():
        return
    if assessment_definitions.registered_bundles():
        return
    try:
        bundles, active_id, _raw = assessment_bundles.load_bundle_packages(
            content.CONTENT_DIR)
        assessment_definitions.install_production_bundles(
            bundles, active_bundle_id=active_id)
    except (content.FrozenContentUnavailable,
            assessment_definitions.AssessmentDefinitionError):
        # 运行输出隐私契约把输出钉死在两个咽喉点,启动钩子不得新增日志。
        # 装载失败的可见面=注册表保持空(scale-protocol 就绪端点如实显示
        # definitions_not_ready)+部署检查对 content/assessment_definitions/
        # 直接复验;评估域逐请求 fail-closed,训练域不受牵连。
        return


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db(db.engine)
    # 显式配置了非法 TTL 时必须在启动阶段失败，不可悄悄变成长期 bearer。
    device_capability.ttl_minutes()
    provider_readiness.ttl_minutes()
    asr.cleanup_scratch()   # 清扫上次进程异常终止残留的云转写临时音频副本
    _install_assessment_bundles_at_startup()
    with DBSession(db.engine) as _s:
        auth.cleanup_expired_sessions(_s)          # 清掉过期会话
        _has_users = auth.has_any_user(_s)
        auth.mark_users_present(_has_users)        # 库里已有账号 → 认证生效(即便忘设 REQUIRE_AUTH)
        # 账号与床旁 PIN 缺一不可；同机双窗也不依赖共享 cookie。
        auth.assert_deploy_credentials(_s)
    yield


app = FastAPI(title="南医大 · AI 语言沟通训练系统", version="0.0.1", lifespan=lifespan)


@app.exception_handler(content.FrozenContentUnavailable)
async def _frozen_content_unavailable_response(
        _request: Request, _exc: content.FrozenContentUnavailable):
    """Expose no parser, schema, digest, or filesystem detail over HTTP."""
    return JSONResponse(
        status_code=503,
        content={"detail": {
            "code": "frozen_content_unavailable",
            "message": "服务器冻结训练内容暂不可用，请停止当前操作并联系管理员",
        }},
        headers={"Cache-Control": "private, no-store", "Pragma": "no-cache"},
    )

# ---------------- 集中认证/授权门(具名账号 + 老人端设备 PIN)----------------
# 共享 PIN 只代表“已配对的老人端设备”，不是准管理员。路由与角色白名单
# 集中在 access_policy.py，避免处理器之间产生认证缝隙。


def _client_ip(request: Request) -> str:
    # 依赖 uvicorn --proxy-headers 把可信代理的 XFF 改写进 request.client.host；
    # 不自行解析 XFF(裸解析可被伪造绕过限速)。
    return request.client.host if request.client else "?"


def _actor(request: Request) -> str | None:
    """当前请求的审计身份：账号 display_id 或回环 M0 的固定本地身份。"""
    return getattr(request.state, "actor", None)


_OPERATIONAL_ERROR_CODES = frozenset({
    "audit_append_failed",
    "login_audit_append_failed",
    "audio_rollback_cleanup_failed",
})


def _emit_operational_error(code: str) -> None:
    """Emit an identifier-free, exception-text-free operational signal."""
    safe_code = code if code in _OPERATIONAL_ERROR_CODES else "operational_error"
    print(f"[ops] code={safe_code}", flush=True)


def _is_loopback_request(request: Request) -> bool:
    """开放 M0 只属于本机回环；不能因忘配认证而把研究 API 暴露到局域网。"""
    host = _client_ip(request).split("%", 1)[0]
    if host == "testclient":  # Starlette TestClient 的不可伪造进程内 peer 名。
        return True
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return False
    mapped = getattr(address, "ipv4_mapped", None)
    return address.is_loopback or bool(mapped and mapped.is_loopback)


def _require_account_identity(request: Request, action: str, *,
                              roles: set[str] | None = None,
                              allow_local_m0: bool = False) -> str:
    """高敏感动作不接受共享 PIN/开放模式，必须有可审计账号身份。"""
    actor = _actor(request)
    role = getattr(request.state, "actor_role", None)
    if not actor:
        if (allow_local_m0
                and getattr(request.state, "auth_kind", None) == "local_loopback"):
            return "LOCAL-M0"
        raise HTTPException(403, f"{action}必须使用研究者账号登录；共享 PIN 不具名，权限不足")
    if roles is not None and role not in roles:
        raise HTTPException(403, f"账号角色 {role or 'unknown'} 无权{action}")
    return actor


def _audit(s: DBSession, request: Request, action: str, summary: str, *,
           patient_id: str | None = None, session_id: str | None = None, turn_id: int | None = None) -> None:
    """best-effort 审计追加:记账失败绝不拖垮临床写(锁分不能因审计库抖动而失败),但也不静默——打日志。
    summary 只传元数据,绝不含患者回答文本/姓名。"""
    try:
        audit.record(s, actor=_actor(request) or "PIN/本地", action=action, summary=summary,
                     patient_id=patient_id, session_id=session_id, turn_id=turn_id)
    except Exception:  # noqa: BLE001 —— 审计是补充记录,临床写已成功,不回滚
        _emit_operational_error("audit_append_failed")


_DATA_STEWARD_VISIBLE_SESSION_STATUSES = frozenset(
    {"completed", "aborted", "failed"})


def _session_runtime_status(session_id: str, s: DBSession) -> str:
    state = s.get(SessionRuntimeState, session_id)
    return state.status if state is not None else "active"


def _require_session_operator(
        request: Request, sess: TrainSession, s: DBSession, action: str, *,
        mutation: bool = False,
        allow_withdrawn_safety_exit: bool = False,
        not_found_detail: str = "场次不存在") -> str:
    """Enforce one named researcher owner across the admitted session lifecycle.

    Patient-device capability routes deliberately do not call this account
    guard.  In local loopback M0 there is no named account; protected
    deployments fail closed on a missing/mismatched ``trainer_id``.  Admins may
    supervise another operator's session, but every mutating use writes a named,
    text-free audit authorization before the mutation is attempted.  A future
    cross-researcher review path must use an explicit reviewer/team assignment;
    terminal status alone never transfers ownership.
    """
    # Dual-use DEVICE routes such as /plan do not stamp auth_kind in open M0,
    # but the middleware has already restricted them to an actual loopback
    # peer. Preserve the documented local-only M0 workflow without creating a
    # network/deployment ownership bypass.
    local_m0 = (
        getattr(request.state, "auth_kind", None) == "local_loopback"
        or (not auth.auth_active() and _is_loopback_request(request))
    )
    if local_m0:
        if mutation and not allow_withdrawn_safety_exit:
            restriction_reason = _session_read_restriction_reason(sess, s)
            if restriction_reason is not None:
                _raise_withdrawn_session_read_conflict(
                    sess, resource="session_mutation",
                    reason_code=restriction_reason)
        return "LOCAL-M0"
    actor = _require_account_identity(
        request, action,
        roles={"researcher", "data_steward", "admin"},
        allow_local_m0=True,
    )
    role = getattr(request.state, "actor_role", None)
    status = _session_runtime_status(sess.session_id, s)
    if role == "data_steward":
        if mutation or status not in _DATA_STEWARD_VISIBLE_SESSION_STATUSES:
            raise HTTPException(status_code=403, detail={
                "code": "session_terminal_read_required",
                "message": "数据管理员只能读取已关闭的场次，不得变更场次事实",
            })
        return actor
    assigned = (sess.trainer_id or "").strip()
    if role == "researcher" and assigned != actor:
        # Deliberately conceal existence on both read and write paths.  The
        # caller may select a resource-specific generic detail so a foreign
        # item/turn is indistinguishable from that resource genuinely missing.
        raise HTTPException(status_code=404, detail=not_found_detail)
    if role == "admin" and assigned != actor and mutation:
        _audit(
            s, request, "session_operator_admin_supervision",
            f"supervised_action={action} runtime_status={status}",
            patient_id=sess.patient_id, session_id=sess.session_id,
        )
    # Ownership must be decided before checking withdrawal so another
    # researcher cannot use a 409/403 difference as a withdrawal oracle. Every
    # account mutation then shares one content fence; routes cannot accidentally
    # reopen an aborted/tombstoned participant through a forgotten local check.
    if mutation and not allow_withdrawn_safety_exit:
        restriction_reason = _session_read_restriction_reason(sess, s)
        if restriction_reason is not None:
            _raise_withdrawn_session_read_conflict(
                sess, resource="session_mutation",
                reason_code=restriction_reason)
    return actor


def _load_session_for_operator(
        request: Request, session_id: str, s: DBSession, action: str, *,
        mutation: bool = False,
        allow_withdrawn_safety_exit: bool = False,
        not_found_detail: str = "场次不存在") -> TrainSession:
    """Resolve and conceal one account-scoped session before state checks.

    Admission, runtime and autopilot diagnostics are intentionally absent from
    this helper.  Callers must run those gates only after this object-level
    authorization succeeds, so a historical or foreign row is
    indistinguishable from a genuinely missing id to another researcher.
    """
    sess = s.get(TrainSession, session_id)
    if sess is None:
        raise HTTPException(404, not_found_detail)
    _require_session_operator(
        request,
        sess,
        s,
        action,
        mutation=mutation,
        allow_withdrawn_safety_exit=allow_withdrawn_safety_exit,
        not_found_detail=not_found_detail,
    )
    return sess


def _preauthorize_session_subject_fence(
        request: Request, session_id: str, s: DBSession, action: str,
        *, allow_withdrawn_safety_exit: bool = False,
        not_found_detail: str = "场次不存在") -> str:
    """Authorize the object, then reset before acquiring its subject fence.

    The first pass intentionally performs no mutation/withdrawal diagnostics,
    so a foreign researcher cannot use terminal-state differences as an
    existence oracle.  Every caller must re-lock the Session row and repeat the
    mutation authorization after entering ``governance_lock.subject_fence``.
    """
    sess = _load_session_for_operator(
        request,
        session_id,
        s,
        action,
        mutation=False,
        allow_withdrawn_safety_exit=allow_withdrawn_safety_exit,
        not_found_detail=not_found_detail,
    )
    patient_id = sess.patient_id
    s.rollback()
    s.expire_all()
    return patient_id


def _require_session_read_operator(
        request: Request, sess: TrainSession, s: DBSession, action: str) -> str:
    """Authorize an account read without exposing another researcher's row.

    A foreign researcher and a genuinely missing session intentionally share
    the same public response.  Other denials retain their own contract: in
    particular, an active session still tells a data steward that terminal-only
    access is required, and write paths continue to use
    ``_require_session_operator(..., mutation=True)`` directly.
    """
    return _require_session_operator(request, sess, s, action)


def _require_session_read_operator_if_admitted(
        request: Request, sess: TrainSession, s: DBSession, action: str) -> bool:
    """Authorize every session read; report whether bedside admission exists.

    Historical/orphan rows remain available as read-only evidence to their
    exact owner (plus terminal data governance/admin), but admission failure is
    never an ownership bypass.
    """
    # Object authorization must precede admission/state diagnostics.  Otherwise
    # a foreign researcher could distinguish an orphan/corrupt session from a
    # missing id through the VisitPlan conflict returned below.
    _require_session_read_operator(request, sess, s, action)
    admitted = True
    try:
        _require_started_visit_plan_session(sess.session_id, s, sess=sess)
    except HTTPException as exc:
        detail = exc.detail
        if (exc.status_code == 409 and isinstance(detail, dict)
                and detail.get("code") == "session_visit_plan_admission_required"):
            admitted = False
        else:
            raise
    return admitted


def _expensive_rate_limit(
        request: Request, principal: str, *, resource_path: str | None = None,
) -> JSONResponse | None:
    retry = resource_limits.consume(
        request.method, resource_path or request.url.path, principal)
    if retry is None:
        return None
    return JSONResponse(
        status_code=429,
        content={"detail": "该设备或账号请求过于频繁，请稍后重试", "code": "resource_rate_limited"},
        headers={
            "Retry-After": str(max(1, math.ceil(retry))),
            "Cache-Control": "private, no-store",
            "Pragma": "no-cache",
        },
    )


@app.middleware("http")
async def console_auth_guard(request: Request, call_next):
    path = request.url.path
    raw_path_value = request.scope.get("raw_path")
    raw_path = (
        raw_path_value.decode("latin-1")
        if isinstance(raw_path_value, bytes)
        else path
    )
    # Reserved namespaces and staff answer bundles have one canonical URL
    # spelling.  Reject alternate case/separators/recursive encodings before
    # credential handling so anonymous and authenticated callers observe the
    # same 404 and no proxy/filesystem normalization can revive a static file.
    if (
        access_policy.noncanonical_protected_namespace_alias(raw_path)
        or access_policy.answer_bundle_alias_must_be_hidden(raw_path)
    ):
        return JSONResponse(status_code=404, content={
            "detail": "资源不存在",
            "code": "resource_not_found",
        })
    rule = access_policy.access_rule(request.method, path)
    if rule.kind == access_policy.AccessKind.PUBLIC:
        return await call_next(request)
    if not auth.auth_active() and rule.kind != access_policy.AccessKind.DEVICE_PAIR:
        if not _is_loopback_request(request):
            return JSONResponse(status_code=403, content={
                "detail": "未启用认证的 M0 模式只允许本机回环访问；局域网或公网必须配置研究者账号与设备配对",
                "code": "local_mode_loopback_required",
            })
        # 回环 M0 可由明确选择的处理器换取固定 LOCAL-M0 身份；默认仍保持无
        # 具名主体，避免原始录音/审计等高敏感读取因开发模式被自动放宽。
        if rule.kind == access_policy.AccessKind.ACCOUNT:
            request.state.auth_kind = "local_loopback"
        return await call_next(request)

    ip = _client_ip(request)
    pair_limit_key = f"pair:{ip}"
    if (rule.kind == access_policy.AccessKind.DEVICE_PAIR
            and auth.is_locked(pair_limit_key)):
        return JSONResponse(status_code=429, content={
            "detail": "尝试过多，暂时锁定，请稍后再试",
            "code": "auth_locked",
        })

    cookie = request.cookies.get(COOKIE_NAME)
    # Never even compare a PIN outside the one pairing endpoint.  Otherwise the
    # different response for a correct PIN becomes an unlimited six-digit oracle
    # on every DEVICE route, bypassing the pair-route lockout.
    supplied_pin = (request.headers.get("x-console-pin")
                    if rule.kind == access_policy.AccessKind.DEVICE_PAIR else None)
    supplied_capability = request.headers.get("x-device-capability")
    supplied_csrf = request.headers.get("x-csrf-token")
    unsafe_method = request.method.upper() not in {"GET", "HEAD", "OPTIONS"}
    fetch_site = request.headers.get("sec-fetch-site")
    # 浏览器明示请求不是同源时，所有受保护写路径（账号、能力、PIN 配对）
    # 都在解析凭据前拒绝。未携带 Fetch Metadata 的非浏览器客户端仍可按凭据校验。
    if unsafe_method and fetch_site and fetch_site != "same-origin":
        return JSONResponse(status_code=403, content={
            "detail": "写请求来源不是本平台同源页面",
            "code": "request_origin_rejected",
        })
    pin = os.environ.get("CONSOLE_PIN")
    pin_valid = bool(
        rule.kind == access_policy.AccessKind.DEVICE_PAIR
        and pin and supplied_pin is not None
        and secrets.compare_digest(supplied_pin.encode("utf-8"), pin.encode("utf-8"))
    )

    # 1) 会话 cookie(真实账号,携带审计身份)。display_id/role 必须在 session 打开时就取出:
    #    resolve_session 内部节流写 last_seen 会 commit,默认 expire_on_commit 令 user 过期,
    #    出了 with 块再读属性会 DetachedInstanceError → 500(每约5分钟一次)。
    actor = actor_role = None
    if cookie:
        with DBSession(db.engine) as s:
            user = auth.resolve_session(s, cookie)
            if user:
                actor, actor_role = user.display_id, user.role
    explicit_device_capability = bool(
        supplied_capability is not None
        and rule.kind in {access_policy.AccessKind.DEVICE,
                          access_policy.AccessKind.DEVICE_LIVE_WRITE})
    if (actor is not None and rule.kind != access_policy.AccessKind.DEVICE_PAIR
            and not explicit_device_capability):
        if not actor or actor_role not in rule.roles:
            # 设备路径仍可继续尝试独立 capability；账号路径则立即拒绝。
            if not (supplied_capability and rule.kind in {
                    access_policy.AccessKind.DEVICE,
                    access_policy.AccessKind.DEVICE_LIVE_WRITE}):
                return JSONResponse(status_code=403, content={
                    "detail": f"账号角色 {actor_role or 'unknown'} 无权{rule.label}",
                    "code": "role_forbidden",
                })
        elif not unsafe_method or auth.verify_csrf(cookie, supplied_csrf):
            if rule.kind == access_policy.AccessKind.DEVICE_LIVE_WRITE:
                # This multiplexed endpoint carries both console commands and
                # patient-device facts. A signed-in researcher may write the
                # former, but must never be a confused deputy for microphone or
                # persisted-audio acknowledgements.
                try:
                    live_payload = await request.json()
                except Exception:  # noqa: BLE001 - reject at the auth boundary
                    return JSONResponse(status_code=422, content={
                        "detail": "live state 载荷必须是 JSON 对象",
                        "code": "invalid_device_payload",
                    })
                live_kind = (live_payload.get("kind")
                             if isinstance(live_payload, dict) else None)
                if live_kind in access_policy.DEVICE_LIVE_WRITE_KINDS:
                    detail_code = (
                        "audio_disposition_device_capability_required"
                        if live_kind in ("audioSaved", "audioDisposalConfirmed")
                        else "device_capability_required"
                    )
                    return JSONResponse(status_code=403, content={
                        # Keep the stable top-level authorization code used by
                        # the generic access-policy client, while preserving
                        # the more specific nested recovery code expected by
                        # the audio terminal-disposition protocol. Neither
                        # response consults a raw audio id, so known and
                        # unknown assets remain indistinguishable here.
                        "detail": {
                            "code": detail_code,
                            "message": "老人端录音事实必须由当前已配对设备上报",
                        },
                        "code": "device_capability_required",
                    })
            request.state.actor = actor
            request.state.actor_role = actor_role
            request.state.auth_kind = "account"
            limited = _expensive_rate_limit(request, f"account:{actor}:{ip}")
            if limited is not None:
                return limited
            return await call_next(request)
        elif not (supplied_capability and rule.kind in {
                access_policy.AccessKind.DEVICE,
                access_policy.AccessKind.DEVICE_LIVE_WRITE}):
            return JSONResponse(status_code=403, content={
                "detail": "账号写请求缺少或携带了错误的会话防伪证明，请刷新后重试",
                "code": "csrf_required",
            })

    # 2) 短时 capability 只能授权老人端白名单，不能授权账号或配对。
    if supplied_capability is not None and rule.kind in {
            access_policy.AccessKind.DEVICE,
            access_policy.AccessKind.DEVICE_LIVE_WRITE}:
        with DBSession(db.engine) as s:
            capability_status, capability_row = device_capability.resolve_capability(
                s, supplied_capability)
            if capability_status in {
                    device_capability.CapabilityResolution.VALID,
                    device_capability.CapabilityResolution.RECOVERY_ONLY}:
                capability_session_id = capability_row.session_id
                capability_device_hash = capability_row.device_id_hash
                capability_token_hash = capability_row.token_hash
            else:
                capability_session_id = capability_device_hash = capability_token_hash = None
            # Digest-matched identity of a *rejected* row, carried out of the
            # read-only resolution purely so the terminal-pause exception below
            # can re-lock and re-resolve that exact row. INVALID never yields a
            # row, so an unknown or malformed bearer carries nothing.
            capability_row_session = (
                capability_row.session_id if capability_row is not None else None)
            capability_row_hash = (
                capability_row.token_hash if capability_row is not None else None)
        if capability_status not in {
                device_capability.CapabilityResolution.VALID,
                device_capability.CapabilityResolution.RECOVERY_ONLY}:
            # Exactly one narrow exception, and it never calls the route: an
            # expired bearer addressing its own session's autopilot-next may
            # still finish a legacy recovery whose judgement is already durable.
            # Refusing would strand that scope in processing_attempt forever,
            # while the pause needs no device, no provider and no command
            # projection. The response stays the original 401 either way.
            outcome = _try_stale_bearer_terminal_pause(
                request, path, capability_status, capability_row_session,
                capability_row_hash)
            codes = {
                device_capability.CapabilityResolution.INVALID: "device_capability_invalid",
                device_capability.CapabilityResolution.EXPIRED: "device_capability_expired",
                device_capability.CapabilityResolution.REVOKED: "device_capability_revoked",
                device_capability.CapabilityResolution.RECOVERY_ONLY:
                    "device_capability_recovery_only",
            }
            # The locked read inside that transaction is newer than this
            # preflight, so a bearer revoked or re-paired while we waited for
            # the row lock is answered with its current status, not a stale one.
            reported = (outcome.current if outcome.current in codes
                        else capability_status)
            return JSONResponse(status_code=401, content={
                "detail": "设备配对已失效，请由研究者重新配对",
                "code": codes[reported],
            })
        request.state.auth_kind = "device_capability"
        request.state.device_capability_session_id = capability_session_id
        request.state.device_capability_token_hash = capability_token_hash
        request.state.device_capability_recovery_only = (
            capability_status == device_capability.CapabilityResolution.RECOVERY_ONLY)
        limited = _expensive_rate_limit(
            request, f"device:{capability_device_hash}:{ip}")
        if limited is not None:
            return limited
        if (rule.kind == access_policy.AccessKind.DEVICE
                and capability_status == device_capability.CapabilityResolution.VALID):
            return await call_next(request)
        if (rule.kind == access_policy.AccessKind.DEVICE
                and access_policy.device_recovery_candidate(request.method, path)):
            return await call_next(request)
        if rule.kind == access_policy.AccessKind.DEVICE_LIVE_WRITE:
            # 必须在进入业务处理器前检查 kind，不能先写 session/cursor/rapportStep
            # 再“晚拒绝”。Starlette 会缓存 body，后续 Pydantic 仍可正常解析。
            try:
                payload = await request.json()
            except Exception:  # noqa: BLE001 - 留在认证边界内 fail-closed
                return JSONResponse(status_code=422, content={
                    "detail": "live state 载荷必须是 JSON 对象",
                    "code": "invalid_device_payload",
                })
            kind = payload.get("kind") if isinstance(payload, dict) else None
            if (kind in access_policy.DEVICE_LIVE_WRITE_KINDS
                    and (capability_status == device_capability.CapabilityResolution.VALID
                         # 410 终态回执/删除回执必须在 RECOVERY_ONLY 下仍可达:
                         # 场次离开 LiveState 后设备才收到 410 是常态。
                         or kind in ("audioSaved", "audioDisposalConfirmed"))):
                return await call_next(request)
        if capability_status == device_capability.CapabilityResolution.RECOVERY_ONLY:
            # Same narrow exception for a recovery-only bearer, which is the
            # usual state once the session has left LiveState.
            outcome = _try_stale_bearer_terminal_pause(
                request, path, capability_status, capability_session_id,
                capability_token_hash)
            if outcome.current in {
                    device_capability.CapabilityResolution.EXPIRED,
                    device_capability.CapabilityResolution.REVOKED}:
                # Decayed further while we held the row lock; report what the
                # locked transaction actually saw.
                return JSONResponse(status_code=401, content={
                    "detail": "设备配对已失效，请由研究者重新配对",
                    "code": ("device_capability_expired"
                             if outcome.current
                             is device_capability.CapabilityResolution.EXPIRED
                             else "device_capability_revoked"),
                })
            return JSONResponse(status_code=401, content={
                "detail": "本场设备配对只可恢复已落库的录音回执，请重新配对",
                "code": "device_capability_recovery_only",
            })
        return JSONResponse(status_code=401, content={
            "detail": "设备能力无权执行该操作",
            "code": "device_capability_forbidden",
        })

    # 3) PIN 只用于配对，不能直接访问 DEVICE / DEVICE_LIVE_WRITE。
    # bytes 比较避免非 ASCII 头触发 TypeError。
    if pin_valid and rule.kind == access_policy.AccessKind.DEVICE_PAIR:
        request.state.auth_kind = "device_pair"
        auth.record_success(pair_limit_key)
        limited = _expensive_rate_limit(request, f"device-pair:{ip}")
        if limited is not None:
            return limited
        return await call_next(request)
    # 只有"错 PIN"才计入限速。会话 cookie 是 256 位随机 token,在线爆破无意义,不计——否则
    # 会话过期/被吊销后浏览器仍带旧 cookie 每3秒轮询,会把研究者/整院共享出口 IP 误锁在门外。
    if supplied_pin is not None and rule.kind == access_policy.AccessKind.DEVICE_PAIR:
        auth.record_failure(pair_limit_key)
    if rule.kind == access_policy.AccessKind.DEVICE_PAIR:
        return JSONResponse(status_code=401, content={
            "detail": "配对需要正确的设备 PIN",
            "code": "device_pair_pin_required",
        })
    if rule.kind in {
            access_policy.AccessKind.DEVICE,
            access_policy.AccessKind.DEVICE_LIVE_WRITE}:
        return JSONResponse(status_code=401, content={
            "detail": "请先使用 PIN 完成设备配对",
            "code": "device_pair_required",
        })
    if rule.kind == access_policy.AccessKind.ACCOUNT:
        return JSONResponse(status_code=401, content={
            "detail": f"{rule.label}必须使用具名研究账号登录",
            "code": "account_required",
        })
    return JSONResponse(status_code=401, content={
        "detail": "需要登录或有效的设备配对",
        "code": "authentication_required",
    })


@app.middleware("http")
async def browser_security_guard(request: Request, call_next):
    # 部署显式给 TRUSTED_HOSTS 后拒绝 Host 投毒；不配置时保持 localhost 开发与
    # 医院临时 IP 兼容。HSTS 仍只由已正确终止 TLS 的 Caddy 设置，避免直连自签误锁。
    if not http_security.host_allowed(request.headers.get("host")):
        response = http_security.apply_security_headers(JSONResponse(
            status_code=400,
            content={"detail": "Host 不在部署允许列表", "code": "untrusted_host"},
        ))
    else:
        response = http_security.apply_security_headers(await call_next(request))
    rule = access_policy.access_rule(request.method, request.url.path)
    # Every protected research response may contain a subject id, clinical
    # state, audit metadata, an audio reference, or a credential-dependent
    # denial.  Shared clinic browsers and intermediary caches must never retain
    # those responses.  PUBLIC assets/health remain cacheable under their own
    # policy; all other surfaces are uniformly private and non-storable.
    if rule.kind != access_policy.AccessKind.PUBLIC:
        response.headers["Cache-Control"] = "private, no-store"
        response.headers["Pragma"] = "no-cache"
        vary = {part.strip() for part in response.headers.get("Vary", "").split(",")
                if part.strip()}
        vary.add("Cookie")
        if rule.kind in {
                access_policy.AccessKind.DEVICE_PAIR,
                access_policy.AccessKind.DEVICE,
                access_policy.AccessKind.DEVICE_LIVE_WRITE}:
            vary.update({"X-Console-Pin", "X-Device-Capability"})
        response.headers["Vary"] = ", ".join(sorted(vary))
    return response


# ---------------- 账号认证接口 ----------------
class LoginIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    username: str = PydanticField(min_length=1, max_length=128)
    password: str = PydanticField(min_length=1, max_length=1024)


def _secure_cookie(request: Request) -> bool:
    return request.url.scheme == "https" \
        or os.environ.get("SESSION_COOKIE_SECURE", "").strip() in ("1", "true", "yes")


def _set_csrf_cookie(response: Response, request: Request, session_token: str) -> None:
    # JS 只读派生证明，不暴露 HttpOnly 会话 bearer；host-only + Strict，绝不设 Domain。
    response.set_cookie(
        CSRF_COOKIE_NAME, auth.csrf_token(session_token), httponly=False,
        samesite="strict", secure=_secure_cookie(request), path="/", max_age=12 * 3600,
    )
    response.headers["Cache-Control"] = "private, no-store"


@app.post("/auth/login")
def auth_login(body: LoginIn, request: Request, response: Response, s: DBSession = Depends(get_session)):
    ip = _client_ip(request)
    login_limit_key = f"login:{ip}"
    if auth.is_locked(login_limit_key):
        raise HTTPException(429, "尝试过多，暂时锁定，请稍后再试")
    user = auth.authenticate(s, body.username.strip(), body.password)
    if not user:
        auth.record_failure(login_limit_key)
        raise HTTPException(401, "用户名或密码错误")
    auth.record_success(login_limit_key)
    token = auth.create_session(s, user.username)
    response.set_cookie(COOKIE_NAME, token, httponly=True, samesite="lax",
                        secure=_secure_cookie(request), path="/", max_age=12 * 3600)
    _set_csrf_cookie(response, request, token)
    try:                       # 登录事件:此时 request.state 尚无 actor,直接署名登录账号
        audit.record(s, actor=user.display_id, action="login", summary=f"登录成功 role={user.role}")
    except Exception:          # noqa: BLE001 —— 审计失败不拦登录
        _emit_operational_error("login_audit_append_failed")
    return {"display_id": user.display_id, "role": user.role, "username": user.username}


@app.post("/auth/logout")
def auth_logout(request: Request, response: Response, s: DBSession = Depends(get_session)):
    auth.revoke_session(s, request.cookies.get(COOKIE_NAME))
    response.delete_cookie(COOKIE_NAME, path="/")
    response.delete_cookie(CSRF_COOKIE_NAME, path="/")
    response.headers["Cache-Control"] = "private, no-store"
    return {"ok": True}


@app.get("/auth/me")
def auth_me(request: Request, response: Response, s: DBSession = Depends(get_session)):
    token = request.cookies.get(COOKIE_NAME)
    user = auth.resolve_session(s, token)
    if not user:
        raise HTTPException(401, "未登录")
    _set_csrf_cookie(response, request, token)
    return {"display_id": user.display_id, "role": user.role, "username": user.username}


@app.get("/auth/config")
def auth_config(s: DBSession = Depends(get_session)):
    """开放读口：告诉前端该显示哪种门(账号登录 / PIN / 全开),不泄任何凭据。"""
    return {
        "auth_required": auth.auth_active(),
        "accounts_enabled": auth.has_any_user(s),
        "pin_enabled": auth.pin_configured(),
    }


# ---------------- 研究审计账本(只读;哈希链防篡改)----------------
@app.get("/audit")
def list_audit(patient_id: str | None = None, session_id: str | None = None,
               limit: int = 200, s: DBSession = Depends(get_session)):
    """按受试者/场次过滤审计条目(最新在前)。只出元数据,永不含患者作答文本/姓名。"""
    q = select(AuditLog)
    if patient_id:
        q = q.where(AuditLog.patient_id == patient_id)
    if session_id:
        q = q.where(AuditLog.session_id == session_id)
    q = q.order_by(AuditLog.id.desc()).limit(min(max(limit, 1), 1000))
    rows = list(s.exec(q))
    return [{"id": r.id, "ts": r.ts, "actor": r.actor, "action": r.action,
             "patient_id": r.patient_id, "session_id": r.session_id, "turn_id": r.turn_id,
             "summary": r.summary, "entry_hash": r.entry_hash} for r in rows]


@app.get("/audit/verify")
def audit_verify(s: DBSession = Depends(get_session)):
    """从头重算哈希链,报告是否被篡改(ok / 断裂位置)。"""
    return audit.verify_chain(s)


@app.get("/health")
def health():
    return {"status": "ok", "service": "language-training-platform"}


# ---------------- 患者 / 场次 ----------------
_CONSENT_GRANTED_STATUSES = frozenset({
    "已同意", "已取得", "已签署", "有效",
    "consented", "obtained", "signed", "active", "valid",
})
_CONSENT_DENIED_STATUSES = frozenset({
    "未同意", "已撤回", "拒绝", "不同意",
    "denied", "withdrawn", "refused", "declined", "rejected",
})


def _simulation_enabled() -> bool:
    """模拟路径必须由部署者显式开启；公网生产默认关闭，不能由请求方单独绕过准入。"""
    return os.environ.get("ALLOW_SIMULATION_DATA", "").strip().lower() in {"1", "true", "yes"}


def _require_simulation_enabled() -> None:
    if not _simulation_enabled():
        raise HTTPException(409, "模拟数据路径未启用；仅测试/演示环境可设置 ALLOW_SIMULATION_DATA=1")


def _research_eligibility_issues(patient: Patient) -> list[str]:
    """真实研究场次的最小、可审计准入条件。空值一律不是通过。"""
    issues: list[str] = []
    if patient.is_simulation_subject:
        issues.append("is_simulation_subject=true 的模拟档案不得进入真实研究")
    consent_status = (patient.consent_status or "").strip().casefold()
    if consent_status not in _CONSENT_GRANTED_STATUSES:
        issues.append("consent_status 未明确为已同意/有效")
    if patient.consent_type is None:
        issues.append("consent_type 未填写")
    elif patient.consent_type == ConsentType.代理同意加本人赞同:
        if patient.proxy_consent is not True:
            issues.append("代理同意路径要求 proxy_consent 明确为 true")
        if patient.assent_obtained is not True:
            issues.append("代理同意路径要求 assent_obtained 明确为 true")
    if patient.mandarin_eligible is not True:
        issues.append("mandarin_eligible 必须明确为 true")
    if patient.recording_allowed is not True:
        issues.append("recording_allowed 必须明确为 true")
    if (patient.withdrawal_status or "").strip():
        issues.append(f"withdrawal_status={patient.withdrawal_status}")
    return issues


def _simulation_subject_issues(patient: Patient) -> list[str]:
    """模拟可以缺少尚未获取的同意/录音值，但绝不得绕过任何明确拒绝。"""
    issues: list[str] = []
    if patient.is_simulation_subject is not True:
        issues.append("模拟场次只能绑定 is_simulation_subject=true 的专用档案")
    consent_status = (patient.consent_status or "").strip().casefold()
    if consent_status in _CONSENT_DENIED_STATUSES:
        issues.append(f"consent_status={patient.consent_status} 明确禁止处理")
    if patient.recording_allowed is False:
        issues.append("recording_allowed=false 不得被模拟路径绕过")
    if (patient.withdrawal_status or "").strip():
        issues.append(f"withdrawal_status={patient.withdrawal_status}")
    return issues


def _require_simulation_subject(patient: Patient) -> None:
    issues = _simulation_subject_issues(patient)
    if issues:
        raise HTTPException(409, "受试者未满足模拟隔离要求：" + "；".join(issues))


def _expected_data_classification(is_simulation: bool) -> str:
    return "simulation" if is_simulation else "research"


_RESEARCH_ATTEMPT_FIELDS = frozenset({
    # Explicit allowlist: processing_owner/lease/claimed_at/generation are worker
    # fencing implementation details and must never cross a research read API.
    "id", "session_id", "item_id", "turn_seq", "response_role", "attempt_seq",
    "raw_audio_id", "prompt_level", "cue_type", "duration_seconds", "asr_text",
    "asr_confidence", "asr_engine_version", "operational_answer_type",
    "operational_score", "operational_needs_review", "judge_mode",
    "judge_engine_version", "judge_reason", "matched_on", "contains_target",
    "judge_portrait_used", "processing_status", "error_code", "created_at",
    "processed_at", "is_simulation",
})


def _research_attempt_projection(attempt: AttemptEvent) -> dict:
    """Project an attempt for research readers without worker lease internals.

    An allowlist is deliberate: adding a future internal column to AttemptEvent
    cannot silently expose it through /attempts or /journal.
    """
    return attempt.model_dump(include=_RESEARCH_ATTEMPT_FIELDS)


def _session_read_restriction_reason(
        sess: TrainSession, s: DBSession) -> str | None:
    patient = s.get(Patient, sess.patient_id)
    if patient is not None:
        if (patient.withdrawal_status or "").strip():
            return "subject_withdrawn"
        if (patient.consent_status or "").strip().casefold() in _CONSENT_DENIED_STATUSES:
            # A legacy record may express study withdrawal only through the
            # consent field.  Existing session content must still fail closed.
            return "subject_withdrawn"
    withdrawn_audio = s.exec(select(AudioAssetRow.raw_audio_id).where(
        AudioAssetRow.session_id == sess.session_id,
        or_(
            AudioAssetRow.withdrawn.is_(True),
            func.trim(func.coalesce(AudioAssetRow.withdrawal_status, "")) != "",
        ),
    )).first()
    if withdrawn_audio is not None:
        return "recording_withdrawn"
    return None


def _session_content_counts(session_id: str, s: DBSession) -> dict[str, int]:
    """Return content-free governance metadata for a withdrawn session."""
    return {
        "items": int(s.exec(select(func.count(ItemEvent.id)).where(
            ItemEvent.session_id == session_id)).one() or 0),
        "turns": int(s.exec(select(func.count(TurnEvent.id)).join(
            ItemEvent, TurnEvent.item_event_id == ItemEvent.id).where(
                ItemEvent.session_id == session_id)).one() or 0),
        "audios": int(s.exec(select(func.count(AudioAssetRow.raw_audio_id)).where(
            AudioAssetRow.session_id == session_id)).one() or 0),
        "abnormal": int(s.exec(select(func.count(AbnormalEvent.id)).where(
            AbnormalEvent.session_id == session_id)).one() or 0),
        "attempts": int(s.exec(select(func.count(AttemptEvent.id)).where(
            AttemptEvent.session_id == session_id)).one() or 0),
        "interactions": int(s.exec(select(func.count(InteractionEvent.id)).where(
            InteractionEvent.session_id == session_id)).one() or 0),
        "audio_receipts": int(s.exec(select(func.count(AudioCaptureReceipt.server_seq)).where(
            AudioCaptureReceipt.session_id == session_id)).one() or 0),
    }


def _withdrawn_session_tombstone(
        sess: TrainSession, s: DBSession, *, reason_code: str,
        counts: dict[str, int] | None = None) -> dict:
    """Content-free projection retained only for governance reconciliation."""
    return {
        "schema_version": 1,
        "session_id": sess.session_id,
        "content_available": False,
        "reason_code": reason_code,
        "record_counts": counts if counts is not None else _session_content_counts(
            sess.session_id, s),
    }


def _raise_withdrawn_session_read_conflict(
        sess: TrainSession, *, resource: str, reason_code: str) -> NoReturn:
    raise HTTPException(
        status_code=409,
        detail={
            "code": "subject_withdrawn_content_unavailable",
            "message": "受试者或录音已撤回；该研究内容读取已关闭",
            "session_id": sess.session_id,
            "resource": resource,
            "reason_code": reason_code,
        },
        headers={"Cache-Control": "private, no-store", "Pragma": "no-cache"},
    )


def _require_classified_session(sess: TrainSession) -> None:
    expected = _expected_data_classification(sess.is_simulation)
    if sess.data_classification != expected:
        raise HTTPException(
            409,
            f"场次 data_classification={sess.data_classification} 未明确归类为 {expected}，"
            "旧数据须先完成受控分类",
        )


def _require_research_eligible(patient: Patient) -> None:
    issues = _research_eligibility_issues(patient)
    if issues:
        raise HTTPException(409, "受试者未满足真实研究准入：" + "；".join(issues))


@app.post("/patients", response_model=Patient)
def create_patient(p: Patient, s: DBSession = Depends(get_session)):
    if s.get(Patient, p.patient_id):
        raise HTTPException(409, f"patient_id {p.patient_id} 已存在")
    server_owned_cloud_fields = (
        p.cloud_processing_allowed,
        p.cloud_processing_provider_id,
        p.cloud_processing_notice_version,
        p.cloud_processing_consented_at,
        p.cloud_processing_revoked_at,
    )
    if any(value is not None for value in server_owned_cloud_fields):
        raise HTTPException(
            422, "云处理授权及 provider/告知版本必须通过具名账号 PATCH 由服务器写入")
    if (p.withdrawal_status or "").strip() or p.governance_revision != 0:
        raise HTTPException(
            422, "研究撤回状态与治理版本由服务器账本管理，建档请求不得自报")
    s.add(p)
    s.commit()
    s.refresh(p)
    return p


@app.get("/patients")
def list_patients(request: Request, s: DBSession = Depends(get_session)):
    """受试者登记表(准备区/训练台/分析后台的选择列表)。只出研究编号与合规摘要,无姓名。
    附场次数与最近训练日,供研究者按编号选人——绝不显示场次编号(那是后台概念)。"""
    patients = list(s.exec(select(Patient).order_by(Patient.patient_id)))
    sessions = list(s.exec(select(TrainSession)))
    withdrawal_events = {
        row.patient_id: row for row in s.exec(select(PatientWithdrawalEvent))
    }
    runtime_statuses = {
        row.session_id: row.status for row in s.exec(select(SessionRuntimeState))
    }
    role = getattr(request.state, "actor_role", None)
    actor = (_actor(request) or "").strip()

    # Patient rows form the study roster and are intentionally shared with the
    # named study team.  Session aggregates are different: exposing a global
    # count in the recovery picker lets one researcher infer that another
    # operator has a session even though every session detail route correctly
    # rejects that read.  Scope the aggregate projection to exactly the same
    # lifecycle visibility policy as the session list endpoint.
    if role == "researcher":
        sessions = [
            row for row in sessions
            if (row.trainer_id or "").strip() == actor
        ]
    elif role == "data_steward":
        sessions = [
            row for row in sessions
            if runtime_statuses.get(row.session_id, "active")
            in _DATA_STEWARD_VISIBLE_SESSION_STATUSES
        ]
    by_patient: dict[str, list] = {}
    for sess in sessions:
        by_patient.setdefault(sess.patient_id, []).append(sess)
    out = []
    for p in patients:
        rows = by_patient.get(p.patient_id, [])
        dates = [r.training_date for r in rows if r.training_date]
        eligibility_issues = _research_eligibility_issues(p)
        withdrawal_event = withdrawal_events.get(p.patient_id)
        out.append({
            "patient_id": p.patient_id,
            "is_simulation_subject": p.is_simulation_subject,
            "dementia_severity": p.dementia_severity,
            "mandarin_eligible": p.mandarin_eligible,
            "consent_status": p.consent_status,
            "consent_type": p.consent_type.value if hasattr(p.consent_type, "value") else p.consent_type,
            "recording_allowed": p.recording_allowed,
            "cloud_processing_allowed": p.cloud_processing_allowed,
            "cloud_processing_provider_id": p.cloud_processing_provider_id,
            "cloud_processing_notice_version": p.cloud_processing_notice_version,
            "withdrawal_status": p.withdrawal_status,
            "governance_revision": p.governance_revision,
            "withdrawal_event_id": (
                withdrawal_event.event_id if withdrawal_event else None),
            "withdrawal_reason_code": (
                withdrawal_event.reason_code if withdrawal_event else None),
            "withdrawal_occurred_at": (
                withdrawal_event.occurred_at if withdrawal_event else None),
            "research_eligible": not eligibility_issues,
            "research_eligibility_issues": eligibility_issues,
            "session_count": len(rows),
            "unfinished_session_count": sum(
                runtime_statuses.get(row.session_id, "active") in _MUTABLE_RUNTIME_STATUSES
                for row in rows
            ),
            "last_training_date": max(dates).isoformat() if dates else None,
        })
    return out


@app.get("/patients/{patient_id}", response_model=Patient)
def get_patient(patient_id: str, s: DBSession = Depends(get_session)):
    p = s.get(Patient, patient_id)
    if not p:
        raise HTTPException(404, "患者不存在")
    return p


_WITHDRAWAL_REASONS = frozenset({
    "participant_request",
    "representative_request",
    "clinical_safety",
    "ethics_or_protocol",
})


class PatientWithdrawalIn(BaseModel):
    """Closed, replay-safe command; no free-text explanation is accepted."""
    model_config = ConfigDict(extra="forbid")

    idempotency_key: str = PydanticField(
        min_length=32,
        max_length=200,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]+$",
    )
    expected_governance_revision: int = PydanticField(ge=0)
    reason_code: Literal[
        "participant_request",
        "representative_request",
        "clinical_safety",
        "ethics_or_protocol",
    ]

    @model_validator(mode="after")
    def reject_low_entropy_key(self):
        # Length alone is not entropy: a copied placeholder such as 32 repeated
        # characters must not become the durable retry identity for withdrawal.
        if len(set(self.idempotency_key)) < 8:
            raise ValueError("idempotency_key 随机度不足")
        return self


def _withdrawal_request_fingerprint(
        patient_id: str, body: PatientWithdrawalIn) -> str:
    canonical = "\x00".join((
        patient_id,
        str(body.expected_governance_revision),
        body.reason_code,
    ))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _withdrawal_receipt(
        event: PatientWithdrawalEvent, *, idempotent: bool) -> dict:
    return {
        "schema_version": 1,
        "event_id": event.event_id,
        "patient_id": event.patient_id,
        "withdrawal_status": "withdrawn",
        "consent_status": "withdrawn",
        "expected_governance_revision": event.expected_revision,
        "governance_revision": event.new_revision,
        "reason_code": event.reason_code,
        "actor_display_id": event.actor_display_id,
        "actor_role": event.actor_role,
        "occurred_at": event.occurred_at,
        "affected_session_count": event.affected_session_count,
        "affected_audio_count": event.affected_audio_count,
        "request_fingerprint": event.request_fingerprint,
        "idempotent": idempotent,
    }


def _project_subject_withdrawal_to_live(
        live: LiveState | None, session_id: str, now: datetime) -> bool:
    """Remove stale bedside training without depending on a loadable protocol."""
    if live is None or _live_session_id(live) != session_id:
        return False
    live.session_json = _json.dumps({
        "sessionId": session_id,
        "paused": False,
        "runtimeStatus": "aborted",
        "wseq": _allocate_live_wseq(live),
    }, ensure_ascii=False)
    cursor = _json_load(live.cursor_json)
    if cursor is not None:
        safe = _safe_cursor(cursor)
        safe.update({
            "sessionId": session_id,
            "screen": "thanks",
            "recording": "stopped",
            "selfStart": False,
            "wseq": _allocate_live_wseq(live),
        })
        live.cursor_json = _json.dumps(safe, ensure_ascii=False)
    else:
        live.cursor_json = None
    rapport = _json_load(live.rapport_json)
    if rapport is not None:
        safe_rapport = _safe_rapport(rapport)
        safe_rapport.update({
            "sessionId": session_id,
            "recording": "stopped",
            "paused": True,
            "wseq": _allocate_live_wseq(live),
        })
        live.rapport_json = _json.dumps(safe_rapport, ensure_ascii=False)
    else:
        live.rapport_json = None
    live.audio_json = None
    live.patient_rec_json = None
    live.patient_current_screen = "thanks"
    live.seq += 1
    live.updated_at = now
    return True


@app.post("/patients/{patient_id}/withdrawal")
def withdraw_patient(
        patient_id: str, body: PatientWithdrawalIn, request: Request,
        response: Response, s: DBSession = Depends(get_session)):
    """Atomically fence processing and append the subject withdrawal receipt."""
    actor_id = _require_account_identity(
        request, "登记研究撤回", roles={"admin"})
    actor_role = getattr(request.state, "actor_role", None)
    if actor_role != "admin":
        raise HTTPException(403, "当前账号无权登记研究撤回")
    key_hash = hashlib.sha256(body.idempotency_key.encode("utf-8")).hexdigest()
    fingerprint = _withdrawal_request_fingerprint(patient_id, body)

    # Never acquire a transaction advisory lock and then roll it back before
    # the protected predicate has been read.  VisitPlan.start owns this same
    # subject fence through its commit, so the session enumeration below is a
    # stable, authoritative re-read rather than a pre-lock snapshot.
    s.rollback()
    s.expire_all()
    with (cloud_processing.serialized_subject_egress(patient_id),
          governance_lock.subject_fence(s, patient_id),
          audio_capture.registration_lock(),
          audio_capture.byte_quota_lock(),
          _LIVE_WRITE_LOCK,
          device_capability.serialized_mutation()):
        replay = s.exec(select(PatientWithdrawalEvent).where(
            PatientWithdrawalEvent.idempotency_key_sha256 == key_hash,
        ).with_for_update()).first()
        if replay is not None:
            if (replay.patient_id != patient_id
                    or replay.request_fingerprint != fingerprint):
                raise HTTPException(status_code=409, detail={
                    "code": "withdrawal_idempotency_conflict",
                    "message": "该幂等标识已绑定另一份研究撤回请求",
                })
            response.headers["Cache-Control"] = "private, no-store"
            response.headers["Pragma"] = "no-cache"
            receipt = _withdrawal_receipt(replay, idempotent=True)
            s.rollback()
            return receipt

        # Cross-worker row-lock order remains shared with export finalization:
        # Session(s) -> Patient -> pending ExportBatch(es) -> Audio(s).  The
        # subject advisory fence additionally prevents a new Session predicate
        # member from appearing between this query and commit.
        sessions = list(s.exec(select(TrainSession).where(
            TrainSession.patient_id == patient_id,
        ).order_by(TrainSession.session_id).with_for_update()))
        # Assessment facts remain historically truthful (never forged closed
        # or completed), but the current event set is locked in this same
        # subject transaction so an assessment mutation cannot cross withdrawal.
        list(s.exec(select(AssessmentEvent).where(
            AssessmentEvent.patient_id == patient_id,
        ).order_by(AssessmentEvent.event_id).with_for_update()))
        patient = s.exec(select(Patient).where(
            Patient.patient_id == patient_id,
        ).with_for_update()).first()
        if patient is None:
            raise HTTPException(404, "患者不存在")
        if patient.is_simulation_subject:
            raise HTTPException(status_code=409, detail={
                "code": "subject_withdrawal_research_only",
                "message": "研究撤回账本只用于真实研究档案；模拟档案不得冒充受试者撤回",
            })
        if (patient.withdrawal_status or "").strip():
            raise HTTPException(status_code=409, detail={
                "code": "subject_already_withdrawn",
                "message": "受试者已进入研究撤回终态；普通接口不能恢复",
                "governance_revision": patient.governance_revision,
            })
        if patient.governance_revision != body.expected_governance_revision:
            raise HTTPException(status_code=409, detail={
                "code": "patient_governance_revision_conflict",
                "message": "受试者治理状态已变化，请刷新权威档案后重新确认",
                "governance_revision": patient.governance_revision,
            })

        now = datetime.now(timezone.utc).replace(tzinfo=None)
        session_ids = [row.session_id for row in sessions]
        # ExportBatch intentionally stores no raw session id.  Lock every
        # not-yet-published batch in stable order; finalizers lock their exact
        # session/patient before one of these rows, so no patient can publish
        # across a withdrawal that already owns this governance transaction.
        list(s.exec(select(ExportBatch).where(
            ExportBatch.status.in_(["staging", "artifacts_ready"]),
        ).order_by(ExportBatch.batch_id).with_for_update()))
        audio_rows = list(s.exec(select(AudioAssetRow).where(
            AudioAssetRow.session_id.in_(session_ids),
        ).order_by(AudioAssetRow.raw_audio_id).with_for_update())) if session_ids else []

        # The patient row is the first DB governance lock.  Every later capture,
        # pairing, runtime, or AI commit rechecks it under the same LiveState
        # serialization boundary, so withdrawal wins before any new fact commits.
        patient.withdrawal_status = "withdrawn"
        patient.consent_status = "withdrawn"
        patient.cloud_processing_allowed = False
        patient.cloud_processing_revoked_at = now
        patient.governance_revision += 1
        s.add(patient)

        live = _live_row_for_update(s)
        for sess in sessions:
            try:
                autopilot_service.fence_autonomous_scope_for_external_stop(
                    s,
                    session_id=sess.session_id,
                    reason_code="subject_withdrawal",
                    source="subject_withdrawal",
                    actor_type="researcher",
                    actor_id=actor_id,
                    now=now,
                )
            except autopilot_service.AutopilotServiceError as exc:
                _autopilot_write_failure(s, exc)

            runtime_state = s.exec(select(SessionRuntimeState).where(
                SessionRuntimeState.session_id == sess.session_id,
            ).with_for_update()).first()
            # Withdrawal closes both the bedside plane and the asynchronous
            # research-review window. Leaving ``intervention_completed`` in
            # place would still admit confirmation/closeout/completion writes.
            if (runtime_state is None
                    or runtime_state.status in _RESEARCH_REVIEW_RUNTIME_STATUSES):
                runtime_state = runtime_state or SessionRuntimeState(
                    session_id=sess.session_id, status="active", revision=0)
                runtime_state.status = "aborted"
                runtime_state.aborted_at = now
                runtime_state.completed_at = None
                runtime_state.ended_by = actor_id
                runtime_state.end_reason = "subject_withdrawal"
                runtime_state.revision += 1
                runtime_state.updated_at = now
                s.add(runtime_state)
                _project_subject_withdrawal_to_live(
                    live, sess.session_id, now)

            capabilities = list(s.exec(select(PatientDeviceCapability).where(
                PatientDeviceCapability.session_id == sess.session_id,
            ).with_for_update()))
            for capability in capabilities:
                if capability.revoked_at is None:
                    capability.active_session_key = None
                    capability.recovery_only_at = capability.recovery_only_at or now
                    s.add(capability)

        for audio in audio_rows:
            audio.withdrawn = True
            audio.withdrawal_status = "isolated_by_subject_withdrawal"
            s.add(audio)
        if live is not None:
            s.add(live)

        event = PatientWithdrawalEvent(
            event_id="withdrawal-" + secrets.token_urlsafe(24),
            patient_id=patient_id,
            expected_revision=body.expected_governance_revision,
            new_revision=patient.governance_revision,
            idempotency_key_sha256=key_hash,
            request_fingerprint=fingerprint,
            reason_code=body.reason_code,
            actor_display_id=actor_id,
            actor_role=actor_role,
            occurred_at=now,
            affected_session_count=len(sessions),
            affected_audio_count=len(audio_rows),
        )
        s.add(event)
        try:
            s.commit()
        except IntegrityError:
            s.rollback()
            winner = s.exec(select(PatientWithdrawalEvent).where(
                PatientWithdrawalEvent.idempotency_key_sha256 == key_hash,
            )).first()
            if (winner is not None and winner.patient_id == patient_id
                    and winner.request_fingerprint == fingerprint):
                response.headers["Cache-Control"] = "private, no-store"
                response.headers["Pragma"] = "no-cache"
                return _withdrawal_receipt(winner, idempotent=True)
            raise HTTPException(status_code=409, detail={
                "code": "patient_withdrawal_concurrent_conflict",
                "message": "研究撤回并发写入未安全收敛，请读取权威回执",
            })
        s.refresh(event)

    _audit(
        s, request, "subject_withdrawal",
        f"研究撤回 reason={event.reason_code} revision={event.new_revision} "
        f"sessions={event.affected_session_count} audios={event.affected_audio_count}",
        patient_id=patient_id,
    )
    response.headers["Cache-Control"] = "private, no-store"
    response.headers["Pragma"] = "no-cache"
    return _withdrawal_receipt(event, idempotent=False)


@app.get("/patients/{patient_id}/withdrawal")
def get_patient_withdrawal(
        patient_id: str, request: Request, response: Response,
        s: DBSession = Depends(get_session)):
    _require_account_identity(
        request, "读取研究撤回回执",
        roles={"data_steward", "admin"})
    patient = s.get(Patient, patient_id)
    if patient is None:
        raise HTTPException(404, "患者不存在")
    event = s.exec(select(PatientWithdrawalEvent).where(
        PatientWithdrawalEvent.patient_id == patient_id,
    ).order_by(PatientWithdrawalEvent.occurred_at.desc())).first()
    if event is None or patient.withdrawal_status != "withdrawn":
        raise HTTPException(status_code=404, detail={
            "code": "subject_withdrawal_not_found",
            "message": "该受试者没有权威研究撤回回执",
        })
    response.headers["Cache-Control"] = "private, no-store"
    response.headers["Pragma"] = "no-cache"
    return _withdrawal_receipt(event, idempotent=True)


@app.get("/governance/withdrawn-audio")
def list_withdrawn_audio_governance(
        request: Request, response: Response, patient_id: str | None = None,
        s: DBSession = Depends(get_session)):
    _require_account_identity(
        request, "读取撤回录音治理列表", roles={"admin"})
    query = select(AudioAssetRow, TrainSession).join(
        TrainSession, AudioAssetRow.session_id == TrainSession.session_id,
    ).where(or_(
        AudioAssetRow.withdrawn.is_(True),
        func.trim(func.coalesce(AudioAssetRow.withdrawal_status, "")) != "",
    ))
    if patient_id:
        query = query.where(TrainSession.patient_id == patient_id)
    rows = list(s.exec(query.order_by(
        TrainSession.patient_id,
        AudioAssetRow.session_id,
        AudioAssetRow.raw_audio_id,
    )))
    # 本地副本删除回执聚合:哪些设备声称已删、最近一次何时。0 回执必须显式可见,
    # 面板才能回答"这位受试者的录音副本是否已从所有设备清除"。
    disposal_rows = list(s.exec(
        select(
            AudioLocalCopyDisposalReceipt.raw_audio_id,
            func.count(AudioLocalCopyDisposalReceipt.id),
            func.max(AudioLocalCopyDisposalReceipt.reported_at),
        ).group_by(AudioLocalCopyDisposalReceipt.raw_audio_id)))
    disposal_by_raw_id = {
        raw_id: (int(count), latest)
        for raw_id, count, latest in disposal_rows
    }

    def _disposal_projection(raw_id: str) -> tuple[int, str | None]:
        # reported_at 库内是 naive-UTC;对外序列化必须带显式时区,
        # 否则面板会把 UTC 当本地时间直显(北京差 8 小时)。
        count, latest = disposal_by_raw_id.get(raw_id, (0, None))
        if latest is None:
            return count, None
        if isinstance(latest, datetime):
            return count, latest.replace(tzinfo=timezone.utc).isoformat()
        return count, f"{latest}+00:00"

    response.headers["Cache-Control"] = "private, no-store"
    response.headers["Pragma"] = "no-cache"
    projected = []
    for audio, sess in rows:
        disposal_count, disposal_last_at = _disposal_projection(
            audio.raw_audio_id)
        projected.append({
            "raw_audio_id": audio.raw_audio_id,
            "session_id": audio.session_id,
            "patient_id": sess.patient_id,
            "status": audio.status,
            "withdrawn": audio.withdrawn,
            "withdrawal_status": audio.withdrawal_status,
            "delete_gate_passed": audio.delete_gate_passed,
            "local_copy_disposal_device_count": disposal_count,
            "local_copy_disposal_last_at": disposal_last_at,
        })
    return projected


class CloudProcessingExpectedState(BaseModel):
    """研究者确认时看到的服务器授权快照；所有字段都必须逐项匹配。"""
    model_config = ConfigDict(extra="forbid")

    allowed: bool | None
    provider_id: str | None
    notice_version: str | None
    consented_at: datetime | None
    revoked_at: datetime | None
    withdrawal_status: str | None
    governance_revision: int = PydanticField(ge=0)


class CloudProcessingConsentIn(BaseModel):
    """条件写：浏览器表达决定，服务器校验快照并独占写入自己的 policy 事实。"""
    model_config = ConfigDict(extra="forbid")

    allowed: bool
    expected: CloudProcessingExpectedState
    policy_provider_id: str | None = None
    policy_notice_version: str | None = None

    @model_validator(mode="after")
    def require_policy_snapshot_for_grant(self):
        if self.allowed and (
                not (self.policy_provider_id or "").strip()
                or not (self.policy_notice_version or "").strip()):
            raise ValueError("允许云处理时必须提交刚刚阅读的 provider 与告知版本")
        return self


@app.get("/cloud-processing/policy")
def get_cloud_processing_policy():
    policy = cloud_processing.current_policy()
    return {
        "configured": policy.configured,
        "provider_id": policy.provider_id,
        "notice_version": policy.notice_version,
        "data_categories": [
            "原始回答音频（可能包含声纹）",
            "语音转写文本",
            "题目与运行判类上下文",
        ],
    }


@app.patch("/patients/{patient_id}/cloud-processing", response_model=Patient)
def patch_patient_cloud_processing(
        patient_id: str, body: CloudProcessingConsentIn, request: Request,
        s: DBSession = Depends(get_session)):
    _require_account_identity(
        request, "更新受试者云处理授权", roles={"researcher", "admin"},
        allow_local_m0=True)
    # The subject egress lock is acquired before every database/control lock.
    # A successful revoke therefore waits for an already-started provider call,
    # while a call that has not crossed its final authorization fence observes
    # the revocation and never begins egress.
    with cloud_processing.serialized_subject_egress(patient_id), _LIVE_WRITE_LOCK:
        patient = s.exec(select(Patient).where(
            Patient.patient_id == patient_id).with_for_update()).first()
        if not patient:
            raise HTTPException(404, "患者不存在")
        # Research withdrawal is the terminal governance authority.  Neither a
        # new grant nor a superficially idempotent revoke may rewrite its
        # timestamp or audit meaning after that authority has been recorded.
        if (patient.withdrawal_status or "").strip():
            raise HTTPException(status_code=409, detail={
                "code": "subject_withdrawn_cloud_processing_locked",
                "message": "受试者已撤回；云处理授权事实已封存，不得再次写入",
            })
        expected = body.expected
        current_snapshot = (
            patient.cloud_processing_allowed,
            patient.cloud_processing_provider_id,
            patient.cloud_processing_notice_version,
            patient.cloud_processing_consented_at,
            patient.cloud_processing_revoked_at,
            patient.withdrawal_status,
            patient.governance_revision,
        )
        expected_snapshot = (
            expected.allowed,
            expected.provider_id,
            expected.notice_version,
            expected.consented_at,
            expected.revoked_at,
            expected.withdrawal_status,
            expected.governance_revision,
        )
        if current_snapshot != expected_snapshot:
            raise HTTPException(status_code=409, detail={
                "code": "cloud_processing_state_changed",
                "message": "云处理授权或撤回状态已变化；本次未写入，请重新读取档案并再次确认",
            })
        now = datetime.now()
        if body.allowed:
            policy = cloud_processing.current_policy()
            if not policy.configured:
                raise HTTPException(409, "当前部署未配置完整云处理 policy，不能取得授权")
            if (body.policy_provider_id != policy.provider_id
                    or body.policy_notice_version != policy.notice_version):
                raise HTTPException(status_code=409, detail={
                    "code": "cloud_processing_policy_changed",
                    "message": "云处理方或告知版本已变化；本次未写入，请阅读最新告知后再次确认",
                })
            patient.cloud_processing_allowed = True
            patient.cloud_processing_provider_id = policy.provider_id
            patient.cloud_processing_notice_version = policy.notice_version
            patient.cloud_processing_consented_at = now
            patient.cloud_processing_revoked_at = None
            action = "cloud_processing_consent_granted"
            summary = (
                f"allowed=true provider={policy.provider_id} "
                f"notice_version={policy.notice_version}")
        else:
            patient.cloud_processing_allowed = False
            patient.cloud_processing_revoked_at = now
            action = "cloud_processing_consent_revoked"
            summary = (
                f"allowed=false provider={patient.cloud_processing_provider_id or 'none'} "
                f"notice_version={patient.cloud_processing_notice_version or 'none'}")
            sessions = list(s.exec(select(TrainSession).where(
                TrainSession.patient_id == patient_id)))
            for sess in sessions:
                # Revocation is a patient-level governance fact and must never be
                # rolled back because an old/direct Session row has no admissible
                # started VisitPlan.  Such rows are already isolated from every
                # live path, so there is no autonomous/runtime plane to stop.
                try:
                    _require_started_visit_plan_session(
                        sess.session_id,
                        s,
                        sess=sess,
                        allow_test_escape=False,
                    )
                except HTTPException as exc:
                    detail = exc.detail
                    if (exc.status_code == 409 and isinstance(detail, dict)
                            and detail.get("code")
                            == "session_visit_plan_admission_required"):
                        continue
                    raise
                try:
                    autopilot_service.fence_autonomous_scope_for_external_stop(
                        s,
                        session_id=sess.session_id,
                        reason_code="cloud_processing_revoked",
                        source="cloud_processing_consent_revoked",
                        actor_type="system",
                    )
                except autopilot_service.AutopilotServiceError as exc:
                    _autopilot_write_failure(s, exc)
                state = s.get(SessionRuntimeState, sess.session_id)
                # 缺 runtime 行按隐式 active 处理；已 paused/终态不伪造新的状态变化。
                if state is None or state.status == "active":
                    _pause_runtime_in_transaction(sess.session_id, s)
        s.add(patient)
        s.commit()
        s.refresh(patient)
    # 审计只记录受控元数据，不记录回答、告知自由文本或其他画像。
    _audit(s, request, action, summary, patient_id=patient_id)
    return patient


@app.get("/patients/{patient_id}/sessions")
def list_patient_sessions(patient_id: str, request: Request,
                          s: DBSession = Depends(get_session)):
    """只读恢复入口：列出患者既有场次，便于异常中断后取回续做。"""
    if not s.get(Patient, patient_id):
        raise HTTPException(404, "患者不存在")
    rows = list(s.exec(select(TrainSession)
                       .where(TrainSession.patient_id == patient_id)
                       .order_by(TrainSession.training_date, TrainSession.session_sitting_no,
                                 TrainSession.session_id)))
    runtime_statuses = {
        row.session_id: row.status for row in s.exec(
            select(SessionRuntimeState).where(
                SessionRuntimeState.session_id.in_([item.session_id for item in rows])))
    } if rows else {}
    role = getattr(request.state, "actor_role", None)
    actor = _actor(request)
    visible = []
    for row in rows:
        status = runtime_statuses.get(row.session_id, "active")
        if role == "data_steward" and status not in _DATA_STEWARD_VISIBLE_SESSION_STATUSES:
            continue
        if (role == "researcher"
                and (row.trainer_id or "").strip() != (actor or "")):
            continue
        visible.append({**row.model_dump(), "runtime_status": status})
    return visible


# ---------------- 测试前训练安排 / 今日队列 ----------------
_VISIT_PLAN_WRITE_LOCK = threading.RLock()


def _direct_session_test_escape_enabled() -> bool:
    """Keep legacy fixture construction explicit and impossible in a deployment.

    The direct-session endpoint exists only so isolated pytest fixtures can seed
    historical rows.  Requiring pytest's per-test marker as well as the opt-in
    flag prevents an accidentally copied deployment environment variable from
    reopening either direct creation or runtime admission.
    """
    return (
        os.environ.get("NMU_TEST_ALLOW_DIRECT_SESSION_CREATE", "").strip().lower()
        in {"1", "true", "yes"}
        and bool(os.environ.get("PYTEST_CURRENT_TEST"))
    )


def _visit_plan_admission_conflict() -> NoReturn:
    raise HTTPException(status_code=409, detail={
        "code": "session_visit_plan_admission_required",
        "message": (
            "该场次未关联一条完整且已启动的训练安排，已隔离；"
            "不能进入床旁运行、采集或 AI 自动干预"
        ),
    })


def _require_started_visit_plan_session(
        session_id: str, s: DBSession, *, sess: TrainSession | None = None,
        allow_test_escape: bool = True,
) -> TrainSession:
    """Admit only the exact Session atomically created by VisitPlan.start.

    Historical/direct rows remain available through read-only research and
    governance endpoints, but they never gain an implicit grandfather path into
    live control, recording, attempt processing, or autopilot.  Beyond the link
    and status, all server-owned plan/session binding facts are rechecked so a
    manually attached or partially corrupted row cannot borrow another plan.
    """
    resolved = sess or s.get(TrainSession, session_id)
    if resolved is None:
        raise HTTPException(404, "场次不存在")
    if allow_test_escape and _direct_session_test_escape_enabled():
        return resolved

    plan_id = (resolved.visit_plan_id or "").strip()
    plan = s.get(VisitPlan, plan_id) if plan_id else None
    if plan is None or plan.status != "started":
        _visit_plan_admission_conflict()
    runtime_state = s.get(SessionRuntimeState, session_id)

    def enum_value(value: object) -> str:
        raw = getattr(value, "value", value)
        return str(raw)

    binding_matches = (
        plan.patient_id == resolved.patient_id
        # A due plan may legitimately be started late; start_plan records the
        # actual research-local date on Session while preserving the originally
        # scheduled date on the immutable plan.
        and resolved.training_date is not None
        and plan.scheduled_date <= resolved.training_date
        and plan.session_sitting_no == resolved.session_sitting_no
        and plan.week_no == resolved.week_no
        and enum_value(plan.phase_type) == enum_value(resolved.phase_type)
        and enum_value(plan.event_line) == enum_value(resolved.event_line)
        and plan.item_bank_version_id == resolved.item_bank_version_id
        # Every frozen definition binding start_plan copied onto the Session,
        # compared only for equality: a genuine pre-protocol pair is
        # ``NULL == NULL`` and must still be admitted, so that its one legacy
        # recovery can finish and the scope can be closed out safely. Requiring
        # a non-null repeat binding here would strand exactly that data.
        and plan.item_bank_definition_digest
        == resolved.item_bank_definition_digest
        and plan.autopilot_protocol_version_id
        == resolved.autopilot_protocol_version_id
        and plan.autopilot_protocol_definition_digest
        == resolved.autopilot_protocol_definition_digest
        and plan.repeat_protocol_version_id
        == resolved.repeat_protocol_version_id
        and plan.repeat_protocol_definition_digest
        == resolved.repeat_protocol_definition_digest
        and plan.autopilot_profile_version_id
        == resolved.autopilot_profile_version_id
        and plan.autopilot_profile_definition_digest
        == resolved.autopilot_profile_definition_digest
        # Temporary D1A gate: no runtime, worker, ACK or completion consumer
        # resolves a demo profile yet, so even a Plan/Session pair that matches
        # perfectly stays out of bedside control.  Remove this clause only once
        # every D1B consumer reads the same Session-frozen resolver.
        and plan.autopilot_profile_version_id is None
        and plan.autopilot_profile_definition_digest is None
        and resolved.autopilot_profile_version_id is None
        and resolved.autopilot_profile_definition_digest is None
        and plan.is_simulation == resolved.is_simulation
        and plan.data_classification == resolved.data_classification
        and bool((plan.protocol_slot_key or "").strip())
        and bool((plan.approved_by or "").strip())
        and plan.approved_at is not None
        and bool((plan.started_by or "").strip())
        and (resolved.trainer_id or "").strip() == (plan.started_by or "").strip()
        and plan.started_at is not None
        and runtime_state is not None
    )
    if not binding_matches:
        _visit_plan_admission_conflict()
    return resolved


def _raise_visit_plan_error(exc: visit_plan_service.VisitPlanError) -> NoReturn:
    raise HTTPException(status_code=exc.status_code, detail={
        "code": exc.code,
        "message": exc.message,
    }) from exc


def _visit_plan_write_conflict(s: DBSession, exc: IntegrityError) -> NoReturn:
    s.rollback()
    raise HTTPException(status_code=409, detail={
        "code": "visit_plan_concurrency_conflict",
        "message": "训练安排已被其他请求变更，请刷新后重试",
    }) from exc


def _visit_plan_audit_summary(
        result: visit_plan_contract.VisitPlanReceipt) -> str:
    """训练安排审计只保留研究者可理解的受控元数据。

    plan_id 是后台并发/幂等绑定键，不是业务语义；将它写入可视审计
    摘要会让分析界面泄露内部编号。关联性由 AuditLog.patient_id/session_id
    的结构化列保留，摘要不再重复 opaque id。
    """
    return (
        f"status={result.status} revision={result.revision} "
        f"date={result.scheduled_date.isoformat()} week={result.week_no}"
    )


@app.post(
    "/visit-plans",
    response_model=visit_plan_contract.VisitPlanReceipt,
)
def create_visit_plan(
        body: visit_plan_contract.VisitPlanCreateIn,
        request: Request,
        s: DBSession = Depends(get_session)):
    actor_id = _require_account_identity(
        request, "创建训练安排", roles={"researcher", "admin"},
        allow_local_m0=True)
    with _VISIT_PLAN_WRITE_LOCK:
        s.rollback()
        s.expire_all()
        try:
            with governance_lock.subject_fence(s, body.patient_id):
                result = visit_plan_service.create_plan(
                    s, body=body, actor_id=actor_id)
                s.commit()
        except visit_plan_service.VisitPlanError as exc:
            s.rollback()
            _raise_visit_plan_error(exc)
        except IntegrityError as exc:
            _visit_plan_write_conflict(s, exc)
    _audit(
        s, request, "visit_plan_create",
        _visit_plan_audit_summary(result),
        patient_id=result.patient_id,
    )
    return result


@app.get(
    "/visit-plans/today",
    response_model=visit_plan_contract.VisitPlanTodayOut,
)
def get_today_visit_plans(
        request: Request, response: Response,
        s: DBSession = Depends(get_session)):
    viewer_actor_id = _require_account_identity(
        request, "读取今日训练安排", allow_local_m0=True)
    try:
        result = visit_plan_service.today_queue(
            s,
            viewer_actor_id=viewer_actor_id,
            viewer_role=getattr(request.state, "actor_role", None),
        )
    except visit_plan_service.VisitPlanError as exc:
        s.rollback()
        _raise_visit_plan_error(exc)
    response.headers["Cache-Control"] = "private, no-store"
    response.headers["Pragma"] = "no-cache"
    return result


@app.get(
    "/visit-plans",
    response_model=list[visit_plan_contract.VisitPlanReceipt],
)
def list_visit_plans(
        patient_id: str, request: Request,
        response: Response,
        s: DBSession = Depends(get_session)):
    viewer_actor_id = _require_account_identity(
        request, "读取受试者训练安排", allow_local_m0=True)
    include_withdrawn = getattr(request.state, "actor_role", None) == "admin"
    try:
        result = visit_plan_service.list_for_patient(
            s,
            patient_id=patient_id,
            include_withdrawn=include_withdrawn,
            viewer_actor_id=viewer_actor_id,
            viewer_role=getattr(request.state, "actor_role", None),
        )
    except visit_plan_service.VisitPlanError as exc:
        s.rollback()
        _raise_visit_plan_error(exc)
    response.headers["Cache-Control"] = "private, no-store"
    response.headers["Pragma"] = "no-cache"
    return result


def _mutate_visit_plan(
        *, plan_id: str,
        body: visit_plan_contract.VisitPlanMutationIn,
        request: Request,
        action: Literal["approve", "start"],
        s: DBSession,
) -> visit_plan_contract.VisitPlanReceipt:
    actor_id = _require_account_identity(
        request, f"{'审批' if action == 'approve' else '启动'}训练安排",
        roles={"researcher", "admin"}, allow_local_m0=True)
    with _VISIT_PLAN_WRITE_LOCK:
        s.rollback()
        s.expire_all()
        try:
            patient_id = visit_plan_service.patient_id_for_plan_fence(
                s, plan_id)
            # The preflight is not authoritative. End its read transaction,
            # acquire the subject predicate fence (plus actor for start), then
            # re-lock/revalidate the plan and keep the fence through commit.
            s.rollback()
            s.expire_all()
            operation = (
                visit_plan_service.approve_plan
                if action == "approve" else visit_plan_service.start_plan)
            fence = (
                governance_lock.subject_actor_fence(
                    s, patient_id, actor_id)
                if action == "start"
                else governance_lock.subject_fence(s, patient_id)
            )
            with fence:
                result = operation(
                    s, plan_id=plan_id, body=body, actor_id=actor_id)
                s.commit()
        except visit_plan_service.VisitPlanError as exc:
            s.rollback()
            _raise_visit_plan_error(exc)
        except IntegrityError as exc:
            _visit_plan_write_conflict(s, exc)
    _audit(
        s, request, f"visit_plan_{action}",
        _visit_plan_audit_summary(result),
        patient_id=result.patient_id,
        session_id=result.session_id,
    )
    return result


@app.post(
    "/visit-plans/{plan_id}/approve",
    response_model=visit_plan_contract.VisitPlanReceipt,
)
def approve_visit_plan(
        plan_id: str, body: visit_plan_contract.VisitPlanMutationIn,
        request: Request, s: DBSession = Depends(get_session)):
    return _mutate_visit_plan(
        plan_id=plan_id, body=body, request=request, action="approve", s=s)


@app.post(
    "/visit-plans/{plan_id}/start",
    response_model=visit_plan_contract.VisitPlanReceipt,
)
def start_visit_plan(
        plan_id: str, body: visit_plan_contract.VisitPlanMutationIn,
        request: Request, s: DBSession = Depends(get_session)):
    return _mutate_visit_plan(
        plan_id=plan_id, body=body, request=request, action="start", s=s)


@app.post(
    "/visit-plans/{plan_id}/cancel",
    response_model=visit_plan_contract.VisitPlanReceipt,
)
def cancel_visit_plan(
        plan_id: str, body: visit_plan_contract.VisitPlanCancelIn,
        request: Request, s: DBSession = Depends(get_session)):
    actor_id = _require_account_identity(
        request, "取消训练安排", roles={"researcher", "admin"},
        allow_local_m0=True)
    with _VISIT_PLAN_WRITE_LOCK:
        s.rollback()
        s.expire_all()
        try:
            patient_id = visit_plan_service.patient_id_for_plan_fence(
                s, plan_id)
            s.rollback()
            s.expire_all()
            with governance_lock.subject_fence(s, patient_id):
                result = visit_plan_service.cancel_plan(
                    s, plan_id=plan_id, body=body, actor_id=actor_id)
                s.commit()
        except visit_plan_service.VisitPlanError as exc:
            s.rollback()
            _raise_visit_plan_error(exc)
        except IntegrityError as exc:
            _visit_plan_write_conflict(s, exc)
    _audit(
        s, request, "visit_plan_cancel",
        _visit_plan_audit_summary(result),
        patient_id=result.patient_id,
    )
    return result


# ---------------- 独立正式评估事件（前测 / 后测 / 随访）----------------


def _raise_assessment_error(
        exc: assessment_service.AssessmentServiceError) -> NoReturn:
    raise HTTPException(status_code=exc.status_code, detail={
        "code": exc.code,
        "message": exc.message,
    }) from exc


def _assessment_integrity_conflict(
        s: DBSession, exc: IntegrityError) -> NoReturn:
    s.rollback()
    raise HTTPException(status_code=409, detail={
        "code": "assessment_concurrency_conflict",
        "message": "正式评估事实已被另一请求变更，请刷新后重试",
    }) from exc


def _assessment_actor_context(
        request: Request, action: str, *, roles: set[str]) -> tuple[str, str]:
    actor_id = _require_account_identity(
        request, action, roles=roles, allow_local_m0=True)
    actor_role = getattr(request.state, "actor_role", None)
    if actor_id == "LOCAL-M0":
        actor_role = "local_m0"
    if actor_role not in {"researcher", "data_steward", "admin", "local_m0"}:
        raise HTTPException(403, f"当前账号无权{action}")
    return actor_id, actor_role


def _assessment_authorize_event(
        s: DBSession, request: Request, event_id: str, *, action: str,
        mutation: bool = False) -> tuple[AssessmentEvent, str, str]:
    roles = {"researcher", "admin"} if mutation else {
        "researcher", "data_steward", "admin"}
    actor_id, actor_role = _assessment_actor_context(
        request, action, roles=roles)
    event = s.get(AssessmentEvent, event_id)
    if event is None:
        raise HTTPException(404, "正式评估事件不存在")
    if actor_role in {"researcher", "local_m0"}:
        if (event.assigned_assessor_id or "").strip() != actor_id:
            # 不向另一研究员暴露事件存在、状态或撤回事实。
            raise HTTPException(404, "正式评估事件不存在")
    if actor_role == "data_steward" and event.status not in {"closed", "cancelled"}:
        raise HTTPException(status_code=403, detail={
            "code": "assessment_terminal_read_required",
            "message": "数据管理员只能读取已收口或已取消的正式评估",
        })
    if mutation:
        _require_assessment_subject_readable(s, event.patient_id)
    return event, actor_id, actor_role


def _assessment_authorize_instance(
        s: DBSession, request: Request, instance_id: str, *, action: str,
        mutation: bool = True,
) -> tuple[AssessmentInstance, AssessmentEvent, str, str]:
    instance = s.get(AssessmentInstance, instance_id)
    if instance is None:
        # 先用通用事件不存在口径，避免形成 instance id 探针。
        _assessment_actor_context(
            request, action,
            roles={"researcher", "admin"} if mutation else {
                "researcher", "data_steward", "admin"})
        raise HTTPException(404, "正式评估实例不存在")
    event, actor_id, actor_role = _assessment_authorize_event(
        s, request, instance.event_id, action=action, mutation=mutation)
    if instance.patient_id != event.patient_id:
        raise HTTPException(status_code=409, detail={
            "code": "assessment_state_invalid",
            "message": "正式评估实例与事件的受试者绑定不一致",
        })
    return instance, event, actor_id, actor_role


def _require_assessment_subject_readable(
        s: DBSession, patient_id: str) -> Patient:
    patient = s.get(Patient, patient_id)
    if patient is None:
        raise HTTPException(404, "受试者档案不存在")
    if session_admission.patient_content_sealed(patient):
        raise HTTPException(status_code=409, detail={
            "code": "subject_withdrawn_content_unavailable",
            "message": "受试者已撤回或拒绝；正式评估内容读取已关闭",
            "patient_id": patient_id,
        })
    return patient


def _require_assessment_write_readiness(*, scoring: bool = False) -> dict:
    readiness = scale_protocol.scale_protocol_readiness()
    required_true = (
        "definition_ready",
        "definition_artifact_enforcement_ready",
        "definition_artifacts_ready",
        "formal_result_contract_ready",
        "workflow_policy_ready",
        "workflow_contract_ready",
        "workflow_policy_enforcement_ready",
        "workflow_ready",
        "ready_for_research",
        "instance_creation_enabled",
    )
    ready = (
        readiness.get("schema_version") == "scale-protocol-readiness.v4"
        and readiness.get("training_metrics_are_formal_scale_results") is False
        and all(readiness.get(field) is True for field in required_true)
        and (not scoring or readiness.get("automatic_scoring_enabled") is True)
    )
    if not ready:
        blockers = readiness.get("blocking_issues")
        blocker_codes = [
            row.get("code") for row in blockers
            if isinstance(row, dict) and isinstance(row.get("code"), str)
        ] if isinstance(blockers, list) else []
        raise HTTPException(status_code=409, detail={
            "code": "formal_assessment_not_ready",
            "message": (
                "正式两表的 PI 定义、授权运行时包、工作流政策或平台合同尚未全部就绪；"
                "当前保持只读，禁止建立或推进正式评估"
            ),
            "readiness_status": readiness.get("status", "not_ready"),
            "blocking_codes": blocker_codes,
        })
    return readiness


def _assessment_read_headers(response: Response) -> None:
    response.headers["Cache-Control"] = "private, no-store"
    response.headers["Pragma"] = "no-cache"


@app.get(
    "/assessment-events/today",
    response_model=assessment_contract.AssessmentTodayOut,
)
def get_today_assessment_events(
        request: Request, response: Response,
        as_of_date: date | None = None,
        s: DBSession = Depends(get_session)):
    actor_id, actor_role = _assessment_actor_context(
        request, "读取今日正式评估待办", roles={"researcher", "admin"})
    target_date = as_of_date or visit_plan_service._research_today()  # noqa: SLF001
    assigned = None if actor_role == "admin" else actor_id
    try:
        result = assessment_service.today_events(
            s, as_of_date=target_date, assigned_assessor_id=assigned)
    except assessment_service.AssessmentServiceError as exc:
        s.rollback()
        _raise_assessment_error(exc)
    _assessment_read_headers(response)
    return result


@app.get(
    "/patients/{patient_id}/assessment-events",
    response_model=list[assessment_contract.AssessmentEventOut],
)
def list_patient_assessment_events(
        patient_id: str, request: Request, response: Response,
        s: DBSession = Depends(get_session)):
    actor_id, actor_role = _assessment_actor_context(
        request, "读取受试者正式评估事件",
        roles={"researcher", "data_steward", "admin"})
    _require_assessment_subject_readable(s, patient_id)
    statement = select(AssessmentEvent).where(
        AssessmentEvent.patient_id == patient_id)
    if actor_role in {"researcher", "local_m0"}:
        statement = statement.where(
            AssessmentEvent.assigned_assessor_id == actor_id)
    elif actor_role == "data_steward":
        statement = statement.where(
            AssessmentEvent.status.in_({"closed", "cancelled"}))
    rows = list(s.exec(statement.order_by(
        AssessmentEvent.scheduled_date,
        AssessmentEvent.created_at,
        AssessmentEvent.event_id,
    )))
    try:
        result = [assessment_service.event_receipt(s, row.event_id) for row in rows]
    except assessment_service.AssessmentServiceError as exc:
        s.rollback()
        _raise_assessment_error(exc)
    _assessment_read_headers(response)
    return result


@app.get(
    "/assessment-events/{event_id}",
    response_model=assessment_contract.AssessmentEventOut,
)
def get_assessment_event(
        event_id: str, request: Request, response: Response,
        s: DBSession = Depends(get_session)):
    event, _actor_id, _actor_role = _assessment_authorize_event(
        s, request, event_id, action="读取正式评估事件")
    _require_assessment_subject_readable(s, event.patient_id)
    try:
        result = assessment_service.event_receipt(s, event_id)
    except assessment_service.AssessmentServiceError as exc:
        s.rollback()
        _raise_assessment_error(exc)
    _assessment_read_headers(response)
    return result


@app.post(
    "/patients/{patient_id}/assessment-events",
    response_model=assessment_contract.AssessmentEventOut,
)
def create_assessment_event(
        patient_id: str, body: assessment_contract.CreateAssessmentEventIn,
        request: Request, s: DBSession = Depends(get_session)):
    actor_id, actor_role = _assessment_actor_context(
        request, "建立正式评估事件", roles={"researcher", "admin"})
    _require_assessment_subject_readable(s, patient_id)
    _require_assessment_write_readiness()
    s.rollback()
    s.expire_all()
    try:
        with governance_lock.subject_fence(s, patient_id):
            _require_assessment_write_readiness()
            result = assessment_service.create_event(
                s,
                patient_id=patient_id,
                body=body,
                assigned_assessor_id=actor_id,
                actor_id=actor_id,
                actor_role=actor_role,
            )
            s.commit()
    except assessment_service.AssessmentServiceError as exc:
        s.rollback()
        _raise_assessment_error(exc)
    except IntegrityError as exc:
        _assessment_integrity_conflict(s, exc)
    _audit(
        s, request, "assessment_event_create",
        f"event={result.event_id} timepoint={result.timepoint} "
        f"status={result.status} revision={result.revision}",
        patient_id=result.patient_id,
    )
    return result


def _assessment_event_mutation_preflight(
        event_id: str, request: Request, s: DBSession, action: str,
) -> tuple[str, str, str, str]:
    event, actor_id, actor_role = _assessment_authorize_event(
        s, request, event_id, action=action, mutation=True)
    # Authorization deliberately precedes readiness diagnostics, so another
    # researcher's opaque id cannot be used as a platform-state oracle.
    _require_assessment_write_readiness()
    patient_id = event.patient_id
    assigned_assessor_id = event.assigned_assessor_id
    s.rollback()
    s.expire_all()
    return patient_id, assigned_assessor_id, actor_id, actor_role


@app.post(
    "/assessment-events/{event_id}/start",
    response_model=assessment_contract.AssessmentEventOut,
)
def start_assessment_event(
        event_id: str, body: assessment_contract.StartAssessmentEventIn,
        request: Request, s: DBSession = Depends(get_session)):
    patient_id, assigned_id, actor_id, actor_role = (
        _assessment_event_mutation_preflight(
            event_id, request, s, "启动正式评估事件"))
    try:
        with governance_lock.subject_actor_fence(s, patient_id, assigned_id):
            _assessment_authorize_event(
                s, request, event_id, action="启动正式评估事件", mutation=True)
            _require_assessment_write_readiness()
            try:
                visit_plan_service.assert_patient_ready_for_new_work(
                    s,
                    patient_id,
                    exclude_assessment_event_id=event_id,
                )
                visit_plan_service.assert_actor_ready_for_new_work(
                    s,
                    assigned_id,
                    exclude_assessment_event_id=event_id,
                )
            except visit_plan_service.VisitPlanError as exc:
                s.rollback()
                raise HTTPException(status_code=exc.status_code, detail={
                    "code": exc.code,
                    "message": exc.message,
                }) from exc
            result = assessment_service.start_event(
                s, event_id=event_id, body=body,
                actor_id=actor_id, actor_role=actor_role)
            s.commit()
    except assessment_service.AssessmentServiceError as exc:
        s.rollback()
        _raise_assessment_error(exc)
    except IntegrityError as exc:
        _assessment_integrity_conflict(s, exc)
    _audit(
        s, request, "assessment_event_start",
        f"event={result.event_id} status={result.status} revision={result.revision}",
        patient_id=result.patient_id,
    )
    return result


@app.post(
    "/assessment-events/{event_id}/cancel",
    response_model=assessment_contract.AssessmentEventOut,
)
def cancel_assessment_event(
        event_id: str, body: assessment_contract.CancelAssessmentEventIn,
        request: Request, s: DBSession = Depends(get_session)):
    patient_id, _assigned_id, actor_id, actor_role = (
        _assessment_event_mutation_preflight(
            event_id, request, s, "取消正式评估事件"))
    try:
        with governance_lock.subject_fence(s, patient_id):
            _assessment_authorize_event(
                s, request, event_id, action="取消正式评估事件", mutation=True)
            _require_assessment_write_readiness()
            result = assessment_service.cancel_event(
                s, event_id=event_id, body=body,
                actor_id=actor_id, actor_role=actor_role)
            s.commit()
    except assessment_service.AssessmentServiceError as exc:
        s.rollback()
        _raise_assessment_error(exc)
    except IntegrityError as exc:
        _assessment_integrity_conflict(s, exc)
    _audit(
        s, request, "assessment_event_cancel",
        f"event={result.event_id} reason={body.reason_code} revision={result.revision}",
        patient_id=result.patient_id,
    )
    return result


def _assessment_instance_mutation_preflight(
        instance_id: str, request: Request, s: DBSession, action: str,
) -> tuple[str, str, str, str]:
    _instance, event, actor_id, actor_role = _assessment_authorize_instance(
        s, request, instance_id, action=action, mutation=True)
    _require_assessment_write_readiness()
    patient_id = event.patient_id
    event_id = event.event_id
    s.rollback()
    s.expire_all()
    return patient_id, event_id, actor_id, actor_role


def _assessment_artifact_authorizer(
        db: DBSession, event, instance, item_key: str,
        item_revision: int, digest: str) -> bool:
    """逐题录音授权收据核销:五元组绑定+未消费,消费与响应同事务(收据 150 S3)。"""
    row = db.exec(select(AssessmentRecordingAuthorization).where(
        AssessmentRecordingAuthorization.authorization_digest == digest,
    ).with_for_update()).first()
    if row is None or row.consumed_at is not None:
        return False
    if (row.event_id != event.event_id
            or row.instance_id != instance.instance_id
            or row.patient_id != event.patient_id
            or row.item_key != item_key
            or row.item_revision != item_revision):
        return False
    row.consumed_at = datetime.now()
    db.add(row)
    db.flush()
    return True


class IssueRecordingAuthorizationIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    item_key: str = PydanticField(min_length=1, max_length=200)


@app.post("/assessment-instances/{instance_id}/recording-authorizations")
def issue_assessment_recording_authorization(
        instance_id: str, body: IssueRecordingAuthorizationIn,
        request: Request, s: DBSession = Depends(get_session)):
    """签发一次性逐题录音授权收据;绑定当前 item_revision,越期即作废须重签。"""
    patient_id, event_id, actor_id, _actor_role = (
        _assessment_instance_mutation_preflight(
            instance_id, request, s, "签发逐题录音授权"))
    try:
        with governance_lock.subject_fence(s, patient_id):
            instance, event, _aid, _arole = _assessment_authorize_instance(
                s, request, instance_id,
                action="签发逐题录音授权", mutation=True)
            _require_assessment_write_readiness()
            if event.status != "in_progress" or instance.status != "in_progress":
                raise HTTPException(status_code=409, detail={
                    "code": "assessment_state_invalid",
                    "message": "只有进行中的评估实例可以签发录音授权",
                })
            registered = assessment_service.registered_definition_for(
                s, event, instance)
            frozen_keys = {item.item_key for item in registered.snapshot.items}
            if body.item_key not in frozen_keys:
                raise HTTPException(status_code=409, detail={
                    "code": "assessment_item_unknown",
                    "message": "条目不在冻结定义内,拒绝签发录音授权",
                })
            latest_revision = assessment_service.latest_item_revision(
                s, instance_id=instance_id, item_key=body.item_key)
            item_revision = latest_revision + 1
            authorization_id = f"ara_{secrets.token_hex(12)}"
            digest = "sha256:" + hashlib.sha256("\x00".join((
                event.patient_id, event_id, instance_id, body.item_key,
                str(item_revision), secrets.token_hex(16),
            )).encode("utf-8")).hexdigest()
            row = AssessmentRecordingAuthorization(
                authorization_id=authorization_id,
                event_id=event_id,
                instance_id=instance_id,
                patient_id=event.patient_id,
                item_key=body.item_key,
                item_revision=item_revision,
                authorization_digest=digest,
                issued_by=actor_id,
            )
            s.add(row)
            s.commit()
    except assessment_service.AssessmentServiceError as exc:
        s.rollback()
        _raise_assessment_error(exc)
    except IntegrityError as exc:
        _assessment_integrity_conflict(s, exc)
    _audit(
        s, request, "assessment_recording_authorization",
        f"event={event_id} instance={instance_id} item={body.item_key} "
        f"revision={item_revision}",
        patient_id=patient_id,
    )
    return {
        "authorization_id": authorization_id,
        "authorized_artifact_digest": digest,
        "item_key": body.item_key,
        "item_revision": item_revision,
    }


@app.put(
    "/assessment-instances/{instance_id}/responses/{item_key}",
    response_model=assessment_contract.AssessmentEventOut,
)
def submit_assessment_response(
        instance_id: str, item_key: str,
        body: assessment_contract.SubmitAssessmentResponseIn,
        request: Request, s: DBSession = Depends(get_session)):
    patient_id, event_id, actor_id, actor_role = (
        _assessment_instance_mutation_preflight(
            instance_id, request, s, "保存正式评估条目响应"))
    try:
        with governance_lock.subject_fence(s, patient_id):
            _assessment_authorize_instance(
                s, request, instance_id,
                action="保存正式评估条目响应", mutation=True)
            _require_assessment_write_readiness()
            result = assessment_service.submit_response(
                s, event_id=event_id, instance_id=instance_id,
                item_key=item_key, body=body,
                actor_id=actor_id, actor_role=actor_role,
                artifact_authorizer=_assessment_artifact_authorizer)
            s.commit()
    except assessment_service.AssessmentServiceError as exc:
        s.rollback()
        _raise_assessment_error(exc)
    except IntegrityError as exc:
        _assessment_integrity_conflict(s, exc)
    _audit(
        s, request, "assessment_item_response",
        f"event={result.event_id} instance={instance_id} revision={result.revision}",
        patient_id=result.patient_id,
    )
    return result


@app.post(
    "/assessment-instances/{instance_id}/complete",
    response_model=assessment_contract.AssessmentEventOut,
)
def complete_assessment_instance(
        instance_id: str, body: assessment_contract.CompleteAssessmentInstanceIn,
        request: Request, s: DBSession = Depends(get_session)):
    patient_id, event_id, actor_id, actor_role = (
        _assessment_instance_mutation_preflight(
            instance_id, request, s, "完成正式评估实例计分"))
    try:
        with governance_lock.subject_fence(s, patient_id):
            _assessment_authorize_instance(
                s, request, instance_id,
                action="完成正式评估实例计分", mutation=True)
            _require_assessment_write_readiness(scoring=True)
            result = assessment_service.complete_instance(
                s, event_id=event_id, instance_id=instance_id, body=body,
                actor_id=actor_id, actor_role=actor_role)
            s.commit()
    except assessment_service.AssessmentServiceError as exc:
        s.rollback()
        _raise_assessment_error(exc)
    except IntegrityError as exc:
        _assessment_integrity_conflict(s, exc)
    _audit(
        s, request, "assessment_instance_complete",
        f"event={result.event_id} instance={instance_id} revision={result.revision}",
        patient_id=result.patient_id,
    )
    return result


@app.post(
    "/assessment-instances/{instance_id}/approve-defer",
    response_model=assessment_contract.AssessmentEventOut,
)
def approve_assessment_deferral(
        instance_id: str, body: assessment_contract.ApproveAssessmentDeferralIn,
        request: Request, s: DBSession = Depends(get_session)):
    # Access policy and handler both require admin; local M0 is retained only
    # for isolated synthetic tests and is rejected by the service for research.
    patient_id, event_id, actor_id, actor_role = (
        _assessment_instance_mutation_preflight(
            instance_id, request, s, "批准正式评估延期"))
    if actor_role not in {"admin", "local_m0"}:
        raise HTTPException(403, "只有管理员可批准正式评估延期")
    try:
        with governance_lock.subject_fence(s, patient_id):
            _assessment_authorize_instance(
                s, request, instance_id,
                action="批准正式评估延期", mutation=True)
            _require_assessment_write_readiness()
            result = assessment_service.approve_deferral(
                s, event_id=event_id, instance_id=instance_id, body=body,
                actor_id=actor_id, actor_role=actor_role)
            s.commit()
    except assessment_service.AssessmentServiceError as exc:
        s.rollback()
        _raise_assessment_error(exc)
    except IntegrityError as exc:
        _assessment_integrity_conflict(s, exc)
    _audit(
        s, request, "assessment_deferral_approve",
        f"event={result.event_id} instance={instance_id} "
        f"reason={body.reason_code} revision={result.revision}",
        patient_id=result.patient_id,
    )
    return result


@app.post(
    "/assessment-events/{event_id}/close",
    response_model=assessment_contract.AssessmentEventOut,
)
def close_assessment_event(
        event_id: str, body: assessment_contract.CloseAssessmentEventIn,
        request: Request, s: DBSession = Depends(get_session)):
    patient_id, _assigned_id, actor_id, actor_role = (
        _assessment_event_mutation_preflight(
            event_id, request, s, "保存正式评估收尾并关闭事件"))
    try:
        with governance_lock.subject_fence(s, patient_id):
            _assessment_authorize_event(
                s, request, event_id,
                action="保存正式评估收尾并关闭事件", mutation=True)
            _require_assessment_write_readiness()
            result = assessment_service.close_event(
                s, event_id=event_id, body=body,
                actor_id=actor_id, actor_role=actor_role)
            s.commit()
    except assessment_service.AssessmentServiceError as exc:
        s.rollback()
        _raise_assessment_error(exc)
    except IntegrityError as exc:
        _assessment_integrity_conflict(s, exc)
    _audit(
        s, request, "assessment_event_close",
        f"event={result.event_id} status={result.status} revision={result.revision} "
        f"switch_allowed={bool(result.closeout and result.closeout.switch_allowed)}",
        patient_id=result.patient_id,
    )
    return result


@app.post("/sessions", response_model=TrainSession)
def create_session(sess: TrainSession, s: DBSession = Depends(get_session)):
    # 正式产品只能由已审批 VisitPlan 原子开场；保留这个处理器仅供隔离测试构造
    # 历史场次，默认部署、回环 M0 和浏览器都不能绕过计划账本。
    if not _direct_session_test_escape_enabled():
        raise HTTPException(status_code=409, detail={
            "code": "direct_session_creation_disabled",
            "message": "场次必须由已审批训练安排启动；禁止直接建立场次",
        })
    # Refused before any database work.  The server never accepts a client
    # profile digest, and a direct historical session has no VisitPlan to carry
    # a demo binding, so letting either column through would only surface as an
    # opaque IntegrityError instead of a stable contract failure.
    if (sess.autopilot_profile_version_id is not None
            or sess.autopilot_profile_definition_digest is not None):
        raise HTTPException(status_code=409, detail={
            "code": "direct_session_profile_forbidden",
            "message": "测试专用直接建场不接受自动演示计划绑定；历史场次只能为空绑定",
        })
    if s.get(TrainSession, sess.session_id):
        raise HTTPException(409, f"session_id {sess.session_id} 已存在(可取回续做)")
    patient = s.get(Patient, sess.patient_id)
    if not patient:
        raise HTTPException(404, "患者不存在，先建档")
    # 撤回对真实研究和模拟都不可绕过；模拟用于合成数据/测试，不是继续处理已撤回真人数据的后门。
    if (patient.withdrawal_status or "").strip():
        raise HTTPException(409, f"受试者撤回状态为 {patient.withdrawal_status}，不可建立新场次")
    if sess.is_simulation:
        _require_simulation_enabled()
        _require_simulation_subject(patient)
    else:
        _require_research_eligible(patient)
    # 不信任请求体自报分类；以场次模式权威派生。
    sess.data_classification = _expected_data_classification(sess.is_simulation)
    if not sess.item_bank_version_id:
        raise HTTPException(422, "场次须绑题库版本号 item_bank_version_id")
    if not 1 <= sess.week_no <= 8:
        raise HTTPException(422, "week_no 必须在 1..8")
    phase = sess.phase_type.value if hasattr(sess.phase_type, "value") else str(sess.phase_type)
    event = sess.event_line.value if hasattr(sess.event_line, "value") else str(sess.event_line)
    allowed_context = (
        (sess.week_no == 1 and phase == "关系建立" and event == "关系建立环节")
        or (sess.week_no == 1 and phase in {"基线测评", "前测"} and event == "基线测评窗")
        or (2 <= sess.week_no <= 8 and phase == "正式训练" and event == "正式训练")
    )
    if not allowed_context:
        raise HTTPException(422, "week_no / phase_type / event_line 组合不符合已定事件线")
    bank_week = (sess.week_no if sess.week_no >= 2
                 else content.RAPPORT_ANCHOR_WEEK)
    try:
        bank = content.load_item_bank_for_week(bank_week)
    except content.TrainingWeekContentUnavailable as exc:
        raise HTTPException(409, str(exc))
    if sess.item_bank_version_id != bank.version_id:
        raise HTTPException(409, f"场次版本 {sess.item_bank_version_id} 与当前题库 {bank.version_id} 不符")
    readiness = content.content_readiness(bank)
    if not sess.is_simulation and readiness["ready_for_research"] is not True:
        raise HTTPException(
            409,
            "当前题库尚未达到真实研究冻结/质控门禁；仅可在显式模拟档案与模拟场次中使用",
        )
    if phase == "正式训练" and sess.week_no not in bank.supported_training_weeks:
        raise HTTPException(409, f"第{sess.week_no}周材料尚未结构化并双人校对，禁止建正式训练场次")
    protocol = content.load_autopilot_protocol(
        content.CONTENT_DIR / "autopilot_protocol_v1.json")
    protocol_issues = content.validate_autopilot_protocol(protocol)
    if protocol_issues:
        raise HTTPException(status_code=409, detail={
            "code": "autopilot_protocol_invalid",
            "message": "当前自动化协议不可执行",
            "issues": protocol_issues,
        })
    # Test-only construction still follows the production identity contract.
    # Never trust client-supplied digests, protocol version or plan linkage.
    sess.visit_plan_id = None
    sess.item_bank_version_id = bank.version_id
    sess.item_bank_definition_digest = content.item_bank_definition_digest(bank)
    sess.autopilot_protocol_version_id = str(protocol["protocol_version_id"])
    sess.autopilot_protocol_definition_digest = (
        content.autopilot_protocol_definition_digest(protocol))
    s.add(sess)
    s.commit()
    s.refresh(sess)
    return sess


@app.get("/sessions/{session_id}", response_model=TrainSession)
def get_train_session(session_id: str, request: Request,
                      s: DBSession = Depends(get_session)):
    sess = s.get(TrainSession, session_id)
    if not sess:
        raise HTTPException(404, "场次不存在")
    _require_session_read_operator_if_admitted(
        request, sess, s, "读取或恢复场次")
    return sess


# ---------------- 内容 ----------------
_CONTENT_BUNDLE_HEADERS = {
    "Cache-Control": "private, no-store",
    "Pragma": "no-cache",
    "Content-Type": "application/json; charset=utf-8",
    "Accept-Ranges": "none",
}


def _require_staff_content_bundle_access(request: Request) -> None:
    # Middleware is the primary account boundary.  Keep a handler-local check
    # as defense in depth for direct function calls or future middleware order
    # changes.  Explicit loopback M0 remains available only to the documented
    # local development mode; deployed network access still requires an account.
    _require_account_identity(
        request,
        "读取工作人员端冻结内容包",
        roles={"researcher", "data_steward", "admin"},
        allow_local_m0=True,
    )


def _content_bundle_unavailable() -> NoReturn:
    raise HTTPException(status_code=503, detail={
        "code": "staff_content_bundle_unavailable",
        "message": "工作人员端冻结内容包未通过服务端校验，当前不可读取",
    })


def _content_bundle_response(request: Request, payload: dict) -> Response:
    """Return one fixed staff-only definition without a static-file path.

    The auth middleware has already required a named account (or explicit
    loopback M0 development identity).  HEAD deliberately exposes neither a
    filesystem path nor a reusable static-cache entry.
    """
    if request.method.upper() == "HEAD":
        return PlainResponse(status_code=200, headers=_CONTENT_BUNDLE_HEADERS)
    return JSONResponse(content=payload, headers=_CONTENT_BUNDLE_HEADERS)


@app.api_route(
    "/content/item-bank-bundle", methods=["GET", "HEAD"],
    include_in_schema=False,
)
def get_item_bank_bundle(request: Request, week: int = 2):
    _require_staff_content_bundle_access(request)
    if not 2 <= week <= 8:
        raise HTTPException(422, "week 必须在 2..8")
    try:
        bank = content.load_item_bank_for_week(week)
        if content.validate_item_bank(bank)["errors"]:
            _content_bundle_unavailable()
    except HTTPException:
        raise
    except (OSError, TypeError, ValueError):
        _content_bundle_unavailable()
    return _content_bundle_response(request, content.item_bank_definition(bank))


@app.api_route(
    "/content/week1-script", methods=["GET", "HEAD"],
    include_in_schema=False,
)
def get_week1_script_bundle(request: Request):
    _require_staff_content_bundle_access(request)
    try:
        script = content.load_week1_script(content.CONTENT_DIR / "week1_script.json")
        if content.validate_week1_script(script):
            _content_bundle_unavailable()
    except HTTPException:
        raise
    except (OSError, TypeError, ValueError):
        _content_bundle_unavailable()
    return _content_bundle_response(request, script)


@app.api_route(
    "/content/autopilot-protocol", methods=["GET", "HEAD"],
    include_in_schema=False,
)
def get_autopilot_protocol_bundle(request: Request):
    _require_staff_content_bundle_access(request)
    try:
        protocol = content.load_autopilot_protocol(
            content.CONTENT_DIR / "autopilot_protocol_v1.json")
        if content.validate_autopilot_protocol(protocol):
            _content_bundle_unavailable()
    except HTTPException:
        raise
    except (OSError, TypeError, ValueError):
        _content_bundle_unavailable()
    return _content_bundle_response(request, protocol)


@app.get("/content/item-bank")
def get_item_bank():
    # 就绪探针保持第 2 周语义(P0a 自动化验收探针);逐周结构化状态由
    # structured_training_weeks / training_week_content 字段另行发布。
    bank = content.load_item_bank_for_week(2)
    protocol = content.load_autopilot_protocol(
        content.CONTENT_DIR / "autopilot_protocol_v1.json")
    readiness = content.content_readiness(bank)
    protocol_issues = content.validate_autopilot_protocol(protocol)
    item_bank_digest = content.item_bank_definition_digest(bank)
    protocol_digest = content.autopilot_protocol_definition_digest(protocol)
    # ``unsupported_operational_rubrics`` is a useful content-QC projection,
    # but it only covers three open-answer roles in each double-element item.
    # The runtime admission contract is stronger: every position in the exact
    # server-built plan must have all frozen fields and interaction protocol.
    # Publish that same full-plan scan to the console so it can never announce
    # readiness from the smaller rubric subset while the server would refuse.
    positions = autopilot_positions.build_positions(
        bank, week_no=2, event_line="正式训练")
    position_gaps = tuple(
        gap
        for position in positions
        if (gap := autopilot_positions.readiness_gap(bank, position)) is not None
    )
    gap_counts = {
        code: sum(gap.code == code for gap in position_gaps)
        for code in (
            "source_field_unavailable",
            "operational_rubric_unavailable",
            "operational_protocol_unavailable",
        )
    }
    source_unstructured = readiness["source_unstructured_positions"]
    source_unstructured_count = readiness["source_unstructured_position_count"]
    selector_issues: list[dict[str, object]] = []
    admission_issue: dict[str, object] | None = None
    active_repeat_protocol = repeat_intent.active_protocol()
    readiness_session = TrainSession(
        session_id="content-readiness",
        patient_id="content-readiness",
        week_no=2,
        phase_type="正式训练",
        event_line="正式训练",
        item_bank_version_id=bank.version_id,
        item_bank_definition_digest=item_bank_digest,
        autopilot_protocol_version_id=str(
            protocol.get("protocol_version_id") or ""),
        autopilot_protocol_definition_digest=protocol_digest,
        # The readiness probe must look exactly like a session a new VisitPlan
        # would create, including the frozen repeat binding.
        repeat_protocol_version_id=active_repeat_protocol.version_id,
        repeat_protocol_definition_digest=(
            active_repeat_protocol.definition_digest),
        is_simulation=True,
        data_classification="simulation",
    )
    if not protocol_issues:
        # Exercise the exact selector/allowlist contract for every structurally
        # supported position.  Known rubric/source gaps remain separately visible
        # and are not allowed to hide defects in the positions that look ready.
        for position in positions:
            if autopilot_positions.readiness_gap(bank, position) is not None:
                continue
            try:
                autopilot_service._select_p0a_content(  # noqa: SLF001
                    readiness_session,
                    bank,
                    protocol,
                    item_id=position.item_id,
                    turn_seq=position.turn_seq,
                )
            except autopilot_service.AutopilotServiceError as exc:
                selector_issues.append({
                    "item_id": position.item_id,
                    "turn_seq": position.turn_seq,
                    "response_role": position.response_role,
                    "code": exc.code,
                })
        try:
            # This is the same full-plan admission fence used by start_p0a.
            autopilot_service._require_entire_plan_supported(  # noqa: SLF001
                readiness_session, bank, protocol)
        except autopilot_service.AutopilotServiceError as exc:
            admission_issue = {
                "code": exc.code,
                "context": exc.context,
            }
    # 逐周结构化状态:索引登记且可解析的周才进清单;坏周 fail-closed 不出现。
    week_files = content.load_item_bank_index()
    training_week_content: dict[str, dict[str, object]] = {}
    for wk in sorted(week_files):
        try:
            week_bank = bank if wk == 2 else content.load_item_bank_for_week(wk)
            week_readiness = (readiness if wk == 2
                              else content.content_readiness(week_bank))
        except ValueError:
            continue
        training_week_content[str(wk)] = {
            "item_bank_version_id": week_bank.version_id,
            "qc_status": week_bank.qc_status,
            "ready_for_research": week_readiness["ready_for_research"],
        }
    return {
        "version_id": bank.version_id,
        "item_bank_definition_digest": item_bank_digest,
        "autopilot_protocol_version_id": protocol.get("protocol_version_id"),
        "autopilot_protocol_definition_digest": protocol_digest,
        "protocol_validation_issues": protocol_issues,
        "selector_validation_issues": selector_issues,
        "autopilot_admission_validation_issue": admission_issue,
        "single_count": len(bank.single_element),
        "double_count": len(bank.double_element),
        "multi_count": len(bank.multi_element),
        "supported_training_weeks": list(bank.supported_training_weeks),
        "structured_training_weeks": sorted(
            int(week) for week in training_week_content),
        "training_week_content": training_week_content,
        "qc_status": bank.qc_status,
        "ready_for_research": readiness["ready_for_research"],
        "operational_autopilot_ready": (
            not protocol_issues
            and not selector_issues
            and admission_issue is None
        ),
        "operational_position_count": len(positions),
        "unsupported_operational_position_count": len(position_gaps),
        "source_protocol_position_count": readiness[
            "source_protocol_position_count"],
        "source_unstructured_position_count": source_unstructured_count,
        "source_unstructured_positions": source_unstructured,
        "delivery_unsupported_position_count": (
            len(position_gaps) + source_unstructured_count
        ),
        "unsupported_operational_positions": [
            f"{gap.position.item_id}:{gap.position.response_role}"
            for gap in position_gaps
        ],
        "unsupported_operational_position_counts_by_code": gap_counts,
        "unsupported_operational_position_gaps": [
            {
                "item_id": gap.position.item_id,
                "turn_seq": gap.position.turn_seq,
                "response_role": gap.position.response_role,
                "code": gap.code,
                "detail": gap.detail,
            }
            for gap in position_gaps
        ],
        "unsupported_operational_rubrics": readiness["unsupported_operational_rubrics"],
        "source_document_sha256": bank.meta.get("source_document_sha256"),
        "source_normalized_text_sha256": bank.meta.get(
            "source_normalized_text_sha256"),
        "draft_revision": bank.meta.get("draft_revision"),
        "errata_fixed": bank.errata_fixed,
        "errors": readiness["errors"], "warnings": readiness["warnings"],
    }


@app.get("/content/scale-protocol")
def get_scale_protocol_readiness():
    """Expose the frozen category contract without inventing an instrument."""
    return scale_protocol.scale_protocol_readiness()


# ---------------- 评分（接纯函数）----------------
class SingleItemIn(BaseModel):
    item_id: str
    final_correct: int
    spontaneous_correct: int
    prompt_level: int
    duration_seconds: float | None = None


class DoubleItemIn(BaseModel):
    item_id: str
    left_name: int
    left_function: int
    right_name: int
    right_function: int
    relation: float


class MultiItemIn(BaseModel):
    item_id: str
    key_elements: dict


@app.post("/score/single")
def score_single(items: list[SingleItemIn]):
    try:
        return scoring.score_single_element(
            [scoring.SingleElementItem(**i.model_dump()) for i in items])
    except ValueError as e:
        raise HTTPException(422, str(e))


@app.post("/score/double")
def score_double(items: list[DoubleItemIn]):
    try:
        return scoring.score_double_element(
            [scoring.DoubleElementItem(**i.model_dump()) for i in items])
    except ValueError as e:
        raise HTTPException(422, str(e))


@app.post("/score/multi")
def score_multi(items: list[MultiItemIn]):
    try:
        return scoring.score_multi_element(
            [scoring.MultiElementItem(**i.model_dump()) for i in items])
    except ValueError as e:
        raise HTTPException(422, str(e))


# ---------------- 判分入口：★画像守卫在边界 ----------------
@app.post("/judge/build-input")
def judge_build_input(payload: dict):
    """构造判分输入。若载荷混入任何画像字段 → 400（画像不进判分的边界防线）。"""
    try:
        ji = judging.build_judge_input(**payload)
    except judging.PortraitLeakError as e:
        raise HTTPException(400, str(e))
    except (ValueError, TypeError) as e:
        raise HTTPException(422, str(e))
    return {"resolved_text": judging.resolve_response_text(ji),
            "judge_portrait_used": ji.judge_portrait_used}


# ---------------- 音频删除闸门 ----------------
def _row_to_asset(r: AudioAssetRow) -> audio_gate.AudioAsset:
    return audio_gate.AudioAsset(raw_audio_id=r.raw_audio_id, status=r.status,
                                 is_reliability_sample=r.is_reliability_sample, withdrawn=r.withdrawn)


def _load_row(raw_audio_id: str, s: DBSession) -> AudioAssetRow:
    r = s.get(AudioAssetRow, raw_audio_id)
    if not r:
        raise HTTPException(404, "音频不存在")
    return r


def _require_authoritative_export_copy(
        r: AudioAssetRow, s: DBSession) -> Path:
    """Revalidate the immutable export ledger and its exact controlled copy.

    Status alone is never deletion authority: upgraded databases may contain
    legacy ``exported``/``checksum_verified``/``deletable`` rows created before
    ExportBatch receipts existed.  Every normal online step toward physical
    deletion therefore rechecks the batch, manifest, artifacts and bytes.
    """
    if not r.export_batch_id:
        raise HTTPException(409, "音频尚未经权威场次导出，无受控副本可验证")
    if s.get(ExportBatch, r.export_batch_id) is None:
        raise HTTPException(status_code=409, detail={
            "code": "legacy_export_batch_unverified",
            "message": (
                "历史导出缺少 ExportBatch/Artifact/manifest 权威账本；"
                "在线请求不得推进或使用原音频删除门，请离线审计迁移或重新受控导出"
            ),
        })
    try:
        # Filesystem/ledger integrity alone is insufficient: a published batch
        # becomes unusable immediately after consent or withdrawal authority
        # changes, even though its immutable receipts remain.
        verified_result = export.get_export_batch_result(s, r.export_batch_id)
        audio_code = export_security.pseudonymize_audio(r.raw_audio_id)
    except (ValueError, RuntimeError) as exc:
        raise HTTPException(409, str(exc))
    controlled_copy = export.find_exported_audio_blob(
        r.raw_audio_id, r.export_batch_id,
        data_classification=r.data_classification)
    if not controlled_copy or not r.checksum:
        raise HTTPException(409, "本批次受控音频副本不存在或未登记校验值")

    def reject_binding(message: str) -> NoReturn:
        raise HTTPException(status_code=409, detail={
            "code": "controlled_export_artifact_unverified",
            "message": message,
        })

    touched = verified_result.get("audio_touched")
    if not isinstance(touched, list) or touched.count(audio_code) != 1:
        reject_binding("原始音频未在本批次权威音频清单中唯一登记")

    try:
        controlled_root = Path(export.CONTROLLED_AUDIO_DIR).resolve(strict=True)
        controlled_copy = controlled_copy.resolve(strict=True)
        actual_relative_path = controlled_copy.relative_to(
            controlled_root).as_posix()
        expected_relative_path = (
            f"{r.data_classification}/{r.export_batch_id}/audio/"
            f"{controlled_copy.name}"
        )
        digest, byte_count = audio_store.sha256_file(controlled_copy)
    except (OSError, RuntimeError, ValueError):
        reject_binding("受控音频副本的物理路径或摘要无法权威验证")
    if actual_relative_path != expected_relative_path:
        reject_binding("受控音频副本与本批次预期路径不一致")

    artifacts = verified_result.get("artifacts")
    if not isinstance(artifacts, list) or not all(
            isinstance(artifact, dict) for artifact in artifacts):
        reject_binding("本批次 artifact 权威清单不完整")
    # Bind the selected filesystem blob back to exactly one manifest artifact.
    # Looking at both the exact path and the pseudonymized basename makes a
    # second extension, wrong path, wrong kind/hash, or duplicated projection
    # ambiguous instead of silently choosing whichever file happens to exist.
    related_artifacts = []
    for artifact in artifacts:
        relative_path = artifact.get("relative_path")
        if (relative_path == actual_relative_path
                or (isinstance(relative_path, str)
                    and Path(relative_path).name.startswith(f"{audio_code}."))):
            related_artifacts.append(artifact)
    if len(related_artifacts) != 1:
        reject_binding("受控音频副本未唯一绑定到本批次 artifact 清单")
    artifact = related_artifacts[0]
    if (artifact.get("realm")
            != f"{r.data_classification}_controlled_audio"
            or artifact.get("kind") != "controlled_audio"
            or artifact.get("relative_path") != actual_relative_path
            or artifact.get("sha256") != digest
            or artifact.get("byte_count") != byte_count
            or digest != r.checksum):
        reject_binding("受控音频副本的类型、路径或摘要与权威 artifact 不一致")
    return controlled_copy


class AudioIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    raw_audio_id: str = PydanticField(
        min_length=1, max_length=160,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,159}$")
    session_id: str | None = PydanticField(default=None, min_length=1, max_length=128)
    # 有场次时以 Session 为权威来源；仅当请求显式传入时才校验矛盾。
    # 无场次时必须显式传 true，不允许缺省值伪装模拟。
    is_simulation: bool | None = None
    turn_key: str | None = PydanticField(default=None, min_length=1, max_length=200,
                                         pattern=r"^[^\r\n\x00]+$")
    is_reliability_sample: bool = False
    contains_direct_identifier: bool = False


class DeviceAudioRegistrationOut(BaseModel):
    """Device ACK deliberately omits canonical turn_key and classification facts."""
    raw_audio_id: str
    registered: Literal[True] = True


def _raise_researcher_audio_registration_device_required() -> NoReturn:
    """Hide whether a client-selected id is missing or owned by another session.

    A named researcher may recover an exact immutable ACK for their own session,
    but only a paired bedside device (or the server-owned autopilot command path)
    may introduce a new raw id.  Keeping one response for both cases prevents an
    account from using its own authorized session as an audio-id existence oracle.
    """
    raise HTTPException(status_code=403, detail={
        "code": "audio_registration_device_required",
        "message": "新的录音登记必须由当前已配对受试者设备或服务端自动流程创建",
    })


_MUTABLE_RUNTIME_STATUSES = {"active", "paused"}
_TERMINAL_RUNTIME_STATUSES = {"completed", "aborted", "failed"}
_RESEARCH_REVIEW_RUNTIME_STATUSES = _MUTABLE_RUNTIME_STATUSES | {"intervention_completed"}


def _ensure_runtime_writable(session_id: str, s: DBSession, action: str) -> SessionRuntimeState | None:
    """终态是服务端事实：一旦结束，浏览器刷新、旧标签页或延迟请求都不得重开场次。"""
    _require_started_visit_plan_session(session_id, s)
    state = s.get(SessionRuntimeState, session_id)
    if state is not None and state.status not in _MUTABLE_RUNTIME_STATUSES:
        raise HTTPException(409, f"场次已进入终态 {state.status}；禁止{action}")
    return state


def _ensure_manual_plane_writable(
        session_id: str, s: DBSession, action: str, *,
        allow_after_intervention: bool = False) -> None:
    """Reject stale/manual control writes while the server owns automation.

    Callers must hold the LiveState write lock/row for the whole check-and-commit
    transaction.  That is the same serialization boundary used by P0a start, so
    an old tab cannot pass this check and commit after autonomous ownership was
    acquired.

    P0a deliberately retains ``mode=autonomous,status=scope_completed`` as an
    immutable control-plane fact.  Selected research-review writes may bypass
    that stale ownership only after locking and proving the separate runtime has
    entered ``intervention_completed``; active/paused bedside writes remain
    blocked exactly as before.

    A session frozen before the repeat protocol existed stops for good once its
    single legacy recovery has finished.  Draining closes the patient screen and
    a researcher may take over, which flips the control plane to
    ``manual``/``paused`` — and that alone used to be enough to pass this gate
    and resume the old manual flow.  It must not be: the session never froze
    repeat semantics, so a patient asking to hear the prompt again would be
    scored as an answer.  The frozen binding is therefore resolved here too,
    after the autonomous check so the pre-takeover error code is unchanged.
    """
    resolved = _require_started_visit_plan_session(session_id, s)
    if allow_after_intervention:
        runtime_state = s.exec(select(SessionRuntimeState).where(
            SessionRuntimeState.session_id == session_id,
        ).with_for_update()).first()
        if (runtime_state is not None
                and runtime_state.status == "intervention_completed"):
            return
    state = s.exec(select(SessionAutopilotState).where(
        SessionAutopilotState.session_id == session_id,
    ).with_for_update()).first()
    if state is not None and state.mode == "autonomous":
        raise HTTPException(status_code=409, detail={
            "code": "autopilot_manual_control_locked",
            "message": f"服务端自动驾驶已接管当前场次；禁止{action}",
        })
    if _direct_session_test_escape_enabled():
        # Only the explicit pytest escape, which already exempts these sessions
        # from plan admission above. It cannot be reached in production, and
        # honouring it here keeps the many historical directly-constructed test
        # fixtures — none of which froze a repeat binding — from failing on a
        # gate that has nothing to do with what they cover. The autonomous
        # ownership check above still runs for them.
        return
    try:
        # The session's own historical binding, resolved through the versioned
        # registry — never today's active protocol, and never a second copy of
        # this resolution logic.
        autopilot_service.session_repeat_protocol(resolved)
    except autopilot_service.AutopilotServiceError as exc:
        code = {
            "autopilot_repeat_binding_missing": "session_repeat_binding_missing",
            "autopilot_repeat_protocol_unavailable":
                "session_repeat_protocol_unavailable",
        }.get(exc.code)
        if code is None:
            raise
        raise HTTPException(status_code=409, detail={
            "code": code,
            "message": f"场次缺少可用的冻结重复请求协议绑定；禁止{action}",
        }) from exc


def _ensure_research_review_writable(
        session_id: str, s: DBSession, action: str) -> SessionRuntimeState | None:
    """床旁干预结束后仍允许异步确认、锁分和补记现场情况。"""
    _require_started_visit_plan_session(session_id, s)
    state = s.get(SessionRuntimeState, session_id)
    if state is not None and state.status not in _RESEARCH_REVIEW_RUNTIME_STATUSES:
        raise HTTPException(409, f"场次已进入终态 {state.status}；禁止{action}")
    return state


def _ensure_post_intervention_review_writable(
        session_id: str, s: DBSession, action: str) -> SessionRuntimeState:
    """Research truth is writable only in the explicit review window.

    ``active``/``paused`` still belong to the bedside intervention.  Allowing a
    reviewer to confirm or permanently lock a turn in either state can change
    the automation path (or freeze a provisional attempt) before the immutable
    intervention outcome snapshot exists.  Missing runtime rows are also
    rejected: historical/direct rows do not silently acquire a review window.
    """
    _require_started_visit_plan_session(session_id, s)
    state = s.get(SessionRuntimeState, session_id)
    if state is None or state.status != "intervention_completed":
        runtime_status = state.status if state is not None else "missing"
        raise HTTPException(status_code=409, detail={
            "code": "research_review_requires_intervention_completion",
            "message": (
                "须先由服务端完成床旁干预并生成不可变汇总，"
                f"当前状态为 {runtime_status}；禁止{action}"
            ),
            "runtime_status": runtime_status,
        })
    return state


def _ensure_recording_allowed_for_session(session_id: str | None, s: DBSession,
                                          *, is_simulation: bool | None) -> bool:
    if not session_id:
        if is_simulation is not True:
            raise HTTPException(409, "真实研究音频必须绑定 session_id；无场次音频仅允许显式模拟")
        _require_simulation_enabled()
        return True
    sess = s.get(TrainSession, session_id)
    if not sess:
        raise HTTPException(404, "音频关联场次不存在")
    _require_classified_session(sess)
    _ensure_runtime_writable(session_id, s, "登记、上传或下发录音指令")
    patient = s.get(Patient, sess.patient_id)
    if not patient:
        raise HTTPException(409, "音频关联场次缺少受试者档案，禁止登记或上传音频")
    if is_simulation is not None and is_simulation != sess.is_simulation:
        raise HTTPException(409, "音频 is_simulation 与关联场次不一致")
    if (patient.withdrawal_status or "").strip():
        raise HTTPException(409, f"受试者撤回状态为 {patient.withdrawal_status}，禁止登记或上传音频")
    if sess.is_simulation:
        _require_simulation_enabled()
        _require_simulation_subject(patient)
    else:
        _require_research_eligible(patient)
    return sess.is_simulation


def _device_capability_session_id(request: Request) -> str | None:
    if getattr(request.state, "auth_kind", None) != "device_capability":
        return None
    value = getattr(request.state, "device_capability_session_id", None)
    return value if isinstance(value, str) and value else None


def _require_device_capability_token_hash(request: Request, action: str) -> str:
    """Autopilot device truth must come from a paired device, never an account."""
    if getattr(request.state, "auth_kind", None) != "device_capability":
        raise HTTPException(403, detail={
            "code": "device_capability_required",
            "message": f"{action}必须由当前已配对的受试者设备完成",
        })
    value = getattr(request.state, "device_capability_token_hash", None)
    if not isinstance(value, str) or not value:
        raise HTTPException(401, detail={
            "code": "device_capability_invalid",
            "message": "受试者设备配对缺少有效能力摘要",
        })
    return value


def _require_patient_device_truth(request: Request, action: str) -> None:
    """Protected deployments require a paired device for patient-origin facts.

    Loopback M0 remains usable for isolated development, where no account, PIN,
    or device capability exists and middleware already rejects every remote peer.
    """
    if not auth.auth_active() and _is_loopback_request(request):
        return
    _require_device_capability_token_hash(request, action)


def _raise_autopilot_http_error(exc: autopilot_service.AutopilotServiceError) -> None:
    status = 422 if exc.code == "autopilot_input_invalid" else 404 \
        if exc.code == "autopilot_session_unavailable" else 409
    detail = {"code": exc.code, "message": exc.message}
    detail.update(exc.context)
    raise HTTPException(
        status,
        detail=detail,
    ) from exc


def _capability_recovery_only(request: Request) -> bool:
    return bool(getattr(request.state, "device_capability_recovery_only", False))


def _reject_recovery_only_new_write(request: Request) -> None:
    if _capability_recovery_only(request):
        _raise_device_conflict(
            "device_capability_recovery_only",
            "设备配对只可恢复已落库的录音回执，不能创建新事实",
        )


def _raise_device_conflict(code: str, message: str) -> None:
    raise HTTPException(409, detail={"code": code, "message": message})


def _require_capability_bound_session(
        request: Request, session_id: str | None, action: str) -> str | None:
    """Capability 只能操作其签发时绑定的场次；账号路径保持原有权限。"""
    bound = _device_capability_session_id(request)
    if bound is None:
        return None
    if not session_id or session_id != bound:
        _raise_device_conflict(
            "device_session_mismatch",
            f"受试者端设备{action}必须与本次配对场次一致",
        )
    return bound


def _require_device_route_session_read(
        request: Request, session_id: str, s: DBSession, action: str) -> TrainSession:
    """Authorize one session-scoped read shared by account and patient device.

    ``AccessKind.DEVICE`` is a transport-level union: either a capability or a
    named account may reach the handler.  A named account must not inherit the
    capability's narrow bedside projection as a cross-session read bypass, so it
    still passes the normal owner/admin/terminal-steward object policy.  A real
    capability keeps its exact bound-session and VisitPlan admission semantics.
    """
    _require_capability_bound_session(request, session_id, action)
    sess = s.get(TrainSession, session_id)
    if sess is None:
        raise HTTPException(404, "场次不存在")
    if getattr(request.state, "auth_kind", None) != "device_capability":
        _require_session_read_operator(request, sess, s, action)
    return _require_started_visit_plan_session(session_id, s, sess=sess)


def _require_capability_current_live(
        request: Request, s: DBSession, action: str,
        *, live_row: LiveState | None = None) -> str | None:
    """需要实时呈现/新写入时，绑定场次还必须仍是当前 live 场次。"""
    bound = _device_capability_session_id(request)
    if bound is None:
        return None
    row = live_row if live_row is not None else s.get(LiveState, 1)
    if _live_session_id(row) != bound:
        _raise_device_conflict(
            "device_session_changed",
            f"操作端场次已变化，受试者端设备不能继续{action}",
        )
    return bound


def _require_capability_active_for_write(
        request: Request, s: DBSession, session_id: str) -> None:
    """Final check in the same lock/transaction that will commit a new fact."""
    _require_started_visit_plan_session(session_id, s)
    if _device_capability_session_id(request) is None:
        return
    status, _row = device_capability.revalidate_active_for_write(
        s,
        getattr(request.state, "device_capability_token_hash", None),
        session_id,
    )
    if status == device_capability.CapabilityResolution.VALID:
        return
    if status == device_capability.CapabilityResolution.RECOVERY_ONLY:
        _raise_device_conflict(
            "device_capability_recovery_only",
            "设备配对只可恢复已落库的录音回执，不能创建新事实",
        )
    code = {
        device_capability.CapabilityResolution.INVALID: "device_capability_invalid",
        device_capability.CapabilityResolution.EXPIRED: "device_capability_expired",
        device_capability.CapabilityResolution.REVOKED: "device_capability_revoked",
    }[status]
    raise HTTPException(401, detail={
        "code": code,
        "message": "设备配对已失效，请由研究者重新配对",
    })


def _require_capability_valid_for_exact_ack(
        request: Request, s: DBSession, session_id: str) -> None:
    """Allow active/recovery tokens for immutable ACKs, but never revoked/expired ones."""
    # Transport retries remain read/idempotent only for an admitted session.
    # Historical orphan audio is available through governance reads, not through
    # a revived patient-device capture channel.
    _require_started_visit_plan_session(session_id, s)
    if _device_capability_session_id(request) is None:
        return
    status, _row = device_capability.revalidate_active_for_write(
        s,
        getattr(request.state, "device_capability_token_hash", None),
        session_id,
    )
    if status in {
            device_capability.CapabilityResolution.VALID,
            device_capability.CapabilityResolution.RECOVERY_ONLY}:
        return
    code = {
        device_capability.CapabilityResolution.INVALID: "device_capability_invalid",
        device_capability.CapabilityResolution.EXPIRED: "device_capability_expired",
        device_capability.CapabilityResolution.REVOKED: "device_capability_revoked",
    }[status]
    raise HTTPException(401, detail={
        "code": code,
        "message": "设备配对已失效，请由研究者重新配对",
    })


@app.post("/sessions/{session_id}/recording-authorization")
def recording_authorization(session_id: str, request: Request,
                            s: DBSession = Depends(get_session)):
    """每次真正打开麦克风前的无状态授权检查；撤回、准入变化或终态立即 fail-closed。"""
    _require_capability_bound_session(request, session_id, "申请录音授权的场次")
    with _LIVE_WRITE_LOCK, device_capability.serialized_mutation():
        live = _live_row_for_update(s)
        sess = s.get(TrainSession, session_id)
        if not sess:
            raise HTTPException(404, "场次不存在")
        if getattr(request.state, "auth_kind", None) == "account":
            _require_session_operator(
                request, sess, s, "申请床旁录音授权", mutation=True)
        _require_capability_current_live(
            request, s, "申请新的录音授权", live_row=live)
        _require_capability_active_for_write(request, s, session_id)
        _ensure_manual_plane_writable(
            session_id, s, "使用未绑定自动驾驶命令的通用录音授权")
        effective_simulation = _ensure_recording_allowed_for_session(
            session_id, s, is_simulation=sess.is_simulation)
        state = s.get(SessionRuntimeState, session_id)
        return {
            "allowed": True,
            "runtime_status": state.status if state else "active",
            "is_simulation": effective_simulation,
        }


@app.post(
    "/sessions/{session_id}/autopilot/commands/{command_key}/recording-authorization"
)
def autopilot_recording_authorization(
        session_id: str, command_key: str, request: Request,
        s: DBSession = Depends(get_session)):
    """Authorize one microphone opening for the exact pending record command."""
    token_hash = _require_device_capability_token_hash(
        request, "申请自动驾驶录音授权")
    _require_capability_bound_session(
        request, session_id, "申请自动驾驶录音授权")
    with _LIVE_WRITE_LOCK, device_capability.serialized_mutation():
        s.rollback()
        s.expire_all()
        live = _live_row_for_update(s)
        _require_capability_bound_session(
            request, session_id, "申请自动驾驶录音授权")
        _require_capability_current_live(
            request, s, "申请自动驾驶录音授权", live_row=live)
        _require_capability_active_for_write(request, s, session_id)
        try:
            effective_simulation = autopilot_service.authorize_recording_command(
                s,
                session_id=session_id,
                command_key=command_key,
                capability_token_hash=token_hash,
            )
        except autopilot_service.AutopilotServiceError as exc:
            s.rollback()
            _raise_autopilot_http_error(exc)
    return {
        "allowed": True,
        "runtime_status": "active",
        "is_simulation": effective_simulation,
    }


@app.post("/audio", response_model=AudioAssetRow | DeviceAudioRegistrationOut)
def create_audio(a: AudioIn, request: Request, s: DBSession = Depends(get_session)):
    if not audio_store.SAFE_ID.fullmatch(a.raw_audio_id):
        raise HTTPException(422, "非法音频 id")
    _require_capability_bound_session(request, a.session_id, "登记录音")

    resolved_turn_key = a.turn_key
    patient_ref_input = False

    def same_registration(existing: AudioAssetRow) -> bool:
        return (
            existing.session_id == a.session_id
            and existing.turn_key == resolved_turn_key
            and existing.is_reliability_sample == a.is_reliability_sample
            and existing.contains_direct_identifier == a.contains_direct_identifier
            and (a.is_simulation is None or existing.is_simulation == a.is_simulation)
        )

    # 全部音频新事实统一按 registration -> live -> capability 取锁。
    # live/state 写路径是 live -> capability，因此不得在等待 live 前先持有
    # capability；否则与 audioSaved 并发时会形成双向等待。进锁后再丢弃
    # ORM 缓存并重读 live/capability/session/binding，避免锁外预检形成 TOCTOU。
    with (audio_capture.registration_lock(), _LIVE_WRITE_LOCK,
          device_capability.serialized_mutation()):
        s.rollback()
        s.expire_all()
        locked_live = _live_row_for_update(s)
        _require_capability_bound_session(request, a.session_id, "登记录音")

        # A named operator must be authorized for the requested session before
        # this route looks up the client-selected raw id.  Otherwise an exact
        # replay of another operator's id can take the immutable ACK shortcut
        # and return that operator's AudioAssetRow.
        if (a.session_id
                and getattr(request.state, "auth_kind", None) == "account"):
            requested_session = s.get(TrainSession, a.session_id)
            if requested_session is None:
                raise HTTPException(404, "场次不存在")
            _require_session_operator(
                request, requested_session, s, "核对录音登记场次")

        existing = s.exec(
            select(AudioAssetRow)
            .where(AudioAssetRow.raw_audio_id == a.raw_audio_id)
            .with_for_update()
        ).first()

        # Every protected account is supervisory here: it may recover an exact
        # immutable ACK that already belongs to a session, but it may not create
        # a client-selected raw id or adopt an orphan row.  A researcher also
        # receives this same opaque denial for a foreign id, preserving the
        # missing-vs-foreign anti-oracle contract.  New facts remain confined to
        # the paired device and server-owned autopilot creation paths below.
        if getattr(request.state, "auth_kind", None) == "account":
            role = getattr(request.state, "actor_role", None)
            if (existing is None
                    or not existing.session_id
                    or (role == "researcher"
                        and existing.session_id != a.session_id)):
                _raise_researcher_audio_registration_device_required()

        if (existing is not None and existing.session_id
                and getattr(request.state, "auth_kind", None) == "account"):
            existing_session = s.get(TrainSession, existing.session_id)
            if existing_session is None:
                raise HTTPException(409, "音频关联场次不存在")
            _require_session_operator(
                request, existing_session, s, "核对录音登记回执")
        if a.turn_key is not None and a.session_id is not None:
            ref_session = s.get(TrainSession, a.session_id)
            if ref_session is None:
                raise HTTPException(404, "场次不存在")
            resolved_turn_key, patient_ref_input = _canonicalize_patient_turn_input(
                request,
                ref_session,
                a.turn_key,
                legacy_exact_turn_key=(
                    existing.turn_key
                    if (existing is not None
                        and existing.session_id == a.session_id
                        and existing.patient_turn_ref_version == 1)
                    else None
                ),
            )

        def registration_response(row: AudioAssetRow):
            if (patient_ref_input
                    or getattr(request.state, "auth_kind", None)
                    in {"account", "device_capability"}):
                return DeviceAudioRegistrationOut(raw_audio_id=row.raw_audio_id)
            return row

        if existing is not None:
            if not same_registration(existing):
                raise HTTPException(409, "raw_audio_id 已绑定不同的录音登记")
            # 纯登记 ACK 不创建/改变证据；允许 ACK 丢失后在终态或切场恢复 outbox。
            if existing.session_id:
                _require_capability_valid_for_exact_ack(
                    request, s, existing.session_id)
            return registration_response(existing)

        if a.session_id:
            # Autonomous record commands pre-create their exact AudioAssetRow.
            # Therefore only this existing-row ACK path is valid while owned;
            # a client-chosen raw id is always a legacy/manual capture attempt.
            _ensure_manual_plane_writable(
                a.session_id, s, "使用客户端自选 raw_audio_id 登记新录音")
            linked = s.get(TrainSession, a.session_id)
            if (linked is not None
                    and getattr(request.state, "auth_kind", None) == "account"):
                _require_session_operator(
                    request, linked, s, "登记床旁录音", mutation=True)
        _reject_recovery_only_new_write(request)
        _require_capability_current_live(
            request, s, "登记新的录音", live_row=locked_live)
        if a.session_id:
            _require_capability_active_for_write(request, s, a.session_id)
        effective_simulation = _ensure_recording_allowed_for_session(
            a.session_id, s, is_simulation=a.is_simulation)
        if a.session_id:
            state = s.get(SessionRuntimeState, a.session_id)
            if state is not None and state.status == "paused":
                raise HTTPException(
                    409, "场次暂停期间禁止登记新音频；仅可补传暂停前已登记的录音")
            linked_session = s.get(TrainSession, a.session_id)
            assert linked_session is not None
            _require_current_patient_turn_for_device(
                request,
                linked_session,
                locked_live,
                state or _runtime_row(a.session_id, s),
                resolved_turn_key or "",
                opaque_input=patient_ref_input,
            )
        if a.turn_key is not None and not a.turn_key.strip():
            raise HTTPException(422, "turn_key 不得为空白")
        _validate_audio_turn_key(a.session_id, resolved_turn_key, s)
        try:
            audio_capture.assert_registration_quota(s, a.session_id, resolved_turn_key)
        except audio_capture.AudioQuotaExceeded as exc:
            raise HTTPException(409, str(exc)) from exc
        except audio_capture.AudioLimitConfigurationError as exc:
            raise HTTPException(500, str(exc)) from exc

        payload = a.model_dump(exclude={"is_simulation"})
        payload["turn_key"] = resolved_turn_key
        classification = "simulation"
        if a.session_id:
            linked_session = s.get(TrainSession, a.session_id)
            if linked_session is None:
                raise HTTPException(404, "场次不存在")
            classification = linked_session.data_classification
        row = AudioAssetRow(**payload, is_simulation=effective_simulation,
                            data_classification=classification)
        s.add(row)
        try:
            s.commit()
        except IntegrityError as exc:
            # 多进程同时重放同一登记时，以唯一键落下的事实为准。回滚后
            # 重读并重验 capability；这是纯 ACK，可以在切场后恢复，但不能
            # 凭已失效或不匹配的能力返回任何录音事实。
            s.rollback()
            s.expire_all()
            winner = s.exec(
                select(AudioAssetRow)
                .where(AudioAssetRow.raw_audio_id == a.raw_audio_id)
                .with_for_update()
            ).first()
            if (winner is not None and winner.session_id
                    and getattr(request.state, "auth_kind", None) == "account"):
                winner_session = s.get(TrainSession, winner.session_id)
                if winner_session is None:
                    raise HTTPException(409, "音频关联场次不存在")
                _require_session_operator(
                    request, winner_session, s, "恢复录音登记回执")
            if winner is not None and same_registration(winner):
                _require_capability_bound_session(
                    request, winner.session_id, "恢复录音登记回执")
                if winner.session_id:
                    _require_capability_valid_for_exact_ack(
                        request, s, winner.session_id)
                return registration_response(winner)
            raise HTTPException(409, "raw_audio_id 已并发绑定不同的录音登记") from exc
        s.refresh(row)
        return registration_response(row)


def _transition_locked(r: AudioAssetRow, s: DBSession, fn) -> AudioAssetRow:
    """Apply one gate transition inside the caller's governance transaction."""
    if r.withdrawn or (r.withdrawal_status or "").strip():
        raise HTTPException(409, "录音已撤回或隔离，拒绝继续推进治理状态")
    asset = _row_to_asset(r)
    try:
        fn(asset)
    except audio_gate.AudioGateError as e:
        raise HTTPException(409, str(e))
    r.status = asset.status
    s.add(r)
    s.commit()
    s.refresh(r)
    return r


@app.post("/audio/{raw_audio_id}/export", response_model=AudioAssetRow)
def audio_export(raw_audio_id: str, s: DBSession = Depends(get_session)):
    _load_row(raw_audio_id, s)
    raise HTTPException(409, "不允许脱离场次导出盲标 exported；请调用 POST /sessions/{session_id}/export")


@app.post("/audio/{raw_audio_id}/checksum", response_model=AudioAssetRow)
def audio_checksum(raw_audio_id: str, request: Request, s: DBSession = Depends(get_session)):
    """导出期校验：只校验本批次的受控导出副本，不以采集源文件冒充。"""
    # Keep the same governance -> live lock order as subject withdrawal.  The
    # database row locks acquired during export verification remain held until
    # the transition commit, closing the cross-worker consent/withdrawal race.
    with governance_lock.SUBJECT_EXPORT_WRITE_LOCK, _LIVE_WRITE_LOCK:
        s.rollback()
        s.expire_all()
        r = s.exec(select(AudioAssetRow).where(
            AudioAssetRow.raw_audio_id == raw_audio_id,
        ).with_for_update()).first()
        if r is None:
            raise HTTPException(404, "音频不存在")
        _require_authoritative_export_copy(r, s)
        updated = _transition_locked(r, s, audio_gate.mark_checksum_verified)
    _audit(s, request, "audio_checksum_verified",
           f"验证受控录音副本 checksum {raw_audio_id}", session_id=updated.session_id)
    return updated


@app.post("/audio/{raw_audio_id}/reliability-review", response_model=AudioAssetRow)
def audio_reliability_review(raw_audio_id: str, request: Request,
                             s: DBSession = Depends(get_session)):
    with governance_lock.SUBJECT_EXPORT_WRITE_LOCK, _LIVE_WRITE_LOCK:
        s.rollback()
        s.expire_all()
        r = s.exec(select(AudioAssetRow).where(
            AudioAssetRow.raw_audio_id == raw_audio_id,
        ).with_for_update()).first()
        if r is None:
            raise HTTPException(404, "音频不存在")
        _require_authoritative_export_copy(r, s)
        updated = _transition_locked(
            r, s, audio_gate.mark_reliability_review_done)
    _audit(s, request, "audio_reliability_review",
           f"完成录音信度复核 {raw_audio_id}", session_id=updated.session_id)
    return updated


@app.delete("/audio/{raw_audio_id}")
def audio_delete(
        raw_audio_id: str, request: Request,
        expected_session_id: str | None = Query(default=None, alias="session_id"),
        source: Literal["auto", "manual", "withdrawal"] = "auto",
        s: DBSession = Depends(get_session)):
    """先提交逻辑终态，再耐久删除原始字节。

    未达闸门条件（导出+校验[+信度复核]）→ 409，杜绝到期盲删。
    DB commit 失败时绝不 unlink；物理清理失败时 DB 保持 deleted，
    后续对已 deleted+gate 的同一 DELETE 幂等重试清理。
    """
    cleanup_error: Exception | None = None
    bytes_deleted = False
    session_id: str | None = None
    # Match upload/live's raw-id -> byte-quota -> live -> asset order.  The
    # process-shared raw-id lock spans logical commit and physical cleanup, so a
    # delayed upload/receipt/disposition request cannot interleave between them.
    with (governance_lock.SUBJECT_EXPORT_WRITE_LOCK,
          audio_store.blob_mutation_lock(raw_audio_id),
          audio_capture.byte_quota_lock(), _LIVE_WRITE_LOCK):
        r = s.exec(select(AudioAssetRow).where(
            AudioAssetRow.raw_audio_id == raw_audio_id).with_for_update()).first()
        if r is None:
            raise HTTPException(404, "音频不存在")
        if source in {"manual", "withdrawal"}:
            if not expected_session_id:
                raise HTTPException(status_code=422, detail={
                    "code": "audio_delete_session_required",
                    "message": "人工或撤回物理删除必须携带当前场次并由服务器核对",
                })
            if r.session_id != expected_session_id:
                raise HTTPException(status_code=409, detail={
                    "code": "audio_delete_session_mismatch",
                    "message": "录音不属于当前场次，已拒绝物理删除",
                })
        if source == "withdrawal" and not (
                r.withdrawn or (r.withdrawal_status or "").strip()):
            raise HTTPException(status_code=409, detail={
                "code": "audio_not_subject_withdrawn",
                "message": "该录音没有权威研究撤回隔离事实，不能使用撤回删除通道",
            })
        runtime_state = (
            s.get(SessionRuntimeState, r.session_id) if r.session_id else None)
        if (runtime_state is not None and runtime_state.status == "aborted"
                and source != "withdrawal"):
            raise HTTPException(status_code=409, detail={
                "code": "aborted_session_evidence_retained",
                "message": "普通中止保留只读证据；物理删除只能由受试者撤回治理流程授权",
            })
        if os.environ.get("ENABLE_AUDIO_DELETE") != "1":
            raise HTTPException(
                409, "物理删除默认禁用；须在受控环境显式设置 ENABLE_AUDIO_DELETE=1")
        session_id = r.session_id
        is_withdrawn = r.withdrawn or bool((r.withdrawal_status or "").strip())
        if is_withdrawn and source != "withdrawal":
            raise HTTPException(status_code=409, detail={
                "code": "audio_withdrawal_delete_source_required",
                "message": "撤回录音只能使用显式 withdrawal 治理删除通道",
            })
        # Even a legacy logical ``deleted`` marker is not proof that its
        # original bytes may be unlinked.  Normal cleanup retries must reprove
        # the same immutable export authority; withdrawal uses its explicit
        # terminal governance channel instead.
        if source != "withdrawal":
            _require_authoritative_export_copy(r, s)
        if r.status == AudioStatus.deleted:
            if not r.delete_gate_passed:
                raise HTTPException(
                    409, "音频已标记 deleted 但缺少删除闸门证据，禁止猜测清理")
        else:
            # withdrawal_status 也是服务端终态隔离事实；与显式
            # withdrawn 一样可覆盖普通导出闸门，以履行撤回删除。
            asset = _row_to_asset(r)
            asset.withdrawn = asset.withdrawn or bool(
                (r.withdrawal_status or "").strip())
            try:
                audio_gate.request_delete(asset, source=source)
            except audio_gate.AudioGateError as e:
                raise HTTPException(409, str(e))
            r.status = asset.status
            r.delete_gate_passed = True               # 审计:闸门放行标记
            s.add(r)
            try:
                # 必须在任何 unlink 之前成功；失败分支不得调用物理层。
                s.commit()
            except BaseException:
                s.rollback()
                raise
        try:
            bytes_deleted = audio_store.delete_blob(
                raw_audio_id, mutation_lock_held=True)
        except Exception as exc:  # 逻辑 deleted 已落库，将物理清理留给幂等重试
            cleanup_error = exc
    if cleanup_error is not None:
        _audit(
            s, request, "audio_delete_cleanup_pending",
            f"录音 {raw_audio_id} 逻辑删除已提交，物理清理待重试(source={source})",
            session_id=session_id,
        )
        raise HTTPException(
            500,
            detail={
                "code": "audio_physical_cleanup_pending",
                "message": "录音已进入不可恢复的逻辑删除状态，但物理字节清理尚未确认；请重试 DELETE",
            },
            headers={"Cache-Control": "private, no-store", "Pragma": "no-cache"},
        )
    _audit(s, request, "audio_delete", f"删除录音 {raw_audio_id}(source={source},字节={bytes_deleted})",
           session_id=session_id)
    return {"raw_audio_id": raw_audio_id, "status": AudioStatus.deleted, "deleted_by": source,
            "bytes_deleted": bytes_deleted}


def _require_audio_read_operator(
        row: AudioAssetRow, request: Request, s: DBSession,
        action: str) -> TrainSession:
    """Authorize before exposing withdrawal, integrity, or existence details.

    Unknown ids already return the generic 404 from ``_load_row``.  Return the
    same projection for a foreign researcher's audio (or an active session to a
    data steward) so the status code cannot be used as an audio/withdrawal
    oracle.  Owners, terminal data stewards, and admins continue to the exact
    governance checks below.
    """
    if not row.session_id:
        raise HTTPException(404, "音频不存在")
    sess = s.get(TrainSession, row.session_id)
    if not sess:
        raise HTTPException(404, "音频不存在")
    try:
        _require_session_operator(
            request, sess, s, action, not_found_detail="音频不存在")
    except HTTPException as exc:
        if exc.status_code == 403:
            raise HTTPException(404, "音频不存在") from None
        raise
    return sess


def _require_audio_upload_operator(
        row: AudioAssetRow, request: Request, s: DBSession,
        action: str) -> TrainSession | None:
    """Conceal unbound/foreign assets before admission or upload diagnostics.

    A paired device may address only the asset already bound to its exact
    session.  A named account may supervise a linked session through the normal
    session policy, but cannot use a pre-existing unbound raw id as an arbitrary
    voice upload slot.  Loopback M0 keeps its isolated fixture workflow.
    """
    bound_session_id = _device_capability_session_id(request)
    if bound_session_id is not None:
        if row.session_id != bound_session_id:
            raise HTTPException(404, "音频不存在")
        sess = s.get(TrainSession, bound_session_id)
        if sess is None:
            raise HTTPException(404, "音频不存在")
        return sess

    if getattr(request.state, "auth_kind", None) == "account":
        if not row.session_id:
            raise HTTPException(404, "音频不存在")
        sess = s.get(TrainSession, row.session_id)
        if sess is None:
            raise HTTPException(404, "音频不存在")
        _require_session_operator(
            request,
            sess,
            s,
            action,
            mutation=True,
            not_found_detail="音频不存在",
        )
        return sess

    # Open M0 is constrained to a process-local loopback peer by middleware.
    # Still validate a supplied session so a broken foreign key never reaches
    # upload/integrity diagnostics.
    if row.session_id:
        sess = s.get(TrainSession, row.session_id)
        if sess is None:
            raise HTTPException(404, "音频不存在")
        _require_session_operator(
            request,
            sess,
            s,
            action,
            mutation=True,
            not_found_detail="音频不存在",
        )
        return sess
    return None


def _ensure_audio_read_allowed(
        row: AudioAssetRow, s: DBSession, *,
        sess: TrainSession | None = None) -> TrainSession:
    """原声/元数据每次读取都重查分类、撤回和当前受试者资格。"""
    if not row.session_id:
        raise HTTPException(409, "无场次音频不开放读取")
    sess = sess or s.get(TrainSession, row.session_id)
    if not sess:
        raise HTTPException(409, "音频关联场次不存在")
    _require_classified_session(sess)
    if (row.is_simulation != sess.is_simulation
            or row.data_classification != sess.data_classification):
        raise HTTPException(409, "音频与场次数据分类不一致")
    if row.withdrawn or (row.withdrawal_status or "").strip():
        raise HTTPException(409, "音频已撤回或隔离，禁止读取")
    patient = s.get(Patient, sess.patient_id)
    if not patient:
        raise HTTPException(409, "音频关联场次缺少受试者档案")
    if sess.is_simulation:
        _require_simulation_enabled()
        _require_simulation_subject(patient)
    else:
        _require_research_eligible(patient)
    return sess


@app.get("/audio/{raw_audio_id}", response_model=AudioAssetRow)
def audio_get(raw_audio_id: str, request: Request, s: DBSession = Depends(get_session)):
    _require_account_identity(
        request, "读取音频元数据",
        roles={"researcher", "data_steward", "admin"})
    row = _load_row(raw_audio_id, s)
    sess = _require_audio_read_operator(
        row, request, s, "读取场次音频元数据")
    _ensure_audio_read_allowed(row, s, sess=sess)
    _audit(s, request, "audio_metadata_read", f"读取录音元数据 {raw_audio_id}",
           patient_id=sess.patient_id, session_id=sess.session_id)
    return row


@app.put("/audio/{raw_audio_id}/blob")
async def audio_upload_blob(raw_audio_id: str, request: Request, s: DBSession = Depends(get_session)):
    """常量内存流式保存原件，DB/磁盘一致后返回可核验上传收据。"""
    if not audio_store.SAFE_ID.fullmatch(raw_audio_id):
        raise HTTPException(422, "非法音频 id")
    row = _load_row(raw_audio_id, s)          # 须先 POST /audio 登记元数据
    linked_session = _require_audio_upload_operator(
        row, request, s, "上传床旁录音")
    _require_capability_bound_session(request, row.session_id, "上传录音")
    if row.session_id:
        _require_started_visit_plan_session(
            row.session_id, s, sess=linked_session)

    def ensure_asset_contract(asset: AudioAssetRow) -> None:
        """只核对已落事实，不把终态/切场当成 ACK 丢失的拒绝理由。"""
        if asset.data_classification not in {"research", "simulation"}:
            raise HTTPException(409, "音频 data_classification 非法")
        if asset.is_simulation != (asset.data_classification == "simulation"):
            raise HTTPException(409, "音频 simulation 标志与分类不一致")
        if asset.session_id:
            linked = s.get(TrainSession, asset.session_id)
            if linked is None:
                raise HTTPException(409, "音频关联场次不存在")
            if (asset.data_classification != linked.data_classification
                    or asset.is_simulation != linked.is_simulation):
                raise HTTPException(409, "音频分类与关联场次不一致")

    def persisted_facts(asset: AudioAssetRow) -> audio_store.AudioBlobFacts | None:
        try:
            return audio_capture.verify_persisted_audio(asset)
        except audio_capture.AudioCaptureIntegrityError:
            # 全 NULL/部分旧字段可由同字节重放收口；verify_known_audio_state
            # 已先拦下 DB 声称已上传却缺文件、hash/size 不同等损坏。
            return None

    def reject_terminal_or_withdrawn(asset: AudioAssetRow) -> None:
        if (asset.status == AudioStatus.deleted or asset.withdrawn
                or bool((asset.withdrawal_status or "").strip())):
            raise HTTPException(409, "音频已删除或撤回，禁止上传或恢复原始字节")

    ensure_asset_contract(row)
    reject_terminal_or_withdrawn(row)
    try:
        audio_capture.verify_known_audio_state(row)
    except audio_capture.AudioCaptureIntegrityError as exc:
        raise HTTPException(409, str(exc)) from exc
    already_persisted = persisted_facts(row)
    if already_persisted is None:
        # An empty registered slot is not an existing byte fact.  In protected
        # deployments only the paired patient device may supply its first blob;
        # named owner/admin accounts remain supervisory exact-ACK readers.
        _require_patient_device_truth(request, "上传新的录音字节")
        # Recovery-only tokens may prove an already-persisted identical blob ACK,
        # but must be rejected before request.stream()/upload-slot consumption.
        _reject_recovery_only_new_write(request)
        _require_capability_current_live(request, s, "上传新的录音字节")
        _ensure_recording_allowed_for_session(
            row.session_id, s, is_simulation=row.is_simulation)
        if row.status != AudioStatus.recorded:
            raise HTTPException(409, "音频已进入导出/校验/删除流程，禁止覆盖采集字节")

    declared_bytes: int | None = None
    content_length = request.headers.get("content-length")
    if content_length is not None:
        try:
            declared_bytes = int(content_length)
        except ValueError:
            raise HTTPException(422, "Content-Length 非法")
        if declared_bytes < 0:
            raise HTTPException(422, "Content-Length 非法")
        if declared_bytes > audio_store.MAX_AUDIO_BLOB_BYTES:
            raise HTTPException(413, "音频超过 64 MiB 上限")
    if already_persisted is None:
        try:
            audio_capture.assert_declared_byte_quota(s, row, declared_bytes)
        except audio_capture.AudioQuotaExceeded as exc:
            raise HTTPException(409, str(exc)) from exc
        except audio_capture.AudioLimitConfigurationError as exc:
            raise HTTPException(500, str(exc)) from exc

    # Do not carry SQLite's initial read transaction across a slow request body:
    # it would retain a SHARED lock and could block DELETE/withdrawal or unrelated
    # commits even though no raw-id file lock is held.  All mutable gates are
    # reloaded below after staging completes.
    s.rollback()

    # First consume the network body into a hidden, fsync'd pending file.  It is
    # neither visible as a blob nor protected by the raw-id lock, so a stalled
    # client can never block DELETE/withdrawal.  Publication happens only after all
    # final gates are reloaded under the short raw-id critical section below.
    pending: audio_store.AudioBlobPending | None = None
    mutation_lease: audio_store.AudioBlobMutationLease | None = None
    saved: audio_store.AudioBlobSaveResult | None = None
    try:
        try:
            with audio_capture.upload_slot():
                pending = await audio_store.stage_blob_stream(
                    raw_audio_id, request.stream(), request.headers.get("content-type"))
        except audio_capture.AudioUploadBusy as exc:
            raise HTTPException(429, str(exc), headers={"Retry-After": "2"}) from exc
        except audio_capture.AudioLimitConfigurationError as exc:
            raise HTTPException(500, str(exc)) from exc
        except audio_store.AudioBlobTooLarge as exc:
            raise HTTPException(413, "音频超过 64 MiB 上限") from exc
        except ValueError as exc:
            # MIME 和容器签名错误均发生在临时文件发布前，finally 会清理临时字节。
            status = 422 if "空音频" in str(exc) else 415
            raise HTTPException(status, str(exc)) from exc
        except audio_store.AudioStoreIntegrityError as exc:
            raise HTTPException(409, str(exc)) from exc

        try:
            mutation_lease = await audio_store.acquire_blob_mutation_lease(raw_audio_id)
        except audio_store.AudioBlobMutationBusy as exc:
            raise HTTPException(429, str(exc), headers={"Retry-After": "1"}) from exc

        # 流读取可能持续数分钟。结束旧读事务并在配额锁内重新加载全部门禁，防止
        # 期间发生撤回、场次切换、终态或并发上传后仍用陈旧快照提交。
        with (audio_capture.byte_quota_lock(), _LIVE_WRITE_LOCK,
              device_capability.serialized_mutation()):
            s.rollback()
            row = s.exec(select(AudioAssetRow).where(
                AudioAssetRow.raw_audio_id == raw_audio_id).with_for_update()).first()
            if row is None:
                raise HTTPException(404, "音频不存在")
            _require_audio_upload_operator(
                row, request, s, "上传床旁录音")
            _require_capability_bound_session(request, row.session_id, "上传录音")
            if row.session_id:
                _require_capability_valid_for_exact_ack(request, s, row.session_id)
            ensure_asset_contract(row)
            # Check terminal/withdrawal before consulting or publishing bytes.  If
            # DELETE won while the body was staging, the pending file is simply
            # discarded and can never resurrect the voice original.
            reject_terminal_or_withdrawn(row)
            try:
                audio_capture.verify_known_audio_state(row)
            except audio_capture.AudioCaptureIntegrityError as exc:
                raise HTTPException(409, str(exc)) from exc
            complete = persisted_facts(row)
            if complete is not None:
                # HTTP 回包丢失后，客户端必须能在场次终态/切场后用
                # 完全相同的字节拿回纯 ACK。这里不改 DB、不重算配额。
                expected_format = audio_store.ext_for(
                    pending.normalized_mime).lstrip(".")
                if (complete.checksum != pending.checksum
                        or complete.byte_count != pending.byte_count
                        or row.audio_format != expected_format):
                    raise HTTPException(409, "上传重放与服务端已落音频事实不一致")
                return {
                    "raw_audio_id": raw_audio_id,
                    "bytes": pending.byte_count,
                    "checksum": pending.checksum,
                    "format": row.audio_format,
                    "idempotent": True,
                    "uploaded_at": row.uploaded_at,
                }
            # Re-check the patient-device principal after the slow body was
            # staged and before publication.  This is intentionally after the
            # complete-fact ACK branch: owner/admin exact replay remains a read,
            # while a still-empty slot can never be filled by an account.
            _require_patient_device_truth(request, "上传新的录音字节")
            _require_capability_current_live(request, s, "上传新的录音字节")
            if row.session_id:
                _require_capability_active_for_write(request, s, row.session_id)
            _ensure_recording_allowed_for_session(
                row.session_id, s, is_simulation=row.is_simulation)
            if row.status != AudioStatus.recorded:
                raise HTTPException(409, "音频已进入导出/校验/删除流程，禁止覆盖采集字节")
            if row.checksum is not None and row.checksum.lower() != pending.checksum:
                raise HTTPException(409, "数据库 checksum 与音频原件不一致")
            if row.byte_count is not None and row.byte_count != pending.byte_count:
                raise HTTPException(409, "数据库 byte_count 与音频原件不一致")
            try:
                audio_capture.assert_final_byte_quota(s, row, pending.byte_count)
            except audio_capture.AudioQuotaExceeded as exc:
                raise HTTPException(409, str(exc)) from exc
            except audio_capture.AudioLimitConfigurationError as exc:
                raise HTTPException(500, str(exc)) from exc
            try:
                saved = audio_store.publish_staged_blob(
                    raw_audio_id, pending, mutation_lock_held=True)
            except audio_store.AudioBlobConflict as exc:
                raise HTTPException(409, str(exc)) from exc
            except audio_store.AudioStoreIntegrityError as exc:
                raise HTTPException(409, str(exc)) from exc
            physical = audio_store.blob_facts(raw_audio_id)
            if (physical is None or physical.checksum != saved.checksum
                    or physical.byte_count != saved.byte_count):
                raise HTTPException(409, "音频发布后物理原件完整性检查失败")
            row.checksum = saved.checksum
            row.byte_count = saved.byte_count
            # Every other capture timestamp (command issued_at, ACK
            # received_at, receipt received_at) is UTC-naive. A local-naive
            # value here would read as ~8h in the future in Asia/Shanghai
            # and break every ordering proof built on those rows.
            row.uploaded_at = row.uploaded_at or utc_now_naive()
            row.audio_format = saved.path.suffix.lstrip(".")
            s.add(row)
            s.commit()
    except BaseException:
        # 只清理由本请求新发布、且尚未被任何并发请求写成 DB 权威事实的文件。
        if saved is not None and not saved.idempotent:
            try:
                s.rollback()
                s.expire_all()
                current = s.get(AudioAssetRow, raw_audio_id)
                committed = bool(
                    current and isinstance(current.checksum, str)
                    and current.checksum.lower() == saved.checksum
                    and current.byte_count == saved.byte_count
                    and current.status != AudioStatus.deleted
                    and not current.withdrawn
                    and not bool((current.withdrawal_status or "").strip()))
                if not committed:
                    audio_store.delete_blob_if_matches(
                        raw_audio_id, saved.checksum, saved.byte_count,
                        mutation_lock_held=True)
            except Exception:  # noqa: BLE001 - 保留证据优先，不掩盖原始错误
                _emit_operational_error("audio_rollback_cleanup_failed")
        raise
    finally:
        # The hard-linked target (if any) is independent; the hidden staging inode
        # is always removed before the short raw-id lease is released.
        audio_store.discard_staged_blob(pending)
        if mutation_lease is not None:
            mutation_lease.release()

    _audit(s, request, "audio_upload",
           f"保存录音 checksum={saved.checksum} bytes={saved.byte_count} "
           f"idempotent={saved.idempotent}", session_id=row.session_id)
    return {"raw_audio_id": raw_audio_id, "bytes": saved.byte_count,
            "checksum": saved.checksum, "format": row.audio_format,
            "idempotent": saved.idempotent, "uploaded_at": row.uploaded_at}


@app.get("/audio/{raw_audio_id}/blob")
def audio_download_blob(raw_audio_id: str, request: Request,
                        s: DBSession = Depends(get_session)):
    """回放存储字节(操作端复核用,同源本机)。"""
    _require_account_identity(
        request, "读取原始录音",
        roles={"researcher", "data_steward", "admin"})
    row = _load_row(raw_audio_id, s)
    sess = _require_audio_read_operator(
        row, request, s, "回放场次原始录音")
    _ensure_audio_read_allowed(row, s, sess=sess)
    if request.headers.get("range"):
        raise HTTPException(416, "原始录音不开放 Range 分段读取")
    p = audio_store.find_blob(raw_audio_id)
    if not p:
        raise HTTPException(404, "无音频字节(未上传或已删除)")
    _audit(s, request, "audio_blob_read", f"读取原始录音 {raw_audio_id}",
           patient_id=sess.patient_id, session_id=sess.session_id)
    return FileResponse(p, headers={
        "Cache-Control": "private, no-store",
        "Pragma": "no-cache",
        "Accept-Ranges": "none",
    })


# ---------------- 跨设备实时状态(内网双设备:平板老人端 + 电脑操作端)----------------


_LIVE_SLOT = {"session": "session_json", "cursor": "cursor_json",
              "rapportStep": "rapport_json", "audioSaved": "audio_json",
              "patientRec": "patient_rec_json"}

_CURSOR_SCREENS = {"idle", "present", "record", "thanks", "paused", "done"}
_RECORDING_STATES = {"idle", "armed", "recording", "stopped"}
PATIENT_ONLINE_TTL_SECONDS = 15
_LIVE_WRITE_LOCK = threading.RLock()
_SERVER_WSEQ = 0


class PatientHeartbeatIn(BaseModel):
    """老人端最小回执；配置 CONSOLE_PIN 时同样须认证，且拒绝额外敏感字段。"""
    model_config = ConfigDict(extra="forbid")

    session_id: str = PydanticField(min_length=1, max_length=128)
    screen: Literal[
        "idle", "waiting", "loading", "rapport", "present", "record", "thanks",
        "paused", "complete", "done", "error",
    ]
    cursor_wseq: int | None = PydanticField(default=None, ge=0)
    # 仅为旧/弱网客户端诊断兼容；在线真值一律采用服务器收件时间，绝不信任客户端时钟。
    client_ts: datetime | None = None


class RuntimeCursorIn(BaseModel):
    """受 PIN 保护的正式训练游标写入契约，不接受内容文本或回答。
    自动驾驶反馈同样不载文本:fbKey 只指向题库/协议里的固定话术,老人端本地查表回填。"""
    model_config = ConfigDict(extra="forbid")

    screen: Literal["idle", "present", "record", "thanks", "done"]
    itemIdx: int = PydanticField(ge=0)
    turnIdx: int = PydanticField(ge=0)
    responseRole: str | None = PydanticField(default=None, max_length=64)
    cueLevel: int | None = PydanticField(default=None, ge=0, le=3)
    recording: Literal["idle", "armed", "recording", "stopped"] = "idle"
    recSeq: int | None = PydanticField(default=None, ge=0)
    rawAudioId: str | None = PydanticField(default=None, max_length=160)
    selfStart: bool | None = None
    fbKey: Literal["self", "cued1_unknown", "cued1_close", "cued1_silence",
                   "cued2", "namefix_l", "namefix_r"] | None = None
    fbItemId: str | None = PydanticField(default=None, max_length=160)
    fbSeq: int | None = PydanticField(default=None, ge=0)
    wseq: int | None = PydanticField(default=None, ge=0)
    sessionId: str | None = PydanticField(default=None, min_length=1, max_length=128)
    session_id: str | None = PydanticField(default=None, min_length=1, max_length=128)
    # 乐观并发:带上调用方已知的 revision,与服务端不一致即 409——旧标签页/旧设备
    # 不能凭过期位置静默把老人端游标倒回旧题。不带则跳过检查(兼容脚本化调用)。
    expected_revision: int | None = PydanticField(default=None, ge=0)


class LiveSessionPayload(BaseModel):
    """患者端可见的场次握手；不接收姓名、画像或其他业务字段。"""
    model_config = ConfigDict(extra="forbid")

    sessionId: str | None = PydanticField(default=None, min_length=1, max_length=128)
    session_id: str | None = PydanticField(default=None, min_length=1, max_length=128)
    weekNo: int | None = PydanticField(default=None, ge=1, le=8)
    eventLine: str | None = PydanticField(default=None, min_length=1, max_length=64)
    mode: Literal["task", "rapport"] | None = None
    itemBankVersionId: str | None = PydanticField(default=None, min_length=1, max_length=128)
    wseq: int | None = PydanticField(default=None, ge=0)


class LiveRapportPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    sessionId: str | None = PydanticField(default=None, min_length=1, max_length=128)
    session_id: str | None = PydanticField(default=None, min_length=1, max_length=128)
    sectionKey: str = PydanticField(min_length=1, max_length=100,
                                    pattern=r"^[^\r\n\x00]+$")
    questionIdx: int = PydanticField(ge=0)
    recording: Literal["idle", "armed", "recording", "stopped"] = "idle"
    recSeq: int | None = PydanticField(default=None, ge=0)
    rawAudioId: str | None = PydanticField(default=None, max_length=160)
    assentGate: bool | None = None
    containsDirectIdentifier: bool
    wseq: int | None = PydanticField(default=None, ge=0)


class LiveAudioSavedPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    rawAudioId: str = PydanticField(min_length=1, max_length=160,
                                    pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,159}$")
    durationSeconds: float = PydanticField(ge=0, le=21_600)
    byteCount: int = PydanticField(ge=1, le=audio_store.MAX_AUDIO_BLOB_BYTES)
    checksum: str = PydanticField(pattern=r"^[0-9A-Fa-f]{64}$")
    turnKey: str = PydanticField(min_length=1, max_length=200,
                                 pattern=r"^[^\r\n\x00]+$")
    sessionId: str | None = PydanticField(default=None, min_length=1, max_length=128)
    session_id: str | None = PydanticField(default=None, min_length=1, max_length=128)
    containsDirectIdentifier: bool | None = None


class LivePatientRecPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    active: bool
    turnKey: str = PydanticField(min_length=1, max_length=200,
                                 pattern=r"^[^\r\n\x00]+$")
    sessionId: str | None = PydanticField(default=None, min_length=1, max_length=128)
    session_id: str | None = PydanticField(default=None, min_length=1, max_length=128)
    failureCode: Literal[
        "microphone_start_timeout",
        "microphone_permission_denied",
        "microphone_not_found",
        "microphone_start_failed",
        "recording_authorization_failed",
    ] | None = None
    failureId: str | None = PydanticField(
        default=None,
        pattern=evidence_ledger.PATIENT_REC_FAILURE_ID_PATTERN,
    )

    @model_validator(mode="after")
    def failure_fields_are_atomic_and_inactive(self):
        code_supplied = "failureCode" in self.model_fields_set
        id_supplied = "failureId" in self.model_fields_set
        if code_supplied != id_supplied:
            raise ValueError("patientRec failureCode/failureId 必须同时提供")
        if code_supplied:
            if self.failureCode is None or self.failureId is None:
                raise ValueError("patientRec failureCode/failureId 不得为 null")
            if not evidence_ledger.valid_patient_rec_failure_fact(
                    self.failureCode, self.failureId):
                raise ValueError("patientRec failureCode/failureId 格式非法")
            if self.active:
                raise ValueError("patientRec active=true 不得携带设备失败")
        return self


class LiveAudioDisposalConfirmedPayload(BaseModel):
    """设备删除本地副本后的回执:410 audio_terminal_disposition detail 的原样回显。

    严格封闭契约——字段集合、四个常量与 410 detail 完全一致;设备不得增删改任何
    字段。服务端随后逐项与资产行比对,不一致即拒。
    """
    model_config = ConfigDict(extra="forbid")

    code: Literal["audio_terminal_disposition"]
    schemaVersion: Literal[1]
    action: Literal["discard_local_copy"]
    reason: Literal["deleted", "withdrawn"]
    rawAudioId: str = PydanticField(min_length=1, max_length=160,
                                    pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,159}$")
    sessionId: str = PydanticField(min_length=1, max_length=128)
    turnKey: str = PydanticField(min_length=1, max_length=200,
                                 pattern=r"^[^\r\n\x00]+$")
    byteCount: int = PydanticField(ge=1, le=audio_store.MAX_AUDIO_BLOB_BYTES)
    checksum: str = PydanticField(pattern=r"^[0-9A-Fa-f]{64}$")
    containsDirectIdentifier: bool


_LIVE_PAYLOAD_MODELS = {
    "session": LiveSessionPayload,
    "cursor": RuntimeCursorIn,
    "rapportStep": LiveRapportPayload,
    "audioSaved": LiveAudioSavedPayload,
    "patientRec": LivePatientRecPayload,
    "audioDisposalConfirmed": LiveAudioDisposalConfirmedPayload,
}


class LiveIn(BaseModel):
    """实时写入边界：先按 kind 收紧 payload，再进入场次/计划语义校验。"""
    model_config = ConfigDict(extra="forbid")

    kind: Literal["session", "cursor", "rapportStep", "audioSaved", "patientRec",
                  "audioDisposalConfirmed"]
    payload: dict

    @model_validator(mode="after")
    def validate_payload_contract(self):
        payload_model = _LIVE_PAYLOAD_MODELS[self.kind]
        self.payload = payload_model.model_validate(self.payload).model_dump(exclude_none=True)
        return self


def _json_load(text: str | None) -> dict | None:
    if not text:
        return None
    try:
        value = _json.loads(text)
    except (TypeError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def _live_session_id(row: LiveState | None) -> str | None:
    payload = _json_load(row.session_json) if row else None
    value = (payload or {}).get("sessionId") or (payload or {}).get("session_id")
    return value if isinstance(value, str) and value else None


class DevicePairIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    deviceId: str = PydanticField(
        min_length=16, max_length=128, pattern=r"^[A-Za-z0-9_-]+$")


@app.post("/device/pair")
def device_pair(body: DevicePairIn, request: Request, response: Response,
                s: DBSession = Depends(get_session)):
    """当面用 PIN 配对；明文 capability 只在这一次响应中出现。"""
    if getattr(request.state, "auth_kind", None) != "device_pair":
        raise HTTPException(403, detail={
            "code": "device_pair_forbidden",
            "message": "此接口只接受当面设备配对",
        })
    with _LIVE_WRITE_LOCK, device_capability.serialized_mutation():
        live = _live_row_for_update(s)
        session_id = _live_session_id(live)
        if not session_id:
            _raise_device_conflict("device_pair_no_live_session", "当前没有可配对的训练场次")
        sess = s.get(TrainSession, session_id)
        if sess is None:
            _raise_device_conflict("device_pair_session_missing", "当前训练场次不存在")
        _require_started_visit_plan_session(session_id, s, sess=sess)
        _require_classified_session(sess)
        state = s.get(SessionRuntimeState, session_id)
        runtime_status = state.status if state is not None else "active"
        if runtime_status not in _MUTABLE_RUNTIME_STATUSES:
            _raise_device_conflict("device_pair_session_inactive", "当前训练场次不可配对")
        # A same-session rotation (an active capability already exists) must
        # fail-safe: any server-hosted P0a scope waiting on, or mid-capture/
        # attempt on, the old device's audio is paused in the same commit as
        # the rotation itself below, so a stale in-flight result from the old
        # device generation cannot silently materialize and route a command
        # to the newly paired device, and a failure anywhere in this combined
        # operation rolls back the capability rotation too — never a
        # committed new capability with a not-yet-fenced old claim.
        had_active_capability = s.exec(select(PatientDeviceCapability).where(
            PatientDeviceCapability.session_id == session_id,
            PatientDeviceCapability.revoked_at.is_(None),
        )).first() is not None
        try:
            token, capability = device_capability.create_capability(
                s, session_id=session_id, device_id=body.deviceId)
        except device_capability.DeviceCapabilityConfigurationError as exc:
            raise HTTPException(500, detail={
                "code": "device_capability_configuration_invalid",
                "message": "设备能力时限配置无效",
            }) from exc
        except (RuntimeError, ValueError, IntegrityError) as exc:
            # A concurrent-pairing collision must fail the whole request, not
            # retry internally: this transaction's LiveState/session/runtime
            # reads (including had_active_capability) become stale the moment
            # any rollback happens. The client retries the entire request,
            # re-acquiring a fresh snapshot from scratch.
            s.rollback()
            raise HTTPException(503, detail={
                "code": "device_pair_unavailable",
                "message": "暂时无法完成设备配对，请重试",
            }) from exc
        if had_active_capability:
            try:
                fenced = autopilot_service.fence_autonomous_scope_for_device_rotation(
                    s, session_id=session_id)
                if fenced:
                    _pause_runtime_in_transaction(session_id, s)
            except autopilot_service.AutopilotServiceError as exc:
                _autopilot_write_failure(s, exc)
            except IntegrityError as exc:
                _autopilot_integrity_conflict(s, exc)
        # Single commit for the whole operation: pairing (create_capability
        # only staged/flushed above) and any rotation fencing land in one
        # atomic snapshot, or neither does.
        s.commit()
        s.refresh(capability)
    # Bearer appears exactly once.  Do not rely on POST's usual cache behavior.
    response.headers["Cache-Control"] = "private, no-store"
    response.headers["Pragma"] = "no-cache"
    expires_at = capability.expires_at.replace(tzinfo=timezone.utc).isoformat().replace(
        "+00:00", "Z")
    return {
        "capability": token,
        "sessionId": capability.session_id,
        "expiresAt": expires_at,
    }


def _payload_session_id(payload: dict) -> str | None:
    camel = payload.get("sessionId")
    snake = payload.get("session_id")
    if camel is not None and snake is not None and camel != snake:
        raise HTTPException(422, "sessionId 与 session_id 不一致")
    value = camel if camel is not None else snake
    return value if isinstance(value, str) and value else None


_PUBLIC_LIVE_FIELDS = {
    "session": {"sessionId", "weekNo", "eventLine", "mode", "itemBankVersionId",
                "paused", "runtimeStatus", "wseq"},
    "cursor": {"sessionId", "screen", "itemIdx", "turnIdx", "responseRole",
               "cueLevel", "recording", "recSeq", "selfStart",
               "fbKey", "fbSeq", "wseq"},
    "rapportStep": {"sessionId", "sectionKey", "questionIdx", "recording", "recSeq",
                    "assentGate", "containsDirectIdentifier", "paused", "wseq"},
}


def _public_live_projection(kind: str, text: str | None) -> dict | None:
    """即使数据库里留有旧版/异常额外键，患者免 PIN 读口也只返回呈现白名单。"""
    payload = _json_load(text)
    if payload is None:
        return None
    allowed = _PUBLIC_LIVE_FIELDS[kind]
    return {key: value for key, value in payload.items() if key in allowed}


def _live_row_for_update(s: DBSession) -> LiveState | None:
    return s.exec(select(LiveState).where(LiveState.id == 1).with_for_update()).first()


def _wseq_from(payload: dict | None) -> int | None:
    value = (payload or {}).get("wseq")
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def _stamp_command_source(payload: dict, _previous: dict | None, _kind: str) -> None:
    """仅留存客户端序号供诊断，不让它参与服务端顺序判定。

    不同设备、不同场次的本地计数器不在同一个时钟域内；强制比较会把合法的
    切场、重连或客户端重启误判为回退。命令总序只由 ``_allocate_live_wseq``
    在服务端接收顺序上生成。
    """
    payload.pop("sourceWseq", None)
    incoming = _wseq_from(payload)
    if incoming is not None:
        payload["sourceWseq"] = incoming


def _allocate_live_wseq(row: LiveState) -> int:
    """由服务端分配唯一递增命令序号；客户端传入值不参与决策，避免旧/快钟设备污染。"""
    global _SERVER_WSEQ
    floors = [int(datetime.now().timestamp() * 1000), row.command_wseq or 0, _SERVER_WSEQ]
    for payload in (_json_load(row.session_json), _json_load(row.cursor_json),
                    _json_load(row.rapport_json)):
        value = _wseq_from(payload)
        if value is not None:
            floors.append(value)
    value = max(floors) + 1
    _SERVER_WSEQ = value
    row.command_wseq = value
    return value


def _allocate_runtime_wseq(state: SessionRuntimeState) -> int:
    """场次不在当前 LiveState 时仍给其安全恢复指针分配新的服务端序号。"""
    global _SERVER_WSEQ
    floors = [int(datetime.now().timestamp() * 1000), _SERVER_WSEQ]
    for payload in (_json_load(state.cursor_json), _json_load(state.rapport_json)):
        value = _wseq_from(payload)
        if value is not None:
            floors.append(value)
    value = max(floors) + 1
    _SERVER_WSEQ = value
    return value


def _prime_live_wseq_from_runtime(row: LiveState, state: SessionRuntimeState | None) -> None:
    """进程重启后，非当前场次 runtime 可能比 LiveState 更新；握手前先纳入序号下界。"""
    if state is None:
        return
    for payload in (_json_load(state.cursor_json), _json_load(state.rapport_json)):
        value = _wseq_from(payload)
        if value is not None:
            row.command_wseq = max(row.command_wseq or 0, value)


def _presence_payload(row: LiveState | None, now: datetime | None = None) -> dict:
    now = now or datetime.now()
    last_seen = row.patient_last_seen_at if row else None
    online = bool(last_seen and now - last_seen <= timedelta(seconds=PATIENT_ONLINE_TTL_SECONDS))
    return {
        "session_id": row.patient_ack_session_id if row else None,
        "screen": row.patient_current_screen if row else None,
        "last_seen_at": last_seen,
        "online": online,
        "cursor_wseq": row.patient_ack_seq if row else None,
    }


def _runtime_row(session_id: str, s: DBSession) -> SessionRuntimeState:
    row = s.get(SessionRuntimeState, session_id)
    if row is None:
        row = SessionRuntimeState(session_id=session_id, status="active", revision=0)
    return row


def _runtime_row_for_update(
        session_id: str, s: DBSession) -> SessionRuntimeState:
    """Lock the runtime authority row, creating it under a locked Session row."""
    row = s.exec(select(SessionRuntimeState).where(
        SessionRuntimeState.session_id == session_id,
    ).with_for_update()).first()
    if row is None:
        row = SessionRuntimeState(
            session_id=session_id, status="active", revision=0)
        s.add(row)
        s.flush()
    return row


def _runtime_payload(session_id: str, row: SessionRuntimeState | None) -> dict:
    return {
        "sessionId": session_id,
        "status": row.status if row else "active",
        "revision": row.revision if row else 0,
        "cursor": _json_load(row.cursor_json) if row else None,
        "rapportStep": _json_load(row.rapport_json) if row else None,
        "pausedAt": row.paused_at if row else None,
        "resumedAt": row.resumed_at if row else None,
        "interventionCompletedAt": row.intervention_completed_at if row else None,
        "interventionEndedBy": row.intervention_ended_by if row else None,
        "completedAt": row.completed_at if row else None,
        "abortedAt": row.aborted_at if row else None,
        "endedBy": row.ended_by if row else None,
        "endReason": (
            _abort_public_reason(row.end_reason)
            if row and row.status == "aborted" else row.end_reason if row else None
        ),
        "updatedAt": row.updated_at if row else None,
    }


def _safe_cursor(payload: dict) -> dict:
    """恢复时绝不自动重开麦克风，也不携带在途音频指针。"""
    safe = dict(payload)
    if safe.get("screen") in {"record", "paused"}:
        safe["screen"] = "present"
    safe["recording"] = "idle"
    safe["selfStart"] = False
    safe.pop("rawAudioId", None)
    return safe


def _safe_rapport(payload: dict) -> dict:
    safe = dict(payload)
    safe["recording"] = "idle"
    safe.pop("rawAudioId", None)
    safe.pop("paused", None)
    return safe


def _session_plan_for_runtime(sess: TrainSession) -> runtime.SessionPlan:
    bank = _load_bank_for_session(sess)
    event = sess.event_line.value if hasattr(sess.event_line, "value") else str(sess.event_line)
    try:
        return runtime.build_session_plan(bank, sess.week_no, event)
    except ValueError as e:
        raise HTTPException(409, str(e))


def _validate_session_handshake(sess: TrainSession, payload: dict) -> None:
    event = sess.event_line.value if hasattr(sess.event_line, "value") else str(sess.event_line)
    checks = (
        ("weekNo", sess.week_no),
        ("eventLine", event),
        ("itemBankVersionId", sess.item_bank_version_id),
    )
    for key, expected in checks:
        if key == "weekNo" and key in payload and (
                not isinstance(payload[key], int) or isinstance(payload[key], bool)):
            raise HTTPException(422, "session payload 的 weekNo 必须是整数")
        if key in payload and payload[key] != expected:
            raise HTTPException(409, f"session payload 的 {key} 与数据库场次不一致")
    expected_mode = "rapport" if sess.week_no == 1 else "task"
    if "mode" in payload and payload["mode"] != expected_mode:
        raise HTTPException(409, "session payload 的 mode 与数据库场次不一致")


def _require_live_payload_session(
        payload: dict, row: LiveState, request: Request, s: DBSession, *,
        action: str) -> TrainSession:
    session_id = _payload_session_id(payload)
    if not session_id:
        raise HTTPException(422, "live payload 必须显式携带 sessionId 或 session_id")
    sess = s.get(TrainSession, session_id)
    if not sess:
        raise HTTPException(404, "live payload 场次不存在")
    if getattr(request.state, "auth_kind", None) != "device_capability":
        _require_session_operator(
            request,
            sess,
            s,
            action,
            mutation=True,
            not_found_detail="live payload 场次不存在",
        )
    # Only an already-authorized account (or the exact paired capability) may
    # learn whether its requested session is the current shared live slot.
    if session_id != _live_session_id(row):
        raise HTTPException(409, "live payload 场次与当前操作端场次不一致")
    return _require_started_visit_plan_session(session_id, s, sess=sess)


def _canonicalize_patient_turn_input(
        request: Request, sess: TrainSession, turn_key: str,
        *, legacy_exact_turn_key: str | None = None,
) -> tuple[str, bool]:
    """Resolve a patient-device position ref without persisting a new secret.

    New task writes from a protected device must use ``itm-NNNN#turn``.  A legacy
    canonical key is accepted only when the caller is replaying an already
    registered asset whose immutable canonical turn_key is exactly equal; this
    preserves registered/uploaded outboxes without opening a target-word oracle.
    Account requests retain the canonical contract.  Week-1 relationship keys do
    not encode an answer and remain canonical until the rapport protocol receives
    its own opaque position migration.
    """
    if sess.week_no == 1:
        return turn_key, getattr(request.state, "auth_kind", None) == "device_capability"

    is_opaque = turn_key.startswith("itm-")
    if is_opaque:
        try:
            canonical, _item_idx, _turn_seq = patient_presentation.resolve_task_turn_ref(
                _session_plan_for_runtime(sess), turn_key)
        except ValueError as exc:
            raise HTTPException(422, "设备 turn ref 不属于本场次冻结计划") from exc
        return canonical, True

    if getattr(request.state, "auth_kind", None) == "device_capability":
        if legacy_exact_turn_key is not None and turn_key == legacy_exact_turn_key:
            return turn_key, True
        raise HTTPException(
            422,
            detail={
                "code": "device_opaque_turn_ref_required",
                "message": "设备新写入必须使用场次题位引用",
            },
        )
    return turn_key, False


def _require_current_patient_turn_for_device(
        request: Request, sess: TrainSession, live: LiveState | None,
        state: SessionRuntimeState, canonical_turn_key: str,
        *, opaque_input: bool,
) -> None:
    """Opaque/untrusted device writes may only bind the server-owned current turn."""
    if not opaque_input and getattr(request.state, "auth_kind", None) != "device_capability":
        return
    if live is None:
        raise HTTPException(409, "服务端尚无可绑定的当前录音位置")
    expected, _item_id, _turn_seq = _current_patient_rec_turn(sess, live, state)
    if canonical_turn_key != expected:
        raise HTTPException(409, "设备 turn ref 与服务端当前冻结位置不一致")


def _validate_audio_turn_key(session_id: str | None, turn_key: str | None,
                             s: DBSession) -> None:
    if turn_key is None:
        return
    if not session_id:
        raise HTTPException(422, "turn_key 必须绑定 session_id")
    sess = s.get(TrainSession, session_id)
    if not sess:
        raise HTTPException(404, "音频关联场次不存在")
    if sess.week_no == 1:
        script = content.load_week1_script(content.CONTENT_DIR / "week1_script.json")
        allowed = {f"关系建立·{section.get('key')}" for section in script.get("sections", [])
                   if section.get("key")}
    else:
        plan = _session_plan_for_runtime(sess)
        allowed = {f"{item.item_id}#{turn.turn_seq}"
                   for item in plan.items for turn in item.turns}
    if turn_key not in allowed:
        raise HTTPException(422, "turn_key 不属于该场次绑定的冻结计划/脚本")


def _patient_rec_turn_identity(
        sess: TrainSession, turn_key: str) -> tuple[str | None, int | None]:
    """Resolve a validated failure turn key without consulting the mutable cursor."""
    if sess.week_no == 1:
        script = content.load_week1_script(content.CONTENT_DIR / "week1_script.json")
        allowed = {
            f"关系建立·{section.get('key')}"
            for section in script.get("sections", []) if section.get("key")
        }
        if turn_key not in allowed:
            raise HTTPException(422, "turnKey 不属于该场次冻结关系建立脚本")
        # Week 1 has no task turn_seq.  Retain the frozen section identity in
        # item_id so a failure id cannot be replayed against another section.
        return turn_key, None

    plan = _session_plan_for_runtime(sess)
    for item in plan.items:
        for turn in item.turns:
            if turn_key == f"{item.item_id}#{turn.turn_seq}":
                return item.item_id, turn.turn_seq
    raise HTTPException(422, "turnKey 不属于该场次绑定的冻结计划")


def _current_patient_rec_turn(
        sess: TrainSession, live: LiveState, state: SessionRuntimeState,
) -> tuple[str, str | None, int | None]:
    """Return the server-owned current turn without re-running recording authorization.

    The cursor/rapport snapshot was validated when it was written.  A microphone failure
    must remain reportable precisely when recording authorization or device startup has
    failed, so this lookup validates only the frozen position identity.
    """
    if sess.week_no == 1:
        rapport = _json_load(state.rapport_json) or _json_load(live.rapport_json)
        section_key = (rapport or {}).get("sectionKey")
        if not isinstance(section_key, str) or not section_key:
            raise HTTPException(409, "当前场次尚无可绑定的关系建立录音位置")
        turn_key = f"关系建立·{section_key}"
        return turn_key, turn_key, None

    cursor = _json_load(state.cursor_json) or _json_load(live.cursor_json)
    item_idx = (cursor or {}).get("itemIdx")
    turn_idx = (cursor or {}).get("turnIdx")
    if (not isinstance(item_idx, int) or isinstance(item_idx, bool)
            or not isinstance(turn_idx, int) or isinstance(turn_idx, bool)):
        raise HTTPException(409, "当前场次尚无可绑定的训练录音位置")
    plan = _session_plan_for_runtime(sess)
    if item_idx < 0 or item_idx >= len(plan.items):
        raise HTTPException(409, "当前训练题位已超出冻结计划")
    item = plan.items[item_idx]
    if turn_idx < 0 or turn_idx >= len(item.turns):
        raise HTTPException(409, "当前训练环节已超出冻结计划")
    turn = item.turns[turn_idx]
    return f"{item.item_id}#{turn.turn_seq}", item.item_id, turn.turn_seq


def _turn_is_locked(s: DBSession, session_id: str, item_id: str, turn_seq: int) -> bool:
    items = list(s.exec(select(ItemEvent).where(
        ItemEvent.session_id == session_id, ItemEvent.item_id == item_id)))
    for item in items:
        if item.id is None:
            continue
        turns = s.exec(select(TurnEvent).where(
            TurnEvent.item_event_id == item.id, TurnEvent.turn_seq == turn_seq))
        if any(turn.score_locked for turn in turns):
            return True
    return False


def _validate_cursor(sess: TrainSession, payload: dict, s: DBSession) -> None:
    item_idx = payload.get("itemIdx")
    turn_idx = payload.get("turnIdx")
    if (not isinstance(item_idx, int) or isinstance(item_idx, bool)
            or not isinstance(turn_idx, int) or isinstance(turn_idx, bool)):
        raise HTTPException(422, "itemIdx/turnIdx 必须是非负整数")
    if item_idx < 0 or turn_idx < 0:
        raise HTTPException(422, "itemIdx/turnIdx 不得小于 0")
    if payload.get("screen") not in _CURSOR_SCREENS:
        raise HTTPException(422, "未知患者画面 screen")
    if payload.get("recording", "idle") not in _RECORDING_STATES:
        raise HTTPException(422, "未知 recording 状态")

    plan = _session_plan_for_runtime(sess)
    if item_idx >= len(plan.items):
        raise HTTPException(422, "itemIdx 超出场次冻结计划")
    item = plan.items[item_idx]
    if turn_idx >= len(item.turns):
        raise HTTPException(422, "turnIdx 超出题目冻结计划")
    turn = item.turns[turn_idx]
    supplied_role = payload.get("responseRole")
    if supplied_role is not None and supplied_role != turn.response_role:
        raise HTTPException(422, "responseRole 与冻结计划当前位置不一致")

    asks_to_record = payload.get("recording") in {"armed", "recording"} or payload.get("selfStart") is True
    if asks_to_record:
        _ensure_recording_allowed_for_session(
            sess.session_id, s, is_simulation=sess.is_simulation)
        if _turn_is_locked(s, sess.session_id, item.item_id, turn.turn_seq):
            raise HTTPException(409, "当前位置已锁分，禁止重新下发录音状态")


def _validate_rapport(sess: TrainSession, payload: dict, s: DBSession) -> None:
    if sess.week_no != 1:
        raise HTTPException(409, "rapportStep 仅属于第1周关系建立场次")
    section_key = payload.get("sectionKey")
    question_idx = payload.get("questionIdx")
    if not isinstance(section_key, str) or not section_key:
        raise HTTPException(422, "sectionKey 不得为空")
    if not isinstance(question_idx, int) or isinstance(question_idx, bool) or question_idx < 0:
        raise HTTPException(422, "questionIdx 必须是非负整数")
    script = content.load_week1_script(content.CONTENT_DIR / "week1_script.json")
    section = next((row for row in script.get("sections", []) if row.get("key") == section_key), None)
    if section is None:
        raise HTTPException(422, "sectionKey 不在冻结关系建立脚本中")
    questions = section.get("questions") or []
    if (questions and question_idx >= len(questions)) or (not questions and question_idx != 0):
        raise HTTPException(422, "questionIdx 超出冻结关系建立脚本")
    recording = payload.get("recording", "idle")
    if recording not in _RECORDING_STATES:
        raise HTTPException(422, "未知 recording 状态")
    if recording in {"armed", "recording"}:
        _ensure_recording_allowed_for_session(
            sess.session_id, s, is_simulation=sess.is_simulation)


def _pause_projection(payload: dict, wseq: int) -> dict:
    paused = _safe_cursor(payload)
    paused["screen"] = "paused"
    paused["recording"] = "stopped"
    paused["wseq"] = wseq
    return paused


def _set_live_session_paused(row: LiveState, paused: bool) -> int | None:
    """暂停是场次级状态；即使尚无 cursor/rapport，患者端也必须立即收到休息指令。"""
    payload = _json_load(row.session_json)
    if payload is None:
        return None
    payload["paused"] = paused
    wseq = _allocate_live_wseq(row)
    payload["wseq"] = wseq
    row.session_json = _json.dumps(payload, ensure_ascii=False)
    return wseq


def _restore_runtime_to_live(row: LiveState, state: SessionRuntimeState | None) -> None:
    """把持久位置投影回 live；每个恢复快照重新分配高于所有旧快照的服务端 wseq。"""
    row.cursor_json = None
    row.rapport_json = None
    if state is None:
        return
    changed = False
    cursor = _json_load(state.cursor_json)
    if cursor:
        cursor = _safe_cursor(cursor)
        cursor["sessionId"] = state.session_id
        cursor.pop("session_id", None)
        wseq = _allocate_live_wseq(row)
        cursor["wseq"] = wseq
        state.cursor_json = _json.dumps(cursor, ensure_ascii=False)
        projected = (_pause_projection(cursor, wseq)
                     if state.status == "paused" else cursor)
        row.cursor_json = _json.dumps(projected, ensure_ascii=False)
        changed = True

    rapport = _json_load(state.rapport_json)
    if rapport:
        rapport = _safe_rapport(rapport)
        rapport["sessionId"] = state.session_id
        rapport.pop("session_id", None)
        wseq = _allocate_live_wseq(row)
        rapport["wseq"] = wseq
        state.rapport_json = _json.dumps(rapport, ensure_ascii=False)
        projected = dict(rapport)
        if state.status == "paused":
            projected.update({"recording": "stopped", "paused": True})
        row.rapport_json = _json.dumps(projected, ensure_ascii=False)
        changed = True

    if changed:
        state.revision += 1
        state.updated_at = datetime.now()


def _apply_audio_disposal_confirmed(
        body: "LiveIn", request: Request, s: DBSession) -> dict:
    """kind=audioDisposalConfirmed:设备确认已物理删除本地副本的治理回执(收据 144)。

    回执是治理信号,不是门禁:不触碰 LiveState/runtime,只在逐项核验 410 回显与
    资产行一致后落一条只追加回执。同一 (raw_audio_id, 设备) 幂等;设备是不可信端,
    回执只证明"该设备声称已删",价值在治理可见性。
    """
    payload = body.payload
    session_id = payload["sessionId"]
    # 与 audioSaved 恢复路径同款 confused-deputy 双防线:中间件已挡账号 cookie,
    # 这里再挡一次,防止未来路由调整让账号路径漏进业务层。
    if (auth.auth_active()
            and getattr(request.state, "auth_kind", None) != "device_capability"):
        raise HTTPException(403, detail={
            "code": "audio_disposition_device_capability_required",
            "message": "本地副本删除回执只能由精确绑定本场次的设备能力凭据上报",
        })
    _require_capability_bound_session(request, session_id, "上报本地副本删除回执")
    with _LIVE_WRITE_LOCK, device_capability.serialized_mutation():
        s.rollback()
        s.expire_all()
        _require_started_visit_plan_session(session_id, s)
        # 410 常在场次离开 LiveState 后才到达设备,RECOVERY_ONLY 必须仍可上报;
        # 已吊销/过期凭据照旧拒绝。
        status, cap_row = device_capability.revalidate_active_for_write(
            s, getattr(request.state, "device_capability_token_hash", None),
            session_id)
        if status not in {device_capability.CapabilityResolution.VALID,
                          device_capability.CapabilityResolution.RECOVERY_ONLY}:
            code = {
                device_capability.CapabilityResolution.INVALID:
                    "device_capability_invalid",
                device_capability.CapabilityResolution.EXPIRED:
                    "device_capability_expired",
                device_capability.CapabilityResolution.REVOKED:
                    "device_capability_revoked",
            }[status]
            raise HTTPException(401, detail={
                "code": code, "message": "设备配对已失效，请由研究者重新配对"})
        if cap_row is None:
            raise HTTPException(403, detail={
                "code": "audio_disposition_device_capability_required",
                "message": "本地副本删除回执只能由精确绑定本场次的设备能力凭据上报",
            })
        asset = s.exec(select(AudioAssetRow).where(
            AudioAssetRow.raw_audio_id == payload["rawAudioId"]
        ).with_for_update()).first()
        # 与 410 路径同口径:外域 id 与不存在 id 响应完全一致,不可枚举。
        if asset is None or asset.session_id != session_id:
            raise HTTPException(404, detail={
                "code": "audio_disposition_unknown",
                "message": "服务端没有该录音的登记事实，禁止删除本地副本",
            })
        sess = s.get(TrainSession, session_id)
        if sess is None:
            raise HTTPException(409, "音频关联场次不存在")
        canonical_turn_key, _patient_ref = _canonicalize_patient_turn_input(
            request, sess, payload["turnKey"],
            legacy_exact_turn_key=(
                asset.turn_key if asset.patient_turn_ref_version == 1 else None))
        if asset.status == AudioStatus.deleted:
            # 与 410 发放端 verify_terminal_disposition 逐字一致:deleted 而缺
            # 删除闸门证据的行从未被授权过 410,其"删除回执"是协议违规,不落账。
            if not asset.delete_gate_passed:
                raise HTTPException(409, detail={
                    "code": "audio_disposal_mismatch",
                    "message": "录音标记 deleted 但缺少删除闸门证据，"
                               "服务端从未授权该本地删除",
                })
            expected_reason = "deleted"
        elif asset.withdrawn or bool((asset.withdrawal_status or "").strip()):
            expected_reason = "withdrawn"
        else:
            raise HTTPException(409, detail={
                "code": "audio_disposal_not_terminal",
                "message": "录音仍是活跃研究事实，不存在可确认的终态删除",
            })
        checksum = payload["checksum"].lower()
        if (asset.turn_key != canonical_turn_key
                or payload["reason"] != expected_reason
                or (asset.checksum or "").lower() != checksum
                or asset.byte_count != payload["byteCount"]
                or asset.contains_direct_identifier
                != payload["containsDirectIdentifier"]):
            raise HTTPException(409, detail={
                "code": "audio_disposal_mismatch",
                "message": "删除回执与服务端资产终态事实不一致，拒绝登记",
            })
        reporter = cap_row.device_id_hash
        existing = s.exec(select(AudioLocalCopyDisposalReceipt).where(
            AudioLocalCopyDisposalReceipt.raw_audio_id == payload["rawAudioId"],
            AudioLocalCopyDisposalReceipt.reporter_device_id_hash == reporter,
        )).first()
        if existing is not None:
            return {"code": "audio_disposal_recorded",
                    "rawAudioId": payload["rawAudioId"], "duplicate": True}
        s.add(AudioLocalCopyDisposalReceipt(
            raw_audio_id=payload["rawAudioId"],
            session_id=session_id,
            turn_key=canonical_turn_key,
            reason=expected_reason,
            checksum=checksum,
            byte_count=payload["byteCount"],
            contains_direct_identifier=payload["containsDirectIdentifier"],
            reporter_device_id_hash=reporter,
        ))
        s.commit()
        _audit(s, request, "audio_local_copy_disposed",
               f"设备确认删除本地录音副本 {payload['rawAudioId']}"
               f"(reason={expected_reason},device={reporter[:12]})",
               session_id=session_id)
        return {"code": "audio_disposal_recorded",
                "rawAudioId": payload["rawAudioId"], "duplicate": False}


@app.put("/live/state")
def live_put(body: LiveIn, request: Request, s: DBSession = Depends(get_session)):
    """写实时状态；服务端重签 wseq，并把游标同步到当前场次恢复行。"""
    if body.kind == "audioDisposalConfirmed":
        # 治理回执不写 LiveState 槽位,走独立只追加路径。
        return _apply_audio_disposal_confirmed(body, request, s)
    slot = _LIVE_SLOT.get(body.kind)
    if not slot:
        raise HTTPException(422, f"未知 kind {body.kind!r}")

    if body.kind in access_policy.DEVICE_LIVE_WRITE_KINDS:
        _require_capability_bound_session(
            request, _payload_session_id(body.payload), "上报录音状态")

    def apply(row: LiveState) -> tuple[
            LiveState, int | None, AudioCaptureReceipt | None, bool]:
        payload = dict(body.payload)
        command_wseq: int | None = None
        capture_receipt: AudioCaptureReceipt | None = None
        receipt_idempotent = False
        if body.kind == "session":
            previous_session_id = _live_session_id(row)
            session_id = _payload_session_id(payload)
            if not session_id:
                raise HTTPException(422, "session payload 缺 sessionId")
            if getattr(request.state, "auth_kind", None) == "account":
                sess = _load_session_for_operator(
                    request,
                    session_id,
                    s,
                    "握手或激活床旁场次",
                    mutation=True,
                )
                if previous_session_id and previous_session_id != session_id:
                    previous = s.get(TrainSession, previous_session_id)
                    if previous is not None:
                        _require_session_operator(
                            request, previous, s, "切换当前床旁场次",
                            mutation=True,
                            allow_withdrawn_safety_exit=True)
            else:
                sess = s.get(TrainSession, session_id)
                if not sess:
                    raise HTTPException(404, "场次不存在")
                _require_session_operator(
                    request, sess, s, "握手或激活床旁场次", mutation=True)
            _require_started_visit_plan_session(session_id, s, sess=sess)
            _ensure_manual_plane_writable(
                session_id, s, "从旧操作端重新握手或覆盖实时场次")
            _validate_session_handshake(sess, payload)
            _stamp_command_source(payload, _json_load(row.session_json), "session")
            payload["sessionId"] = session_id
            payload.pop("session_id", None)
            state = s.get(SessionRuntimeState, session_id)
            _ensure_runtime_writable(session_id, s, "重新握手或激活")
            _prime_live_wseq_from_runtime(row, state)
            command_wseq = _allocate_live_wseq(row)
            payload["wseq"] = command_wseq
            if state and state.status == "paused":
                # pause 可能在首次 live 握手之前已落 runtime。后到握手必须继承休息状态，
                # 否则老人端会因“无游标”停在加载页，而不是明确的暂停屏。
                payload["paused"] = True
            # 新场次握手清瞬时回报，但从该场次自己的 runtime 行恢复安全位置，避免串场或覆盖。
            row.audio_json = None
            row.patient_rec_json = None
            if previous_session_id != session_id:
                device_capability.mark_session_recovery_only(s, previous_session_id)
                row.patient_ack_session_id = None
                row.patient_current_screen = None
                row.patient_last_seen_at = None
                row.patient_ack_seq = None
            setattr(row, slot, _json.dumps(payload, ensure_ascii=False))
            _restore_runtime_to_live(row, state)
            if state:
                s.add(state)
            command_wseq = row.command_wseq
        elif body.kind in {"cursor", "rapportStep"}:
            sess = _require_live_payload_session(
                payload,
                row,
                request,
                s,
                action="推进床旁运行位置",
            )
            _ensure_manual_plane_writable(
                sess.session_id, s, "从旧操作端推进实时游标")
            _stamp_command_source(payload, _json_load(getattr(row, slot)), body.kind)
            session_id = sess.session_id
            payload["sessionId"] = session_id
            payload.pop("session_id", None)
            state = _runtime_row(session_id, s)
            _ensure_runtime_writable(session_id, s, "写入实时游标")
            if state.status == "paused":
                raise HTTPException(409, "场次已暂停；须先恢复后才能推进游标")
            if body.kind == "cursor":
                _validate_cursor(sess, payload, s)
            else:
                _validate_rapport(sess, payload, s)
            command_wseq = _allocate_live_wseq(row)
            payload["wseq"] = command_wseq
            if body.kind == "cursor":
                state.cursor_json = _json.dumps(_safe_cursor(payload), ensure_ascii=False)
            else:
                state.rapport_json = _json.dumps(_safe_rapport(payload), ensure_ascii=False)
            state.revision += 1
            state.updated_at = datetime.now()
            s.add(state)
            setattr(row, slot, _json.dumps(payload, ensure_ascii=False))
        else:
            # 瞬时老人端回报也必须显式绑定当前场次，避免切场竞态污染新场 journal。
            sess = _require_live_payload_session(
                payload,
                row,
                request,
                s,
                action="上报老人端录音状态",
            )
            state = _runtime_row(sess.session_id, s)
            _ensure_runtime_writable(sess.session_id, s, "写入实时录音回报")
            if body.kind == "audioSaved":
                asset = _load_row(payload["rawAudioId"], s)
                canonical_turn_key, _patient_ref = _canonicalize_patient_turn_input(
                    request,
                    sess,
                    payload["turnKey"],
                    legacy_exact_turn_key=(
                        asset.turn_key
                        if (asset.session_id == sess.session_id
                            and asset.patient_turn_ref_version == 1)
                        else None
                    ),
                )
                if asset.session_id != sess.session_id or asset.turn_key != canonical_turn_key:
                    raise HTTPException(409, "audioSaved 与已登记音频的场次/环节不一致")
                # 持久化、控制台投影与证据账本始终使用 canonical turn_key。
                payload["turnKey"] = canonical_turn_key
                reported_identifier = payload.get("containsDirectIdentifier")
                if (reported_identifier is not None
                        and reported_identifier != asset.contains_direct_identifier):
                    raise HTTPException(409, "audioSaved 直接标识标记与音频登记不一致")
                payload["containsDirectIdentifier"] = asset.contains_direct_identifier
                payload["checksum"] = payload["checksum"].lower()
                try:
                    capture_receipt, receipt_idempotent = audio_capture.append_receipt(
                        s, row=asset, session_id=sess.session_id,
                        turn_key=payload["turnKey"],
                        duration_seconds=payload["durationSeconds"],
                        byte_count=payload["byteCount"],
                        checksum=payload["checksum"],
                    )
                except audio_capture.AudioCaptureIntegrityError as exc:
                    raise HTTPException(409, str(exc)) from exc
                if receipt_idempotent:
                    # 同一 raw id/完全相同事实是纯 ACK：不把 LiveState.seq 再推进，
                    # 也不允许迟到重放覆盖后来完成的另一条单槽快照。
                    return row, command_wseq, capture_receipt, True
            elif body.kind == "patientRec" and "failureCode" in payload:
                # 麦克风启动失败是服务端权威技术暂停，不是一次 Attempt。
                # LiveState 已在外层加锁，capability 也在同一事务内重验；
                payload["sessionId"] = sess.session_id
                payload.pop("session_id", None)

                # InteractionEvent 是跨 resume/游标前移的持久幂等账本。
                # 必须在读“当前转位”或 paused 之前查它：HTTP ACK 丢失后，
                # 研究者可能已恢复并前移，旧重放仍只能是纯 ACK。
                failure_events = list(s.exec(select(InteractionEvent).where(
                    InteractionEvent.session_id == sess.session_id,
                    InteractionEvent.event_type == "technical_pause",
                ).order_by(InteractionEvent.event_seq)))
                matching_failure_events: list[tuple[InteractionEvent, dict]] = []
                for event in failure_events:
                    try:
                        event_payload = evidence_ledger.validate_stored_payload(
                            event.event_type, event.payload_json)
                    except ValueError as exc:
                        raise HTTPException(
                            409, "技术暂停证据损坏，禁止继续处理设备失败") from exc
                    if event_payload.get("failure_id") == payload["failureId"]:
                        matching_failure_events.append((event, event_payload))

                try:
                    canonical_turn_key, patient_ref_input = _canonicalize_patient_turn_input(
                        request, sess, payload["turnKey"])
                    payload["turnKey"] = canonical_turn_key
                    requested_item_id, requested_turn_seq = _patient_rec_turn_identity(
                        sess, payload["turnKey"])
                except HTTPException as exc:
                    if matching_failure_events:
                        raise HTTPException(
                            409, "failureId 已绑定另一个麦克风失败事实") from exc
                    raise

                for event, event_payload in matching_failure_events:
                    persisted_fact = (
                        event_payload.get("error_code"), event.item_id, event.turn_seq)
                    incoming_fact = (
                        payload["failureCode"], requested_item_id, requested_turn_seq)
                    if persisted_fact != incoming_fact:
                        raise HTTPException(409, "failureId 已绑定另一个麦克风失败事实")
                if matching_failure_events:
                    return row, command_wseq, capture_receipt, True

                # 兼容已经写入新版单槽但账本尚未完整的中断数据；
                # 正常新事实一定同事务写入上面的持久账本。
                previous = _json_load(row.patient_rec_json)
                if previous and previous.get("failureId") == payload["failureId"]:
                    persisted_fact = {
                        "active": previous.get("active"),
                        "turnKey": previous.get("turnKey"),
                        "sessionId": _payload_session_id(previous),
                        "failureCode": previous.get("failureCode"),
                        "failureId": previous.get("failureId"),
                    }
                    incoming_fact = {
                        "active": payload["active"],
                        "turnKey": payload["turnKey"],
                        "sessionId": sess.session_id,
                        "failureCode": payload["failureCode"],
                        "failureId": payload["failureId"],
                    }
                    if persisted_fact != incoming_fact:
                        raise HTTPException(409, "failureId 已绑定另一个麦克风失败事实")
                    # 同 ID 同事实纯 ACK：不增 live seq/runtime revision/event_seq。
                    return row, command_wseq, capture_receipt, True

                # 新 ID 才必须和服务端当前转位一致，禁止设备伪造/滞后环节。
                current_turn_key, item_id, turn_seq = _current_patient_rec_turn(
                    sess, row, state)
                if payload["turnKey"] != current_turn_key:
                    raise HTTPException(409, "patientRec 失败与当前训练环节不一致")
                if state.status == "paused":
                    # 暂停后的迟到新 ID 不能覆盖首个故障证据，更不能重复记账。
                    return row, command_wseq, capture_receipt, True

                try:
                    autopilot_service.fence_autonomous_scope_for_external_stop(
                        s,
                        session_id=sess.session_id,
                        reason_code=payload["failureCode"],
                        source="patient_rec_failure",
                        actor_type="device",
                        capability_token_hash=_require_device_capability_token_hash(
                            request, "上报麦克风技术失败"),
                        expected_item_id=item_id,
                        expected_turn_seq=turn_seq,
                        idempotency_token=payload["failureId"],
                    )
                except autopilot_service.AutopilotServiceError as exc:
                    _autopilot_write_failure(s, exc)

                _append_interaction(
                    s, sess, "technical_pause",
                    {
                        "error_code": payload["failureCode"],
                        "failure_id": payload["failureId"],
                    },
                    item_id=item_id, turn_seq=turn_seq,
                )
                _pause_runtime_in_transaction(sess.session_id, s)
                row.patient_rec_json = _json.dumps(payload, ensure_ascii=False)
                s.add(row)
                # pause helper 已将 live seq 与 runtime revision 各推进一次。
                return row, command_wseq, capture_receipt, False
            else:
                canonical_turn_key, patient_ref_input = _canonicalize_patient_turn_input(
                    request, sess, payload["turnKey"])
                _require_current_patient_turn_for_device(
                    request,
                    sess,
                    row,
                    state,
                    canonical_turn_key,
                    opaque_input=patient_ref_input,
                )
                payload["turnKey"] = canonical_turn_key
                _validate_audio_turn_key(sess.session_id, canonical_turn_key, s)
                if body.kind == "patientRec" and state.status == "paused":
                    # 技术失败已暂停时，迟到的 active/stopped 回报均不覆盖故障投影。
                    return row, command_wseq, capture_receipt, True
            setattr(row, slot, _json.dumps(payload, ensure_ascii=False))
        row.seq += 1
        row.updated_at = datetime.now()
        s.add(row)
        return row, command_wseq, capture_receipt, receipt_idempotent

    def existing_audio_saved_ack(locked_live: LiveState) -> dict | None:
        if body.kind != "audioSaved":
            return None
        payload = body.payload
        incoming_turn_key = payload["turnKey"]
        ack_session_id = _payload_session_id(payload) or ""
        # In a protected deployment audioSaved is a patient-device fact.  Reject
        # an account confused deputy before consulting raw ids so known/unknown
        # assets cannot be distinguished through this recovery route.
        if (auth.auth_active()
                and getattr(request.state, "auth_kind", None)
                != "device_capability"):
            raise HTTPException(403, detail={
                "code": "audio_disposition_device_capability_required",
                "message": "录音回执与终态本地清理只能由精确绑定本场次的设备能力凭据确认",
            })
        # 必须先证明 bearer 精确绑定该场次，再读/锁音频行；
        # 否则坏 bearer 或跨场 token 可以把 raw id 存在性当成侧信道。
        _require_capability_bound_session(
            request, ack_session_id or None, "恢复录音回执")
        if ack_session_id:
            _require_capability_valid_for_exact_ack(request, s, ack_session_id)
        # Lock the same asset row DELETE locks before checking terminal/file facts.
        # The outer raw-id lease prevents physical unlink between this proof and ACK.
        asset = s.exec(select(AudioAssetRow).where(
            AudioAssetRow.raw_audio_id == payload["rawAudioId"]
        ).with_for_update()).first()
        # A capability may only learn about assets in its own immutable session.
        # Foreign ids and absent ids deliberately share the exact response.
        if asset is None or asset.session_id != ack_session_id:
            raise HTTPException(404, detail={
                "code": "audio_disposition_unknown",
                "message": "服务端没有该录音的登记事实，禁止删除本地副本",
            })
        ack_session = s.get(TrainSession, ack_session_id)
        if ack_session is None:
            raise HTTPException(409, "音频关联场次不存在")
        canonical_turn_key, _patient_ref = _canonicalize_patient_turn_input(
            request,
            ack_session,
            incoming_turn_key,
            # Only rows backfilled as v1 may accept a legacy canonical outbox.
            # v2 rows must never become a guess-and-compare target-word oracle.
            legacy_exact_turn_key=(
                asset.turn_key if asset.patient_turn_ref_version == 1 else None
            ),
        )

        terminal = (
            asset.status == AudioStatus.deleted
            or asset.withdrawn
            or bool((asset.withdrawal_status or "").strip())
        )
        if terminal:
            try:
                disposition = audio_capture.verify_terminal_disposition(
                    s,
                    row=asset,
                    raw_audio_id=payload["rawAudioId"],
                    session_id=ack_session_id,
                    turn_key=canonical_turn_key,
                    duration_seconds=payload["durationSeconds"],
                    byte_count=payload["byteCount"],
                    checksum=payload["checksum"],
                    contains_direct_identifier=payload.get(
                        "containsDirectIdentifier"),
                )
            except audio_capture.AudioCaptureIntegrityError as exc:
                raise HTTPException(409, detail={
                    "code": "audio_disposition_integrity_failure",
                    "message": str(exc),
                }) from exc
            assert disposition is not None
            # 严格协议：只有这个 410 detail 可以触发客户端本地删除。
            # 不附加 message/时间/内部 id，避免前端将宽松响应误认为授权。
            raise HTTPException(410, detail={
                "code": "audio_terminal_disposition",
                "schemaVersion": 1,
                "action": "discard_local_copy",
                "reason": disposition.reason,
                "rawAudioId": disposition.raw_audio_id,
                "sessionId": disposition.session_id,
                # 客户端终态处置契约必须精确回显它 outbox 中的引用；
                # canonical 仅在服务端校验，不反向泄露给 opaque 设备。
                "turnKey": incoming_turn_key,
                "byteCount": disposition.byte_count,
                "checksum": disposition.checksum,
                "containsDirectIdentifier": disposition.contains_direct_identifier,
            })
        try:
            receipt = audio_capture.existing_receipt_ack(
                s,
                raw_audio_id=payload["rawAudioId"],
                session_id=ack_session_id,
                turn_key=canonical_turn_key,
                duration_seconds=payload["durationSeconds"],
                byte_count=payload["byteCount"],
                checksum=payload["checksum"],
                contains_direct_identifier=payload.get("containsDirectIdentifier"),
            )
        except audio_capture.AudioCaptureIntegrityError as exc:
            raise HTTPException(409, str(exc)) from exc
        if receipt is None:
            return None
        return {
            "seq": locked_live.seq,
            "audioReceipt": {
                "serverSeq": receipt.server_seq,
                "rawAudioId": receipt.raw_audio_id,
                "idempotent": True,
            },
        }

    def authorize_and_apply() -> tuple[
            tuple[LiveState, int | None, AudioCaptureReceipt | None, bool] | None,
            dict | None]:
        # All device live writes lock LiveState before the capability row.  For
        # audioSaved the enclosing order is raw-id -> byte quota -> live -> cap ->
        # asset, identical to upload/delete and safe across PostgreSQL workers.
        locked_live = _live_row_for_update(s) or LiveState(id=1, seq=0)
        ack = existing_audio_saved_ack(locked_live)
        if ack is not None:
            return None, ack
        if body.kind in access_policy.DEVICE_LIVE_WRITE_KINDS:
            device_session_id = _payload_session_id(body.payload)
            if device_session_id:
                # No immutable receipt matched; reaching apply creates a new fact.
                _require_capability_active_for_write(request, s, device_session_id)
        return apply(locked_live), None

    with ExitStack() as locks:
        if body.kind == "audioSaved":
            locks.enter_context(audio_store.blob_mutation_lock(body.payload["rawAudioId"]))
            locks.enter_context(audio_capture.byte_quota_lock())
        locks.enter_context(_LIVE_WRITE_LOCK)
        locks.enter_context(device_capability.serialized_mutation())
        applied, exact_ack = authorize_and_apply()
        if exact_ack is not None:
            return exact_ack
        assert applied is not None
        row, command_wseq, capture_receipt, receipt_idempotent = applied
        try:
            s.commit()
        except IntegrityError:
            # 多进程空库/同 raw receipt 竞态：回滚并在已落库事实上重放。
            s.rollback()
            existing = _live_row_for_update(s)
            if existing is None:
                raise
            applied, exact_ack = authorize_and_apply()
            if exact_ack is not None:
                return exact_ack
            assert applied is not None
            row, command_wseq, capture_receipt, receipt_idempotent = applied
            s.commit()
        s.refresh(row)
    result = {"seq": row.seq}
    if command_wseq is not None:
        result["wseq"] = command_wseq
    if capture_receipt is not None:
        s.refresh(capture_receipt)
        result["audioReceipt"] = {
            "serverSeq": capture_receipt.server_seq,
            "rawAudioId": capture_receipt.raw_audio_id,
            "idempotent": receipt_idempotent,
        }
    return result


def _autopilot_wake_projection(
        s: DBSession, live_session_id: str | None) -> dict | None:
    """跨设备的服务器所有权唤醒：证明床旁必须重探测一次，仅此而已。

    只在当前 live 场次的 autopilot 状态严格证明 server_owned 时返回，且只含
    sessionId 与 state_revision——没有命令载荷、没有 kind、没有 token、没有设备
    指纹、没有账号数据。disabled/manual/inactive/非法状态一律不发唤醒；状态异常
    时 fail closed 成"无唤醒"，不能让一条可选投影把无关的实时呈现打挂。
    """
    if not live_session_id:
        return None
    try:
        status = autopilot_service.get_autopilot_status(
            s, session_id=live_session_id)
    except autopilot_service.AutopilotServiceError:
        return None
    if (status.server_owned is not True
            or status.scope_key != autopilot_service.P0A_SCOPE_KEY
            or status.state_revision < 1):
        return None
    return {"sessionId": live_session_id, "stateRevision": status.state_revision}


@app.get("/live/state")
def live_get(request: Request, s: DBSession = Depends(get_session)):
    """患者端最小读快照：仅含呈现所需 session/cursor/rapportStep 与所有权唤醒。"""
    with _LIVE_WRITE_LOCK:
        row = s.get(LiveState, 1)
        _require_capability_current_live(
            request, s, "读取实时呈现", live_row=row)
        live_session_id = _live_session_id(row)
        if live_session_id:
            _require_device_route_session_read(
                request, live_session_id, s, "读取实时呈现")
        if not row:
            return {"seq": 0, "session": None, "cursor": None, "rapportStep": None,
                    "autopilotWake": None}
        return {"seq": row.seq,
                "session": _public_live_projection("session", row.session_json),
                "cursor": _public_live_projection("cursor", row.cursor_json),
                "rapportStep": _public_live_projection("rapportStep", row.rapport_json),
                "autopilotWake": _autopilot_wake_projection(s, live_session_id)}


@app.get("/live/console-state")
def live_console_get(request: Request, s: DBSession = Depends(get_session)):
    """研究者端完整实时快照；配置 CONSOLE_PIN 时由中间件保护。"""
    row = s.get(LiveState, 1)
    live_session_id = _live_session_id(row)
    if live_session_id:
        sess = s.get(TrainSession, live_session_id)
        if sess is None:
            raise HTTPException(404, "场次不存在")
        _require_session_read_operator(request, sess, s, "读取床旁实时状态")
        _require_started_visit_plan_session(live_session_id, s, sess=sess)
    if not row:
        return {"seq": 0, "session": None, "cursor": None, "rapportStep": None,
                "audioSaved": None, "patientRec": None,
                "patientPresence": _presence_payload(None)}
    return {"seq": row.seq, "session": _json_load(row.session_json),
            "cursor": _json_load(row.cursor_json),
            "rapportStep": _json_load(row.rapport_json),
            "audioSaved": _json_load(row.audio_json),
            "patientRec": _json_load(row.patient_rec_json),
            "patientPresence": _presence_payload(row)}


@app.post("/live/patient-heartbeat")
def patient_heartbeat(body: PatientHeartbeatIn, request: Request,
                      s: DBSession = Depends(get_session)):
    """老人端最小在线/当前画面回执；不改变操作端命令 seq。"""
    _require_patient_device_truth(request, "上报在场状态")
    _require_capability_bound_session(request, body.session_id, "上报在场状态")
    with _LIVE_WRITE_LOCK, device_capability.serialized_mutation():
        row = _live_row_for_update(s)
        _require_capability_current_live(
            request, s, "上报在场状态", live_row=row)
        if not row or _live_session_id(row) != body.session_id:
            raise HTTPException(409, "heartbeat 场次不是当前操作端场次")
        if not s.get(TrainSession, body.session_id):
            raise HTTPException(404, "heartbeat 场次不存在")
        # Middleware resolution is only an early rejection.  Revalidate inside
        # the live-row transaction so a queued old-token heartbeat cannot create
        # new presence facts after same-session re-pair has revoked it.
        _require_capability_active_for_write(request, s, body.session_id)

        # ack 序号只作展示线索,不参与顺序判定,照单保留:同机部署下患者端显示的游标
        # 常来自 BroadcastChannel(客户端时钟域戳),在操作端 HTTP 写落库前会"超前"于
        # row.command_wseq——按超前拒绝会让每次推进后的第一拍心跳都被 409,在场判定滞后。

        now = datetime.now()
        row.patient_ack_session_id = body.session_id
        row.patient_current_screen = body.screen
        row.patient_last_seen_at = now
        row.patient_ack_seq = body.cursor_wseq
        s.add(row)
        s.commit()
        s.refresh(row)
    return {"ok": True, "server_time": now,
            "patientPresence": _presence_payload(row, now)}


@app.get("/sessions/{session_id}/runtime")
def get_session_runtime(session_id: str, request: Request,
                        s: DBSession = Depends(get_session)):
    sess = s.get(TrainSession, session_id)
    if sess is None:
        raise HTTPException(404, "场次不存在")
    _require_session_read_operator(request, sess, s, "读取或恢复场次运行态")
    _require_started_visit_plan_session(session_id, s, sess=sess)
    return _runtime_payload(session_id, s.get(SessionRuntimeState, session_id))


@app.put("/sessions/{session_id}/runtime/cursor")
def put_session_runtime_cursor(session_id: str, body: RuntimeCursorIn,
                               request: Request,
                               s: DBSession = Depends(get_session)):
    patient_id = _preauthorize_session_subject_fence(
        request, session_id, s, "写入场次运行位置")
    with governance_lock.subject_fence(s, patient_id), _LIVE_WRITE_LOCK:
        sess = s.exec(select(TrainSession).where(
            TrainSession.session_id == session_id,
        ).with_for_update()).first()
        if not sess:
            raise HTTPException(404, "场次不存在")
        _require_session_operator(
            request, sess, s, "写入场次运行位置", mutation=True)
        _require_started_visit_plan_session(session_id, s, sess=sess)
        _live_row_for_update(s)
        _ensure_manual_plane_writable(
            session_id, s, "从旧操作端推进持久运行游标")
        payload = body.model_dump(exclude_none=True)
        expected_revision = payload.pop("expected_revision", None)
        supplied_session_id = _payload_session_id(payload)
        if supplied_session_id and supplied_session_id != session_id:
            raise HTTPException(409, "runtime cursor payload 与路径场次不一致")
        payload["sessionId"] = session_id
        payload.pop("session_id", None)
        state = _runtime_row_for_update(session_id, s)
        _ensure_runtime_writable(session_id, s, "写入运行游标")
        if expected_revision is not None and expected_revision != state.revision:
            raise HTTPException(409, "runtime revision 已变化;请先刷新场次状态再写入")
        _stamp_command_source(payload, _json_load(state.cursor_json), "runtime cursor")
        _validate_cursor(sess, payload, s)
        if state.status == "paused":
            raise HTTPException(409, "场次已暂停；须先恢复后才能推进游标")

        live = _live_row_for_update(s)
        if live and _live_session_id(live) == session_id:
            payload["wseq"] = _allocate_live_wseq(live)
            live.cursor_json = _json.dumps(payload, ensure_ascii=False)
            live.seq += 1
            live.updated_at = datetime.now()
            s.add(live)
        else:
            payload["wseq"] = _allocate_runtime_wseq(state)
        state.cursor_json = _json.dumps(_safe_cursor(payload), ensure_ascii=False)
        state.revision += 1
        state.updated_at = datetime.now()
        s.add(state)
        s.commit()
        s.refresh(state)
    return _runtime_payload(session_id, state)


def _pause_runtime_in_transaction(session_id: str, s: DBSession) -> SessionRuntimeState:
    """在调用方事务内权威暂停场次并投影老人端收麦。

    不在此函数内 commit，因此 AI 技术失败可以把 Attempt、Interaction、Runtime
    和 LiveState 作为一个原子事务落库。已是 paused 时不重复增加 revision/wseq。
    """
    state = _runtime_row(session_id, s)
    _ensure_runtime_writable(session_id, s, "暂停")
    if state.status == "paused":
        return state

    live = _live_row_for_update(s)
    live_is_current = bool(live and _live_session_id(live) == session_id)
    cursor = _json_load(state.cursor_json) or (
        _json_load(live.cursor_json) if live_is_current and live else None)
    if cursor:
        cursor = _safe_cursor(cursor)
        cursor["sessionId"] = session_id
        cursor.pop("session_id", None)
        wseq = (_allocate_live_wseq(live) if live_is_current and live
                else _allocate_runtime_wseq(state))
        cursor["wseq"] = wseq
        state.cursor_json = _json.dumps(cursor, ensure_ascii=False)
        if live_is_current and live:
            live.cursor_json = _json.dumps(
                _pause_projection(cursor, wseq), ensure_ascii=False)

    rapport = _json_load(state.rapport_json) or (
        _json_load(live.rapport_json) if live_is_current and live else None)
    if rapport:
        rapport = _safe_rapport(rapport)
        rapport["sessionId"] = session_id
        rapport.pop("session_id", None)
        wseq = (_allocate_live_wseq(live) if live_is_current and live
                else _allocate_runtime_wseq(state))
        rapport["wseq"] = wseq
        state.rapport_json = _json.dumps(rapport, ensure_ascii=False)
        if live_is_current and live:
            projected = dict(rapport)
            projected.update({"recording": "stopped", "paused": True})
            live.rapport_json = _json.dumps(projected, ensure_ascii=False)

    if live_is_current and live:
        _set_live_session_paused(live, True)

    now = datetime.now()
    state.status = "paused"
    state.paused_at = now
    state.revision += 1
    state.updated_at = now
    s.add(state)
    if live_is_current and live:
        live.seq += 1
        live.updated_at = now
        s.add(live)
    return state


@app.post("/sessions/{session_id}/pause")
def pause_session(session_id: str, request: Request,
                  s: DBSession = Depends(get_session)):
    patient_id = _preauthorize_session_subject_fence(
        request, session_id, s, "暂停场次")
    with (governance_lock.subject_fence(s, patient_id),
          _LIVE_WRITE_LOCK,
          device_capability.serialized_mutation()):
        sess = s.exec(select(TrainSession).where(
            TrainSession.session_id == session_id,
        ).with_for_update()).first()
        if not sess:
            raise HTTPException(404, "场次不存在")
        _require_session_operator(request, sess, s, "暂停场次", mutation=True)
        _require_started_visit_plan_session(session_id, s, sess=sess)
        _live_row_for_update(s)
        try:
            # Control ownership, pending command fencing, runtime pause and the
            # patient stop projection are one commit.  A partial pause is never
            # exposed after a process/database failure.  Keep the cross-worker
            # database order Session -> Live -> Autopilot -> Runtime; the
            # process-local live mutex is not a PostgreSQL deadlock fence.
            autopilot_service.pause_autonomous_scope_for_researcher(
                s, session_id=session_id, actor_id=_actor(request))
            _runtime_row_for_update(session_id, s)
            state = _pause_runtime_in_transaction(session_id, s)
            s.commit()
            s.refresh(state)
        except autopilot_service.AutopilotServiceError as exc:
            _autopilot_write_failure(s, exc)
        except IntegrityError as exc:
            _autopilot_integrity_conflict(s, exc)
    return _runtime_payload(session_id, state)


@app.post("/sessions/{session_id}/resume")
def resume_session(session_id: str, request: Request,
                   s: DBSession = Depends(get_session)):
    patient_id = _preauthorize_session_subject_fence(
        request, session_id, s, "恢复场次")
    with governance_lock.subject_fence(s, patient_id), _LIVE_WRITE_LOCK:
        sess = s.exec(select(TrainSession).where(
            TrainSession.session_id == session_id,
        ).with_for_update()).first()
        if not sess:
            raise HTTPException(404, "场次不存在")
        _require_session_operator(request, sess, s, "恢复场次", mutation=True)
        _require_started_visit_plan_session(session_id, s, sess=sess)
        _live_row_for_update(s)
        _ensure_manual_plane_writable(
            session_id, s, "在尚无安全接管流程时恢复人工控制")
        state = _runtime_row_for_update(session_id, s)
        _ensure_runtime_writable(session_id, s, "恢复")
        cursor = _json_load(state.cursor_json)
        if cursor:
            _validate_cursor(sess, cursor, s)  # 题库/版本/位置失配时 fail-closed，不盲恢复。
        rapport = _json_load(state.rapport_json)
        if rapport:
            _validate_rapport(sess, rapport, s)
        if state.status == "paused":
            live = _live_row_for_update(s)
            live_is_current = bool(live and _live_session_id(live) == session_id)
            if cursor:
                cursor = _safe_cursor(cursor)
                cursor["sessionId"] = session_id
                cursor.pop("session_id", None)
                wseq = (_allocate_live_wseq(live) if live_is_current and live
                        else _allocate_runtime_wseq(state))
                cursor["wseq"] = wseq
                state.cursor_json = _json.dumps(cursor, ensure_ascii=False)
                if live_is_current and live:
                    live.cursor_json = state.cursor_json
            if rapport:
                rapport = _safe_rapport(rapport)
                rapport["sessionId"] = session_id
                rapport.pop("session_id", None)
                wseq = (_allocate_live_wseq(live) if live_is_current and live
                        else _allocate_runtime_wseq(state))
                rapport["wseq"] = wseq
                state.rapport_json = _json.dumps(rapport, ensure_ascii=False)
                if live_is_current and live:
                    live.rapport_json = state.rapport_json
            if live_is_current and live:
                _set_live_session_paused(live, False)
                # 恢复是研究者的明确操作；清掉已处置的故障投影，
                # 否则 console 仍会把本场判为设备失败并永久禁用推进。
                live.patient_rec_json = None
            state.status = "active"
            state.resumed_at = datetime.now()
            state.revision += 1
            state.updated_at = state.resumed_at
            s.add(state)
            if live_is_current and live:
                live.seq += 1
                live.updated_at = datetime.now()
                s.add(live)
        # Also end the advisory-lock transaction for an already-active replay.
        s.commit()
        s.refresh(state)
    return _runtime_payload(session_id, state)


def _audio_capture_evidence_is_verified(
        row: AudioAssetRow, s: DBSession, *,
        require_capture_receipt: bool) -> bool:
    """Verify DB upload facts, physical bytes, and the immutable device receipt.

    Direct-session pytest fixtures predate the patient-device receipt protocol,
    so they may omit that row.  Product sessions are VisitPlan-bound and always
    require it; any present receipt is validated even for a legacy fixture.
    """
    if row.status == AudioStatus.deleted:
        return False
    try:
        audio_capture.verify_persisted_audio(row)
    except audio_capture.AudioCaptureIntegrityError:
        return False

    receipts = list(s.exec(select(AudioCaptureReceipt).where(
        AudioCaptureReceipt.raw_audio_id == row.raw_audio_id)))
    if len(receipts) > 1:
        return False
    if not receipts:
        return not require_capture_receipt
    receipt = receipts[0]
    return (
        receipt.session_id == row.session_id
        and receipt.turn_key == row.turn_key
        and receipt.byte_count == row.byte_count
        and receipt.checksum == (row.checksum or "").lower()
        and receipt.data_classification == row.data_classification
        and receipt.is_simulation == row.is_simulation
        and receipt.contains_direct_identifier == row.contains_direct_identifier
    )


def _verified_audio_ids_for_session(
        sess: TrainSession, audios: list[AudioAssetRow],
        s: DBSession) -> set[str]:
    require_capture_receipt = bool((sess.visit_plan_id or "").strip())
    return {
        row.raw_audio_id for row in audios
        if _audio_capture_evidence_is_verified(
            row, s, require_capture_receipt=require_capture_receipt)
    }


def _assess_rapport_completion(
        sess: TrainSession, s: DBSession,
) -> tuple[runtime.SessionPlan, session_completion.RapportCompletionAssessment]:
    """第 1 周关系建立的独立完成口径；床旁结束与最终完成共用同一证据判定。"""
    plan = _session_plan_for_runtime(sess)
    script = content.load_week1_script(content.CONTENT_DIR / "week1_script.json")
    state = s.exec(select(SessionRuntimeState).where(
        SessionRuntimeState.session_id == sess.session_id)).first()
    live = s.exec(select(LiveState).where(LiveState.id == 1)).first()
    live_is_current = live is not None and _live_session_id(live) == sess.session_id
    rapport_position = (_json_load(state.rapport_json) if state else None) or (
        _json_load(live.rapport_json) if live_is_current and live else None)
    # 耐久快照恒为 idle(_safe_rapport)；实时录音指令真值只在 live 槽仍属本场次时可读。
    live_rapport = _json_load(live.rapport_json) if live_is_current and live else None
    live_recording_state = (live_rapport or {}).get("recording")
    # 老人端麦克风真值回报:设备报告仍在采集时,即使指令面已 idle 也不得收口。
    live_patient_rec = (
        _json_load(live.patient_rec_json) if live_is_current and live else None)
    patient_rec_active = bool(
        isinstance(live_patient_rec, dict)
        and live_patient_rec.get("active") is True
        and _payload_session_id(live_patient_rec) in (None, sess.session_id)
    )
    audios = list(s.exec(select(AudioAssetRow).where(
        AudioAssetRow.session_id == sess.session_id)))
    scoring_item_count = len(list(s.exec(select(ItemEvent.id).where(
        ItemEvent.session_id == sess.session_id))))
    attempt_count = len(list(s.exec(select(AttemptEvent.id).where(
        AttemptEvent.session_id == sess.session_id))))
    verified_audio_ids = _verified_audio_ids_for_session(sess, audios, s)
    return plan, session_completion.assess_rapport_completion(
        script,
        rapport_position,
        live_recording_state,
        audios,
        scoring_item_count,
        session_id=sess.session_id,
        is_simulation=sess.is_simulation,
        data_classification=sess.data_classification,
        blob_exists=lambda raw_audio_id: raw_audio_id in verified_audio_ids,
        attempt_count=attempt_count,
        patient_rec_active=patient_rec_active,
        recording_truth_readable=live_is_current,
    )


def _assess_session_completion(sess: TrainSession, s: DBSession) -> tuple[
        runtime.SessionPlan,
        session_completion.CompletionAssessment
        | session_completion.RapportCompletionAssessment]:
    if sess.week_no == 1:
        return _assess_rapport_completion(sess, s)
    plan = _session_plan_for_runtime(sess)
    items = list(s.exec(select(ItemEvent).where(ItemEvent.session_id == sess.session_id)))
    item_ids = [item.id for item in items if item.id is not None]
    turns = (list(s.exec(select(TurnEvent).where(TurnEvent.item_event_id.in_(item_ids))))
             if item_ids else [])
    audios = list(s.exec(select(AudioAssetRow).where(
        AudioAssetRow.session_id == sess.session_id)))
    attempts = list(s.exec(select(AttemptEvent).where(
        AttemptEvent.session_id == sess.session_id)))
    verified_audio_ids = _verified_audio_ids_for_session(sess, audios, s)
    return plan, session_completion.assess_completion_with_audio(
        plan, items, turns, audios, attempts,
        session_id=sess.session_id,
        is_simulation=sess.is_simulation,
        data_classification=sess.data_classification,
        blob_exists=lambda raw_audio_id: raw_audio_id in verified_audio_ids,
    )


def _assess_intervention_completion(
        sess: TrainSession, s: DBSession,
) -> tuple[
        runtime.SessionPlan,
        session_completion.InterventionCompletionAssessment
        | session_completion.RapportCompletionAssessment]:
    if sess.week_no == 1:
        return _assess_rapport_completion(sess, s)
    plan = _session_plan_for_runtime(sess)
    items = list(s.exec(select(ItemEvent).where(ItemEvent.session_id == sess.session_id)))
    item_ids = [item.id for item in items if item.id is not None]
    turns = (list(s.exec(select(TurnEvent).where(TurnEvent.item_event_id.in_(item_ids))))
             if item_ids else [])
    audios = list(s.exec(select(AudioAssetRow).where(
        AudioAssetRow.session_id == sess.session_id)))
    attempts = list(s.exec(select(AttemptEvent).where(
        AttemptEvent.session_id == sess.session_id)))
    verified_audio_ids = _verified_audio_ids_for_session(sess, audios, s)
    return plan, session_completion.assess_intervention_completion(
        plan, items, turns, audios, attempts,
        session_id=sess.session_id,
        is_simulation=sess.is_simulation,
        data_classification=sess.data_classification,
        blob_exists=lambda raw_audio_id: raw_audio_id in verified_audio_ids,
    )


_OUTCOME_SUMMARY_SCHEMA_VERSION = "session-outcome-summary.v1"
_OUTCOME_SUMMARY_GENERATOR_VERSION = "server-authoritative-closeout.v1"


def _ensure_session_outcome_summary(
        sess: TrainSession,
        plan: runtime.SessionPlan,
        assessment: (session_completion.InterventionCompletionAssessment
                     | session_completion.RapportCompletionAssessment),
        s: DBSession) -> SessionOutcomeSummary:
    """Persist the one immutable, text-free operational snapshot atomically."""
    if not assessment.ready:
        raise HTTPException(409, "床旁干预证据未通过，不能生成场次自动汇总")
    turn_evidence = [
        session_closeout.SessionTurnEvidence(
            item_id=item.item_id,
            turn_seq=turn.turn_seq,
            matched=True,
            audio_evidenced=True,
        )
        for item in plan.items
        for turn in item.turns
    ]
    attempts = list(s.exec(select(AttemptEvent).where(
        AttemptEvent.session_id == sess.session_id).order_by(
            AttemptEvent.item_id, AttemptEvent.turn_seq,
            AttemptEvent.attempt_seq)))
    interactions = list(s.exec(select(InteractionEvent).where(
        InteractionEvent.session_id == sess.session_id).order_by(
            InteractionEvent.event_seq)))
    try:
        values = session_closeout.build_session_outcome_summary(
            session_id=sess.session_id,
            schema_version=_OUTCOME_SUMMARY_SCHEMA_VERSION,
            generator_version=_OUTCOME_SUMMARY_GENERATOR_VERSION,
            item_bank_version_id=sess.item_bank_version_id,
            is_simulation=sess.is_simulation,
            data_classification=sess.data_classification,
            turn_evidence=turn_evidence,
            attempts=attempts,
            interactions=interactions,
            generated_at=datetime.now(),
        )
    except ValueError as exc:
        raise HTTPException(status_code=409, detail={
            "code": "outcome_summary_source_invalid",
            "message": "自动场次汇总的服务端证据不一致，已停止结束流程",
            "reason": str(exc),
        }) from exc

    summary_counts = (
        values.expected_turns,
        values.matched_turns,
        values.completed_attempt_turns,
        values.audio_evidenced_turns,
    )
    assessment_counts = (
        assessment.expected_turns,
        assessment.matched_turns,
        assessment.completed_attempt_turns,
        assessment.audio_evidenced_turns,
    )
    if summary_counts != assessment_counts:
        raise HTTPException(status_code=409, detail={
            "code": "outcome_summary_assessment_mismatch",
            "message": "自动汇总与床旁结束门禁计数不一致，已停止结束流程",
        })

    existing = s.get(SessionOutcomeSummary, sess.session_id)
    if existing is not None:
        if existing.source_digest != values.source_digest:
            raise HTTPException(status_code=409, detail={
                "code": "outcome_summary_source_changed",
                "message": "不可变自动汇总已存在，但服务端来源摘要发生变化",
            })
        return existing
    row = SessionOutcomeSummary(**values.to_dict())
    s.add(row)
    s.flush()
    return row


def _project_terminal_to_live(live: LiveState | None, sess: TrainSession,
                              state: SessionRuntimeState,
                              plan: runtime.SessionPlan) -> bool:
    """当场老人端立即进入安全结束投影；不在当前 live 的场次不得污染别人画面。"""
    if live is None or _live_session_id(live) != sess.session_id:
        return False

    session_payload = _json_load(live.session_json) or {"sessionId": sess.session_id}
    session_payload["sessionId"] = sess.session_id
    session_payload.pop("session_id", None)
    # 终态不是“可恢复的暂停”，避免老客户端显示继续按钮。
    session_payload["paused"] = False
    session_payload["runtimeStatus"] = state.status
    session_payload["wseq"] = _allocate_live_wseq(live)
    live.session_json = _json.dumps(session_payload, ensure_ascii=False)

    cursor = _json_load(state.cursor_json) or _json_load(live.cursor_json)
    if cursor is None and plan.items:
        final = next(((idx, item) for idx, item in reversed(list(enumerate(plan.items)))
                      if item.turns), None)
        if final is not None:
            final_item_idx, final_item = final
            final_turn_idx = len(final_item.turns) - 1
            cursor = {
                "itemIdx": final_item_idx,
                "turnIdx": final_turn_idx,
                "responseRole": final_item.turns[final_turn_idx].response_role,
                "cueLevel": 0,
            }
    if cursor is not None:
        projected = _safe_cursor(cursor)
        projected.update({
            "sessionId": sess.session_id,
            "screen": "thanks",
            "recording": "stopped",
            "selfStart": False,
            "wseq": _allocate_live_wseq(live),
        })
        projected.pop("session_id", None)
        projected.pop("rawAudioId", None)
        live.cursor_json = _json.dumps(projected, ensure_ascii=False)
        persisted = dict(projected)
        persisted["recording"] = "idle"
        state.cursor_json = _json.dumps(persisted, ensure_ascii=False)

    rapport = _json_load(state.rapport_json) or _json_load(live.rapport_json)
    if rapport is not None:
        projected_rapport = _safe_rapport(rapport)
        projected_rapport.update({
            "sessionId": sess.session_id,
            "recording": "stopped",
            "paused": True,
            "wseq": _allocate_live_wseq(live),
        })
        live.rapport_json = _json.dumps(projected_rapport, ensure_ascii=False)

    # patientRec 是老人端麦克风真值回报；终态不得让控制台继续显示“录音中”。
    live.patient_rec_json = None
    live.seq += 1
    live.updated_at = datetime.now()
    return True


_AUTOPILOT_AUTOFINISH_ERROR = "intervention_completion_evidence_incomplete"


def _autofinalize_completed_autopilot_scope(
        s: DBSession,
        *,
        session_id: str,
        live: LiveState | None,
        ack_result: autopilot_service.ApplyDeviceAckResult,
) -> tuple[autopilot_service.ApplyDeviceAckResult,
           session_completion.InterventionCompletionAssessment | None]:
    """Turn a proven final device ACK into the bedside terminal fact.

    This runs before the ACK transaction commits.  If the independently derived
    completion assessment is not ready, the already-real device ACK and
    ``scope_complete`` event are retained while autonomous ownership moves to a
    diagnosable failed state.  We never manufacture ``intervention_completed``.
    """
    if ack_result.status != "scope_completed":
        return ack_result, None

    sess = s.get(TrainSession, session_id)
    runtime_state = s.exec(select(SessionRuntimeState).where(
        SessionRuntimeState.session_id == session_id,
    ).with_for_update()).first()
    if sess is None or runtime_state is None:
        raise autopilot_service.AutopilotServiceError(
            "autopilot_runtime_inactive",
            "自动范围已结束，但场次运行状态不存在",
        )

    # Exact ACK replay after a successful auto-finish is an idempotent read.
    if runtime_state.status in {"intervention_completed", "completed"}:
        if s.get(SessionOutcomeSummary, session_id) is None:
            raise autopilot_service.AutopilotServiceError(
                "autopilot_outcome_summary_missing",
                "场次已结束但缺少不可变自动汇总",
            )
        return ack_result, None

    plan, assessment = _assess_intervention_completion(sess, s)
    if not assessment.ready:
        control = autopilot_service.fail_completed_scope_autofinish(
            s,
            session_id=session_id,
            error_code=_AUTOPILOT_AUTOFINISH_ERROR,
            expected_turns=assessment.expected_turns,
            matched_turns=assessment.matched_turns,
            completed_attempt_turns=assessment.completed_attempt_turns,
            audio_evidenced_turns=assessment.audio_evidenced_turns,
            issue_codes=tuple(issue.code for issue in assessment.issues),
        )
        return ack_result.model_copy(update={
            "status": "failed",
            "state_revision": control.revision,
            "command": None,
        }), assessment

    if runtime_state.status != "active":
        raise autopilot_service.AutopilotServiceError(
            "autopilot_runtime_inactive",
            f"自动范围已结束，但场次运行状态为 {runtime_state.status}",
        )

    # Summary, runtime terminal fact, and patient-device projection become
    # visible in the same commit as the final immutable TTS ACK.
    _ensure_session_outcome_summary(sess, plan, assessment, s)
    now = datetime.now()
    runtime_state.status = "intervention_completed"
    runtime_state.intervention_completed_at = now
    runtime_state.intervention_ended_by = "SERVER-AUTOPILOT"
    runtime_state.revision += 1
    runtime_state.updated_at = now
    live_changed = _project_terminal_to_live(live, sess, runtime_state, plan)
    s.add(runtime_state)
    if live_changed and live is not None:
        s.add(live)
    s.flush()
    return ack_result, assessment


@app.post("/sessions/{session_id}/finish-intervention")
def finish_intervention(session_id: str, request: Request,
                        s: DBSession = Depends(get_session)):
    """结束老人端自动干预并转入异步研究复核，不等待床旁人工确认或锁分。"""
    patient_id = _preauthorize_session_subject_fence(
        request, session_id, s, "结束床旁干预")
    with governance_lock.subject_fence(s, patient_id), _LIVE_WRITE_LOCK:
        sess = s.exec(select(TrainSession).where(
            TrainSession.session_id == session_id,
        ).with_for_update()).first()
        if not sess:
            raise HTTPException(404, "场次不存在")
        _require_session_operator(
            request, sess, s, "结束床旁干预", mutation=True)
        _require_started_visit_plan_session(session_id, s, sess=sess)
        existing = s.exec(select(SessionRuntimeState).where(
            SessionRuntimeState.session_id == session_id,
        ).with_for_update()).first()
        if existing is not None and existing.status in {"intervention_completed", "completed"}:
            result = _runtime_payload(session_id, existing)
            result["outcomeSummaryAvailable"] = (
                s.get(SessionOutcomeSummary, session_id) is not None)
            s.rollback()
            return result
        if existing is not None and existing.status in _TERMINAL_RUNTIME_STATUSES:
            raise HTTPException(
                409, f"场次已进入终态 {existing.status}，不得结束床旁干预")
        if existing is not None and existing.status not in _MUTABLE_RUNTIME_STATUSES:
            raise HTTPException(409, f"场次运行状态 {existing.status} 非法，禁止结束床旁干预")

        plan, assessment = _assess_intervention_completion(sess, s)
        if not assessment.ready:
            raise HTTPException(status_code=409, detail={
                "message": "床旁干预结束门禁未通过；保持当前位置，不切换受试者",
                "assessment": assessment.to_dict(),
            })

        # The operational snapshot and runtime transition share one transaction:
        # neither can become visible without the other.
        _ensure_session_outcome_summary(sess, plan, assessment, s)

        state = existing or SessionRuntimeState(
            session_id=session_id, status="active", revision=0)
        now = datetime.now()
        state.status = "intervention_completed"
        state.intervention_completed_at = now
        state.intervention_ended_by = _actor(request) or "PIN/本地"
        state.revision += 1
        state.updated_at = now
        live = _live_row_for_update(s)
        live_changed = _project_terminal_to_live(live, sess, state, plan)
        s.add(state)
        if live_changed and live is not None:
            s.add(live)
        s.commit()
        s.refresh(state)

    _audit(
        s, request, "session_intervention_complete",
        f"结束床旁干预 计划环节={assessment.expected_turns} "
        f"AI完成={assessment.completed_attempt_turns} 录音证据={assessment.audio_evidenced_turns}",
        patient_id=sess.patient_id, session_id=session_id,
    )
    result = _runtime_payload(session_id, state)
    result["interventionAssessment"] = assessment.to_dict()
    return result


def _session_outcome_payload(row: SessionOutcomeSummary) -> dict:
    return row.model_dump()


def _session_closeout_payload(
        row: SessionCloseoutReport, *, idempotent: bool = False) -> dict:
    return {
        "session_id": row.session_id,
        "schema_version": row.schema_version,
        "report_status": row.status,
        "fatigue_observed": row.fatigue_observed,
        "distress_or_discomfort_observed": row.distress_or_discomfort_observed,
        "participant_declined_to_continue": row.participant_declined_to_continue,
        "staff_assistance_occurred": row.staff_assistance_occurred,
        "environment_interruption_occurred": row.environment_interruption_occurred,
        "device_or_network_interruption_occurred": (
            row.device_or_network_interruption_occurred),
        "note": row.note,
        "revision": row.revision,
        "locked": row.locked_at is not None,
        "recorded_by": row.created_by,
        "recorded_at": row.created_at,
        "updated_by": row.updated_by,
        "updated_at": row.updated_at,
        "locked_by": row.locked_by,
        "locked_at": row.locked_at,
        "idempotent": idempotent,
    }


@app.get("/sessions/{session_id}/outcome-summary")
def get_session_outcome_summary(
        session_id: str, request: Request, response: Response,
        s: DBSession = Depends(get_session)):
    _require_account_identity(
        request, "读取场次自动汇总",
        roles={"researcher", "data_steward", "admin"}, allow_local_m0=True)
    sess = s.get(TrainSession, session_id)
    if not sess:
        raise HTTPException(404, "场次不存在")
    _require_session_read_operator_if_admitted(
        request, sess, s, "读取场次自动汇总")
    response.headers["Cache-Control"] = "private, no-store"
    response.headers["Pragma"] = "no-cache"
    restriction_reason = _session_read_restriction_reason(sess, s)
    if restriction_reason is not None:
        _raise_withdrawn_session_read_conflict(
            sess, resource="outcome_summary", reason_code=restriction_reason)
    row = s.get(SessionOutcomeSummary, session_id)
    if row is None:
        raise HTTPException(status_code=404, detail={
            "code": "outcome_summary_not_generated",
            "message": "该场次尚未通过床旁结束门禁，没有自动汇总",
        })
    return _session_outcome_payload(row)


class SessionCloseoutIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    idempotency_key: str = PydanticField(
        min_length=8, max_length=200,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{7,199}$")
    expected_revision: int = PydanticField(ge=0)
    report_status: Literal[
        "no_additional_observation", "observation_recorded"]
    fatigue_observed: bool = False
    distress_or_discomfort_observed: bool = False
    participant_declined_to_continue: bool = False
    staff_assistance_occurred: bool = False
    environment_interruption_occurred: bool = False
    device_or_network_interruption_occurred: bool = False
    note: str | None = PydanticField(default=None, max_length=2000)

    def normalized(self) -> session_closeout.NormalizedCloseoutPayload:
        try:
            return session_closeout.normalize_closeout_payload({
                "status": self.report_status,
                "fatigue_observed": self.fatigue_observed,
                "distress_or_discomfort_observed": (
                    self.distress_or_discomfort_observed),
                "participant_declined_to_continue": (
                    self.participant_declined_to_continue),
                "staff_assistance_occurred": self.staff_assistance_occurred,
                "environment_interruption_occurred": (
                    self.environment_interruption_occurred),
                "device_or_network_interruption_occurred": (
                    self.device_or_network_interruption_occurred),
                "note": self.note,
            })
        except session_closeout.CloseoutValidationError as exc:
            raise HTTPException(422, str(exc)) from exc


@app.get("/sessions/{session_id}/closeout")
def get_session_closeout(
        session_id: str, request: Request, response: Response,
        s: DBSession = Depends(get_session)):
    _require_account_identity(
        request, "读取现场收尾记录",
        roles={"researcher", "data_steward", "admin"}, allow_local_m0=True)
    sess = s.get(TrainSession, session_id)
    if not sess:
        raise HTTPException(404, "场次不存在")
    _require_session_read_operator_if_admitted(
        request, sess, s, "读取场次现场收尾")
    response.headers["Cache-Control"] = "private, no-store"
    response.headers["Pragma"] = "no-cache"
    restriction_reason = _session_read_restriction_reason(sess, s)
    if restriction_reason is not None:
        _raise_withdrawn_session_read_conflict(
            sess, resource="session_closeout", reason_code=restriction_reason)
    row = s.get(SessionCloseoutReport, session_id)
    if row is None:
        raise HTTPException(status_code=404, detail={
            "code": "session_closeout_not_recorded",
            "message": "该场次尚未保存现场收尾记录",
        })
    return _session_closeout_payload(row)


@app.put("/sessions/{session_id}/closeout")
def save_session_closeout(
        session_id: str, body: SessionCloseoutIn, request: Request,
        s: DBSession = Depends(get_session)):
    actor = _require_account_identity(
        request, "保存现场收尾记录", roles={"researcher", "admin"},
        allow_local_m0=True)
    normalized = body.normalized()
    request_hash = session_closeout.closeout_operation_hash(
        normalized, expected_revision=body.expected_revision)
    idempotent = False
    patient_id = _preauthorize_session_subject_fence(
        request, session_id, s, "保存现场收尾记录")
    with governance_lock.subject_fence(s, patient_id), _LIVE_WRITE_LOCK:
        sess = s.exec(select(TrainSession).where(
            TrainSession.session_id == session_id,
        ).with_for_update()).first()
        if not sess:
            raise HTTPException(404, "场次不存在")
        _require_session_operator(
            request, sess, s, "保存现场收尾记录", mutation=True)
        _require_started_visit_plan_session(session_id, s, sess=sess)
        state = s.exec(select(SessionRuntimeState).where(
            SessionRuntimeState.session_id == session_id,
        ).with_for_update()).first()
        if state is None or state.status != "intervention_completed":
            raise HTTPException(status_code=409, detail={
                "code": "session_closeout_wrong_runtime_state",
                "message": "只有床旁干预结束、研究最终完成之前可保存现场收尾",
                "runtime_status": state.status if state else "active",
            })
        if s.get(SessionOutcomeSummary, session_id) is None:
            raise HTTPException(status_code=409, detail={
                "code": "outcome_summary_required",
                "message": "缺少与床旁结束同事务生成的自动汇总，禁止保存收尾",
            })

        row = s.exec(select(SessionCloseoutReport).where(
            SessionCloseoutReport.session_id == session_id,
        ).with_for_update()).first()
        if row is not None and row.locked_at is not None:
            raise HTTPException(409, "现场收尾记录已随研究完成锁定，不可修改")
        if row is not None and row.last_idempotency_key == body.idempotency_key:
            if row.last_request_hash != request_hash:
                raise HTTPException(status_code=409, detail={
                    "code": "closeout_idempotency_conflict",
                    "message": "同一幂等键对应了不同现场收尾请求",
                })
            idempotent = True
        else:
            current_revision = row.revision if row is not None else 0
            if body.expected_revision != current_revision:
                raise HTTPException(status_code=409, detail={
                    "code": "closeout_revision_conflict",
                    "message": "现场收尾已被其他操作更新，请刷新后重试",
                    "expected_revision": body.expected_revision,
                    "current_revision": current_revision,
                })
            now = datetime.now()
            values = normalized.to_dict()
            if row is None:
                row = SessionCloseoutReport(
                    session_id=session_id,
                    schema_version=session_closeout.CLOSEOUT_SCHEMA_VERSION,
                    status=normalized.status,
                    revision=1,
                    last_idempotency_key=body.idempotency_key,
                    last_request_hash=request_hash,
                    created_by=actor,
                    created_at=now,
                    updated_by=actor,
                    updated_at=now,
                    **{key: value for key, value in values.items()
                       if key != "status"},
                )
            else:
                row.status = normalized.status
                for key, value in values.items():
                    if key != "status":
                        setattr(row, key, value)
                row.revision += 1
                row.last_idempotency_key = body.idempotency_key
                row.last_request_hash = request_hash
                row.updated_by = actor
                row.updated_at = now
            s.add(row)
        # End the advisory-lock transaction on both a new write and an exact
        # idempotent replay; do not leave it held until response cleanup.
        s.commit()
        s.refresh(row)

    if not idempotent:
        flag_count = sum(bool(getattr(row, field))
                         for field in session_closeout.CLOSEOUT_FLAG_FIELDS)
        _audit(
            s, request, "session_closeout_record",
            f"现场收尾 revision={row.revision} status={row.status} flags={flag_count}",
            patient_id=sess.patient_id, session_id=session_id,
        )
    return _session_closeout_payload(row, idempotent=idempotent)


@app.post("/sessions/{session_id}/complete")
def complete_session(session_id: str, request: Request, s: DBSession = Depends(get_session)):
    """前端只能请求完成；冻结计划中每个环节的唯一锁定研究真值均齐备后才能进入 completed。"""
    patient_id = _preauthorize_session_subject_fence(
        request, session_id, s, "完成场次")
    with governance_lock.subject_fence(s, patient_id), _LIVE_WRITE_LOCK:
        sess = s.exec(select(TrainSession).where(
            TrainSession.session_id == session_id,
        ).with_for_update()).first()
        if not sess:
            raise HTTPException(404, "场次不存在")
        _require_session_operator(
            request, sess, s, "完成场次", mutation=True)
        _require_started_visit_plan_session(session_id, s, sess=sess)
        existing = s.exec(select(SessionRuntimeState).where(
            SessionRuntimeState.session_id == session_id,
        ).with_for_update()).first()
        if existing is not None and existing.status == "completed":
            result = _runtime_payload(session_id, existing)
            s.rollback()
            return result
        if existing is not None and existing.status in _TERMINAL_RUNTIME_STATUSES:
            raise HTTPException(409, f"场次已进入终态 {existing.status}，不得改为 completed")
        if existing is None or existing.status != "intervention_completed":
            raise HTTPException(status_code=409, detail={
                "code": "intervention_not_completed",
                "message": "必须先明确结束床旁干预，再完成研究复核；不能从进行中或暂停状态直接完成",
                "runtime_status": existing.status if existing else "active",
            })

        plan, assessment = _assess_session_completion(sess, s)
        if not assessment.ready:
            raise HTTPException(status_code=409, detail={
                "message": "场次完成门禁未通过；保持原位置，不宣布完成",
                "assessment": assessment.to_dict(),
            })

        closeout = s.exec(select(SessionCloseoutReport).where(
            SessionCloseoutReport.session_id == session_id,
        ).with_for_update()).first()
        if closeout is None:
            raise HTTPException(status_code=409, detail={
                "code": "session_closeout_required",
                "message": "必须先保存独立现场收尾记录，才能最终完成研究复核",
            })
        if closeout.locked_at is not None or closeout.locked_by is not None:
            raise HTTPException(status_code=409, detail={
                "code": "session_closeout_lock_state_invalid",
                "message": "场次尚未最终完成，但现场收尾已提前锁定",
            })

        state = existing
        now = datetime.now()
        state.status = "completed"
        state.completed_at = now
        state.aborted_at = None
        state.ended_by = _actor(request) or "PIN/本地"
        state.end_reason = "completion_gate_passed"
        state.revision += 1
        state.updated_at = now
        closeout.locked_by = _actor(request) or "PIN/本地"
        closeout.locked_at = now
        closeout.updated_by = closeout.locked_by
        closeout.updated_at = now
        closeout.revision += 1
        live = _live_row_for_update(s)
        live_changed = _project_terminal_to_live(live, sess, state, plan)
        s.add(state)
        s.add(closeout)
        if live_changed and live is not None:
            s.add(live)
        s.commit()
        s.refresh(state)

    _audit(s, request, "session_complete",
           f"完成场次 计划环节={assessment.expected_turns} 锁定真值={assessment.locked_turns} "
           f"现场收尾版本={closeout.revision}",
           patient_id=sess.patient_id, session_id=session_id)
    result = _runtime_payload(session_id, state)
    result["completionAssessment"] = assessment.to_dict()
    return result


AbortReasonCode = Literal[
    "participant_declined",
    "clinical_safety",
    "technical_failure",
    "researcher_decision",
]

_ABORT_REASON_LABELS: dict[str, str] = {
    "participant_declined": "受试者不愿继续",
    "clinical_safety": "临床安全原因",
    "technical_failure": "设备或系统故障",
    "researcher_decision": "研究者按方案决定中止",
}


class AbortSessionIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason_code: AbortReasonCode
    expected_revision: int = PydanticField(ge=0)
    idempotency_key: str = PydanticField(
        min_length=16, max_length=200,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{15,199}$")


_ABORT_OPERATION_PREFIX = "abort:v1:"


def _abort_operation_facts(value: str | None) -> tuple[str, int, str] | None:
    if not value or not value.startswith(_ABORT_OPERATION_PREFIX):
        return None
    parts = value.split(":")
    if len(parts) != 5 or parts[0] != "abort" or parts[1] != "v1":
        return None
    reason_code, revision_text, key_hash = parts[2:]
    if reason_code not in _ABORT_REASON_LABELS or not revision_text.isdigit():
        return None
    if len(key_hash) != 64 or any(char not in "0123456789abcdef" for char in key_hash):
        return None
    return reason_code, int(revision_text), key_hash


def _abort_operation_value(
        reason_code: str, expected_revision: int, idempotency_key: str) -> str:
    key_hash = hashlib.sha256(idempotency_key.encode("utf-8")).hexdigest()
    return f"{_ABORT_OPERATION_PREFIX}{reason_code}:{expected_revision}:{key_hash}"


def _abort_public_reason(value: str | None) -> str | None:
    facts = _abort_operation_facts(value)
    return facts[0] if facts is not None else value


def _abort_runtime_snapshot_decision(
        state: SessionRuntimeState | None,
        *,
        operation_facts: tuple[str, int, str],
        expected_revision: int,
) -> Literal["apply", "replay"]:
    """Validate one unlocked or locked abort snapshot without mutating it."""
    if state is not None and state.status == "aborted":
        stored = _abort_operation_facts(state.end_reason)
        if stored is not None and stored[2] == operation_facts[2]:
            if stored != operation_facts:
                raise HTTPException(status_code=409, detail={
                    "code": "session_abort_idempotency_conflict",
                    "message": "同一中止幂等键已绑定不同请求事实",
                })
            return "replay"
        if stored != operation_facts:
            raise HTTPException(status_code=409, detail={
                "code": "session_already_aborted",
                "message": "场次已由另一个受控中止请求关闭，不可改写",
            })
    if state is not None and state.status in _TERMINAL_RUNTIME_STATUSES:
        raise HTTPException(
            409, f"场次已进入终态 {state.status}，不得改为 aborted")
    if state is not None and state.status not in _MUTABLE_RUNTIME_STATUSES:
        raise HTTPException(
            409, f"场次运行状态 {state.status} 非法，禁止中止")
    revision = state.revision if state is not None else 0
    if revision != expected_revision:
        raise HTTPException(status_code=409, detail={
            "code": "session_abort_revision_conflict",
            "message": "场次运行修订已变化，请重新核对后中止",
            "current_revision": revision,
        })
    return "apply"


@app.post("/sessions/{session_id}/abort")
def abort_session(session_id: str, body: AbortSessionIn, request: Request,
                  s: DBSession = Depends(get_session)):
    """显式中止不伪装成“完成”；只接收闭表代码。"""
    actor_id = _require_account_identity(
        request,
        "中止场次",
        roles={"researcher", "admin"},
        allow_local_m0=True,
    )
    reason_code = body.reason_code
    operation_value = _abort_operation_value(
        reason_code, body.expected_revision, body.idempotency_key)
    operation_facts = _abort_operation_facts(operation_value)
    assert operation_facts is not None
    patient_id = _preauthorize_session_subject_fence(
        request, session_id, s, "中止场次")
    with governance_lock.subject_fence(s, patient_id), _LIVE_WRITE_LOCK:
        # Session is the always-present authority row. Lock it first so two
        # workers cannot both observe a missing/runtime revision and insert or
        # overwrite divergent abort facts. Runtime admission below is an
        # unlocked preflight; the authoritative row is locked and revalidated
        # only after Live and the autonomous command plane are fenced.
        sess = s.exec(select(TrainSession).where(
            TrainSession.session_id == session_id,
        ).with_for_update()).first()
        if not sess:
            raise HTTPException(404, "场次不存在")
        _require_session_operator(request, sess, s, "中止场次", mutation=True)
        _require_started_visit_plan_session(session_id, s, sess=sess)
        preflight = s.get(SessionRuntimeState, session_id)
        decision = _abort_runtime_snapshot_decision(
            preflight,
            operation_facts=operation_facts,
            expected_revision=body.expected_revision,
        )
        if decision == "replay":
            result = _runtime_payload(session_id, preflight)
            s.rollback()
            return result

        # Canonical cross-worker order: Session -> Live -> Autopilot/Command ->
        # Runtime.  Every fact checked above is repeated against the locked
        # runtime row before any staged fence is committed.
        live = _live_row_for_update(s)
        now = datetime.now()
        try:
            autopilot_service.fence_autonomous_scope_for_external_stop(
                s,
                session_id=session_id,
                reason_code="session_aborted",
                source="session_abort",
                # AutopilotControlEvent.actor_type is the stable control-plane
                # principal class (named human account), not the account's RBAC
                # role.  Admin supervision is recorded separately by
                # _require_session_operator above; writing the unsupported
                # value "admin" here made every admin abort fail before the
                # service even checked whether autonomous control was active.
                actor_type="researcher",
                actor_id=actor_id,
                now=now,
            )
            if preflight is not None:
                s.expire(preflight)
            state = _runtime_row_for_update(session_id, s)
            locked_decision = _abort_runtime_snapshot_decision(
                state,
                operation_facts=operation_facts,
                expected_revision=body.expected_revision,
            )
            if locked_decision == "replay":
                result = _runtime_payload(session_id, state)
                s.rollback()
                return result
        except autopilot_service.AutopilotServiceError as exc:
            _autopilot_write_failure(s, exc)
        except HTTPException:
            # A runtime race detected after the autonomous fence must leave
            # neither half visible.
            s.rollback()
            raise
        state.status = "aborted"
        state.aborted_at = now
        state.completed_at = None
        state.ended_by = actor_id
        # No schema migration: the existing terminal reason column stores the
        # closed reason plus expected revision and a one-way idempotency-key
        # digest. Public runtime projections expose only the reason code.
        state.end_reason = operation_value
        state.revision += 1
        state.updated_at = now
        plan = _session_plan_for_runtime(sess)
        live_changed = _project_terminal_to_live(live, sess, state, plan)
        s.add(state)
        if live_changed and live is not None:
            s.add(live)
        s.commit()
        s.refresh(state)

    _audit(s, request, "session_abort",
           f"中止场次 reason_code={reason_code} label={_ABORT_REASON_LABELS[reason_code]}",
           patient_id=sess.patient_id, session_id=session_id)
    return _runtime_payload(session_id, state)


# ---------------- M3 ASR(可插拔;默认 auto:有 Key 走云端 qwen3-asr,无则降级人工)----------------
@app.get("/asr/hotwords")
def asr_hotwords():
    script = content.load_week1_script(content.CONTENT_DIR / "week1_script.json")
    # 热词并全部已结构化训练周:同一识别引擎服务所有周,词表取并集去重保序。
    hw: list[str] = []
    seen: set[str] = set()
    for index, week in enumerate(sorted(content.load_item_bank_index())):
        bank = content.load_item_bank_for_week(week)
        for word in asr.build_hotwords(bank, script if index == 0 else None):
            if word not in seen:
                seen.add(word)
                hw.append(word)
    return {"engine": asr.get_engine().version, "count": len(hw), "hotwords": hw}


# ---------------- AI provider synthetic readiness (no participant data) ----------------
@app.get("/quality/ai-metrics")
def get_ai_quality_metrics(
        data_classification: Literal["research", "simulation"],
        request: Request, response: Response,
        s: DBSession = Depends(get_session)):
    """Return one deidentified, account-scoped overall quality partition."""
    actor_id = _require_account_identity(
        request, "查看 AI 质量汇总",
        roles={"researcher", "data_steward", "admin"})
    # Reject duplicate and future/ad-hoc dimensions rather than silently
    # accepting a query the privacy contract does not implement.
    if (len(request.query_params.multi_items()) != 1
            or request.query_params.multi_items()[0][0] != "data_classification"):
        raise HTTPException(status_code=422, detail={
            "code": "quality_query_invalid",
            "message": "AI 质量汇总只允许一个 data_classification 查询参数",
        }, headers={"Cache-Control": "private, no-store", "Pragma": "no-cache"})
    if data_classification == "simulation":
        limited = _expensive_rate_limit(
            request,
            f"account:{actor_id}:{_client_ip(request)}",
            resource_path="/quality/ai-metrics/simulation",
        )
        if limited is not None:
            return limited
    response.headers["Cache-Control"] = "private, no-store"
    response.headers["Pragma"] = "no-cache"
    try:
        return ai_quality_service.build_ai_quality_dashboard(
            s,
            actor_id=actor_id,
            actor_role=getattr(request.state, "actor_role", ""),
            data_classification=data_classification,
        )
    except ai_quality_service.QualityScopeTooLarge as exc:
        raise HTTPException(status_code=413, detail={
            "code": "quality_scope_too_large",
            "message": "当前权限范围超过单次质量汇总上限，未返回部分结果",
        }, headers={
            "Cache-Control": "private, no-store", "Pragma": "no-cache",
        }) from exc
    except ai_quality_service.QualityEvidenceLimitExceeded as exc:
        raise HTTPException(status_code=409, detail={
            "code": "quality_evidence_limit_exceeded",
            "message": "质量证据量超过单次安全处理上限，未返回部分结果",
            "resource": exc.resource,
        }, headers={
            "Cache-Control": "private, no-store", "Pragma": "no-cache",
        }) from exc
    except ai_quality_service.QualitySnapshotUnavailable as exc:
        raise HTTPException(status_code=503, detail={
            "code": "quality_snapshot_unavailable",
            "message": "当前无法建立一致的只读质量快照，未返回部分结果",
        }, headers={
            "Cache-Control": "private, no-store", "Pragma": "no-cache",
        }) from exc
    except content.FrozenContentUnavailable:
        # Preserve the app-wide fixed, path-free 503 response for missing or
        # damaged packaged definitions; this subtype also inherits ValueError.
        raise
    except ValueError as exc:
        raise HTTPException(status_code=409, detail={
            "code": "quality_projection_unavailable",
            "message": "质量证据无法按当前聚合合同安全投影",
        }, headers={
            "Cache-Control": "private, no-store", "Pragma": "no-cache",
        }) from exc


@app.get(
    "/ai/provider-readiness",
    response_model=provider_readiness.ReadinessProjection,
)
def get_provider_readiness(
        request: Request, response: Response,
        s: DBSession = Depends(get_session)):
    _require_account_identity(
        request, "查看 AI 服务就绪状态",
        roles={"researcher", "data_steward", "admin"})
    response.headers["Cache-Control"] = "private, no-store"
    return provider_readiness.readiness_projection(s)


@app.post(
    "/ai/provider-readiness/probe",
    response_model=provider_readiness.ReadinessProjection,
)
def probe_provider_readiness(
        request: Request, response: Response,
        s: DBSession = Depends(get_session)):
    """Admin-only synthetic probe; never reads participant or study evidence."""
    actor_id = _require_account_identity(
        request, "执行 AI 服务合成检查", roles={"admin"})
    response.headers["Cache-Control"] = "private, no-store"

    # Capture configuration, close any request transaction, then perform all
    # provider I/O without retaining a DB connection snapshot or clinical row.
    before = provider_readiness.capture_configuration()
    s.rollback()
    result = provider_readiness.run_synthetic_probe(before)
    after = provider_readiness.capture_configuration()
    changed = before.fingerprint != after.fingerprint

    try:
        row = provider_readiness.persist_probe(
            s, result=result, actor_display_id=actor_id,
            config_changed_during_probe=changed)
        audit_summary = (
            "synthetic-only "
            f"required_ready={row.required_capabilities_ready} "
            f"all_configured_ready={row.all_configured_capabilities_ready} "
            f"contract={row.runtime_contract}"
        )
        # audit.record deliberately uses an independent DB session.  Commit the
        # immutable probe receipt before opening that writer; otherwise SQLite
        # always holds this transaction's write lock and silently drops the
        # companion audit row as "database is locked".
        s.commit()
    except IntegrityError:
        s.rollback()
        raise HTTPException(status_code=409, detail={
            "code": "provider_readiness_probe_conflict",
            "message": "AI 服务检查账本并发写入冲突，本次结果未生效",
        })
    _audit(s, request, "provider_readiness_probe", audit_summary)
    return provider_readiness.readiness_projection(s, configuration=after)


@app.post("/asr/transcribe/{raw_audio_id}")
def asr_transcribe(raw_audio_id: str):
    """永久封存的旧入口；环境变量也不得重新打开无证据/无云授权的旁路。"""
    del raw_audio_id
    raise HTTPException(409, "旧 ASR 单点接口已永久关闭；请使用场次 attempt/process 权威证据链")


# ---------------- 神经 TTS(小语的声音;云端白名单闭集优先,本地 piper 降级)----------------
class TtsSpeakIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: str = PydanticField(min_length=1, max_length=500)


class AutopilotTtsIn(BaseModel):
    """Closed request contract: command text is never accepted from a device."""

    model_config = ConfigDict(extra="forbid")


TtsAuthorizationSnapshot = tuple[
    str | None, int | None, bool, str | None, int | None,
]


def _tts_authorization_snapshot(
        live: LiveState | None, s: DBSession) -> TtsAuthorizationSnapshot:
    """Capture only the revisions needed to fence a delayed TTS result."""
    session_id = _live_session_id(live)
    runtime_state = s.get(SessionRuntimeState, session_id) if session_id else None
    return (
        session_id,
        live.seq if live is not None else None,
        runtime_state is not None,
        runtime_state.status if runtime_state is not None else None,
        runtime_state.revision if runtime_state is not None else None,
    )


def _raise_tts_authorization_changed() -> NoReturn:
    """Never return provider bytes after their clinical authorization went stale."""
    raise HTTPException(
        status_code=409,
        detail={
            "code": "tts_authorization_changed",
            "message": "语音合成期间场次或授权已变化，本次音频已丢弃",
            "action": "discard_synthesized_audio",
        },
        headers={
            "Cache-Control": "private, no-store",
            "Pragma": "no-cache",
        },
    )


def _require_tts_session_authorization(
        request: Request,
        s: DBSession,
        *,
        session_id: str,
        live: LiveState | None,
        manual_text: bool,
) -> None:
    """Validate owner/device, runtime, withdrawal, and control-plane authority."""
    live_session = s.get(TrainSession, session_id)
    if live_session is None:
        raise HTTPException(404, "场次不存在")
    if getattr(request.state, "auth_kind", None) == "account":
        _require_session_operator(
            request, live_session, s, "合成实时话术")
    _require_capability_bound_session(request, session_id, "合成实时话术")
    _require_capability_current_live(
        request, s, "合成实时话术", live_row=live)
    _require_capability_active_for_write(request, s, session_id)
    _ensure_runtime_writable(session_id, s, "合成床旁话术")
    restriction_reason = _session_read_restriction_reason(live_session, s)
    if restriction_reason is not None:
        _raise_withdrawn_session_read_conflict(
            live_session,
            resource="tts",
            reason_code=restriction_reason,
        )
    if manual_text:
        _ensure_manual_plane_writable(
            session_id, s, "使用客户端提交文本的通用 TTS 接口")


def _revalidate_tts_after_provider(
        request: Request,
        s: DBSession,
        *,
        expected_snapshot: TtsAuthorizationSnapshot,
        manual_text: bool,
        command_key: str | None = None,
        capability_token_hash: str | None = None,
        expected_text: str | None = None,
        serve_facts: _TtsServeFacts | None = None,
) -> None:
    """Reauthorize a synthesized result under the same live/capability locks.

    Provider/cache I/O has already completed, but its bytes remain process-local
    until this function proves that the exact live/runtime revision and, for an
    autonomous command, the exact command/token/server-derived text still match.
    Only then is the serve fact appended: evidence row exists ⇔ the response is
    actually returned; discarded audio leaves no usage claim.
    """
    with _LIVE_WRITE_LOCK, device_capability.serialized_mutation():
        s.rollback()
        s.expire_all()
        live = _live_row_for_update(s)
        try:
            if _tts_authorization_snapshot(live, s) != expected_snapshot:
                _raise_tts_authorization_changed()
            session_id = expected_snapshot[0]
            if session_id is not None:
                _require_tts_session_authorization(
                    request,
                    s,
                    session_id=session_id,
                    live=live,
                    manual_text=manual_text,
                )
            command_id: int | None = None
            if command_key is not None:
                if (session_id is None
                        or capability_token_hash is None
                        or expected_text is None):
                    _raise_tts_authorization_changed()
                try:
                    current_text = autopilot_service.authorized_tts_text(
                        s,
                        session_id=session_id,
                        command_key=command_key,
                        capability_token_hash=capability_token_hash,
                    )
                except autopilot_service.AutopilotServiceError:
                    _raise_tts_authorization_changed()
                if current_text != expected_text:
                    _raise_tts_authorization_changed()
                command_row = s.exec(select(RuntimeCommand).where(
                    RuntimeCommand.session_id == session_id,
                    RuntimeCommand.idempotency_key == command_key,
                )).first()
                if command_row is None or command_row.id is None:
                    _raise_tts_authorization_changed()
                command_id = command_row.id
            if serve_facts is None:
                s.rollback()
                return
            sess_row = (s.get(TrainSession, session_id)
                        if session_id is not None else None)
            s.add(TtsServeEvidence(
                session_id=session_id,
                command_id=command_id,
                source=("autopilot_command" if command_key is not None
                        else "live_speak"),
                engine_version=serve_facts.engine_version,
                cache_hit=serve_facts.cache_hit,
                result=serve_facts.result,
                byte_count=serve_facts.byte_count,
                text_sha256=serve_facts.text_sha256,
                is_simulation=bool(sess_row.is_simulation) if sess_row else False,
            ))
            s.commit()
        except HTTPException as exc:
            s.rollback()
            detail = exc.detail
            if (isinstance(detail, dict)
                    and detail.get("code") == "tts_authorization_changed"):
                raise
            # Authorization/state conflicts discovered after provider I/O must
            # have one content-free response and never leak playable bytes.
            if 400 <= exc.status_code < 500:
                _raise_tts_authorization_changed()
            raise


@app.post("/tts/speak")
def tts_speak(body: TtsSpeakIn, request: Request,
              s: DBSession = Depends(get_session)):
    """为已配对老人端/具名账号合成一句屏显话术；引擎未接/模型缺失 → 204,前端回退系统语音。
    云引擎只合成白名单文本(题库/脚本/固定话术闭集)——患者字段永不出网,见 tts.cloud_text_allowed。
    使用 POST 避免账号 Cookie 的 GET 被同站邻居用图片/链接触发高成本合成。"""
    with _LIVE_WRITE_LOCK, device_capability.serialized_mutation():
        s.rollback()
        s.expire_all()
        live = _live_row_for_update(s)
        _require_capability_current_live(
            request, s, "合成实时话术", live_row=live)
        live_session_id = _live_session_id(live)
        if live_session_id:
            # DEVICE is a transport union.  A paired capability was already
            # checked against the exact live session above; a named account
            # must independently own that live object (or be an admin) before
            # it may spend provider quota through this generic text endpoint.
            _require_tts_session_authorization(
                request,
                s,
                session_id=live_session_id,
                live=live,
                manual_text=True,
            )
        authorization_snapshot = _tts_authorization_snapshot(live, s)
        # No DB snapshot/row lock may survive provider/cache I/O.
        s.rollback()
    text = body.text.strip()
    if not text:
        raise HTTPException(422, "text 为空")
    response, serve_facts = _synthesize_tts_text(text)
    _revalidate_tts_after_provider(
        request,
        s,
        expected_snapshot=authorization_snapshot,
        manual_text=True,
        serve_facts=serve_facts,
    )
    return response


class _TtsServeFacts(NamedTuple):
    """Server-observed synthesis outcome; the only admissible usage evidence."""
    engine_version: str
    cache_hit: bool
    result: str            # served / degraded
    byte_count: int | None
    text_sha256: str


def _synthesize_tts_text(
        text: str,
        synthesize: Callable[[str], tuple[bytes | None, str, bool]] | None = None,
) -> tuple[PlainResponse, _TtsServeFacts]:
    """Run provider/cache I/O outside the LiveState/capability transaction.

    ``synthesize`` selects the engine policy.  The generic operator path keeps
    its local fallback; the exact autopilot path passes the strict Qwen-only
    synthesizer, so a degraded result there is a real 204 rather than another
    engine's audio.
    """
    data, version, cached = (synthesize or tts.speak)(text)
    text_sha256 = hashlib.sha256(text.encode("utf-8")).hexdigest()
    if data is None:
        # 204 显式禁缓存:补装模型后老人端刷新要立刻吃到 200,不能被启发式缓存钉死在降级态
        facts = _TtsServeFacts(version, False, "degraded", None, text_sha256)
        return PlainResponse(status_code=204, headers={
            "X-Tts-Engine": version, "Cache-Control": "no-store"}), facts
    facts = _TtsServeFacts(version, cached, "served", len(data), text_sha256)
    return PlainResponse(content=data, media_type="audio/wav",
                         headers={"X-Tts-Engine": version, "X-Tts-Cache": "hit" if cached else "miss",
                                  "Cache-Control": "no-store"}), facts


@app.post("/sessions/{session_id}/autopilot/commands/{command_key}/tts")
def autopilot_command_tts(
        session_id: str, command_key: str, request: Request,
        body: AutopilotTtsIn = AutopilotTtsIn(),
        s: DBSession = Depends(get_session)):
    """Synthesize only server-derived text for the exact pending TTS command."""
    del body
    token_hash = _require_device_capability_token_hash(
        request, "合成自动驾驶话术")
    _require_capability_bound_session(
        request, session_id, "合成自动驾驶话术")
    with _LIVE_WRITE_LOCK, device_capability.serialized_mutation():
        s.rollback()
        s.expire_all()
        live = _live_row_for_update(s)
        _require_capability_bound_session(
            request, session_id, "合成自动驾驶话术")
        _require_capability_current_live(
            request, s, "合成自动驾驶话术", live_row=live)
        _require_capability_active_for_write(request, s, session_id)
        _require_tts_session_authorization(
            request,
            s,
            session_id=session_id,
            live=live,
            manual_text=False,
        )
        try:
            text = autopilot_service.authorized_tts_text(
                s,
                session_id=session_id,
                command_key=command_key,
                capability_token_hash=token_hash,
            )
            authorization_snapshot = _tts_authorization_snapshot(live, s)
            # Release the read transaction before provider/cache I/O.
            s.rollback()
        except autopilot_service.AutopilotServiceError as exc:
            s.rollback()
            _raise_autopilot_http_error(exc)
    # Provider/cache I/O can take seconds and must never retain the global live
    # row or capability serialization lock.
    response, serve_facts = _synthesize_tts_text(text, tts.speak_autopilot)
    _revalidate_tts_after_provider(
        request,
        s,
        expected_snapshot=authorization_snapshot,
        manual_text=False,
        command_key=command_key,
        capability_token_hash=token_hash,
        expected_text=text,
        serve_facts=serve_facts,
    )
    return response


@app.get("/tts/speak", include_in_schema=False)
def tts_speak_get_disabled():
    # 显式挡住 SPA catch-all，避免旧 GET 被当成 200 index.html；GET 不再承载高成本副作用。
    raise HTTPException(405, "TTS 合成仅接受带防伪证明或设备凭据的 POST")


# ---------------- R 会话编排 + 逐环节录音/判分/锁分 ----------------
def _load_bank_for_session(sess: TrainSession) -> content.ItemBank:
    bank_week = (sess.week_no if sess.week_no >= 2
                 else content.RAPPORT_ANCHOR_WEEK)
    try:
        bank = content.load_item_bank_for_week(bank_week)
    except content.TrainingWeekContentUnavailable as exc:
        raise HTTPException(409, str(exc))
    if sess.item_bank_version_id and bank.version_id != sess.item_bank_version_id:
        raise HTTPException(409, f"场次绑版本 {sess.item_bank_version_id} 与题库 {bank.version_id} 不符")
    return bank


def _find_bank_item(bank: content.ItemBank, item_id: str) -> dict | None:
    for it in list(bank.single_element) + list(bank.double_element) + list(bank.multi_element):
        if it.get("item_id") == item_id:
            return it
    return None


def _role_target(bank_item: dict, response_role: str) -> str | None:
    """该环节的确定式判分目标词；作用/关系/关键要素类无确定式口径 → None（纯人工）。"""
    return {"命名": bank_item.get("target_word"),
            "左命名": bank_item.get("left_word"),
            "右命名": bank_item.get("right_word")}.get(response_role)


class OperationalRubricUnavailable(RuntimeError):
    """开放回答尚无经内容组冻结的 AI 判定与分级提示口径。"""


def _classify_with_operational_rubric(
        rubric: dict, text: str | None) -> dict:
    """按版本化临床内容 rubric 作确定式运行判类，不产研究真值。"""
    response = rule_judge.normalize(text)
    rubric_version = str(rubric["rubric_version"]).strip()
    if not response:
        return {
            "answer_type": "沉默", "ai_score": 0.0, "needs_review": True,
            "judge_mode": "版本化规则",
            "judge_engine_version": f"rubric/{rubric_version}",
            "judge_reason": None, "matched_on": "silence",
            "contains_target": False, "judge_portrait_used": False,
            "truth_scope": "operational_only",
        }

    expressions = [
        rule_judge.normalize(value)
        for value in rubric.get("acceptable_expressions") or []
        if rule_judge.normalize(value)
    ]
    concepts = [
        rule_judge.normalize(value)
        for value in rubric.get("required_concepts") or []
        if rule_judge.normalize(value)
    ]
    expression_hit = any(value == response or value in response for value in expressions)
    concept_hits = sum(value in response for value in concepts)
    policy = rubric["decision_policy"]
    all_concepts = bool(concepts) and concept_hits == len(concepts)
    correct = (
        expression_hit if policy == "any_acceptable_expression"
        else all_concepts if policy == "all_required_concepts"
        else expression_hit or all_concepts
    )
    partial = not correct and concept_hits > 0
    return {
        "answer_type": "正确" if correct else "部分正确" if partial else "未识别",
        "ai_score": 1.0 if correct else 0.5 if partial else 0.0,
        # 即使运行规则确定，也仍需在研究后台与人工真值比较。
        "needs_review": True,
        "judge_mode": "版本化规则",
        "judge_engine_version": f"rubric/{rubric_version}",
        "judge_reason": None,
        "matched_on": (
            "rubric:acceptable" if expression_hit
            else f"rubric:concepts:{concept_hits}/{len(concepts)}"
            if concepts else "rubric:none"
        ),
        "contains_target": correct,
        "judge_portrait_used": False,
        "truth_scope": "operational_only",
    }


class AutopilotStartIn(BaseModel):
    """研究者启动 P0a 的最小命令；题目与话术只能由服务端冻结内容派生。"""
    model_config = ConfigDict(extra="forbid")

    idempotency_key: str = PydanticField(
        min_length=8, max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{7,127}$",
    )
    expected_revision: int = PydanticField(ge=0)


class AutopilotDrainAckIn(BaseModel):
    """The device may acknowledge only the route-bound command, with no facts."""

    model_config = ConfigDict(extra="forbid")


class AutopilotTakeoverIn(BaseModel):
    """Account CAS command; all takeover reasons are derived server-side."""

    model_config = ConfigDict(extra="forbid")

    idempotency_key: str = PydanticField(
        min_length=8, max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{7,127}$",
    )
    expected_revision: int = PydanticField(ge=0)


def _autopilot_write_failure(
        s: DBSession, exc: autopilot_service.AutopilotServiceError) -> NoReturn:
    """领域错误永远先回滚，再暴露稳定机器码，不泄露库内细节。"""
    s.rollback()
    _raise_autopilot_http_error(exc)


def _autopilot_integrity_conflict(s: DBSession, exc: IntegrityError) -> NoReturn:
    """多 worker/SQLite 并发碰到唯一约束时 fail-closed，不返回 500。"""
    s.rollback()
    raise HTTPException(status_code=409, detail={
        "code": "autopilot_concurrency_conflict",
        "message": "自动驾驶状态已被其他请求更新，请刷新后重试",
    }) from exc


_ACTIVE_CAPTURE_MARKERS = frozenset({
    "active", "armed", "recording", "starting", "initializing",
})


def _ensure_patient_capture_idle_for_autopilot(live: LiveState | None) -> None:
    """Never acquire server ownership while a legacy microphone may be hot.

    The server only evaluates already-received facts.  It deliberately does not
    rewrite ``selfStart``/``recording`` to manufacture a stopped microphone; the
    patient device must first report an inactive state (or have no prior capture
    fact in a fresh session).
    """
    if live is None:
        return
    cursor = _json_load(live.cursor_json) or {}
    rapport = _json_load(live.rapport_json) or {}
    patient_rec = _json_load(live.patient_rec_json) or {}
    command_capture_active = any(
        snapshot.get("selfStart") is True
        or str(snapshot.get("recording") or "").strip().lower()
        in _ACTIVE_CAPTURE_MARKERS
        for snapshot in (cursor, rapport)
    )
    device_capture_active = (
        patient_rec.get("active") is True
        or any(
            str(patient_rec.get(key) or "").strip().lower()
            in _ACTIVE_CAPTURE_MARKERS
            for key in ("recording", "state", "status")
        )
    )
    if command_capture_active or device_capture_active:
        raise HTTPException(status_code=409, detail={
            "code": "autopilot_patient_microphone_active",
            "message": "受试者端录音仍可能在运行；请先完成收麦回执再启动自动驾驶",
        })


@app.post(
    "/sessions/{session_id}/autopilot/start",
    response_model=autopilot_service.AutopilotStatusReceipt,
)
def autopilot_start(
        session_id: str, body: AutopilotStartIn, request: Request,
        s: DBSession = Depends(get_session)):
    """具名研究者只能启动显式开关下的模拟 P0a 窄范围。"""
    actor_id = _require_account_identity(
        request, "启动自动驾驶", roles={"researcher", "admin"},
        allow_local_m0=True)
    patient_id = _preauthorize_session_subject_fence(
        request, session_id, s, "启动自动驾驶")
    # 与设备写入共用唯一锁序。进锁后抛弃锁外 ORM
    # 快照，并锁定 LiveState；service 在同一事务内重验场次、
    # runtime、录音授权与唯一活跃设备后才能写命令。
    with (governance_lock.subject_fence(s, patient_id),
          _LIVE_WRITE_LOCK,
          device_capability.serialized_mutation()):
        sess = s.exec(select(TrainSession).where(
            TrainSession.session_id == session_id,
        ).with_for_update()).first()
        if sess is None:
            raise HTTPException(404, "场次不存在")
        _require_session_operator(
            request, sess, s, "启动自动驾驶", mutation=True)
        _require_started_visit_plan_session(session_id, s, sess=sess)
        live = _live_row_for_update(s)
        _runtime_row_for_update(session_id, s)
        # After object ownership and admission are proven, this metadata-only
        # gate still runs before any autopilot ownership or command row is
        # written. Key presence alone is never accepted as provider evidence.
        try:
            provider_readiness.require_start_ready(s)
        except provider_readiness.ProviderReadinessConflict as exc:
            s.rollback()
            raise HTTPException(
                status_code=409,
                detail=provider_readiness.conflict_detail(exc)) from exc
        _ensure_patient_capture_idle_for_autopilot(live)
        try:
            autopilot_service.start_p0a(
                s,
                session_id=session_id,
                idempotency_key=body.idempotency_key,
                expected_revision=body.expected_revision,
                actor_id=actor_id,
            )
            result = autopilot_service.get_autopilot_status(
                s, session_id=session_id)
            s.commit()
        except autopilot_service.AutopilotServiceError as exc:
            _autopilot_write_failure(s, exc)
        except IntegrityError as exc:
            _autopilot_integrity_conflict(s, exc)
    return result


@app.get(
    "/sessions/{session_id}/autopilot/status",
    response_model=autopilot_service.AutopilotStatusReceipt,
)
def autopilot_status(
        session_id: str, request: Request,
        s: DBSession = Depends(get_session)):
    """Account-only ownership receipt; never includes patient command content."""
    sess = s.get(TrainSession, session_id)
    if sess is None:
        raise HTTPException(404, "场次不存在")
    _require_session_read_operator(request, sess, s, "读取自动驾驶控制状态")
    _require_started_visit_plan_session(session_id, s, sess=sess)
    try:
        return autopilot_service.get_autopilot_status(
            s, session_id=session_id)
    except autopilot_service.AutopilotServiceError as exc:
        s.rollback()
        _raise_autopilot_http_error(exc)


@app.post(
    "/sessions/{session_id}/autopilot/commands/{command_key}/drain-ack",
    response_model=autopilot_service.DrainAckReceipt,
)
def autopilot_drain_ack(
        session_id: str, command_key: str, request: Request,
        body: AutopilotDrainAckIn = AutopilotDrainAckIn(),
        s: DBSession = Depends(get_session)):
    """Accept an empty, command-bound proof that patient media is fully stopped."""
    del body  # Validation is intentional; the route derives every persisted fact.
    token_hash = _require_device_capability_token_hash(
        request, "回执自动驾驶收麦状态")
    _require_capability_bound_session(
        request, session_id, "回执自动驾驶收麦状态")
    with _LIVE_WRITE_LOCK, device_capability.serialized_mutation():
        s.rollback()
        s.expire_all()
        live = _live_row_for_update(s)
        _require_capability_current_live(
            request, s, "回执自动驾驶收麦状态", live_row=live)
        # Lock order is LiveState -> capability -> SessionAutopilotState.
        _require_capability_active_for_write(request, s, session_id)
        try:
            result = autopilot_service.acknowledge_device_drain(
                s,
                session_id=session_id,
                command_key=command_key,
                capability_token_hash=token_hash,
            )
            s.commit()
        except autopilot_service.AutopilotServiceError as exc:
            _autopilot_write_failure(s, exc)
        except IntegrityError as exc:
            _autopilot_integrity_conflict(s, exc)
    return result


@app.get(
    "/sessions/{session_id}/autopilot/drain-target",
    response_model=autopilot_service.DrainTargetProjection,
)
def autopilot_drain_target(
        session_id: str, request: Request, response: Response,
        s: DBSession = Depends(get_session)):
    """Recover only an opaque exact drain target after patient-page refresh."""
    token_hash = _require_device_capability_token_hash(
        request, "恢复自动驾驶收麦目标")
    _require_capability_bound_session(
        request, session_id, "恢复自动驾驶收麦目标")
    with _LIVE_WRITE_LOCK, device_capability.serialized_mutation():
        s.rollback()
        s.expire_all()
        live = _live_row_for_update(s)
        _require_capability_current_live(
            request, s, "恢复自动驾驶收麦目标", live_row=live)
        # Preserve the write-plane ordering even though this projection itself
        # does not mutate: LiveState -> capability -> autopilot state.
        _require_capability_active_for_write(request, s, session_id)
        try:
            result = autopilot_service.get_drain_target(
                s,
                session_id=session_id,
                capability_token_hash=token_hash,
            )
        except autopilot_service.AutopilotServiceError as exc:
            s.rollback()
            _raise_autopilot_http_error(exc)
    response.headers["Cache-Control"] = "private, no-store"
    return result


@app.post(
    "/sessions/{session_id}/autopilot/takeover",
    response_model=autopilot_service.AutopilotStatusReceipt,
)
def autopilot_takeover(
        session_id: str, body: AutopilotTakeoverIn, request: Request,
        s: DBSession = Depends(get_session)):
    """Atomically release server ownership after durable media-stop evidence."""
    actor_id = _require_account_identity(
        request, "显式接管自动驾驶", roles={"researcher", "admin"},
        allow_local_m0=True)
    patient_id = _preauthorize_session_subject_fence(
        request,
        session_id,
        s,
        "接管自动驾驶",
        allow_withdrawn_safety_exit=True,
    )
    with (governance_lock.subject_fence(s, patient_id),
          _LIVE_WRITE_LOCK,
          device_capability.serialized_mutation()):
        # Account takeover does not require a device mutation, but it takes the
        # same global row/lock order as drain ACK so neither can pass stale facts.
        sess = s.exec(select(TrainSession).where(
            TrainSession.session_id == session_id,
        ).with_for_update()).first()
        if sess is None:
            raise HTTPException(404, "场次不存在")
        _require_session_operator(
            request, sess, s, "接管自动驾驶",
            mutation=True,
            allow_withdrawn_safety_exit=True,
        )
        _require_started_visit_plan_session(session_id, s, sess=sess)
        _live_row_for_update(s)
        _runtime_row_for_update(session_id, s)
        try:
            result = autopilot_service.takeover_autopilot_to_manual(
                s,
                session_id=session_id,
                idempotency_key=body.idempotency_key,
                expected_revision=body.expected_revision,
                actor_id=actor_id,
            )
            s.commit()
        except autopilot_service.AutopilotServiceError as exc:
            _autopilot_write_failure(s, exc)
        except IntegrityError as exc:
            _autopilot_integrity_conflict(s, exc)
    return result


@app.get(
    "/sessions/{session_id}/autopilot/next",
    response_model=autopilot_service.NextCommandProjection | None,
)
def autopilot_next(
        session_id: str, request: Request, background_tasks: BackgroundTasks,
        s: DBSession = Depends(get_session)):
    """只向精确绑定当前场次的活跃设备投影当前一条命令。"""
    token_hash = _require_device_capability_token_hash(request, "读取自动驾驶命令")
    _require_capability_bound_session(request, session_id, "读取自动驾驶命令")
    result = None
    should_recover_attempt = False
    should_recover_legacy = False
    # A terminal legacy pause is provider-free and device-free: once the
    # judgement has durably committed, nothing about a device capability or a
    # feature toggle can make finishing the scope unsafe, and refusing to
    # finish it would strand the session in processing_attempt for good. It is
    # therefore honoured even on the paths that must still return an error, and
    # it can admit nothing else — no ASR, no judgement, no command, no other
    # write.
    should_pause_legacy = False
    terminal_resolutions: frozenset = frozenset()
    pending_error: Exception | None = None
    # 读取也与重配对串行，避免在轮换设备的中间态泄露旧命令。
    with _LIVE_WRITE_LOCK, device_capability.serialized_mutation():
        s.rollback()
        s.expire_all()
        live = _live_row_for_update(s)
        _require_capability_bound_session(request, session_id, "读取自动驾驶命令")
        # One locked resolution, branched on directly — never on the shape of an
        # exception, and never via the unlocked resolver followed by a gate that
        # could itself cross expiry and throw outside these branches. Running
        # the current-live gate before this would also let a capability demoted
        # to recovery-only by a LiveState switch die on device_session_changed
        # and never reach the terminal closure it is still allowed to finish.
        status, capability = device_capability.revalidate_active_for_write(
            s, token_hash, session_id)
        if (capability is None or capability.session_id != session_id
                or status in {device_capability.CapabilityResolution.REVOKED,
                              device_capability.CapabilityResolution.INVALID}):
            s.rollback()
            pending_error = _capability_error(
                status if capability is not None
                and capability.session_id == session_id
                else device_capability.CapabilityResolution.INVALID)
        elif status is not device_capability.CapabilityResolution.VALID:
            # EXPIRED / RECOVERY_ONLY: terminal-only. No current-live gate, no
            # command projection, no provider work — and the original 401.
            s.rollback()
            should_pause_legacy = True
            terminal_resolutions = _SAFE_CAPABILITY_TRANSITIONS[status]
            pending_error = _capability_error(status)
        else:
            # The locked revalidation above is the *only* capability resolution
            # this handler performs. Calling the combined write gate here would
            # resolve the row a second time and reopen exactly the expiry window
            # this branch exists to close, so only its VisitPlan admission is
            # invoked explicitly.
            _require_started_visit_plan_session(session_id, s)
            _require_capability_current_live(
                request, s, "读取自动驾驶命令", live_row=live)
        if pending_error is None:
            try:
                result = autopilot_service.get_next_command(
                    s,
                    session_id=session_id,
                    capability_token_hash=token_hash,
                )
                state = s.get(SessionAutopilotState, session_id)
                should_recover_attempt = bool(
                    result is None and state is not None
                    and state.scope_key == autopilot_service.P0A_SCOPE_KEY
                    and state.status == "processing_attempt")
            except autopilot_service.AutopilotServiceError as exc:
                # A session frozen before the repeat protocol existed is refused
                # by every generic gate, which would strand its one
                # already-recorded capture forever. The dedicated legacy worker
                # is scheduled instead — still projecting no command, old or
                # new, to the device.
                #
                # Only the one refusal a missing pre-repeat binding actually
                # produces opens that door: feature disablement, content-digest
                # drift, device/capability, control-generation and consent
                # refusals keep failing closed, so the marker can never let a
                # different safety gate be answered with a worker submission.
                if autopilot_orchestration.legacy_terminal_pause_owed(
                        s, session_id=session_id):
                    # The bearer passed the active-capability gate, so a feature
                    # toggle or content drift committed after the judgement must
                    # not strand the scope.
                    s.rollback()
                    should_pause_legacy = True
                    terminal_resolutions = _SAFE_CAPABILITY_TRANSITIONS[
                        device_capability.CapabilityResolution.VALID]
                    pending_error = exc
                elif (exc.code not in _LEGACY_RECOVERY_GATE_CODES
                        or not autopilot_orchestration
                        .legacy_pre_repeat_recovery_pending(
                            s, session_id=session_id)):
                    s.rollback()
                    pending_error = exc
                else:
                    s.rollback()
                    should_recover_legacy = True
    if should_pause_legacy:
        # Committed inline rather than scheduled: FastAPI does not run
        # background tasks when the response is an error, and this path must
        # still return the original refusal. Authorization is re-resolved under
        # the capability row lock inside the transaction that writes, so a
        # revoke committed while we waited for that lock still wins.
        outcome = _commit_http_legacy_terminal_pause(
            token_hash=token_hash, session_id=session_id,
            allowed=terminal_resolutions)
        if (outcome.current is not None
                and outcome.current
                is not device_capability.CapabilityResolution.VALID):
            # The locked read is the authority: never answer a bearer that has
            # since decayed or been revoked with a stale non-auth refusal.
            pending_error = _capability_error(outcome.current)
    if pending_error is not None:
        if isinstance(pending_error, HTTPException):
            raise pending_error
        _raise_autopilot_http_error(pending_error)
    if should_recover_attempt:
        # FastAPI awaits BackgroundTasks, so that task only submits to our executor;
        # provider I/O runs in the executor and cannot delay this GET response.
        background_tasks.add_task(
            autopilot_orchestration.submit,
            session_id,
            _run_p0a_attempt_worker,
        )
    if should_recover_legacy:
        background_tasks.add_task(
            autopilot_orchestration.submit,
            session_id,
            _run_legacy_repeat_recovery_worker,
        )
    return result


@app.post(
    "/sessions/{session_id}/autopilot/commands/{command_key}/acks",
    response_model=autopilot_service.ApplyDeviceAckResult,
)
def autopilot_command_ack(
        session_id: str, command_key: str,
        body: autopilot_contract.AutopilotAckIn, request: Request,
        background_tasks: BackgroundTasks,
        s: DBSession = Depends(get_session)):
    """设备回执的落库、命令 CAS 与后续路由必须是同一次提交。"""
    token_hash = _require_device_capability_token_hash(request, "回执自动驾驶命令")
    _require_capability_bound_session(request, session_id, "回执自动驾驶命令")
    preflight_session = s.get(TrainSession, session_id)
    if preflight_session is None:
        raise HTTPException(404, "场次不存在")
    patient_id = preflight_session.patient_id
    s.rollback()
    s.expire_all()
    with (governance_lock.subject_fence(s, patient_id),
          _LIVE_WRITE_LOCK,
          device_capability.serialized_mutation()):
        locked_session = s.exec(select(TrainSession).where(
            TrainSession.session_id == session_id,
        ).with_for_update()).first()
        if locked_session is None:
            raise HTTPException(404, "场次不存在")
        live = _live_row_for_update(s)
        _require_capability_bound_session(request, session_id, "回执自动驾驶命令")
        _require_capability_current_live(
            request, s, "回执自动驾驶命令", live_row=live)
        _require_capability_active_for_write(request, s, session_id)
        # A final ACK may transition the runtime to intervention_completed.
        # Lock that authority row before applying the command so no stale
        # terminal snapshot can overwrite withdrawal/finish/complete.
        s.exec(select(SessionRuntimeState).where(
            SessionRuntimeState.session_id == session_id,
        ).with_for_update()).first()
        intervention_assessment = None
        try:
            result = autopilot_service.apply_device_ack(
                s,
                session_id=session_id,
                command_key=command_key,
                capability_token_hash=token_hash,
                ack=body,
            )
            result, intervention_assessment = _autofinalize_completed_autopilot_scope(
                s,
                session_id=session_id,
                live=live,
                ack_result=result,
            )
            s.commit()
        except autopilot_service.AutopilotServiceError as exc:
            _autopilot_write_failure(s, exc)
        except IntegrityError as exc:
            _autopilot_integrity_conflict(s, exc)
    if body.ack_type == "record_stopped" and result.status == "processing_attempt":
        # The ACK/capture/state transaction is already committed.  The lightweight
        # response task only submits a separate worker and never performs ASR/LLM.
        background_tasks.add_task(
            autopilot_orchestration.submit,
            session_id,
            _run_p0a_attempt_worker,
        )
    if intervention_assessment is not None and intervention_assessment.ready:
        _audit(
            s, request, "session_intervention_auto_complete",
            f"服务器自动结束床旁干预 计划环节={intervention_assessment.expected_turns} "
            f"AI完成={intervention_assessment.completed_attempt_turns} "
            f"录音证据={intervention_assessment.audio_evidenced_turns}",
            session_id=session_id,
        )
    return result


def _resolved_session_plan(
        session_id: str, *, request: Request, action: str,
        week_no: int | None, event_line: str | None,
        max_items: int | None, s: DBSession):
    """只在服务端解析并验证场次冻结计划。

    账号端与老人端可以复用同一个场次上下文，但投影必须由各自
    的路由决定；不再让开放开发模式下缺失的认证类型决定是否下发答案。
    """
    sess = _require_device_route_session_read(request, session_id, s, action)
    persisted_event_line = sess.event_line.value if hasattr(sess.event_line, "value") else str(sess.event_line)
    if week_no is not None and week_no != sess.week_no:
        raise HTTPException(409, f"请求 week_no={week_no} 与场次已持久化周次 {sess.week_no} 不符")
    if event_line is not None and event_line != persisted_event_line:
        raise HTTPException(409, f"请求 event_line={event_line!r} 与场次已持久化事件线 {persisted_event_line!r} 不符")
    if max_items is not None and max_items < 0:
        raise HTTPException(422, "max_items 不得小于 0")
    bank = _load_bank_for_session(sess)
    try:
        plan = runtime.build_session_plan(bank, sess.week_no, persisted_event_line, max_items)
    except ValueError as e:
        # 数据约束违反是 422；未结构化/未校对的周次是当前资源状态冲突，fail-closed。
        status = 422 if "1..8" in str(e) else 409
        raise HTTPException(status, str(e))
    return sess, plan


def _patient_plan_projection(plan):
    """老人端只得到不可反推答案的场次题位结构。"""
    return {
        "item_bank_version_id": plan.item_bank_version_id,
        "week_no": plan.week_no,
        "event_line": plan.event_line,
        "total_items": len(plan.items),
        "total_turns": plan.total_turns(),
        "items": [
            {
                "item_ref": patient_presentation.item_ref(item_idx),
                "task_type": item.task_type,
                "presentation_order": item.presentation_order,
                "turns": [
                    {"turn_seq": turn.turn_seq, "response_role": turn.response_role}
                    for turn in item.turns
                ],
            }
            for item_idx, item in enumerate(plan.items)
        ],
    }


@app.get("/sessions/{session_id}/patient-plan")
def patient_session_plan(
        session_id: str, request: Request, response: Response,
        week_no: int | None = None, event_line: str | None = None,
        s: DBSession = Depends(get_session)):
    """专用老人端投影：即使开放本地模式也永不返回 canonical 答案。"""
    _sess, plan = _resolved_session_plan(
        session_id, request=request, action="读取老人端训练计划",
        week_no=week_no, event_line=event_line,
        max_items=None, s=s,
    )
    response.headers["Cache-Control"] = "private, no-store"
    return _patient_plan_projection(plan)


_PATIENT_ASSET_HEADERS = {
    "Cache-Control": "private, no-store",
    "Pragma": "no-cache",
    "X-Content-Type-Options": "nosniff",
    "Cross-Origin-Resource-Policy": "same-origin",
    "Accept-Ranges": "none",
}


def _patient_asset_conflict(code: str, message: str) -> NoReturn:
    raise HTTPException(status_code=409, detail={
        "code": code,
        "message": message,
    })


@app.api_route(
    "/sessions/{session_id}/patient-asset/current",
    methods=["GET", "HEAD"],
)
def patient_current_asset(
        session_id: str, request: Request,
        s: DBSession = Depends(get_session)):
    """只向当前已配对设备返回当前题的私有图片字节。

    路由不接受 image id/index 等选择器。手动面以服务端 LiveState
    当前游标为准；自动驾驶面以当前未结束的服务端命令为准。
    客户端从未把题号、图片 id 或文件名传进来，因此无法枚举未来题。
    """
    token_hash = _require_device_capability_token_hash(
        request, "读取当前题图片")
    if request.query_params:
        raise HTTPException(status_code=422, detail={
            "code": "patient_asset_selector_forbidden",
            "message": "当前题图片端点不接受题目、图片或位置选择参数",
        })
    _require_capability_bound_session(request, session_id, "读取当前题图片")

    # Read and capability rotation share the same lock order as autopilot/next.
    # Re-resolve database rows inside the boundary so a concurrently revoked or
    # re-paired token cannot receive bytes after its middleware preflight.
    with _LIVE_WRITE_LOCK, device_capability.serialized_mutation():
        s.rollback()
        s.expire_all()
        sess = _require_device_route_session_read(
            request, session_id, s, "读取当前题图片")
        live = s.get(LiveState, 1)
        _require_capability_current_live(
            request, s, "读取当前题图片", live_row=live)
        _require_capability_active_for_write(request, s, session_id)
        if live is None or _live_session_id(live) != session_id:
            _patient_asset_conflict(
                "patient_asset_session_not_current",
                "当前操作端没有该场次的呈现状态",
            )

        # Week 1 is explicitly a rapport/baseline flow without an image library.
        # It returns no bytes after exact device/current-session authorization;
        # no runtime cursor is required merely to prove this frozen no-image fact.
        if sess.week_no == 1:
            return PlainResponse(status_code=204, headers=_PATIENT_ASSET_HEADERS)
        runtime_state = s.get(SessionRuntimeState, session_id)
        if runtime_state is None or runtime_state.status != "active":
            _patient_asset_conflict(
                "patient_asset_runtime_not_active",
                "只有正在进行且未暂停的床旁场次可读取题图片",
            )
        # 真实研究呈现由签字工件驱动:交付清单必须是 research_and_simulation
        # scope 且携带可校验的来源转换权利+内容审批事实(patient_asset 契约层
        # 逐项验证,任何异常 fail-closed 为未批准),代码里没有旁路。
        if not sess.is_simulation and not patient_asset.research_release_approved():
            _patient_asset_conflict(
                "patient_asset_research_release_blocked",
                "当前私有图片交付清单仅允许模拟验证，"
                "未完成来源、转换、权利与内容审批前禁止真实研究呈现",
            )

        bank = _load_bank_for_session(sess)
        current_digest = content.item_bank_definition_digest(bank)
        if (not sess.item_bank_definition_digest
                or sess.item_bank_definition_digest != current_digest):
            _patient_asset_conflict(
                "patient_asset_definition_mismatch",
                "场次冻结题库定义与当前内容不一致，拒绝呈现图片",
            )
        plan = _session_plan_for_runtime(sess)

        item_idx: int | None = None
        autopilot_state = s.get(SessionAutopilotState, session_id)
        if autopilot_state is not None and autopilot_state.mode == "autonomous":
            if (autopilot_state.status not in {"waiting_tts", "waiting_recording"}
                    or autopilot_state.current_command_id is None):
                _patient_asset_conflict(
                    "patient_asset_no_current_command",
                    "自动驾驶当前没有可呈现的未结束命令",
                )
            command = s.get(RuntimeCommand, autopilot_state.current_command_id)
            if (
                command is None
                or command.session_id != session_id
                or command.state not in {"pending", "started"}
                or command.control_generation != autopilot_state.control_generation
                or command.runner_generation != autopilot_state.runner_generation
                or command.issued_capability_token_hash != token_hash
                or command.item_bank_version_id != sess.item_bank_version_id
                or command.item_bank_definition_digest != current_digest
            ):
                _patient_asset_conflict(
                    "patient_asset_command_mismatch",
                    "当前自动驾驶命令与场次或已配对设备不一致",
                )
            matching_positions = [
                idx for idx, item in enumerate(plan.items)
                if item.item_id == command.item_id
                and any(turn.turn_seq == command.turn_seq for turn in item.turns)
            ]
            if len(matching_positions) != 1:
                _patient_asset_conflict(
                    "patient_asset_command_position_invalid",
                    "当前自动驾驶命令无法唯一绑定冻结题位",
                )
            item_idx = matching_positions[0]
        else:
            cursor = _json_load(live.cursor_json)
            if cursor is None or _payload_session_id(cursor) not in {None, session_id}:
                _patient_asset_conflict(
                    "patient_asset_cursor_unavailable",
                    "当前场次尚无可安全绑定的呈现游标",
                )
            if cursor.get("screen") not in {"present", "record"}:
                _patient_asset_conflict(
                    "patient_asset_screen_not_presenting",
                    "当前老人端画面不在题目呈现或回答状态",
                )
            raw_item_idx = cursor.get("itemIdx")
            raw_turn_idx = cursor.get("turnIdx")
            if (not isinstance(raw_item_idx, int) or isinstance(raw_item_idx, bool)
                    or not isinstance(raw_turn_idx, int)
                    or isinstance(raw_turn_idx, bool)):
                _patient_asset_conflict(
                    "patient_asset_cursor_invalid",
                    "当前呈现游标损坏，拒绝返回图片",
                )
            if (raw_item_idx < 0 or raw_item_idx >= len(plan.items)
                    or raw_turn_idx < 0
                    or raw_turn_idx >= len(plan.items[raw_item_idx].turns)):
                _patient_asset_conflict(
                    "patient_asset_cursor_out_of_range",
                    "当前呈现游标超出场次冻结计划",
                )
            item_idx = raw_item_idx

        current_item = plan.items[item_idx]
        if not current_item.image_id:
            return PlainResponse(status_code=204, headers=_PATIENT_ASSET_HEADERS)
        if request.headers.get("range"):
            raise HTTPException(status_code=416, detail={
                "code": "patient_asset_range_forbidden",
                "message": "当前题图片不接受分段读取",
            })
        try:
            asset = patient_asset.resolve_runtime_asset(
                bank, current_item.image_id)
            payload = patient_asset.read_runtime_asset(asset)
        except patient_asset.PatientAssetError:
            # Never echo image_id/path/source filename: those fields can reveal
            # the answer even though the URL itself is selector-free.
            _patient_asset_conflict(
                "patient_asset_unavailable",
                "当前题图片未通过安全部署校验",
            )
        return PlainResponse(
            content=payload,
            media_type=asset.media_type,
            headers=_PATIENT_ASSET_HEADERS,
        )


@app.get("/sessions/{session_id}/plan")
def session_plan(session_id: str, request: Request, week_no: int | None = None, event_line: str | None = None,
                 max_items: int | None = None, s: DBSession = Depends(get_session)):
    _sess, plan = _resolved_session_plan(
        session_id, request=request, action="读取或恢复场次冻结计划",
        week_no=week_no, event_line=event_line,
        max_items=max_items, s=s,
    )
    # 账号端保留 canonical item_id/display/scoring_key。设备端的 item_id
    # 本身即可携带目标词，因此使用只表示本 session 冻结题位的
    # opaque item_ref；当前文本由 patient-presentation 另行做 LiveState 绑定投影。
    device_projection = (
        getattr(request.state, "auth_kind", None) == "device_capability"
    )
    if device_projection:
        return _patient_plan_projection(plan)
    return {"item_bank_version_id": plan.item_bank_version_id, "week_no": plan.week_no,
            "event_line": plan.event_line, "total_items": len(plan.items),
            "total_turns": plan.total_turns(),
            "items": [{"item_id": it.item_id, "task_type": it.task_type, "image_id": it.image_id,
                       "presentation_order": it.presentation_order,
                       "display": it.display,
                       "turns": [{"turn_seq": t.turn_seq, "response_role": t.response_role,
                                  "scoring_key": t.scoring_key} for t in it.turns]}
                      for it in plan.items]}


class PatientTaskPresentationOut(BaseModel):
    """受试者设备当前训练转位的最小内容投影。"""
    schema_version: Literal[1] = 1
    mode: Literal["task"] = "task"
    session_id: str
    item_bank_version_id: str
    item_idx: int
    turn_idx: int
    item_ref: str
    turn_seq: int
    response_role: str
    cue_level: int
    cue_text: str | None
    feedback_key: Literal[
        "self", "cued1_unknown", "cued1_close", "cued1_silence",
        "cued2", "namefix_l", "namefix_r",
    ] | None
    feedback_item_ref: str | None
    feedback_seq: int | None
    feedback_text: str | None
    wseq: int | None


class PatientRapportPresentationOut(BaseModel):
    """受试者设备当前关系建立问句的最小内容投影。"""
    schema_version: Literal[1] = 1
    mode: Literal["rapport"] = "rapport"
    session_id: str
    script_version_id: str
    section_key: str
    question_idx: int
    speaker: Literal["机器人", "研究者"]
    text: str | None
    wseq: int | None


@app.get(
    "/sessions/{session_id}/patient-presentation",
    response_model=PatientTaskPresentationOut | PatientRapportPresentationOut,
)
def patient_presentation_get(
        session_id: str, request: Request, s: DBSession = Depends(get_session)):
    """只下发当前服务端游标已授权呈现的一条内容。

    此过渡端点供当前浏览器自动驾驶使用；服务端命令队列全面接管
    语音/录音后应删除。它不接受 item/section 查询参数，因此配对设备
    无法用它枚举未到达的题、后续提示或其他脚本节。
    """
    if request.query_params:
        raise HTTPException(422, "受试者呈现端点不接受题目或脚本选择参数")
    sess = _require_device_route_session_read(
        request, session_id, s, "读取当前老人端呈现内容")
    with _LIVE_WRITE_LOCK:
        live = s.get(LiveState, 1)
        _require_capability_current_live(
            request, s, "读取当前呈现内容", live_row=live)
        if live is None or _live_session_id(live) != session_id:
            raise HTTPException(409, "当前操作端没有该场次的呈现状态")
        if sess.week_no == 1:
            rapport = _json_load(live.rapport_json)
            if rapport is None:
                raise HTTPException(409, "当前关系建立场次尚无可呈现问句")
            if _payload_session_id(rapport) not in {None, session_id}:
                raise HTTPException(409, "关系建立呈现与当前场次不一致")
            section_key = rapport.get("sectionKey")
            question_idx = rapport.get("questionIdx")
            if (not isinstance(section_key, str) or not section_key
                    or not isinstance(question_idx, int)
                    or isinstance(question_idx, bool) or question_idx < 0):
                raise HTTPException(409, "当前关系建立游标损坏，拒绝呈现")
            script = content.load_week1_script(
                content.CONTENT_DIR / "week1_script.json")
            try:
                speaker, text = patient_presentation.resolve_rapport_text(
                    script, section_key=section_key, question_idx=question_idx)
            except ValueError as exc:
                raise HTTPException(409, str(exc)) from exc
            return PatientRapportPresentationOut(
                session_id=session_id,
                script_version_id=str(script["script_version_id"]),
                section_key=section_key,
                question_idx=question_idx,
                speaker=speaker,
                text=text,
                wseq=_wseq_from(rapport),
            )

        cursor = _json_load(live.cursor_json)
        if cursor is None:
            raise HTTPException(409, "当前训练场次尚无可呈现游标")
        if _payload_session_id(cursor) not in {None, session_id}:
            raise HTTPException(409, "训练呈现与当前场次不一致")
        item_idx = cursor.get("itemIdx")
        turn_idx = cursor.get("turnIdx")
        cue_level = cursor.get("cueLevel", 0)
        if (not isinstance(item_idx, int) or isinstance(item_idx, bool)
                or not isinstance(turn_idx, int) or isinstance(turn_idx, bool)
                or not isinstance(cue_level, int) or isinstance(cue_level, bool)):
            raise HTTPException(409, "当前训练游标损坏，拒绝呈现")
        plan = _session_plan_for_runtime(sess)
        if item_idx < 0 or item_idx >= len(plan.items):
            raise HTTPException(409, "当前训练题位已超出冻结计划")
        item = plan.items[item_idx]
        if turn_idx < 0 or turn_idx >= len(item.turns):
            raise HTTPException(409, "当前训练环节已超出冻结计划")
        turn = item.turns[turn_idx]
        response_role = cursor.get("responseRole") or turn.response_role
        if response_role != turn.response_role:
            raise HTTPException(409, "当前训练角色与冻结计划不一致")

        feedback_key = cursor.get("fbKey")
        feedback_item_id = cursor.get("fbItemId")
        feedback_seq = cursor.get("fbSeq")
        supplied_feedback = (
            feedback_key is not None,
            feedback_item_id is not None,
            feedback_seq is not None,
        )
        if any(supplied_feedback) and not all(supplied_feedback):
            raise HTTPException(409, "当前反馈指针不完整，拒绝呈现")
        if (feedback_seq is not None
                and (not isinstance(feedback_seq, int)
                     or isinstance(feedback_seq, bool) or feedback_seq < 0)):
            raise HTTPException(409, "当前反馈序号非法")
        protocol = content.load_autopilot_protocol(
            content.CONTENT_DIR / "autopilot_protocol_v1.json")
        try:
            cue_text, feedback_text = patient_presentation.resolve_task_texts(
                item_id=item.item_id,
                task_type=item.task_type,
                display=item.display,
                response_role=turn.response_role,
                cue_level=cue_level,
                protocol=protocol,
                feedback_key=feedback_key,
                feedback_item_id=feedback_item_id,
            )
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc
        return PatientTaskPresentationOut(
            session_id=session_id,
            item_bank_version_id=plan.item_bank_version_id,
            item_idx=item_idx,
            turn_idx=turn_idx,
            item_ref=patient_presentation.item_ref(item_idx),
            turn_seq=turn.turn_seq,
            response_role=turn.response_role,
            cue_level=cue_level,
            cue_text=cue_text,
            feedback_key=feedback_key,
            feedback_item_ref=(
                patient_presentation.item_ref(item_idx)
                if feedback_item_id is not None else None
            ),
            feedback_seq=feedback_seq,
            feedback_text=feedback_text,
            wseq=_wseq_from(cursor),
        )


class ItemIn(BaseModel):
    item_id: str
    task_type: str
    item_set_type: str = "训练集"
    image_id: str | None = None
    difficulty_level: str | None = None
    presentation_order: int | None = None


@app.post("/sessions/{session_id}/items", response_model=ItemEvent)
def create_item(session_id: str, body: ItemIn, request: Request,
                s: DBSession = Depends(get_session)):
    with _LIVE_WRITE_LOCK:
        _live_row_for_update(s)
        sess = s.get(TrainSession, session_id)
        if not sess:
            raise HTTPException(404, "场次不存在")
        _require_session_operator(
            request, sess, s, "新建题目事件", mutation=True)
        _ensure_manual_plane_writable(session_id, s, "从旧操作端新建题目事件")
        _ensure_runtime_writable(session_id, s, "新建题目事件")
        ie = ItemEvent(session_id=session_id, **body.model_dump())
        s.add(ie)
        try:
            s.commit()
            s.refresh(ie)
        except IntegrityError as exc:
            s.rollback()
            raise HTTPException(
                409, "同一场次的冻结计划题已有唯一 ItemEvent") from exc
    return ie


class TurnIn(BaseModel):
    turn_seq: int
    response_role: str | None = None
    raw_audio_id: str | None = None
    asr_text: str | None = None
    asr_confidence: float | None = None
    prompt_level: int | None = None
    cue_type: str | None = None
    duration_seconds: float | None = None


@app.post("/items/{item_event_id}/turns", response_model=TurnEvent)
def create_turn(item_event_id: int, body: TurnIn, request: Request,
                s: DBSession = Depends(get_session)):
    with _LIVE_WRITE_LOCK:
        _live_row_for_update(s)
        item = s.get(ItemEvent, item_event_id)
        if not item:
            raise HTTPException(404, "题目事件不存在")
        sess = s.get(TrainSession, item.session_id)
        if sess is None:
            raise HTTPException(409, "题目事件缺少场次")
        _require_session_operator(
            request, sess, s, "新建回答环节", mutation=True,
            not_found_detail="题目事件不存在")
        _ensure_manual_plane_writable(
            item.session_id, s, "从旧操作端新建环节事件")
        _ensure_runtime_writable(item.session_id, s, "新建环节事件")
        return _create_turn_locked(item_event_id, body, item, s)


def _create_turn_locked(
        item_event_id: int, body: TurnIn, item: ItemEvent,
        s: DBSession) -> TurnEvent:
    payload = body.model_dump()
    if not body.raw_audio_id:
        raise HTTPException(409, "TurnEvent 必须引用已完成 AI attempt 的 raw_audio_id")
    source_attempt = s.exec(select(AttemptEvent).where(
        AttemptEvent.raw_audio_id == body.raw_audio_id)).first()
    # Authenticate the source object against the already-authorized Item/Session
    # before exposing processing status, role or portrait diagnostics.  Foreign
    # global audio identifiers and nonexistent identifiers are intentionally
    # indistinguishable.
    if source_attempt is None or source_attempt.session_id != item.session_id:
        raise HTTPException(404, "source attempt 不存在")
    if source_attempt.processing_status != "completed":
        raise HTTPException(
            409, f"source attempt 状态为 {source_attempt.processing_status}，禁止生成终值 TurnEvent")
    if source_attempt.judge_portrait_used:
        raise HTTPException(409, "source attempt 违反画像禁入判分约束")
    authoritative_role = source_attempt.response_role
    if body.response_role is not None and body.response_role != authoritative_role:
        raise HTTPException(409, "source attempt 与请求的环节角色不一致")
    payload["response_role"] = authoritative_role
    if (source_attempt.item_id != item.item_id
            or source_attempt.turn_seq != body.turn_seq):
        raise HTTPException(409, "source attempt 与题目/环节不一致")
    sess = s.get(TrainSession, item.session_id)
    if not sess:
        raise HTTPException(409, "题目事件缺少关联场次")
    _require_classified_session(sess)
    _frozen_plan_turn(sess, item.item_id, body.turn_seq, authoritative_role)
    audio = s.get(AudioAssetRow, source_attempt.raw_audio_id)
    expected_turn_key = f"{item.item_id}#{body.turn_seq}"
    if (audio is None or audio.session_id != item.session_id
            or audio.turn_key != expected_turn_key
            or audio.is_simulation != sess.is_simulation
            or audio.data_classification != sess.data_classification):
        raise HTTPException(409, "source attempt 的录音归属或数据分类不一致")
    _ensure_audio_read_allowed(audio, s)
    if not audio_store.find_blob(source_attempt.raw_audio_id):
        raise HTTPException(409, "source attempt 缺少原始录音字节")

    authoritative = {
        "asr_text": source_attempt.asr_text,
        "asr_confidence": source_attempt.asr_confidence,
        "prompt_level": source_attempt.prompt_level,
        "cue_type": source_attempt.cue_type,
        "duration_seconds": source_attempt.duration_seconds,
    }
    for field, value in authoritative.items():
        if field in body.model_fields_set and payload[field] != value:
            raise HTTPException(
                409, f"TurnEvent.{field} 与 source attempt 权威证据冲突")
        payload[field] = value
    payload.update({
        "source_attempt_id": source_attempt.id,
        "ai_answer_type": source_attempt.operational_answer_type,
        "ai_score": source_attempt.operational_score,
        "ai_needs_review": source_attempt.operational_needs_review,
        "ai_judge_mode": source_attempt.judge_mode,
        "judge_portrait_used": source_attempt.judge_portrait_used,
    })

    te = TurnEvent(item_event_id=item_event_id, **payload)
    s.add(te)
    try:
        s.commit()
        s.refresh(te)
    except IntegrityError:
        s.rollback()
        raise HTTPException(
            409, "该冻结计划环节或 source attempt 已被 TurnEvent 收口")
    return te


def _load_turn(turn_id: int, s: DBSession) -> TurnEvent:
    t = s.get(TurnEvent, turn_id)
    if not t:
        raise HTTPException(404, "环节不存在")
    return t


class ClassifyIn(BaseModel):
    """自动驾驶逐轮判类输入:题号+环节角色+该轮 ASR 文本,无画像、无患者字段。"""
    model_config = ConfigDict(extra="forbid")

    item_id: str = PydanticField(min_length=1, max_length=160)
    response_role: str = PydanticField(default="命名", max_length=64)
    text: str | None = PydanticField(default=None, max_length=2000)


def _classify_operational(*, item_id: str, response_role: str,
                          text: str | None, bank: content.ItemBank | None = None,
                          allow_llm: bool = True,
                          llm_engine: object | None = None,
                          cloud_llm_allowed: bool = False) -> dict:
    """可复用的自动驾判类：只产运行决策证据，不写 TurnEvent 研究真值。"""
    bank = bank or content.load_item_bank_for_week(2)
    found = _task_type_for_bank_item(bank, item_id)
    if not found:
        raise HTTPException(404, f"题库无此题:{item_id}")
    task_type, bank_item = found
    target = _role_target(bank_item, response_role or "命名")
    if not target:
        rubric = content.operational_rubric_for(bank, item_id, response_role)
        if rubric is None:
            raise OperationalRubricUnavailable(
                f"{item_id}:{response_role} 缺版本化 operational rubric")
        return _classify_with_operational_rubric(rubric, text)

    raw_text = text or ""
    contains = target in raw_text or any(
        candidate and candidate in raw_text
        for candidate in (bank_item.get("acceptable_expressions") or []))
    ji = judging.build_judge_input(
        item_id=item_id, task_type=task_type, target_word=target,
        acceptable_expressions=tuple(bank_item.get("acceptable_expressions", []) or []),
        # 题库里这张表叫 related_but_inaccurate,判分输入侧沿用旧字段名 upper_terms。
        # 之前读的 upper_terms 在题库里根本不存在,恒空,"蔬菜"这类相关但不准确的
        # 回答一路掉到未识别,冻结的 close 分支永远选不中。
        upper_terms=tuple(bank_item.get("related_but_inaccurate", []) or []),
        dialect_synonyms=tuple(bank_item.get("dialect_synonyms", []) or []),
        asr_text=text,
    )
    engine = (llm_engine or llm_judge.get_engine()) if allow_llm else None
    if not raw_text.strip():
        # 空转写没有可判的语义:沉默由确定式规则冻结判定,也没有理由把一个空
        # 载荷推过云边界。
        engine = None
    elif rule_judge.normalize(raw_text) in {
            rule_judge.normalize(term) for term in ji.upper_terms}:
        # 精确命中题库冻结的 related_but_inaccurate 表:这一格由确定式规则定,
        # 不问 LLM。云判分对"蔬菜"这类回答并不稳定,一次判成正确或未识别就会
        # 把冻结的 close 一级提示换掉;命中冻结表本身已经是确定事实。
        # 这不改成功口径——contains_target 仍只认目标词/可接受表达。
        engine = None
    if engine is not None:
        boundary = cloud_processing.provider_boundary(engine)
        if boundary is cloud_processing.DataBoundary.UNKNOWN:
            raise RuntimeError("LLM provider 未声明 local/cloud 数据边界")
        if boundary is cloud_processing.DataBoundary.CLOUD and not cloud_llm_allowed:
            # 没有逐人有效授权时不外发回答文本；确定式规则仍可安全完成运行判类。
            engine = None
    llm_result = engine.judge(ji) if engine is not None else None
    if llm_result is not None:
        return {
            "answer_type": llm_result.answer_type.value,
            "ai_score": llm_result.ai_score,
            "needs_review": llm_result.ai_needs_review,
            "judge_mode": "LLM辅助",
            "judge_engine_version": engine.version,
            # Only a real string is normalised: a whitespace-only reason is the
            # same as none at all. Any other type is passed through completely
            # unchanged — not even ``or None`` — so a falsey invalid value such
            # as ``0`` or ``False`` reaches the closed validator and is rejected
            # there, instead of being silently coerced into a legal-looking NULL.
            "judge_reason": (llm_result.reason.strip() or None
                             if isinstance(llm_result.reason, str)
                             else llm_result.reason),
            "matched_on": None,
            "contains_target": contains,
            "judge_portrait_used": False,
            "truth_scope": "operational_only",
        }
    rule_result = rule_judge.judge_rule_based(ji)
    return {
        "answer_type": (rule_result.answer_type.value
                        if rule_result.answer_type else rule_result.interaction_state),
        "ai_score": rule_result.ai_score,
        "needs_review": rule_result.ai_needs_review,
        "judge_mode": "规则确定式",
        "judge_engine_version": "rule-1",
        "judge_reason": None,
        "matched_on": rule_result.matched_on,
        "contains_target": contains,
        "judge_portrait_used": rule_result.judge_portrait_used,
        "truth_scope": "operational_only",
    }


@app.post("/judge/classify")
def judge_classify(body: ClassifyIn):
    """无场次调试判类固定走本地规则；绝不从这个旁路把作答文本发到云端。"""
    try:
        return _classify_operational(
            item_id=body.item_id, response_role=body.response_role, text=body.text,
            allow_llm=False)
    except OperationalRubricUnavailable as exc:
        raise HTTPException(status_code=409, detail={
            "code": "operational_rubric_unavailable",
            "message": str(exc),
        }) from exc


class ConfirmIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    confirmed_response_text: str = PydanticField(max_length=2000)
    expected_revision: int = PydanticField(ge=0)
    idempotency_key: str = PydanticField(
        min_length=8, max_length=200,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{7,199}$")

    @model_validator(mode="after")
    def strip_and_require_text(self):
        self.confirmed_response_text = self.confirmed_response_text.strip()
        if not self.confirmed_response_text:
            raise ValueError("confirmed_response_text 去除空白后不得为空")
        return self


def _confirmation_text_sha256(text: str | None) -> str:
    # NULL 和空字符串必须是两个不同的研究状态；ConfirmIn 虽禁止空文本，
    # before 仍需要稳定表示“从未确认”。
    payload = b"\x00NULL" if text is None else b"\x01TEXT" + text.encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


@app.patch("/turns/{turn_id}/confirm", response_model=TurnEvent)
def confirm_turn(
        turn_id: int, body: ConfirmIn, request: Request,
        s: DBSession = Depends(get_session)):
    """用具名身份、CAS 和幂等键修订 confirmed，不覆盖 ASR 原文。"""
    actor = _require_account_identity(
        request, "修改研究确认回答", roles={"researcher", "admin"})
    idempotent = False
    with _LIVE_WRITE_LOCK:
        _live_row_for_update(s)
        t = _load_turn(turn_id, s)
        ie = s.get(ItemEvent, t.item_event_id)
        if not ie:
            raise HTTPException(409, "环节缺少可追溯的题目事件，禁止确认")
        sess = s.get(TrainSession, ie.session_id)
        if sess is None:
            raise HTTPException(409, "环节缺少可追溯的场次，禁止确认")
        _require_session_operator(
            request, sess, s, "修改研究确认回答", mutation=True,
            not_found_detail="环节不存在")
        _ensure_manual_plane_writable(
            ie.session_id, s, "在自动干预期间修改研究确认回答",
            allow_after_intervention=True)
        _ensure_post_intervention_review_writable(
            ie.session_id, s, "修改研究确认回答")
        existing = s.exec(select(TurnConfirmationRevision).where(
            TurnConfirmationRevision.idempotency_key == body.idempotency_key,
        ).with_for_update()).first()
        after_hash = _confirmation_text_sha256(body.confirmed_response_text)
        if existing is not None:
            exact_request = (
                existing.turn_id == turn_id
                and existing.session_id == ie.session_id
                and existing.expected_revision == body.expected_revision
                and existing.revision == body.expected_revision + 1
                and existing.actor_display_id == actor
                and existing.after_sha256 == after_hash
            )
            if not exact_request:
                raise HTTPException(status_code=409, detail={
                    "code": "turn_confirmation_idempotency_conflict",
                    "message": "同一幂等键对应了不同确认修订请求",
                })
            # 已有后续版本时不能把旧重放伪装成当前权威文本。
            if (t.confirmation_revision != existing.revision
                    or _confirmation_text_sha256(
                        t.confirmed_response_text) != existing.after_sha256):
                raise HTTPException(status_code=409, detail={
                    "code": "turn_confirmation_replay_superseded",
                    "message": "该幂等请求已被后续确认版本取代，请刷新后重试",
                    "current_revision": t.confirmation_revision,
                })
            idempotent = True
        else:
            if t.score_locked:
                raise HTTPException(409, "已锁分，不得再改 confirmed 文本")
            if body.expected_revision != t.confirmation_revision:
                raise HTTPException(status_code=409, detail={
                    "code": "turn_confirmation_revision_conflict",
                    "message": "人工确认文本已被其他操作更新，请刷新后重试",
                    "expected_revision": body.expected_revision,
                    "current_revision": t.confirmation_revision,
                })
            before_hash = _confirmation_text_sha256(t.confirmed_response_text)
            next_revision = t.confirmation_revision + 1
            t.confirmed_response_text = body.confirmed_response_text
            t.confirmation_revision = next_revision
            s.add(t)
            s.add(TurnConfirmationRevision(
                turn_id=turn_id,
                session_id=ie.session_id,
                revision=next_revision,
                expected_revision=body.expected_revision,
                actor_display_id=actor,
                changed_at=datetime.now(timezone.utc).replace(tzinfo=None),
                before_sha256=before_hash,
                after_sha256=after_hash,
                idempotency_key=body.idempotency_key,
            ))
            try:
                s.commit()
            except IntegrityError as exc:
                s.rollback()
                raise HTTPException(status_code=409, detail={
                    "code": "turn_confirmation_concurrency_conflict",
                    "message": "确认修订与另一并发操作冲突，请刷新后重试",
                }) from exc
            s.refresh(t)
    _audit(
        s, request, "response_confirm_replay" if idempotent else "response_confirm",
        f"confirmation_revision={t.confirmation_revision} idempotent={str(idempotent).lower()}",
        session_id=ie.session_id, turn_id=turn_id)
    return t


@app.post("/turns/{turn_id}/ai-judge", response_model=TurnEvent)
def ai_judge_turn(turn_id: int, request: Request,
                  s: DBSession = Depends(get_session)):
    """规则确定式 AI 初评（永不锁分）。经画像守卫构造 JudgeInput；无确定式口径的环节纯人工。"""
    with _LIVE_WRITE_LOCK:
        _live_row_for_update(s)
        t = _load_turn(turn_id, s)
        ie = s.get(ItemEvent, t.item_event_id)
        if not ie:
            raise HTTPException(409, "环节缺少可追溯的题目事件，禁止 AI 初评")
        sess = s.get(TrainSession, ie.session_id)
        if sess is None:
            raise HTTPException(409, "环节缺少关联场次")
        _require_session_operator(
            request, sess, s, "执行场次 AI 初评", mutation=True,
            not_found_detail="环节不存在")
        _ensure_manual_plane_writable(
            ie.session_id, s, "从旧研究者标签页触发旧 AI 初评")
    _ensure_runtime_writable(ie.session_id, s, "写入 AI 初评")
    if t.source_attempt_id is not None:
        source_attempt = s.get(AttemptEvent, t.source_attempt_id)
        audio = s.get(AudioAssetRow, t.raw_audio_id) if t.raw_audio_id else None
        if (source_attempt is None
                or source_attempt.processing_status != "completed"
                or source_attempt.session_id != sess.session_id
                or source_attempt.item_id != ie.item_id
                or source_attempt.turn_seq != t.turn_seq
                or source_attempt.response_role != (t.response_role or "")
                or source_attempt.raw_audio_id != (t.raw_audio_id or "")
                or source_attempt.is_simulation != sess.is_simulation
                or source_attempt.prompt_level != t.prompt_level
                or source_attempt.asr_text != t.asr_text
                or source_attempt.asr_confidence != t.asr_confidence
                or source_attempt.cue_type != t.cue_type
                or source_attempt.duration_seconds != t.duration_seconds
                or source_attempt.operational_answer_type != t.ai_answer_type
                or source_attempt.operational_score != t.ai_score
                or source_attempt.operational_needs_review != t.ai_needs_review
                or source_attempt.judge_mode != t.ai_judge_mode
                or source_attempt.judge_portrait_used
                or t.judge_portrait_used
                or audio is None
                or audio.session_id != sess.session_id
                or audio.turn_key != f"{ie.item_id}#{t.turn_seq}"
                or audio.is_simulation != sess.is_simulation
                or audio.data_classification != sess.data_classification):
            raise HTTPException(status_code=409, detail={
                "code": "authoritative_attempt_mismatch",
                "message": (
                    "TurnEvent、source attempt、录音与当前场次/题目/环节/"
                    "角色/提示/判定或数据边界不一致，拒绝返回旧 AI 初评"
                ),
            })
        _ensure_audio_read_allowed(audio, s)
        # 已由 authoritative attempt 收口；重试只读返回，绝不二次调用 AI。
        return t
    # 未绑定 AttemptEvent 的 TurnEvent 没有权威音频、提示上下文和处理状态。
    # 旧入口只允许读取已经由 authoritative attempt 收口的结果；任何环境变量
    # 都不能重新打开旧判分/云外发路径。
    raise HTTPException(status_code=409, detail={
        "code": "authoritative_attempt_required",
        "message": "未绑定权威 source attempt，旧 AI 初评路径永久关闭",
    })


class LockIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # Legacy clients may still send these three fields, but none is authoritative:
    # operator comes from authentication, prompt level from source AttemptEvent,
    # and reviewed_score must equal the one human research input element_value.
    reviewer_id: str | None = PydanticField(default=None, max_length=128)
    element_value: float                     # 单要素 final_correct(0/1) / 双要素分环节(0/1，关系0/0.5/1) / 多要素(0/1)
    reviewed_score: float | None = None       # legacy 兼容；提供时必须等于 element_value
    prompt_level: int | None = PydanticField(default=None, ge=0, le=3)


def _task_type_for_bank_item(bank: content.ItemBank, item_id: str) -> tuple[str, dict] | None:
    for task_type, rows in (("单要素", bank.single_element),
                            ("双要素", bank.double_element),
                            ("多要素", bank.multi_element)):
        for row in rows:
            if row.get("item_id") == item_id:
                return task_type, row
    return None


class AttemptProcessIn(BaseModel):
    """权威逐次处理输入：只接受冻结位置、音频引用和受控提示上下文。"""
    model_config = ConfigDict(extra="forbid")

    item_id: str = PydanticField(min_length=1, max_length=160)
    turn_seq: int = PydanticField(ge=1)
    response_role: str = PydanticField(min_length=1, max_length=64)
    raw_audio_id: str = PydanticField(
        min_length=1, max_length=160,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,159}$")
    prompt_level: int = PydanticField(ge=0, le=3)
    cue_type: str | None = PydanticField(
        default=None, min_length=1, max_length=64, pattern=r"^[^\r\n\x00]+$")
    duration_seconds: float | None = PydanticField(default=None, ge=0, le=600)


class InteractionAppendIn(BaseModel):
    """受控交互事件：没有任意 payload，所有值都按 event_type 建模。"""
    model_config = ConfigDict(extra="forbid")

    event_type: Literal[
        "cue_selected", "feedback_selected", "technical_pause", "researcher_takeover"
    ]
    item_id: str | None = PydanticField(default=None, min_length=1, max_length=160)
    turn_seq: int | None = PydanticField(default=None, ge=1)
    attempt_id: int | None = PydanticField(default=None, ge=1)
    prompt_level: int | None = PydanticField(default=None, ge=0, le=3)
    cue_type: str | None = PydanticField(
        default=None, min_length=1, max_length=64, pattern=r"^[^\r\n\x00]+$")
    feedback_key: str | None = PydanticField(
        default=None, min_length=1, max_length=64, pattern=r"^[A-Za-z0-9._-]+$")
    error_code: str | None = PydanticField(
        default=None, min_length=1, max_length=64, pattern=r"^[a-z0-9._-]+$")
    reason_code: str | None = PydanticField(
        default=None, min_length=1, max_length=64, pattern=r"^[a-z0-9._-]+$")

    @model_validator(mode="after")
    def event_specific_fields_only(self):
        common = {"event_type", "item_id", "turn_seq", "attempt_id"}
        specific = {
            "cue_selected": {"prompt_level", "cue_type"},
            "feedback_selected": {"feedback_key"},
            "technical_pause": {"error_code"},
            "researcher_takeover": {"reason_code"},
        }[self.event_type]
        disallowed = self.model_fields_set - common - specific
        if disallowed:
            raise ValueError(
                f"{self.event_type} 请求含不属于该事件的字段 {sorted(disallowed)}")
        return self


class InteractionPresentationCursorIn(RuntimeCursorIn):
    """CAS-bound cursor contract for one atomic bedside presentation."""

    wseq: int = PydanticField(ge=0)
    expected_revision: int = PydanticField(ge=0)
    # Output-only: successful feedback assigns command_wseq to fbSeq.
    fbSeq: None = None


class InteractionPresentationIn(BaseModel):
    """Atomically bind a selected cue/feedback fact to its bedside projection."""
    model_config = ConfigDict(extra="forbid")

    idempotency_key: str = PydanticField(
        min_length=16, max_length=200,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{15,199}$")
    interaction: InteractionAppendIn
    cursor: InteractionPresentationCursorIn

    @model_validator(mode="after")
    def presentation_events_only(self):
        if self.interaction.event_type not in {
                "cue_selected", "feedback_selected"}:
            raise ValueError(
                "只有 cue_selected/feedback_selected 可与床旁呈现原子提交")
        return self


class TechnicalPauseIn(BaseModel):
    """One CAS-bound, atomic researcher technical-stop command.

    The current frozen item/turn is derived from the locked LiveState cursor.
    Clients may identify an existing attempt, but may never choose a different
    position or split the evidence append from the runtime/device stop.
    """
    model_config = ConfigDict(extra="forbid")

    idempotency_key: str = PydanticField(
        min_length=16, max_length=200,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{15,199}$")
    expected_revision: int = PydanticField(ge=0)
    expected_live_wseq: int = PydanticField(ge=0)
    error_code: str = PydanticField(
        min_length=1, max_length=64, pattern=r"^[a-z0-9._-]+$")
    attempt_id: int | None = PydanticField(default=None, ge=1)


def _technical_pause_request_hash(
        session_id: str, body: TechnicalPauseIn) -> str:
    return evidence_ledger.technical_pause_request_hash(
        session_id,
        body.model_dump(exclude_none=True, mode="json"),
    )


def _technical_pause_active_position(
        sess: TrainSession, live: LiveState | None,
        state: SessionRuntimeState | None, body: TechnicalPauseIn,
        s: DBSession) -> tuple[runtime.PlanItem, runtime.PlanTurn]:
    """Validate the complete active CAS and derive its frozen position.

    Callers run this once as a cheap preflight and again after acquiring the
    authoritative runtime row in the global Session->Live->Autopilot->Runtime
    lock order.  The second check prevents a staged fence from ever
    committing against a state that changed after the preflight.
    """
    if live is None or _live_session_id(live) != sess.session_id:
        raise HTTPException(status_code=409, detail={
            "code": "technical_pause_live_session_changed",
            "message": "当前床旁场次已切换；技术证据与暂停均未提交",
        })
    if state is None:
        raise HTTPException(status_code=409, detail={
            "code": "technical_pause_runtime_snapshot_missing",
            "message": "场次尚无可校验的运行快照；技术证据与暂停均未提交",
        })
    if state.status != "active":
        raise HTTPException(status_code=409, detail={
            "code": "technical_pause_runtime_inactive",
            "message": f"场次状态为 {state.status}；不得追加新技术暂停事实",
        })
    if state.revision != body.expected_revision:
        raise HTTPException(status_code=409, detail={
            "code": "technical_pause_revision_changed",
            "message": "场次运行修订已变化；技术证据与暂停均未提交",
            "current_revision": state.revision,
        })

    live_cursor = _json_load(live.cursor_json)
    runtime_cursor = _json_load(state.cursor_json)
    live_wseq = _wseq_from(live_cursor)
    runtime_wseq = _wseq_from(runtime_cursor)
    if live_wseq != body.expected_live_wseq:
        raise HTTPException(status_code=409, detail={
            "code": "technical_pause_wseq_changed",
            "message": "床旁命令序列已变化；技术证据与暂停均未提交",
            "current_wseq": live_wseq,
        })
    canonical_live_cursor = (
        _safe_cursor(live_cursor) if live_cursor is not None else None)
    if (live_cursor is None or runtime_cursor is None
            or _payload_session_id(live_cursor) != sess.session_id
            or _payload_session_id(runtime_cursor) != sess.session_id
            or runtime_wseq != live_wseq
            or runtime_cursor != canonical_live_cursor):
        raise HTTPException(status_code=409, detail={
            "code": "technical_pause_snapshot_diverged",
            "message": "运行态与床旁快照不一致；技术证据与暂停均未提交",
        })
    if live_cursor.get("screen") in {"paused", "done"}:
        raise HTTPException(status_code=409, detail={
            "code": "technical_pause_cursor_inactive",
            "message": "当前床旁游标已停止或结束；不得追加新技术暂停事实",
        })
    _validate_cursor(sess, live_cursor, s)
    plan = _session_plan_for_runtime(sess)
    return (
        plan.items[live_cursor["itemIdx"]],
        plan.items[live_cursor["itemIdx"]].turns[live_cursor["turnIdx"]],
    )


def _technical_pause_receipt_payload(
        receipt: TechnicalPauseReceipt, event: InteractionEvent,
        body: TechnicalPauseIn, state: SessionRuntimeState,
        *, idempotent: bool) -> dict:
    cursor = _json_load(receipt.cursor_json)
    try:
        event_payload = evidence_ledger.validate_stored_payload(
            event.event_type, event.payload_json)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail={
            "code": "technical_pause_receipt_corrupt",
            "message": "原子技术暂停回执的交互证据已损坏",
        }) from exc
    if (cursor is None
            or event.id != receipt.interaction_event_id
            or event.session_id != receipt.session_id
            or event.event_type != "technical_pause"
            or event.attempt_id != body.attempt_id
            or event_payload.get("error_code") != body.error_code
            or _payload_session_id(cursor) != receipt.session_id
            or cursor.get("screen") != "paused"
            or cursor.get("recording") != "stopped"
            or _wseq_from(cursor) != receipt.paused_cursor_wseq
            or state.session_id != receipt.session_id
            or state.revision != receipt.runtime_revision
            or state.status != "paused"):
        raise HTTPException(status_code=409, detail={
            "code": "technical_pause_receipt_corrupt",
            "message": "原子技术暂停回执与证据或暂停状态不一致",
        })
    return {
        "interaction": event,
        "runtime": _runtime_payload(receipt.session_id, state),
        "cursor": cursor,
        "seq": receipt.live_seq,
        "wseq": receipt.paused_cursor_wseq,
        "runtimeRevision": receipt.runtime_revision,
        "idempotent": idempotent,
    }


def _technical_pause_replay(
        s: DBSession, *, session_id: str, body: TechnicalPauseIn,
        request_hash: str) -> dict | None:
    live = _live_row_for_update(s)
    receipt = s.exec(select(TechnicalPauseReceipt).where(
        TechnicalPauseReceipt.session_id == session_id,
        TechnicalPauseReceipt.idempotency_key == body.idempotency_key,
    ).with_for_update()).first()
    if receipt is None:
        return None
    if receipt.request_hash != request_hash:
        raise HTTPException(status_code=409, detail={
            "code": "technical_pause_idempotency_conflict",
            "message": "同一幂等键已绑定另一份技术暂停请求",
        })
    event = s.get(InteractionEvent, receipt.interaction_event_id)
    state = s.exec(select(SessionRuntimeState).where(
        SessionRuntimeState.session_id == session_id,
    ).with_for_update()).first()
    stored_cursor = _json_load(receipt.cursor_json)
    current_cursor = _json_load(live.cursor_json) if live is not None else None
    session_projection = _json_load(live.session_json) if live is not None else None
    if event is None or state is None or stored_cursor is None:
        raise HTTPException(status_code=409, detail={
            "code": "technical_pause_receipt_corrupt",
            "message": "原子技术暂停回执缺少对应证据或状态",
        })
    if (live is None
            or _live_session_id(live) != session_id
            or live.seq != receipt.live_seq
            or current_cursor != stored_cursor
            or _wseq_from(current_cursor) != receipt.paused_cursor_wseq
            or state.revision != receipt.runtime_revision
            or state.status != "paused"
            or not session_projection
            or session_projection.get("paused") is not True):
        raise HTTPException(status_code=409, detail={
            "code": "technical_pause_replay_superseded",
            "message": "该技术暂停回执已被后续场次或床旁状态取代，不得重放旧停止快照",
            "current_wseq": _wseq_from(current_cursor),
            "current_revision": state.revision,
            "current_status": state.status,
        })
    return _technical_pause_receipt_payload(
        receipt, event, body, state, idempotent=True)


def _interaction_presentation_cursor_and_hash(
        session_id: str, body: InteractionPresentationIn) -> tuple[dict, str]:
    """Return one canonical request cursor and its stable semantic digest."""
    cursor = body.cursor.model_dump(exclude_none=True, mode="json")
    supplied_session_id = _payload_session_id(cursor)
    if supplied_session_id and supplied_session_id != session_id:
        raise HTTPException(409, "呈现游标与路径场次不一致")
    cursor["sessionId"] = session_id
    cursor.pop("session_id", None)
    canonical = {
        "schema_version": 1,
        "session_id": session_id,
        "interaction": body.interaction.model_dump(
            exclude_none=True, mode="json"),
        "cursor": cursor,
    }
    encoded = _json.dumps(
        canonical, ensure_ascii=False, sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return cursor, hashlib.sha256(encoded).hexdigest()


def _interaction_presentation_receipt_payload(
        receipt: InteractionPresentationReceipt,
        event: InteractionEvent,
        *, idempotent: bool) -> dict:
    cursor = _json_load(receipt.cursor_json)
    if (cursor is None
            or event.id != receipt.interaction_event_id
            or event.session_id != receipt.session_id
            or _payload_session_id(cursor) != receipt.session_id
            or _wseq_from(cursor) != receipt.command_wseq):
        raise HTTPException(status_code=409, detail={
            "code": "interaction_presentation_receipt_corrupt",
            "message": "原子呈现回执与交互证据不一致",
        })
    return {
        "interaction": event,
        "cursor": cursor,
        "seq": receipt.live_seq,
        "wseq": receipt.command_wseq,
        "runtimeRevision": receipt.runtime_revision,
        "idempotent": idempotent,
    }


def _interaction_presentation_replay(
        s: DBSession, *, session_id: str, idempotency_key: str,
        request_hash: str) -> dict | None:
    # Keep the same LiveState -> evidence/receipt lock order as every fresh
    # presentation write, including the post-rollback race-recovery path.
    live = _live_row_for_update(s)
    receipt = s.exec(select(InteractionPresentationReceipt).where(
        InteractionPresentationReceipt.session_id == session_id,
        InteractionPresentationReceipt.idempotency_key == idempotency_key,
    ).with_for_update()).first()
    if receipt is None:
        return None
    if receipt.request_hash != request_hash:
        raise HTTPException(status_code=409, detail={
            "code": "interaction_presentation_idempotency_conflict",
            "message": "同一幂等键已绑定另一份提示/反馈呈现请求",
        })
    event = s.get(InteractionEvent, receipt.interaction_event_id)
    if event is None:
        raise HTTPException(status_code=409, detail={
            "code": "interaction_presentation_receipt_corrupt",
            "message": "原子呈现回执缺少对应交互证据",
        })
    # A receipt is an exact retry result, not a command that may resurrect an
    # obsolete bedside screen.  Once any later command/pause has replaced the
    # receipt's two authoritative clocks, keep the evidence but fail closed
    # instead of returning a cursor that a client could rebroadcast.
    current_cursor = _json_load(live.cursor_json) if live is not None else None
    state = s.get(SessionRuntimeState, session_id)
    current_wseq = _wseq_from(current_cursor)
    current_revision = state.revision if state is not None else None
    current_status = state.status if state is not None else None
    if (live is None
            or _live_session_id(live) != session_id
            or current_wseq != receipt.command_wseq
            or state is None
            or current_revision != receipt.runtime_revision
            or current_status != "active"):
        raise HTTPException(status_code=409, detail={
            "code": "interaction_presentation_replay_superseded",
            "message": "该呈现回执已被更新的床旁或场次状态取代，不得重放旧游标",
            "current_wseq": current_wseq,
            "current_revision": current_revision,
            "current_status": current_status,
        })
    return _interaction_presentation_receipt_payload(
        receipt, event, idempotent=True)


def _frozen_plan_turn(sess: TrainSession, item_id: str, turn_seq: int,
                      response_role: str | None = None) -> tuple[runtime.PlanItem, runtime.PlanTurn]:
    plan = _session_plan_for_runtime(sess)
    item = next((candidate for candidate in plan.items if candidate.item_id == item_id), None)
    if item is None:
        raise HTTPException(422, "item_id 不属于场次绑定的冻结计划")
    turn = next((candidate for candidate in item.turns
                 if candidate.turn_seq == turn_seq), None)
    if turn is None:
        raise HTTPException(422, "turn_seq 不属于该题冻结计划")
    if response_role is not None and response_role != turn.response_role:
        raise HTTPException(409, "response_role 与冻结计划不一致")
    return item, turn


def _lock_evidence_session(session_id: str, s: DBSession) -> None:
    """在支持行锁的数据库中串行化同场次序号分配；SQLite 仍由唯一约束兜底。"""
    s.exec(select(TrainSession).where(
        TrainSession.session_id == session_id).with_for_update()).first()


def _next_attempt_seq(session_id: str, item_id: str, turn_seq: int,
                      s: DBSession) -> int:
    current = s.exec(select(func.max(AttemptEvent.attempt_seq)).where(
        AttemptEvent.session_id == session_id,
        AttemptEvent.item_id == item_id,
        AttemptEvent.turn_seq == turn_seq,
    )).one()
    return int(current or 0) + 1


def _append_interaction(s: DBSession, sess: TrainSession, event_type: str,
                        payload: dict, *, item_id: str | None = None,
                        turn_seq: int | None = None,
                        attempt: AttemptEvent | None = None) -> InteractionEvent:
    encoded = evidence_ledger.encode_event_payload(event_type, payload)
    current = s.exec(select(func.max(InteractionEvent.event_seq)).where(
        InteractionEvent.session_id == sess.session_id)).one()
    row = InteractionEvent(
        session_id=sess.session_id,
        event_seq=int(current or 0) + 1,
        item_id=item_id,
        turn_seq=turn_seq,
        attempt_id=attempt.id if attempt else None,
        attempt_seq=attempt.attempt_seq if attempt else None,
        event_type=event_type,
        payload_json=encoded,
        created_at=datetime.now(),
        is_simulation=sess.is_simulation,
    )
    s.add(row)
    s.flush()
    return row


def _attempt_rows_payload(attempt: AttemptEvent, s: DBSession,
                          *, idempotent: bool) -> dict:
    interactions = list(s.exec(select(InteractionEvent).where(
        InteractionEvent.attempt_id == attempt.id).order_by(InteractionEvent.event_seq)))
    return {
        "status": attempt.processing_status,
        "idempotent": idempotent,
        "in_progress": False,
        "truth_scope": "operational_only",
        # 与只读账本共用显式投影；worker fencing 字段和未来新增内部列均不下发。
        "attempt": _research_attempt_projection(attempt),
        "interactions": interactions,
    }


def _attempt_in_progress_payload(attempt: AttemptEvent, s: DBSession,
                                 response: Response) -> dict:
    now = datetime.now()
    lease = attempt.processing_lease_expires_at
    retry_after = max(1, math.ceil((lease - now).total_seconds())) if lease else 1
    response.status_code = 202
    response.headers["Retry-After"] = str(retry_after)
    payload = _attempt_rows_payload(attempt, s, idempotent=True)
    payload.update({"in_progress": True, "retry_after_seconds": retry_after})
    return payload


def _claim_lost_payload(claim: evidence_ledger.AttemptClaim, s: DBSession,
                        response: Response, *,
                        control_plane: Literal["manual_http", "autopilot_worker"],
                        ) -> dict:
    """A newer generation or terminal transition won; stale workers only read back.

    ``claim_lost=True`` marks that *this* invocation held a live claim and lost
    the race — as opposed to a fresh recovery admission finding an already-
    terminal row with no race at all. Only the former must never route: the
    completion is provably someone else's, possibly still mid-route itself.
    This is an internal-only signal for :func:`_run_p0a_attempt_worker`'s own
    dispatch and must never appear in a real HTTP response — it is set only
    on the ``autopilot_worker`` plane, whose payload never reaches a client.
    """
    s.rollback()
    current = s.get(AttemptEvent, claim.attempt_id)
    if current is None:
        raise HTTPException(409, "attempt claim 已失效且证据行不存在")
    if current.processing_status in evidence_ledger.TERMINAL_ATTEMPT_STATUSES:
        payload = _attempt_rows_payload(current, s, idempotent=True)
        if control_plane == "autopilot_worker":
            payload["claim_lost"] = True
        return payload
    return _attempt_in_progress_payload(current, s, response)


def _try_recovery_claim(attempt: AttemptEvent, s: DBSession,
                        response: Response) -> tuple[
                            AttemptEvent | None, evidence_ledger.AttemptClaim | None, dict | None]:
    """Claim an abandoned stage, or return the current worker/terminal response."""
    if attempt.processing_status in evidence_ledger.TERMINAL_ATTEMPT_STATUSES:
        return None, None, _attempt_rows_payload(attempt, s, idempotent=True)
    if attempt.processing_status not in evidence_ledger.RECOVERABLE_ATTEMPT_STATUSES:
        raise HTTPException(409, f"attempt 处理阶段异常: {attempt.processing_status}")
    if evidence_ledger.has_active_lease(attempt):
        return None, None, _attempt_in_progress_payload(attempt, s, response)

    owner = evidence_ledger.new_claim_owner()
    if evidence_ledger.try_claim_attempt(s, attempt.id, owner=owner):
        s.commit()
        claimed = s.get(AttemptEvent, attempt.id)
        if claimed is None:
            raise HTTPException(409, "attempt 在接管后丢失")
        return claimed, evidence_ledger.claim_from_attempt(claimed), None

    # 条件 UPDATE 未命中：另一 worker 已续租/接管，或已进入终态。
    s.rollback()
    current = s.get(AttemptEvent, attempt.id)
    if current is None:
        raise HTTPException(409, "attempt 并发接管后证据行不存在")
    if current.processing_status in evidence_ledger.TERMINAL_ATTEMPT_STATUSES:
        return None, None, _attempt_rows_payload(current, s, idempotent=True)
    return None, None, _attempt_in_progress_payload(current, s, response)


def _existing_attempt_for_audio(session_id: str, body: AttemptProcessIn,
                                s: DBSession) -> AttemptEvent | None:
    existing = s.exec(select(AttemptEvent).where(
        AttemptEvent.raw_audio_id == body.raw_audio_id)).first()
    if existing is None:
        return None
    requested = (session_id, body.item_id, body.turn_seq, body.response_role,
                 body.prompt_level, body.cue_type, body.duration_seconds)
    persisted = (existing.session_id, existing.item_id, existing.turn_seq,
                 existing.response_role, existing.prompt_level, existing.cue_type,
                 existing.duration_seconds)
    if requested != persisted:
        raise HTTPException(409, "raw_audio_id 已绑定另一个不同的逐次处理请求")
    return existing


class _CloudEgressNotAuthorized(RuntimeError):
    """The final, serialized authorization read refused a cloud provider call."""

    def __init__(self, issues: list[str]):
        self.issues = tuple(issues)
        super().__init__("；".join(issues) or "云处理未获授权")


@contextmanager
def _serialized_cloud_provider_call(
        *, session_id: str, patient_id: str, provider: object,
        bind: object):
    """Hold the subject egress fence across the actual provider invocation.

    The in-process subject lock covers SQLite/single-process deployments.  The
    locked Session -> Patient rows provide the same total order across workers on
    databases that implement ``FOR UPDATE``.  Consent revocation and research
    withdrawal acquire the identical subject lock before their own governance
    transaction, so after either endpoint returns 200 no stale request can begin
    sending patient audio or text to a cloud provider.
    """
    if cloud_processing.provider_boundary(provider) is not cloud_processing.DataBoundary.CLOUD:
        raise ValueError("serialized cloud provider fence 只能用于 cloud provider")
    with cloud_processing.serialized_subject_egress(patient_id):
        with DBSession(bind) as gate_session:
            current_session = gate_session.exec(select(TrainSession).where(
                TrainSession.session_id == session_id,
            ).with_for_update()).first()
            if current_session is None or current_session.patient_id != patient_id:
                raise _CloudEgressNotAuthorized(["场次与受试者绑定已失效"])
            patient = gate_session.exec(select(Patient).where(
                Patient.patient_id == patient_id,
            ).with_for_update()).first()
            if patient is None:
                raise _CloudEgressNotAuthorized(["受试者档案不存在"])
            issues = cloud_processing.authorization_issues(patient, provider)
            if issues:
                raise _CloudEgressNotAuthorized(issues)
            try:
                yield
            finally:
                # Release the cross-worker row locks only after the provider call
                # has returned (successfully or with an exception).
                gate_session.rollback()


def _cloud_processing_revoked_for_session(session_id: str, s: DBSession) -> bool:
    s.expire_all()
    current_sess = s.get(TrainSession, session_id)
    patient = s.get(Patient, current_sess.patient_id) if current_sess else None
    return bool(patient and (
        patient.cloud_processing_allowed is False
        or patient.cloud_processing_revoked_at is not None
    ))


def _revalidate_attempt_commit_fence(
        session_id: str, body: AttemptProcessIn, s: DBSession, *,
        control_plane: Literal["manual_http", "autopilot_worker"],
        worker_target: autopilot_orchestration.FrozenWorkerTarget | None = None,
) -> TrainSession:
    """Re-check ownership and mutable eligibility in the evidence commit txn.

    Provider calls deliberately run without database/control locks.  Their result
    may be persisted only after this helper reacquires the same LiveState ->
    TrainSession -> autopilot-state order used by start/pause and proves that the
    exact audio is still readable.  A pause wins by invalidating the attempt claim;
    withdrawal/quarantine/feature disablement wins here before any provider-derived
    text or judgement is written.  On the ``autopilot_worker`` plane, every commit
    point re-verifies the same frozen ``worker_target`` (session, exact command,
    raw audio, both generations) within this same lock — not just ``body`` — so a
    forged or lagged target can never ride a coincidentally-matching body through.
    """
    s.rollback()
    s.expire_all()
    _live_row_for_update(s)
    sess = s.exec(select(TrainSession).where(
        TrainSession.session_id == session_id,
    ).with_for_update()).first()
    if sess is None:
        raise HTTPException(404, "场次不存在")

    if control_plane == "manual_http":
        _ensure_manual_plane_writable(
            session_id, s, "提交旧人工 attempt 处理结果")
    elif control_plane == "autopilot_worker":
        if worker_target is None:
            raise autopilot_orchestration.AutopilotOrchestrationError(
                "autopilot_attempt_input_mismatch",
                "内部 worker 缺少冻结目标，拒绝提交",
            )
        # Lock the current state before re-deriving every capture/control fact.
        s.exec(select(SessionAutopilotState).where(
            SessionAutopilotState.session_id == session_id,
        ).with_for_update()).first()
        try:
            derived = autopilot_orchestration.derive_authoritative_attempt_input(
                s, session_id=session_id)
        except autopilot_orchestration.AutopilotOrchestrationError:
            raise
        if body.model_dump(mode="python") != derived.model_dump(mode="python"):
            raise autopilot_orchestration.AutopilotOrchestrationError(
                "autopilot_attempt_input_mismatch",
                "内部 worker 输入与当前权威录音证据不一致",
            )
        current_target = autopilot_orchestration.derive_worker_target(
            s, session_id=session_id)
        if current_target != worker_target:
            raise autopilot_orchestration.AutopilotOrchestrationError(
                "autopilot_attempt_input_mismatch",
                "内部 worker 目标与当前权威录音证据不一致",
            )
    else:  # pragma: no cover - Literal callers, defensive privilege boundary
        raise RuntimeError(f"unsupported attempt control plane: {control_plane!r}")

    asset = s.exec(select(AudioAssetRow).where(
        AudioAssetRow.raw_audio_id == body.raw_audio_id,
    ).with_for_update()).first()
    if asset is None:
        raise HTTPException(404, "音频资产不存在")
    try:
        _require_classified_session(sess)
        if asset.session_id != session_id:
            raise HTTPException(409, "音频资产不属于该场次")
        if asset.turn_key != f"{body.item_id}#{body.turn_seq}":
            raise HTTPException(409, "音频 turn_key 与冻结计划位置不一致")
        _ensure_audio_read_allowed(asset, s)
        _frozen_plan_turn(sess, body.item_id, body.turn_seq, body.response_role)
        if asset.is_simulation != sess.is_simulation:
            raise HTTPException(409, "音频与场次的真实/模拟属性不一致")
        if asset.data_classification != sess.data_classification:
            raise HTTPException(409, "音频与场次 data_classification 不一致")
        _ensure_recording_allowed_for_session(
            session_id, s, is_simulation=asset.is_simulation)
        runtime_state = _ensure_runtime_writable(
            session_id, s, "提交 attempt 处理结果")
        if runtime_state is not None and runtime_state.status != "active":
            raise HTTPException(409, "场次已暂停，丢弃延迟的 attempt 处理结果")
    except HTTPException as exc:
        if control_plane == "autopilot_worker":
            raise autopilot_orchestration.AutopilotOrchestrationError(
                "autopilot_attempt_commit_fence_lost",
                "attempt 提交前的控制或资格门禁已变化",
            ) from exc
        raise
    return sess


def _finish_attempt_failure(claim: evidence_ledger.AttemptClaim, sess: TrainSession,
                            s: DBSession, response: Response,
                            *, stage: Literal["asr", "judgement"], error_code: str,
                            engine_version: str | None,
                            body: AttemptProcessIn,
                            control_plane: Literal["manual_http", "autopilot_worker"],
                            worker_target:
                                autopilot_orchestration.FrozenWorkerTarget | None = None,
                            ) -> dict:
    # 失败证据与权威暂停是一个服务端事务：客户端即使在收到响应前断线，
    # 也不会留下“technical_pause 已记账，但 runtime 仍 active”的危险半状态。
    with _LIVE_WRITE_LOCK:
        sess = _revalidate_attempt_commit_fence(
            sess.session_id, body, s, control_plane=control_plane,
            worker_target=worker_target)
        _lock_evidence_session(sess.session_id, s)
        expected_stage = "received" if stage == "asr" else "asr_completed"
        if claim.stage != expected_stage:
            raise RuntimeError(
                f"failure stage {stage} does not match claim stage {claim.stage}")
        values = {"error_code": error_code, "processed_at": datetime.now()}
        if stage == "asr":
            values["asr_engine_version"] = engine_version
        else:
            values["judge_engine_version"] = engine_version
        if not evidence_ledger.fenced_attempt_update(
                s, claim, expected_status=expected_stage,
                next_status="technical_failure", values=values):
            return _claim_lost_payload(claim, s, response, control_plane=control_plane)
        s.expire_all()
        attempt = s.get(AttemptEvent, claim.attempt_id)
        if attempt is None:
            raise RuntimeError("fenced technical failure lost its attempt row")
        if stage == "asr":
            _append_interaction(s, sess, "asr_failed", {
                "asr_engine_version": engine_version,
                "error_code": error_code,
            }, item_id=attempt.item_id, turn_seq=attempt.turn_seq, attempt=attempt)
        else:
            attempt.judge_engine_version = engine_version
            _append_interaction(s, sess, "judgement_failed", {
                "judge_engine_version": engine_version,
                "error_code": error_code,
            }, item_id=attempt.item_id, turn_seq=attempt.turn_seq, attempt=attempt)
        _append_interaction(s, sess, "technical_pause", {"error_code": error_code},
                            item_id=attempt.item_id, turn_seq=attempt.turn_seq,
                            attempt=attempt)
        # For P0a this stages the control-state pause and stable failure event in
        # this same transaction. Ordinary/manual sessions deliberately no-op.
        staged_autopilot_pause = autopilot_orchestration.stage_processing_failure(
            s,
            session_id=sess.session_id,
            error_code=error_code,
            source="attempt_processing",
            target=worker_target,
        )
        if control_plane == "autopilot_worker" and not staged_autopilot_pause:
            s.rollback()
            raise autopilot_orchestration.AutopilotOrchestrationError(
                "autopilot_attempt_commit_fence_lost",
                "attempt 失败提交时自动驾驶所有权已变化",
            )
        _pause_runtime_in_transaction(sess.session_id, s)
        s.commit()
        s.refresh(attempt)
    return _attempt_rows_payload(attempt, s, idempotent=False)


def _record_asr_success(claim: evidence_ledger.AttemptClaim, sess: TrainSession,
                        s: DBSession, asr_result: asr.AsrResult, *,
                        body: AttemptProcessIn,
                        control_plane: Literal["manual_http", "autopilot_worker"],
                        worker_target:
                            autopilot_orchestration.FrozenWorkerTarget | None = None,
                        ) -> AttemptEvent | None:
    with _LIVE_WRITE_LOCK:
        sess = _revalidate_attempt_commit_fence(
            sess.session_id, body, s, control_plane=control_plane,
            worker_target=worker_target)
        _lock_evidence_session(sess.session_id, s)
        if not evidence_ledger.fenced_attempt_update(
                s, claim, expected_status="received", next_status="asr_completed",
                values={
                    "asr_text": asr_result.asr_text,
                    "asr_confidence": asr_result.asr_confidence,
                    "asr_engine_version": asr_result.engine_version,
                }):
            s.rollback()
            return None
        s.expire_all()
        attempt = s.get(AttemptEvent, claim.attempt_id)
        if attempt is None:
            raise RuntimeError("fenced ASR transition lost its attempt row")
        _append_interaction(s, sess, "asr_completed", {
            "asr_engine_version": asr_result.engine_version,
            "asr_confidence": asr_result.asr_confidence,
            "degraded": False,
            "hotword_hit": asr_result.hotword_hit,
        }, item_id=attempt.item_id, turn_seq=attempt.turn_seq, attempt=attempt)
        s.commit()
        s.refresh(attempt)
        return attempt


def _record_judgement_success(claim: evidence_ledger.AttemptClaim,
                              sess: TrainSession, s: DBSession,
                              result: dict, *, body: AttemptProcessIn,
                              bank: content.ItemBank,
                              control_plane: Literal[
                                  "manual_http", "autopilot_worker"],
                              worker_target:
                                  autopilot_orchestration.FrozenWorkerTarget
                                  | None = None,
                              ) -> AttemptEvent | None:
    with _LIVE_WRITE_LOCK:
        sess = _revalidate_attempt_commit_fence(
            sess.session_id, body, s, control_plane=control_plane,
            worker_target=worker_target)
        _lock_evidence_session(sess.session_id, s)
        if not evidence_ledger.fenced_attempt_update(
                s, claim, expected_status="asr_completed", next_status="completed",
                values={
                    "operational_answer_type": result["answer_type"],
                    "operational_score": result["ai_score"],
                    "operational_needs_review": result["needs_review"],
                    "judge_mode": result["judge_mode"],
                    "judge_engine_version": result["judge_engine_version"],
                    "judge_reason": result["judge_reason"],
                    "matched_on": result["matched_on"],
                    "contains_target": result["contains_target"],
                    "judge_portrait_used": result["judge_portrait_used"],
                    "error_code": None,
                    "processed_at": datetime.now(),
                }):
            s.rollback()
            return None
        s.expire_all()
        attempt = s.get(AttemptEvent, claim.attempt_id)
        if attempt is None:
            raise RuntimeError("fenced judgement transition lost its attempt row")
        _append_interaction(s, sess, "judgement_completed", {
            "answer_type": result["answer_type"],
            "score": result["ai_score"],
            "needs_review": result["needs_review"],
            "judge_mode": result["judge_mode"],
            "judge_engine_version": result["judge_engine_version"],
            "matched_on": result["matched_on"],
            "contains_target": result["contains_target"],
            "truth_scope": "operational_only",
        }, item_id=attempt.item_id, turn_seq=attempt.turn_seq, attempt=attempt)
        if control_plane == "autopilot_worker":
            # A terminal autonomous judgement and its review-ledger projection are
            # one transaction.  The projection is operational-only: it binds the
            # frozen plan/capture receipt but deliberately leaves confirmed text
            # empty and score_locked=false for later independent research review.
            autopilot_service.materialize_terminal_attempt_evidence(
                s,
                session_id=sess.session_id,
                attempt_id=attempt.id,
                bank=bank,
            )
        s.commit()
        s.refresh(attempt)
        return attempt


def _capture_in_progress_payload(capture: AttemptCaptureProcessing, s: DBSession,
                                 response: Response) -> dict:
    """Pre-attempt analog of :func:`_attempt_in_progress_payload`.

    No AttemptEvent exists yet at this stage. This shape only has to satisfy
    ``_run_p0a_attempt_worker``'s internal dispatch, which checks
    ``in_progress`` before ever looking at ``status``/``attempt`` — the
    ``autopilot_worker`` control plane never returns this to a real HTTP
    client.
    """
    now = datetime.now()
    lease = capture.processing_lease_expires_at
    retry_after = max(1, math.ceil((lease - now).total_seconds())) if lease else 1
    response.status_code = 202
    response.headers["Retry-After"] = str(retry_after)
    return {
        "status": capture.processing_status,
        "idempotent": True,
        "in_progress": True,
        "retry_after_seconds": retry_after,
        "truth_scope": "operational_only",
        "attempt": None,
        "interactions": [],
    }


def _verify_capture_attempt_identity(
        capture: AttemptCaptureProcessing, attempt: AttemptEvent) -> None:
    """A terminal capture's FK must agree with its bound Attempt on every fact.

    ``final_attempt_id`` is trusted only after this check; a mismatch is an
    invariant violation, not something to project to any caller.
    """
    matches = (
        capture.session_id == attempt.session_id,
        capture.raw_audio_id == attempt.raw_audio_id,
        capture.item_id == attempt.item_id,
        capture.turn_seq == attempt.turn_seq,
        capture.proof_attempt_seq == attempt.attempt_seq,
        capture.proof_prompt_level == attempt.prompt_level,
        capture.is_simulation == attempt.is_simulation,
    )
    if not all(matches):
        raise RuntimeError(
            "capture-processing row and its bound AttemptEvent disagree on "
            "identity; refusing to project a mismatched result")


_REPEAT_DISPOSITIONS = frozenset({"repeat_replayed", "repeat_limit_paused"})
# Internal worker dispatch markers.  The capture-claim path exists only on the
# ``autopilot_worker`` control plane, so these never reach an HTTP client, and
# they deliberately carry no transcript, command id or capture id.
_REPEAT_TERMINAL_STATUSES = frozenset(_REPEAT_DISPOSITIONS)
# A lost race for the capture claim is observation-only, never a fail-close.
_REPEAT_CLAIM_LOST_CODES = frozenset({
    "autopilot_repeat_capture_cas_conflict",
    "autopilot_repeat_route_cas_conflict",
    "autopilot_repeat_not_current",
})
# A missing, drifted or unresolvable repeat activation is a configuration
# rejection, not a runtime technical failure: the worker must leave the scope
# exactly as it found it instead of consuming the safe-pause path, which exists
# for provider/device failures. A researcher recreates the plan/session instead.
_NON_PAUSING_WORKER_CODES = frozenset({
    "autopilot_repeat_binding_missing",
    "autopilot_repeat_binding_incomplete",
    "autopilot_repeat_protocol_unavailable",
    "autopilot_command_repeat_binding_mismatch",
})
# The single modern-gate refusal a genuinely unbound pre-protocol session
# produces.  Every other refusal — feature flag, content digest, device,
# capability, generation, consent, runtime — must keep failing closed rather
# than being answered with an internal legacy recovery submission.
_LEGACY_RECOVERY_GATE_CODES = frozenset({"autopilot_repeat_binding_missing"})


def _capture_repeat_terminal_payload(
        capture: AttemptCaptureProcessing, s: DBSession) -> dict:
    """Verify a committed repeat outcome and return its internal marker.

    A repeat capture never has an AttemptEvent, so the ordinary attempt
    projection cannot describe it.  Both outcomes are re-proved against their
    own frozen evidence chain before any worker treats them as terminal.
    """
    repeat_evidence.verify_repeat_capture(s, capture)
    return {"status": capture.disposition, "in_progress": False}


def _capture_terminal_attempt_payload(
        capture: AttemptCaptureProcessing, s: DBSession,
        response: Response) -> dict:
    """Project the outcome a terminal capture row already committed.

    An answer candidate or technical failure resolves through its bound
    AttemptEvent; the capture row itself is terminal as soon as ASR resolves,
    but that Attempt may still be non-terminal (``asr_completed``, awaiting
    judgement).  A fenced-out worker reading this must observe a quiet
    in-progress outcome in that case, not a false terminal projection —
    otherwise a stale worker could trigger a spurious fail-close against a
    scope a newer generation still legitimately owns.  An explicit-repeat
    disposition has no Attempt at all and is proved against its own ledger.
    """
    if capture.disposition in _REPEAT_DISPOSITIONS:
        return _capture_repeat_terminal_payload(capture, s)
    if capture.final_attempt_id is None:
        raise RuntimeError("capture reached a terminal status without a bound attempt")
    attempt = s.get(AttemptEvent, capture.final_attempt_id)
    if attempt is None:
        raise RuntimeError("capture's bound attempt row is missing")
    _verify_capture_attempt_identity(capture, attempt)
    if attempt.processing_status in evidence_ledger.RECOVERABLE_ATTEMPT_STATUSES:
        return _attempt_in_progress_payload(attempt, s, response)
    return _attempt_rows_payload(attempt, s, idempotent=True)


def _try_recovery_capture_claim(
        capture: AttemptCaptureProcessing, s: DBSession,
        response: Response) -> tuple[
            AttemptCaptureProcessing | None,
            evidence_ledger.CaptureClaim | None, dict | None]:
    """Claim an abandoned capture lease, or return the current worker/terminal result.

    Mirrors :func:`_try_recovery_claim` one layer earlier: before an
    AttemptEvent (and its attempt_seq) exists at all.
    """
    if capture.processing_status in evidence_ledger.TERMINAL_CAPTURE_STATUSES:
        return None, None, _capture_terminal_attempt_payload(capture, s, response)
    if capture.processing_status not in evidence_ledger.RECOVERABLE_CAPTURE_STATUSES:
        raise HTTPException(409, f"capture 处理阶段异常: {capture.processing_status}")
    if evidence_ledger.has_active_capture_lease(capture):
        return None, None, _capture_in_progress_payload(capture, s, response)

    owner = evidence_ledger.new_claim_owner()
    if evidence_ledger.try_claim_capture(s, capture.id, owner=owner):
        s.commit()
        claimed = s.get(AttemptCaptureProcessing, capture.id)
        if claimed is None:
            raise HTTPException(409, "capture 在接管后丢失")
        return claimed, evidence_ledger.claim_from_capture(claimed), None

    # 条件 UPDATE 未命中：另一 worker 已续租/接管，或已进入终态。
    s.rollback()
    current = s.get(AttemptCaptureProcessing, capture.id)
    if current is None:
        raise HTTPException(409, "capture 并发接管后证据行不存在")
    if current.processing_status in evidence_ledger.TERMINAL_CAPTURE_STATUSES:
        return None, None, _capture_terminal_attempt_payload(current, s, response)
    return None, None, _capture_in_progress_payload(current, s, response)


def _capture_claim_lost_payload(capture_claim: evidence_ledger.CaptureClaim,
                                s: DBSession, response: Response, *,
                                control_plane: Literal[
                                    "manual_http", "autopilot_worker"] = (
                                        "autopilot_worker"),
                                ) -> dict:
    """A newer generation or terminal transition won; stale workers only read back.

    ``claim_lost=True`` marks that *this* invocation held a live capture claim
    and lost the race — see :func:`_claim_lost_payload` for why that must
    never route, even when the underlying attempt already reads ``completed``.
    Internal-only, like :func:`_claim_lost_payload`: this function is only
    ever reached on the ``autopilot_worker`` plane in practice (the capture-
    claim path ``attempt is None`` implies), but the flag is still gated on
    ``control_plane`` explicitly rather than on that structural fact alone.
    """
    s.rollback()
    current = s.get(AttemptCaptureProcessing, capture_claim.capture_id)
    if current is None:
        raise HTTPException(409, "capture claim 已失效且证据行不存在")
    if current.processing_status in evidence_ledger.TERMINAL_CAPTURE_STATUSES:
        payload = _capture_terminal_attempt_payload(current, s, response)
        if control_plane == "autopilot_worker":
            payload["claim_lost"] = True
        return payload
    return _capture_in_progress_payload(current, s, response)


def _materialize_attempt_from_capture(
        capture_claim: evidence_ledger.CaptureClaim, sess: TrainSession,
        s: DBSession, *, body: AttemptProcessIn,
        control_plane: Literal["manual_http", "autopilot_worker"],
        outcome: Literal["asr_completed", "technical_failure"],
        asr_result: asr.AsrResult | None = None,
        error_code: str | None = None,
        engine_version: str | None = None,
        worker_target: autopilot_orchestration.FrozenWorkerTarget | None = None,
) -> AttemptEvent | None:
    """Atomically create the deferred AttemptEvent for one claimed capture.

    R1-foundation: this is the only place attempt_seq is ever consumed for the
    autopilot_worker control plane. Repeat detection is fixed disabled this
    batch, so a successful transcript always disposes ``answer_candidate``; a
    future repeat-classifier would branch here, before attempt_seq is used.
    ``None`` return means the capture claim was lost (fenced out by a newer
    generation or an intervening pause/withdrawal) — including when it was
    already lost *before* this function's own work began. A stale/fenced-out
    worker must never reach the sequence check or insert below using a claim
    that is no longer current: doing so could either collide with a newer
    generation's already-committed attempt_seq, or (if it raced ahead of that
    collision) wrongly report a fail-closed error against a scope a newer
    worker already owns. The caller re-reads state via
    :func:`_capture_claim_lost_payload`.
    """
    with _LIVE_WRITE_LOCK:
        sess = _revalidate_attempt_commit_fence(
            sess.session_id, body, s, control_plane=control_plane,
            worker_target=worker_target)
        _lock_evidence_session(sess.session_id, s)
        now = datetime.now()
        # Prove the claim is still current before any sequence computation or
        # insert, via a real conditional UPDATE — not a SELECT-then-compare,
        # which is not a genuine mutual-exclusion primitive on SQLite (no
        # FOR UPDATE) or across processes. A claim that fails this atomic
        # check is fenced out and must be pure observation.
        if not evidence_ledger.confirm_capture_claim(
                s, capture_claim.capture_id, owner=capture_claim.owner,
                generation=capture_claim.generation, now=now):
            s.rollback()
            return None

        # The capture proof is immutable server state fixed at record_stopped
        # time. Verify it against the live sequence rather than trust it blindly
        # or re-derive a fresh number with ``_next_attempt_seq``.
        current_max = s.exec(select(func.max(AttemptEvent.attempt_seq)).where(
            AttemptEvent.session_id == sess.session_id,
            AttemptEvent.item_id == body.item_id,
            AttemptEvent.turn_seq == body.turn_seq,
        )).one()
        proof_attempt_seq = capture_claim.proof_attempt_seq
        if (int(current_max or 0) + 1 != proof_attempt_seq
                or body.prompt_level != capture_claim.proof_prompt_level):
            s.rollback()
            raise RuntimeError(
                "capture proof attempt_seq/prompt_level does not match the "
                "next server sequence; refusing to guess a value")

        disposition = "answer_candidate" if outcome == "asr_completed" else None
        asr_text = asr_result.asr_text if asr_result is not None else None
        asr_confidence = asr_result.asr_confidence if asr_result is not None else None
        attempt = AttemptEvent(
            session_id=sess.session_id,
            item_id=body.item_id,
            turn_seq=body.turn_seq,
            response_role=body.response_role,
            attempt_seq=proof_attempt_seq,
            raw_audio_id=body.raw_audio_id,
            prompt_level=body.prompt_level,
            cue_type=body.cue_type,
            duration_seconds=body.duration_seconds,
            asr_text=asr_text,
            asr_confidence=asr_confidence,
            asr_engine_version=engine_version,
            error_code=error_code,
            processing_status=outcome,
            processing_owner=None if outcome == "technical_failure" else capture_claim.owner,
            processing_lease_expires_at=(
                None if outcome == "technical_failure"
                else evidence_ledger.lease_deadline(now=now)),
            processing_claimed_at=now,
            processing_generation=1,
            created_at=now,
            processed_at=now if outcome == "technical_failure" else None,
            is_simulation=sess.is_simulation,
            judge_portrait_used=False,
        )
        s.add(attempt)
        try:
            s.flush()
        except IntegrityError:
            s.rollback()
            # Re-confirm with the same atomic conditional UPDATE used above:
            # fail loud only if I still hold the claim (a genuine invariant
            # violation). If a newer generation or terminal transition
            # already won, this is a stale race — observation-only, same as
            # the claim-current check at the top of this function. The
            # rollback above already undid that check's own lease refresh,
            # so this re-confirms fresh rather than trusting a stale read.
            if evidence_ledger.confirm_capture_claim(
                    s, capture_claim.capture_id, owner=capture_claim.owner,
                    generation=capture_claim.generation, now=now):
                raise RuntimeError(
                    "capture materialized attempt collided with an existing "
                    "raw_audio_id/attempt_seq row")
            return None
        if not evidence_ledger.fenced_capture_update(
                s, capture_claim, next_status=outcome,
                values={
                    "asr_confidence": asr_confidence,
                    "asr_engine_version": engine_version,
                    "disposition": disposition,
                    "error_code": error_code,
                    "processed_at": now,
                    "final_attempt_id": attempt.id,
                }):
            s.rollback()
            return None
        _append_interaction(s, sess, "attempt_received", {
            "raw_audio_id": body.raw_audio_id,
            "prompt_level": body.prompt_level,
            "cue_type": body.cue_type,
            "duration_seconds": body.duration_seconds,
            "processing_status": "received",
        }, item_id=body.item_id, turn_seq=body.turn_seq, attempt=attempt)
        if outcome == "asr_completed":
            _append_interaction(s, sess, "asr_completed", {
                "asr_engine_version": engine_version,
                "asr_confidence": asr_confidence,
                "degraded": False,
                "hotword_hit": asr_result.hotword_hit if asr_result is not None else False,
            }, item_id=attempt.item_id, turn_seq=attempt.turn_seq, attempt=attempt)
        else:
            _append_interaction(s, sess, "asr_failed", {
                "asr_engine_version": engine_version,
                "error_code": error_code,
            }, item_id=attempt.item_id, turn_seq=attempt.turn_seq, attempt=attempt)
            _append_interaction(s, sess, "technical_pause", {"error_code": error_code},
                                item_id=attempt.item_id, turn_seq=attempt.turn_seq,
                                attempt=attempt)
            # For P0a this stages the control-state pause and stable failure event
            # in this same transaction, mirroring _finish_attempt_failure exactly.
            staged_autopilot_pause = autopilot_orchestration.stage_processing_failure(
                s,
                session_id=sess.session_id,
                error_code=error_code,
                source="attempt_processing",
                target=worker_target,
            )
            if control_plane == "autopilot_worker" and not staged_autopilot_pause:
                s.rollback()
                raise autopilot_orchestration.AutopilotOrchestrationError(
                    "autopilot_attempt_commit_fence_lost",
                    "attempt 失败提交时自动驾驶所有权已变化",
                )
            _pause_runtime_in_transaction(sess.session_id, s)
        s.commit()
        s.refresh(attempt)
        return attempt


def _detect_repeat_request(
        capture_claim: evidence_ledger.CaptureClaim,
        asr_text: str | None) -> repeat_intent.RepeatMatch | None:
    """Classify a successful transcript against the capture's frozen protocol.

    Only a capture admitted under the protocol may be classified, and the
    admission marker says so — not the mere presence or absence of a binding.
    A ``legacy_pre_repeat`` capture never reaches this function at all: it is
    dispatched to the dedicated legacy recovery worker instead, so an unexpected
    marker here is an invariant violation and fails closed rather than silently
    degrading into the pre-repeat answer flow.

    Resolution then goes through the version and digest frozen on the capture at
    admission time, so a worker recovering after a deployment upgrade keeps
    using the definition the session actually ran under.

    ``stop_reason`` is deliberately not an input: whether the patient tapped
    "说完了" or the max timer closed the microphone, only the complete
    successful transcript decides.
    """
    version_id = capture_claim.repeat_protocol_version_id
    digest = capture_claim.repeat_protocol_definition_digest
    if (capture_claim.repeat_admission_semantics
            != evidence_ledger.REPEAT_BOUND_ADMISSION
            or version_id is None or digest is None):
        raise autopilot_orchestration.AutopilotOrchestrationError(
            "autopilot_repeat_admission_not_bound",
            "采集未按重复请求协议准入，禁止在现代流程内分类",
        )
    protocol = repeat_intent.protocol_for_binding(version_id, digest)
    return repeat_intent.detect(protocol, asr_text)


def _terminalize_capture_as_repeat(
        capture_claim: evidence_ledger.CaptureClaim, sess: TrainSession,
        s: DBSession, *, body: AttemptProcessIn,
        control_plane: Literal["manual_http", "autopilot_worker"],
        match: repeat_intent.RepeatMatch,
        asr_result: asr.AsrResult,
        engine_version: str | None,
        worker_target: autopilot_orchestration.FrozenWorkerTarget | None,
) -> dict | None:
    """Commit one explicit-repeat decision without creating an AttemptEvent.

    ``None`` means the capture claim was lost — the same observation-only
    contract as :func:`_materialize_attempt_from_capture`.  Everything else
    either commits the full repeat transaction (replay command, ledger row,
    capture terminalization and the control CAS) or rolls it back whole.
    """
    with _LIVE_WRITE_LOCK:
        sess = _revalidate_attempt_commit_fence(
            sess.session_id, body, s, control_plane=control_plane,
            worker_target=worker_target)
        _lock_evidence_session(sess.session_id, s)
        now = datetime.now()
        if not evidence_ledger.confirm_capture_claim(
                s, capture_claim.capture_id, owner=capture_claim.owner,
                generation=capture_claim.generation, now=now):
            s.rollback()
            return None
        try:
            result = autopilot_service.route_explicit_repeat(
                s,
                session_id=sess.session_id,
                capture_claim=capture_claim,
                match=match,
                asr_confidence=asr_result.asr_confidence,
                asr_engine_version=engine_version,
            )
        except autopilot_service.AutopilotServiceError as exc:
            s.rollback()
            if exc.code in _REPEAT_CLAIM_LOST_CODES:
                return None
            raise autopilot_orchestration.AutopilotOrchestrationError(
                exc.code, exc.message) from exc
        if result.outcome == "limit_paused":
            # The researcher-facing pause must be atomic with the control CAS.
            _pause_runtime_in_transaction(sess.session_id, s)
        s.commit()
        return {
            "status": ("repeat_replayed" if result.outcome == "replayed"
                       else "repeat_limit_paused"),
            "in_progress": False,
        }


def _prepare_capture_or_attempt_processing(
        session_id: str,
        body: AttemptProcessIn,
        response: Response,
        s: DBSession,
        *,
        worker_target: autopilot_orchestration.FrozenWorkerTarget,
) -> dict | tuple[TrainSession, Path, AttemptEvent, evidence_ledger.AttemptClaim] | tuple[
        TrainSession, Path, evidence_ledger.CaptureClaim]:
    """Autopilot-worker admission: claim a capture row instead of a fresh AttemptEvent.

    Identical eligibility/idempotency checks to :func:`_prepare_attempt_processing`.
    An existing AttemptEvent (ASR already ran at least once) is recovered exactly
    as before. A raw_audio_id with no AttemptEvent yet claims the persistent
    capture-processing row created at record_stopped time; AttemptEvent creation
    is deferred to :func:`_materialize_attempt_from_capture`, after ASR resolves.

    ``worker_target`` is re-verified exactly, within this same lock, before any
    terminal read or claim recovery/creation below — not just via an earlier,
    separately-locked caller-side check. A caller-side precheck and this
    function's own lock are two different transactions; control/runner
    generation can change in the gap between them even while ``body``/its raw
    audio stay identical, so the caller-side check alone is not sufficient to
    keep a stale worker from recovering or creating a claim here.
    """
    with _LIVE_WRITE_LOCK, device_capability.serialized_mutation():
        s.rollback()
        s.expire_all()
        _live_row_for_update(s)
        sess = s.exec(select(TrainSession).where(
            TrainSession.session_id == session_id,
        ).with_for_update()).first()
        if not sess:
            raise HTTPException(404, "场次不存在")

        # Re-derive inside the same state-locking transaction used to claim the
        # capture; an old worker body can never bypass a pause/new command.
        s.exec(select(SessionAutopilotState).where(
            SessionAutopilotState.session_id == session_id,
        ).with_for_update()).first()
        try:
            derived = autopilot_orchestration.derive_authoritative_attempt_input(
                s, session_id=session_id)
        except autopilot_orchestration.AutopilotOrchestrationError as exc:
            raise HTTPException(status_code=409, detail={
                "code": exc.code,
                "message": str(exc),
            }) from exc
        if body.model_dump(mode="python") != derived.model_dump(mode="python"):
            raise HTTPException(status_code=409, detail={
                "code": "autopilot_attempt_input_mismatch",
                "message": "内部 worker 输入与当前权威录音证据不一致",
            })
        current_target = autopilot_orchestration.derive_worker_target(
            s, session_id=session_id)
        if current_target != worker_target:
            raise HTTPException(status_code=409, detail={
                "code": "autopilot_attempt_input_mismatch",
                "message": "内部 worker 目标与当前权威录音证据不一致",
            })

        _require_classified_session(sess)
        asset = s.get(AudioAssetRow, body.raw_audio_id)
        if not asset or asset.session_id != session_id:
            raise HTTPException(404, "音频资产不存在")
        expected_turn_key = f"{body.item_id}#{body.turn_seq}"
        if asset.turn_key != expected_turn_key:
            raise HTTPException(409, "音频 turn_key 与冻结计划位置不一致")
        _ensure_audio_read_allowed(asset, s)
        blob = audio_store.find_blob(body.raw_audio_id)
        if not blob:
            raise HTTPException(409, "音频字节不存在，禁止伪造 ASR 证据")

        existing = _existing_attempt_for_audio(session_id, body, s)
        if existing is not None:
            if existing.processing_status in evidence_ledger.TERMINAL_ATTEMPT_STATUSES:
                return _attempt_rows_payload(existing, s, idempotent=True)
            if evidence_ledger.has_active_lease(existing):
                return _attempt_in_progress_payload(existing, s, response)

        state = _ensure_runtime_writable(session_id, s, "新建逐次 AI 证据")
        if state is not None and state.status == "paused":
            raise HTTPException(409, "场次已暂停，禁止处理新 attempt")
        _frozen_plan_turn(sess, body.item_id, body.turn_seq, body.response_role)

        if asset.is_simulation != sess.is_simulation:
            raise HTTPException(409, "音频与场次的真实/模拟属性不一致")
        if asset.data_classification != sess.data_classification:
            raise HTTPException(409, "音频与场次 data_classification 不一致")
        if asset.withdrawn or (asset.withdrawal_status or "").strip():
            raise HTTPException(409, "音频已进入撤回/隔离流程，禁止 AI 处理")
        _ensure_recording_allowed_for_session(
            session_id, s, is_simulation=asset.is_simulation)

        if existing is not None:
            claimed, recovered_claim, early = _try_recovery_claim(existing, s, response)
            if early is not None:
                return early
            if claimed is None or recovered_claim is None:
                raise RuntimeError("attempt recovery returned no claim and no response")
            return sess, blob, claimed, recovered_claim

        capture = s.exec(select(AttemptCaptureProcessing).where(
            AttemptCaptureProcessing.raw_audio_id == body.raw_audio_id,
        ).with_for_update()).first()
        if capture is None:
            raise HTTPException(
                409, "音频对应的采集处理行不存在，无法开始 AI 处理")
        claimed_capture, recovered_capture_claim, early = _try_recovery_capture_claim(
            capture, s, response)
        if early is not None:
            return early
        if claimed_capture is None or recovered_capture_claim is None:
            raise RuntimeError("capture recovery returned no claim and no response")
        return sess, blob, recovered_capture_claim


def _prepare_attempt_processing(
        session_id: str,
        body: AttemptProcessIn,
        response: Response,
        s: DBSession,
        *,
        control_plane: Literal["manual_http", "autopilot_worker"],
) -> dict | tuple[
        TrainSession, Path, AttemptEvent, evidence_ledger.AttemptClaim]:
    """Atomically admit exactly one control plane and persist its attempt claim.

    Provider I/O deliberately happens after this helper releases the shared
    control locks.  Both manual admission and P0a start lock the same TrainSession
    row, so in a multi-process database either the manual claim or autonomous
    ownership wins; they cannot both pass an earlier snapshot.
    """
    with _LIVE_WRITE_LOCK, device_capability.serialized_mutation():
        s.rollback()
        s.expire_all()
        _live_row_for_update(s)
        sess = s.exec(select(TrainSession).where(
            TrainSession.session_id == session_id,
        ).with_for_update()).first()
        if not sess:
            raise HTTPException(404, "场次不存在")

        if control_plane == "manual_http":
            _ensure_manual_plane_writable(
                session_id, s, "通过旧人工端点处理 attempt")
        elif control_plane == "autopilot_worker":
            # Re-derive inside the same state-locking transaction used to claim
            # the attempt; an old worker body can never bypass a pause/new command.
            s.exec(select(SessionAutopilotState).where(
                SessionAutopilotState.session_id == session_id,
            ).with_for_update()).first()
            try:
                derived = autopilot_orchestration.derive_authoritative_attempt_input(
                    s, session_id=session_id)
            except autopilot_orchestration.AutopilotOrchestrationError as exc:
                raise HTTPException(status_code=409, detail={
                    "code": exc.code,
                    "message": str(exc),
                }) from exc
            if body.model_dump(mode="python") != derived.model_dump(mode="python"):
                raise HTTPException(status_code=409, detail={
                    "code": "autopilot_attempt_input_mismatch",
                    "message": "内部 worker 输入与当前权威录音证据不一致",
                })
        else:  # pragma: no cover - Literal callers, defensive privilege boundary
            raise RuntimeError(f"unsupported attempt control plane: {control_plane!r}")

        _require_classified_session(sess)
        asset = s.get(AudioAssetRow, body.raw_audio_id)
        # The caller is authorized for ``session_id``, not for a global audio
        # identifier.  Test ownership before returning any asset diagnostics so
        # another researcher's IDs cannot be enumerated through 404/409 oracles.
        if not asset or asset.session_id != session_id:
            raise HTTPException(404, "音频资产不存在")
        expected_turn_key = f"{body.item_id}#{body.turn_seq}"
        if asset.turn_key != expected_turn_key:
            raise HTTPException(409, "音频 turn_key 与冻结计划位置不一致")
        _ensure_audio_read_allowed(asset, s)
        blob = audio_store.find_blob(body.raw_audio_id)
        if not blob:
            raise HTTPException(409, "音频字节不存在，禁止伪造 ASR 证据")

        # Even terminal idempotent reads pass the control-plane admission above;
        # an old manual tab cannot use this endpoint as an autonomous side channel.
        existing = _existing_attempt_for_audio(session_id, body, s)
        if existing is not None:
            if existing.processing_status in evidence_ledger.TERMINAL_ATTEMPT_STATUSES:
                return _attempt_rows_payload(existing, s, idempotent=True)
            if evidence_ledger.has_active_lease(existing):
                return _attempt_in_progress_payload(existing, s, response)

        state = _ensure_runtime_writable(session_id, s, "新建逐次 AI 证据")
        if state is not None and state.status == "paused":
            raise HTTPException(409, "场次已暂停，禁止处理新 attempt")
        _frozen_plan_turn(sess, body.item_id, body.turn_seq, body.response_role)

        if asset.is_simulation != sess.is_simulation:
            raise HTTPException(409, "音频与场次的真实/模拟属性不一致")
        if asset.data_classification != sess.data_classification:
            raise HTTPException(409, "音频与场次 data_classification 不一致")
        if asset.withdrawn or (asset.withdrawal_status or "").strip():
            raise HTTPException(409, "音频已进入撤回/隔离流程，禁止 AI 处理")
        _ensure_recording_allowed_for_session(
            session_id, s, is_simulation=asset.is_simulation)

        claim: evidence_ledger.AttemptClaim
        attempt: AttemptEvent
        if existing is not None:
            claimed, recovered_claim, early = _try_recovery_claim(existing, s, response)
            if early is not None:
                return early
            if claimed is None or recovered_claim is None:
                raise RuntimeError("attempt recovery returned no claim and no response")
            attempt, claim = claimed, recovered_claim
        else:
            _lock_evidence_session(session_id, s)
            now = datetime.now()
            owner = evidence_ledger.new_claim_owner()
            attempt = AttemptEvent(
                session_id=session_id,
                item_id=body.item_id,
                turn_seq=body.turn_seq,
                response_role=body.response_role,
                attempt_seq=_next_attempt_seq(
                    session_id, body.item_id, body.turn_seq, s),
                raw_audio_id=body.raw_audio_id,
                prompt_level=body.prompt_level,
                cue_type=body.cue_type,
                duration_seconds=body.duration_seconds,
                processing_status="received",
                processing_owner=owner,
                processing_lease_expires_at=evidence_ledger.lease_deadline(now=now),
                processing_claimed_at=now,
                processing_generation=1,
                created_at=now,
                is_simulation=sess.is_simulation,
                judge_portrait_used=False,
            )
            s.add(attempt)
            try:
                s.flush()
                _append_interaction(s, sess, "attempt_received", {
                    "raw_audio_id": body.raw_audio_id,
                    "prompt_level": body.prompt_level,
                    "cue_type": body.cue_type,
                    "duration_seconds": body.duration_seconds,
                    "processing_status": "received",
                }, item_id=body.item_id, turn_seq=body.turn_seq, attempt=attempt)
                s.commit()
                s.refresh(attempt)
                claim = evidence_ledger.claim_from_attempt(attempt)
            except IntegrityError:
                s.rollback()
                raced = _existing_attempt_for_audio(session_id, body, s)
                if raced is None:
                    raise HTTPException(
                        409, "attempt/event 序号并发冲突，请以同一 raw_audio_id 重试")
                claimed, recovered_claim, early = _try_recovery_claim(
                    raced, s, response)
                if early is not None:
                    return early
                if claimed is None or recovered_claim is None:
                    raise RuntimeError(
                        "raced attempt returned no claim and no response")
                attempt, claim = claimed, recovered_claim
        return sess, blob, attempt, claim


def _process_attempt(
        session_id: str, body: AttemptProcessIn, response: Response,
        s: DBSession, *,
        control_plane: Literal["manual_http", "autopilot_worker"],
        worker_target: autopilot_orchestration.FrozenWorkerTarget | None = None):
    """登记录音后的单一权威 ASR + operational 判类链。

    技术失败也以 200 + ``status=technical_failure`` 返回，便于老人端停在原位置；
    输入/场次/音频违约仍是 4xx。``raw_audio_id`` 是幂等键。
    """
    if control_plane == "autopilot_worker":
        if worker_target is None:
            # Structural invariant: the only caller on this control plane
            # (_run_p0a_attempt_worker) always freezes a target before any
            # provider I/O. A missing target here would mean a fail-close path
            # could fall back to mutating whatever attempt is currently
            # current — fail loud, before any provider I/O or mutation.
            raise RuntimeError(
                "autopilot_worker control plane requires a frozen worker_target")
        # Do not trust a target's mere presence: verify it against the current
        # locked row before any provider I/O. A target's own fields — session,
        # exact raw audio, command id, both generations — must agree with
        # ``body`` and with the live row; a forged or lagged target must never
        # ride a coincidentally-matching body through to a provider call.
        if (worker_target.session_id != session_id
                or worker_target.raw_audio_id != body.raw_audio_id
                or not autopilot_orchestration.worker_target_still_current(
                    s, worker_target)):
            raise autopilot_orchestration.AutopilotOrchestrationError(
                "autopilot_attempt_input_mismatch",
                "内部 worker 目标与当前权威录音证据不一致",
            )
    capture_claim: evidence_ledger.CaptureClaim | None = None
    if control_plane == "autopilot_worker":
        prepared = _prepare_capture_or_attempt_processing(
            session_id, body, response, s, worker_target=worker_target)
    else:
        prepared = _prepare_attempt_processing(
            session_id, body, response, s, control_plane=control_plane)
    if isinstance(prepared, dict):
        return prepared
    if len(prepared) == 4:
        sess, blob, attempt, claim = prepared
    else:
        # R1-foundation: a fresh raw_audio_id for the autopilot_worker control
        # plane claims the persistent capture row instead of an AttemptEvent.
        # ASR runs against the capture claim; the AttemptEvent (and its
        # attempt_seq) is created only after ASR resolves, in
        # _materialize_attempt_from_capture below.
        sess, blob, capture_claim = prepared
        attempt = None
        claim = None

    bank = _load_bank_for_session(sess)
    if attempt is not None:
        attempt_asr_text = attempt.asr_text
        attempt_asr_engine_version = attempt.asr_engine_version
    else:
        attempt_asr_text = None
        attempt_asr_engine_version = None
    # Loading expired ORM attributes above may begin a read transaction after the
    # claim commit.  End it before any provider call; no DB connection/transaction
    # is held while ASR or LLM I/O is in flight.
    s.rollback()

    def finish_failure(
            *, stage: Literal["asr", "judgement"], error_code: str,
            engine_version: str | None) -> dict:
        if attempt is None:
            materialized = _materialize_attempt_from_capture(
                capture_claim, sess, s, body=body, control_plane=control_plane,
                outcome="technical_failure", error_code=error_code,
                engine_version=engine_version, worker_target=worker_target)
            if materialized is None:
                return _capture_claim_lost_payload(
                    capture_claim, s, response, control_plane=control_plane)
            return _attempt_rows_payload(materialized, s, idempotent=False)
        return _finish_attempt_failure(
            claim, sess, s, response,
            stage=stage,
            error_code=error_code,
            engine_version=engine_version,
            body=body,
            control_plane=control_plane,
            worker_target=worker_target,
        )

    if attempt is None or claim.stage == "received":
        asr_engine_version: str | None = None
        try:
            asr_engine = asr.get_engine()
            asr_engine_version = asr_engine.version
            asr_boundary = cloud_processing.provider_boundary(asr_engine)
            if asr_boundary is cloud_processing.DataBoundary.UNKNOWN:
                return finish_failure(
                    stage="asr",
                    error_code="cloud_provider_boundary_unknown",
                    engine_version=asr_engine_version)
            try:
                script = content.load_week1_script(content.CONTENT_DIR / "week1_script.json")
            except Exception:
                script = None
            audio_bytes = blob.read_bytes()
            hotwords = asr.build_hotwords(bank, script)
            s.rollback()
            if asr_boundary is cloud_processing.DataBoundary.CLOUD:
                try:
                    with _serialized_cloud_provider_call(
                            session_id=session_id,
                            patient_id=sess.patient_id,
                            provider=asr_engine,
                            bind=s.get_bind()):
                        asr_result = asr_engine.transcribe(audio_bytes, hotwords)
                except _CloudEgressNotAuthorized:
                    return finish_failure(
                        stage="asr",
                        error_code="cloud_processing_not_authorized",
                        engine_version=asr_engine_version)
            else:
                asr_result = asr_engine.transcribe(audio_bytes, hotwords)
        except (autopilot_orchestration.AutopilotOrchestrationError,
                HTTPException):
            raise
        except Exception:
            return finish_failure(
                stage="asr", error_code="asr_exception",
                engine_version=asr_engine_version)

        if (asr_result.asr_confidence is not None
                and not math.isfinite(asr_result.asr_confidence)):
            return finish_failure(
                stage="asr", error_code="asr_invalid_result",
                engine_version=asr_result.engine_version)
        if asr_result.asr_text is not None and len(asr_result.asr_text) > 2000:
            return finish_failure(
                stage="asr", error_code="asr_invalid_result",
                engine_version=asr_result.engine_version)
        if asr_result.asr_text is None:
            return finish_failure(
                stage="asr", error_code="asr_degraded",
                engine_version=asr_result.engine_version)

        if attempt is None:
            # Classify before any AttemptEvent exists: an explicit repeat must
            # not consume attempt_seq, a cue or a prompt level.
            repeat_match = _detect_repeat_request(
                capture_claim, asr_result.asr_text)
            if repeat_match is not None:
                repeated = _terminalize_capture_as_repeat(
                    capture_claim, sess, s, body=body,
                    control_plane=control_plane, match=repeat_match,
                    asr_result=asr_result,
                    engine_version=asr_result.engine_version,
                    worker_target=worker_target)
                if repeated is None:
                    return _capture_claim_lost_payload(
                        capture_claim, s, response, control_plane=control_plane)
                return repeated
            advanced = _materialize_attempt_from_capture(
                capture_claim, sess, s, body=body, control_plane=control_plane,
                outcome="asr_completed", asr_result=asr_result,
                engine_version=asr_result.engine_version,
                worker_target=worker_target)
            if advanced is None:
                return _capture_claim_lost_payload(
                    capture_claim, s, response, control_plane=control_plane)
        else:
            advanced = _record_asr_success(
                claim, sess, s, asr_result,
                body=body,
                control_plane=control_plane,
                worker_target=worker_target,
            )
            if advanced is None:
                return _claim_lost_payload(claim, s, response, control_plane=control_plane)
        attempt = advanced
        attempt_asr_text = attempt.asr_text
        attempt_asr_engine_version = attempt.asr_engine_version
        claim = evidence_ledger.claim_from_attempt(attempt)
        s.rollback()

    # asr_completed 崩溃恢复不再调 ASR，仅使用已原子落库的转写继续判类。
    if attempt_asr_text is None:
        return finish_failure(
            stage="judgement",
            error_code="asr_recovery_state_invalid",
            engine_version=attempt_asr_engine_version)

    judge_engine_version: str | None = None
    # Do not even start the second provider boundary after pause, takeover,
    # withdrawal, quarantine or feature disablement.
    with _LIVE_WRITE_LOCK:
        _revalidate_attempt_commit_fence(
            session_id, body, s, control_plane=control_plane,
            worker_target=worker_target)
        s.rollback()
    try:
        judge_engine = llm_judge.get_engine()
        judge_boundary = cloud_processing.provider_boundary(judge_engine)
        if judge_boundary is cloud_processing.DataBoundary.UNKNOWN:
            return finish_failure(
                stage="judgement",
                error_code="cloud_provider_boundary_unknown",
                engine_version=getattr(judge_engine, "version", None))
        s.rollback()
        if judge_boundary is cloud_processing.DataBoundary.CLOUD:
            try:
                with _serialized_cloud_provider_call(
                        session_id=session_id,
                        patient_id=sess.patient_id,
                        provider=judge_engine,
                        bind=s.get_bind()):
                    result = _classify_operational(
                        item_id=body.item_id,
                        response_role=body.response_role,
                        text=attempt_asr_text,
                        bank=bank,
                        llm_engine=judge_engine,
                        cloud_llm_allowed=True)
            except _CloudEgressNotAuthorized:
                # Revocation before the cloud boundary still permits the closed,
                # local operational rule path; no patient text leaves the host.
                result = _classify_operational(
                    item_id=body.item_id,
                    response_role=body.response_role,
                    text=attempt_asr_text,
                    bank=bank,
                    allow_llm=False)
        else:
            result = _classify_operational(
                item_id=body.item_id,
                response_role=body.response_role,
                text=attempt_asr_text,
                bank=bank,
                llm_engine=judge_engine)
        judge_engine_version = result["judge_engine_version"]
    except (autopilot_orchestration.AutopilotOrchestrationError,
            HTTPException):
        raise
    except OperationalRubricUnavailable:
        return finish_failure(
            stage="judgement",
            error_code="operational_rubric_unavailable",
            engine_version="rubric-unavailable")
    except Exception:
        try:
            judge_engine_version = llm_judge.get_engine().version
        except Exception:
            judge_engine_version = None
        return finish_failure(
            stage="judgement", error_code="judgement_exception",
            engine_version=judge_engine_version)

    completed = _record_judgement_success(
        claim, sess, s, result,
        body=body,
        bank=bank,
        control_plane=control_plane,
        worker_target=worker_target,
    )
    if completed is None:
        return _claim_lost_payload(claim, s, response, control_plane=control_plane)
    return _attempt_rows_payload(completed, s, idempotent=False)


@app.post("/sessions/{session_id}/attempts/process")
def process_attempt(session_id: str, body: AttemptProcessIn,
                    request: Request, response: Response,
                    s: DBSession = Depends(get_session)):
    """Legacy/manual adapter; server-owned P0a uses the internal worker path."""
    sess = s.get(TrainSession, session_id)
    if sess is None:
        raise HTTPException(404, "场次不存在")
    _require_session_operator(
        request, sess, s, "处理人工平面 AI attempt", mutation=True)
    return _process_attempt(
        session_id, body, response, s, control_plane="manual_http")


def _fail_closed_p0a_attempt_worker(
        session_id: str, error_code: str, *,
        target: autopilot_orchestration.FrozenWorkerTarget | None = None) -> None:
    """Best-effort safe terminal action for an unexpected worker failure.

    This path never raises into the executor.  It only acts while the exact P0a
    capture is still ``processing_attempt``; a competing successful route or human
    state change wins and is left untouched.

    ``target`` must be the exact target this worker froze before any provider I/O.
    A worker that never froze one (its very first read of the pending command
    already failed) has no safe binding to act on: this is pure observation, and
    normal GET recovery is relied on instead — it must never fall back to mutating
    whatever attempt happens to be current.
    """
    if target is None or error_code in _NON_PAUSING_WORKER_CODES:
        return
    try:
        with _LIVE_WRITE_LOCK, device_capability.serialized_mutation():
            with DBSession(db.engine) as worker_db:
                _live_row_for_update(worker_db)
                staged = autopilot_orchestration.stage_processing_failure(
                    worker_db,
                    session_id=session_id,
                    error_code=error_code,
                    source="worker_exception",
                    target=target,
                )
                if not staged:
                    worker_db.rollback()
                    return
                _pause_runtime_in_transaction(session_id, worker_db)
                worker_db.commit()
    except Exception:
        # There is no safe second mutation when the fail-closed transaction itself
        # cannot be proven. GET recovery will retry while processing_attempt remains.
        return


def _run_p0a_attempt_worker(session_id: str) -> None:
    """Run one recoverable P0a attempt outside the ACK request transaction."""
    target: autopilot_orchestration.FrozenWorkerTarget | None = None
    try:
        with DBSession(db.engine) as worker_db:
            # Freeze the immutable target first, from the same locked snapshot,
            # before deriving the authoritative body and before any provider
            # I/O. Deriving the body first would risk losing an already-
            # obtainable target to a later, unrelated gate failure (withdrawal,
            # quarantine, feature disablement) and degrading fail-close into a
            # no-op. Every fail-close below must bind to this exact target so a
            # worker that returns late (after a newer worker has already taken
            # over and completed/routed a different logical attempt) can only
            # observe, never mutate that new attempt.
            target = autopilot_orchestration.derive_worker_target(
                worker_db, session_id=session_id)
            derived = autopilot_orchestration.derive_authoritative_attempt_input(
                worker_db, session_id=session_id)
            if target.raw_audio_id != derived.raw_audio_id:
                # Two reads of the same locked snapshot within one transaction
                # cannot legitimately disagree; fail closed rather than trust
                # either value if they ever do.
                raise autopilot_orchestration.AutopilotOrchestrationError(
                    "autopilot_attempt_target_divergence",
                    "worker target 与 authoritative body 在同一锁定快照内不一致",
                )
            # Release capture/control read locks before ASR/LLM provider I/O.
            worker_db.rollback()
            body = AttemptProcessIn.model_validate(derived.model_dump(mode="python"))
            result = _process_attempt(
                session_id,
                body,
                Response(),
                worker_db,
                control_plane="autopilot_worker",
                worker_target=target,
            )
    except autopilot_orchestration.AutopilotOrchestrationError as exc:
        _fail_closed_p0a_attempt_worker(session_id, exc.code, target=target)
        return
    except HTTPException as exc:
        # A repeat activation/binding rejection is surfaced as a 409 by the
        # admission helper. It is a configuration refusal, so carry its exact
        # code through and leave the scope untouched; every other HTTP failure
        # keeps the existing generic safe-pause.
        detail = exc.detail if isinstance(exc.detail, dict) else {}
        code = detail.get("code")
        _fail_closed_p0a_attempt_worker(
            session_id,
            code if isinstance(code, str) and code in _NON_PAUSING_WORKER_CODES
            else "autopilot_worker_exception",
            target=target,
        )
        return
    except Exception:
        _fail_closed_p0a_attempt_worker(
            session_id, "autopilot_worker_exception", target=target)
        return

    if result.get("in_progress") is True:
        # Another process owns a live attempt lease. A later GET or exact ACK replay
        # re-submits after lease expiry; this worker must neither steal nor pause it.
        return
    status = result.get("status")
    attempt_payload = result.get("attempt")
    if status in _REPEAT_TERMINAL_STATUSES:
        # An explicit repeat already committed everything it owns: either the
        # replay command is issued and current, or the scope is safely paused.
        # There is no Attempt to route and nothing left to fail closed.
        return
    if status == "technical_failure":
        error_code = (
            attempt_payload.get("error_code")
            if isinstance(attempt_payload, dict) else None)
        _fail_closed_p0a_attempt_worker(
            session_id,
            error_code if isinstance(error_code, str)
            else "autopilot_attempt_technical_failure",
            target=target,
        )
        return
    if status != "completed" or not isinstance(attempt_payload, dict):
        _fail_closed_p0a_attempt_worker(
            session_id, "autopilot_attempt_result_invalid", target=target)
        return
    attempt_id = attempt_payload.get("id")
    if not isinstance(attempt_id, int) or isinstance(attempt_id, bool):
        _fail_closed_p0a_attempt_worker(
            session_id, "autopilot_attempt_result_invalid", target=target)
        return
    if result.get("claim_lost") is True:
        # This invocation held a live claim on the exact same target and lost
        # the race to a concurrent worker; the "completed" projection is that
        # worker's write, not this one's. It may not even be routed yet — this
        # must be pure observation, never a race to route it too. A fresh
        # admission-time terminal read (no live claim ever held here) is not
        # marked this way and still falls through to the target-freshness
        # check below, so a genuine recovery GET can still route.
        return

    with DBSession(db.engine) as still_current_db:
        still_current = autopilot_orchestration.worker_target_still_current(
            still_current_db, target)
    if not still_current:
        # A newer worker has already taken this exact target past
        # processing_attempt (already routed, or now routing itself). This
        # completion is not this worker's to route — pure observation; a
        # normal recovery GET against the session's current target will still
        # route correctly on its own.
        return

    try:
        # Provider processing already committed. Follow-up command routing is a new,
        # short transaction under the same LiveState/device/control lock order as ACK.
        with _LIVE_WRITE_LOCK, device_capability.serialized_mutation():
            with DBSession(db.engine) as route_db:
                _live_row_for_update(route_db)
                autopilot_service.route_completed_attempt(
                    route_db,
                    session_id=session_id,
                    attempt_id=attempt_id,
                )
                route_db.commit()
    except Exception:
        # If a concurrent worker already routed, this no-ops because state is no
        # longer processing_attempt. Otherwise capture proof is rechecked and both
        # runtime/control planes are atomically paused.
        _fail_closed_p0a_attempt_worker(
            session_id, "autopilot_attempt_route_failed", target=target)


def _legacy_asr_result_is_well_formed(result: object) -> bool:
    """Full AsrResult contract, checked before anything durable is written.

    A malformed provider or stub must leave the capture exactly as it was so a
    later retry is still possible; it must never reach a checkpoint and persist
    half a transcript or a nonsense confidence.
    """
    if not isinstance(result, asr.AsrResult):
        return False
    # One shared contract with the terminal readback; duplicating it here is
    # exactly how the two sides drifted apart before.
    return autopilot_service.legacy_asr_facts_are_legal(
        text=result.asr_text,
        confidence=result.asr_confidence,
        engine_version=result.engine_version,
        hotword_hit=result.hotword_hit,
    )


def _legacy_recovery_asr(
        target: autopilot_orchestration.LegacyRepeatRecoveryTarget,
        blob: Path, bank: content.ItemBank) -> asr.AsrResult | None:
    """Run the one ASR call a legacy capture still owes, or give up quietly.

    The bytes actually read are hashed and measured against the checksum and
    size frozen in ``target`` before a single byte reaches a provider, so a
    replaced or truncated blob is never transcribed under this chain's identity.

    ``None`` means no usable transcript.  The legacy path deliberately writes
    nothing in that case: it has no failure semantics of its own, and inventing
    a technical-failure Attempt for a pre-protocol recording would manufacture
    evidence the recording never produced.  The capture lease simply expires and
    a later recovery trigger retries, exactly as for an abandoned worker.
    """
    engine = asr.get_engine()
    boundary = cloud_processing.provider_boundary(engine)
    if boundary is cloud_processing.DataBoundary.UNKNOWN:
        return None
    try:
        script = content.load_week1_script(content.CONTENT_DIR / "week1_script.json")
    except Exception:
        script = None
    hotwords = asr.build_hotwords(bank, script)
    audio_bytes = blob.read_bytes()
    if (audio_store.sha256_hex(audio_bytes) != target.audio_checksum
            or len(audio_bytes) != target.audio_byte_count):
        return None
    try:
        if boundary is cloud_processing.DataBoundary.CLOUD:
            with _serialized_cloud_provider_call(
                    session_id=target.session_id,
                    patient_id=target.patient_id,
                    provider=engine,
                    bind=db.engine):
                result = engine.transcribe(audio_bytes, hotwords)
        else:
            result = engine.transcribe(audio_bytes, hotwords)
    except Exception:
        return None
    if not _legacy_asr_result_is_well_formed(result):
        return None
    return result


def _legacy_recovery_judgement(
        target: autopilot_orchestration.LegacyRepeatRecoveryTarget,
        asr_text: str, bank: content.ItemBank) -> dict | None:
    """Judge the recovered transcript through the ordinary operational path.

    Every return value is checked against the same closed contract the terminal
    readback enforces, *before* it can be written. A provider that returns a
    shape the readback would later refuse must not reach a durable write: that
    would leave the Attempt completed-but-unclosable, with no way to retry.
    """
    try:
        engine = llm_judge.get_engine()
        boundary = cloud_processing.provider_boundary(engine)
        if boundary is cloud_processing.DataBoundary.UNKNOWN:
            return None
        if boundary is cloud_processing.DataBoundary.CLOUD:
            try:
                with _serialized_cloud_provider_call(
                        session_id=target.session_id,
                        patient_id=target.patient_id,
                        provider=engine,
                        bind=db.engine):
                    return _classify_operational(
                        item_id=target.attempt_input.item_id,
                        response_role=target.attempt_input.response_role,
                        text=asr_text, bank=bank, llm_engine=engine,
                        cloud_llm_allowed=True)
            except _CloudEgressNotAuthorized:
                return _classify_operational(
                    item_id=target.attempt_input.item_id,
                    response_role=target.attempt_input.response_role,
                    text=asr_text, bank=bank, allow_llm=False)
        result = _classify_operational(
            item_id=target.attempt_input.item_id,
            response_role=target.attempt_input.response_role,
            text=asr_text, bank=bank, llm_engine=engine)
    except Exception:
        return None
    return result


def _legacy_blob_matches(
        blob: Path | None,
        target: autopilot_orchestration.LegacyRepeatRecoveryTarget) -> bool:
    """Do the bytes on disk still hash and measure to the frozen capture?"""
    if blob is None or not blob.exists():
        return False
    data = blob.read_bytes()
    return (audio_store.sha256_hex(data) == target.audio_checksum
            and len(data) == target.audio_byte_count)


def _legacy_recovery_fence_holds(
        worker_db: DBSession, *,
        target: autopilot_orchestration.LegacyRepeatRecoveryTarget,
        expect_stage: str,
        blob: Path | None = None,
        claim_check: Callable[[DBSession], bool] | None = None) -> bool:
    """Re-prove the frozen target between two provider boundaries.

    ASR and judgement are two separate egress points.  A re-pair, cloud-consent
    revocation, researcher pause or takeover committed while a provider call was
    in flight must stop the next step before it happens: the whole evidence
    fingerprint — including the stage-specific Attempt and interaction facts —
    plus device pairing, consent, control generation and runtime status are
    re-derived here from the database, the audio bytes are re-hashed against the
    frozen checksum, and the caller's lease is re-confirmed atomically.
    ``False`` means this worker no longer owns the work: it then writes nothing
    at all and never touches the governance facts that won.
    """
    # Unconditional: a recording that has disappeared is exactly as disqualifying
    # as one whose bytes changed, and must never be treated as "nothing to check".
    if not _legacy_blob_matches(blob, target):
        return False
    with _LIVE_WRITE_LOCK:
        worker_db.rollback()
        worker_db.expire_all()
        try:
            current = autopilot_orchestration.verify_legacy_pre_repeat_recovery(
                worker_db, session_id=target.session_id)
        except autopilot_orchestration.AutopilotOrchestrationError:
            worker_db.rollback()
            return False
        if current.target != target or current.stage != expect_stage:
            worker_db.rollback()
            return False
        held = claim_check is None or claim_check(worker_db)
        if held:
            worker_db.commit()
        else:
            worker_db.rollback()
        return held


def _commit_legacy_recovery_asr(
        worker_db: DBSession, *,
        target: autopilot_orchestration.LegacyRepeatRecoveryTarget,
        claim: evidence_ledger.CaptureClaim,
        asr_result: asr.AsrResult) -> evidence_ledger.AttemptClaim | None:
    """Durable checkpoint 1: the transcript becomes a real ordinary Attempt.

    This is the crash boundary that makes ASR run exactly once.  After it
    commits, the capture is terminal, the Attempt exists at ``asr_completed``
    and every re-entry resumes at judgement instead of transcribing again —
    which could otherwise produce a second, different transcript for the same
    recording.

    The Attempt is created already holding this worker's own lease, and that
    lease is handed straight back as the returned :class:`AttemptClaim`; the
    same worker therefore continues into judgement without racing itself for a
    claim it already owns.  Only a crash lets the lease expire so another worker
    can legitimately take the judgement stage over.  ``None`` means the claim or
    the target was lost, and nothing is written in that case.
    """
    derived = target.attempt_input
    # Preserve raw-id -> LIVE order; the LIFO rollback callback runs before
    # either lock is released, on success, early return or exception.
    with ExitStack() as locks:
        locks.enter_context(
            audio_store.blob_mutation_lock(derived.raw_audio_id))
        locks.enter_context(_LIVE_WRITE_LOCK)
        worker_db.rollback()
        worker_db.expire_all()
        locks.callback(worker_db.rollback)
        # The pre-provider fence hashed the bytes before the transcript
        # existed, and nothing downstream reads the file again.  Re-resolve the
        # authoritative path now and re-prove it, before any durable write:
        # otherwise a recording replaced during transcription would become a
        # permanent Attempt describing bytes that no longer exist.
        if not _legacy_blob_matches(
                audio_store.find_blob(derived.raw_audio_id), target):
            worker_db.rollback()
            return None
        _live_row_for_update(worker_db)
        sess = worker_db.exec(select(TrainSession).where(
            TrainSession.session_id == target.session_id,
        ).with_for_update()).first()
        if sess is None:
            return None
        _lock_evidence_session(target.session_id, worker_db)
        current = autopilot_orchestration.verify_legacy_pre_repeat_recovery(
            worker_db, session_id=target.session_id)
        if (current.target != target
                or current.stage != autopilot_orchestration.LEGACY_STAGE_ASR):
            worker_db.rollback()
            return None
        now = datetime.now()
        if not evidence_ledger.confirm_capture_claim(
                worker_db, claim.capture_id, owner=claim.owner,
                generation=claim.generation, now=now):
            worker_db.rollback()
            return None
        attempt_owner = evidence_ledger.new_claim_owner()
        attempt = AttemptEvent(
            session_id=target.session_id,
            item_id=derived.item_id,
            turn_seq=derived.turn_seq,
            response_role=derived.response_role,
            attempt_seq=claim.proof_attempt_seq,
            raw_audio_id=derived.raw_audio_id,
            prompt_level=derived.prompt_level,
            cue_type=derived.cue_type,
            duration_seconds=derived.duration_seconds,
            asr_text=asr_result.asr_text,
            asr_confidence=asr_result.asr_confidence,
            asr_engine_version=asr_result.engine_version,
            processing_status="asr_completed",
            processing_owner=attempt_owner,
            processing_lease_expires_at=evidence_ledger.lease_deadline(now=now),
            processing_claimed_at=now,
            processing_generation=1,
            created_at=now,
            is_simulation=sess.is_simulation,
            judge_portrait_used=False,
        )
        worker_db.add(attempt)
        worker_db.flush()
        if not evidence_ledger.fenced_capture_update(
                worker_db, claim, next_status="asr_completed",
                values={
                    "asr_confidence": asr_result.asr_confidence,
                    "asr_engine_version": asr_result.engine_version,
                    "disposition": "answer_candidate",
                    "error_code": None,
                    "processed_at": now,
                    "final_attempt_id": attempt.id,
                }):
            worker_db.rollback()
            return None
        for event_type, payload in (
            ("attempt_received", {
                "raw_audio_id": derived.raw_audio_id,
                "prompt_level": derived.prompt_level,
                "cue_type": derived.cue_type,
                "duration_seconds": derived.duration_seconds,
                "processing_status": "received",
            }),
            ("asr_completed", {
                "asr_engine_version": asr_result.engine_version,
                "asr_confidence": asr_result.asr_confidence,
                "degraded": False,
                "hotword_hit": asr_result.hotword_hit,
            }),
        ):
            _append_interaction(
                worker_db, sess, event_type, payload,
                item_id=derived.item_id, turn_seq=derived.turn_seq,
                attempt=attempt)
        worker_db.commit()
        worker_db.expire_all()
        stored = worker_db.get(AttemptEvent, attempt.id)
        if stored is None:  # pragma: no cover - commit postcondition
            raise RuntimeError("legacy recovery lost its attempt row")
        return evidence_ledger.claim_from_attempt(stored)


def _legal_legacy_judgement(result: dict | None) -> dict | None:
    if result is None or not autopilot_service.legacy_judgement_result_is_legal(
            result):
        return None
    return result


def _commit_legacy_recovery_judgement(
        worker_db: DBSession, *,
        target: autopilot_orchestration.LegacyRepeatRecoveryTarget,
        claim: evidence_ledger.AttemptClaim,
        judgement: dict) -> bool:
    """Durable checkpoint 2: the already-persisted Attempt becomes completed."""
    derived = target.attempt_input
    # Preserve raw-id -> LIVE order.  The raw lock is taken before the final
    # byte check and held through the commit, so a legal upload or delete
    # cannot swap the recording while this transaction waits for LIVE.
    with ExitStack() as locks:
        locks.enter_context(
            audio_store.blob_mutation_lock(derived.raw_audio_id))
        worker_db.rollback()
        worker_db.expire_all()
        # The provider judged a transcript of these bytes; re-prove them here,
        # before any durable write, exactly as the ASR checkpoint does.
        if not _legacy_blob_matches(
                audio_store.find_blob(derived.raw_audio_id), target):
            worker_db.rollback()
            return False
        # Hash first, then take the global lock, to keep LIVE held briefly.
        locks.enter_context(_LIVE_WRITE_LOCK)
        # LIFO: rollback runs before either lock is released, on success,
        # early return or exception.
        locks.callback(worker_db.rollback)
        _live_row_for_update(worker_db)
        sess = worker_db.exec(select(TrainSession).where(
            TrainSession.session_id == target.session_id,
        ).with_for_update()).first()
        if sess is None:
            return False
        _lock_evidence_session(target.session_id, worker_db)
        current = autopilot_orchestration.verify_legacy_pre_repeat_recovery(
            worker_db, session_id=target.session_id)
        if (current.target != target
                or current.stage != autopilot_orchestration.LEGACY_STAGE_JUDGEMENT
                or current.attempt_id != claim.attempt_id):
            worker_db.rollback()
            return False
        if not evidence_ledger.fenced_attempt_update(
                worker_db, claim, expected_status="asr_completed",
                next_status="completed",
                values={
                    "operational_answer_type": judgement["answer_type"],
                    "operational_score": judgement["ai_score"],
                    "operational_needs_review": judgement["needs_review"],
                    "judge_mode": judgement["judge_mode"],
                    "judge_engine_version": judgement["judge_engine_version"],
                    "judge_reason": judgement["judge_reason"],
                    "matched_on": judgement["matched_on"],
                    "contains_target": judgement["contains_target"],
                    "judge_portrait_used": judgement["judge_portrait_used"],
                    "error_code": None,
                    "processed_at": datetime.now(),
                }):
            worker_db.rollback()
            return False
        worker_db.expire_all()
        stored = worker_db.get(AttemptEvent, claim.attempt_id)
        if stored is None:  # pragma: no cover - fenced-update postcondition
            raise RuntimeError("legacy recovery lost its attempt row")
        _append_interaction(worker_db, sess, "judgement_completed", {
            "answer_type": judgement["answer_type"],
            "score": judgement["ai_score"],
            "needs_review": judgement["needs_review"],
            "judge_mode": judgement["judge_mode"],
            "judge_engine_version": judgement["judge_engine_version"],
            "matched_on": judgement["matched_on"],
            "contains_target": judgement["contains_target"],
            "truth_scope": "operational_only",
        }, item_id=stored.item_id, turn_seq=stored.turn_seq, attempt=stored)
        worker_db.commit()
        return True


def _commit_legacy_recovery_pause(session_id: str) -> bool:
    """Atomically stop a legacy scope for good once its Attempt is judged."""
    with _LIVE_WRITE_LOCK, device_capability.serialized_mutation():
        with DBSession(db.engine) as pause_db:
            _live_row_for_update(pause_db)
            if not autopilot_orchestration.stage_legacy_repeat_recovery_pause(
                    pause_db, session_id=session_id):
                pause_db.rollback()
                return False
            _pause_runtime_in_transaction(session_id, pause_db)
            pause_db.commit()
            return True


def _claim_legacy_recovery_capture(
        worker_db: DBSession,
        target: autopilot_orchestration.LegacyRepeatRecoveryTarget,
) -> evidence_ledger.CaptureClaim | None:
    capture = worker_db.get(AttemptCaptureProcessing, target.capture_id)
    if evidence_ledger.has_active_capture_lease(capture):
        return None
    owner = evidence_ledger.new_claim_owner()
    if not evidence_ledger.try_claim_capture(
            worker_db, target.capture_id, owner=owner):
        return None
    worker_db.commit()
    claimed = worker_db.get(AttemptCaptureProcessing, target.capture_id)
    return evidence_ledger.claim_from_capture(claimed)


def _claim_legacy_recovery_attempt(
        worker_db: DBSession, attempt_id: int,
) -> evidence_ledger.AttemptClaim | None:
    """Take over an abandoned judgement stage after its lease really expired."""
    attempt = worker_db.get(AttemptEvent, attempt_id)
    if attempt is None or evidence_ledger.has_active_lease(attempt):
        return None
    owner = evidence_ledger.new_claim_owner()
    if not evidence_ledger.try_claim_attempt(
            worker_db, attempt_id, owner=owner):
        return None
    worker_db.commit()
    claimed = worker_db.get(AttemptEvent, attempt_id)
    return evidence_ledger.claim_from_attempt(claimed)


_LEGACY_STABLE_TARGET_FIELDS = (
    "session_id", "patient_id", "record_command_id", "source_command_id",
    "capture_id", "control_generation", "runner_generation", "state_revision",
    "audio_checksum", "audio_byte_count", "base_evidence_fingerprint",
    "attempt_input",
)


def _rebind_legacy_stage(
        worker_db: DBSession, *, session_id: str, expect_stage: str,
        previous: autopilot_orchestration.LegacyRepeatRecoveryTarget,
        expect_attempt_id: int | None,
) -> autopilot_orchestration.LegacyRepeatRecovery | None:
    """Re-freeze the target after a legitimate stage or claim transition.

    Claiming an attempt bumps its ``processing_generation``, and the ASR
    checkpoint creates the Attempt and its interactions outright — both live in
    the stage fingerprint, which is therefore allowed to change here. Nothing
    else is: the entire stable base — plan, session, patient, capability,
    control generations and revision, both commands, ACKs, receipt, audio tuple
    and the derived attempt input — must come back bit-identical, so a mutation
    slipped into the checkpoint or claim window cannot be laundered into a new,
    self-consistent target. Re-verifying alone would never notice that, because
    a rewritten chain is still internally consistent.
    """
    resolved = autopilot_orchestration.verify_legacy_pre_repeat_recovery(
        worker_db, session_id=session_id)
    if (resolved.stage != expect_stage
            or resolved.target.session_id != session_id
            or resolved.attempt_id != expect_attempt_id):
        return None
    if any(getattr(resolved.target, name) != getattr(previous, name)
           for name in _LEGACY_STABLE_TARGET_FIELDS):
        return None
    return resolved


def _try_stale_bearer_terminal_pause(
        request, path: str, status, row_session_id: str | None,
        row_token_hash: str | None) -> TerminalPauseOutcome:
    """The one authentication failure that may still finish a durable pause.

    Scoped to the exact ``GET /sessions/{id}/autopilot/next`` of the very
    session the digest-matched row is bound to, and to ``EXPIRED`` /
    ``RECOVERY_ONLY`` only.  It never calls the route, so no command projection
    and no provider work can follow, and it never changes the response.  Any
    failure inside is swallowed: an authentication refusal must not become a
    500 because an opportunistic recovery could not run.
    """
    if status not in {
            device_capability.CapabilityResolution.EXPIRED,
            device_capability.CapabilityResolution.RECOVERY_ONLY}:
        return TerminalPauseOutcome(False, None)
    if row_session_id is None or row_token_hash is None:
        return TerminalPauseOutcome(False, None)
    requested = access_policy.autopilot_next_session_id(request.method, path)
    if requested is None or requested != row_session_id:
        return TerminalPauseOutcome(False, None)
    try:
        return _commit_http_legacy_terminal_pause(
            token_hash=row_token_hash, session_id=row_session_id,
            allowed=_SAFE_CAPABILITY_TRANSITIONS[status])
    except Exception:  # noqa: BLE001 - opportunistic; the 401 stands either way
        return TerminalPauseOutcome(False, None)


_TERMINAL_PAUSE_ALLOWED_RESOLUTIONS = frozenset({
    device_capability.CapabilityResolution.EXPIRED,
    device_capability.CapabilityResolution.RECOVERY_ONLY,
    device_capability.CapabilityResolution.VALID,
})
# While a caller waits for the capability row lock a capability may only decay.
# Anything outside these sets means the locked row is not the one the caller
# resolved, so it may not act on it.
_SAFE_CAPABILITY_TRANSITIONS = {
    device_capability.CapabilityResolution.VALID: frozenset({
        device_capability.CapabilityResolution.VALID,
        device_capability.CapabilityResolution.EXPIRED,
        device_capability.CapabilityResolution.RECOVERY_ONLY,
    }),
    device_capability.CapabilityResolution.RECOVERY_ONLY: frozenset({
        device_capability.CapabilityResolution.RECOVERY_ONLY,
        device_capability.CapabilityResolution.EXPIRED,
    }),
    device_capability.CapabilityResolution.EXPIRED: frozenset({
        device_capability.CapabilityResolution.EXPIRED,
    }),
}
_CAPABILITY_ERROR_CODES = {
    device_capability.CapabilityResolution.INVALID: (
        "device_capability_invalid", "设备配对已失效，请由研究者重新配对"),
    device_capability.CapabilityResolution.EXPIRED: (
        "device_capability_expired", "设备配对已失效，请由研究者重新配对"),
    device_capability.CapabilityResolution.REVOKED: (
        "device_capability_revoked", "设备配对已失效，请由研究者重新配对"),
    device_capability.CapabilityResolution.RECOVERY_ONLY: (
        "device_capability_recovery_only",
        "本场设备配对只可恢复已落库的录音回执，请重新配对"),
}


class TerminalPauseOutcome(NamedTuple):
    """What the locked terminal transaction committed, and what it saw there.

    ``current`` comes from the same locked read that decided the write, so the
    caller never needs a second resolution and can never answer a bearer that
    has since been revoked with a stale feature/content refusal.
    """

    committed: bool
    current: object | None


def _capability_error(status) -> HTTPException:
    code, message = _CAPABILITY_ERROR_CODES[status]
    # Handler convention: FastAPI already wraps this under "detail", so the
    # payload itself must not carry a second "detail" key.
    return HTTPException(401, detail={"code": code, "message": message})


def _capability_slot_shape_allows_terminal(
        row: PatientDeviceCapability, *, session_id: str, status) -> bool:
    """The row's active/recovery slot must be one of exactly two legal shapes.

    The resolution enum alone is not enough: expiry is resolved before the
    recovery-only demotion, so an orphaned row (no active slot and no demotion
    timestamp), a contradictory one (demoted yet still holding an active slot)
    or one whose active slot belongs to another session can all surface as
    ``EXPIRED`` and would otherwise pass a pure enum test.
    """
    active_shape = (row.active_session_key == session_id
                    and row.recovery_only_at is None)
    recovery_shape = (row.active_session_key is None
                      and row.recovery_only_at is not None)
    if status is device_capability.CapabilityResolution.VALID:
        return active_shape
    if status is device_capability.CapabilityResolution.RECOVERY_ONLY:
        return recovery_shape
    if status is device_capability.CapabilityResolution.EXPIRED:
        return active_shape or recovery_shape
    return False


def _commit_http_legacy_terminal_pause(
        *, token_hash: str, session_id: str,
        allowed: frozenset) -> TerminalPauseOutcome:
    """Finish an already-durable legacy recovery for one exact bearer, atomically.

    Authorization and mutation are one critical section.  Resolving the bearer
    in an earlier transaction and committing in a later one leaves a real window
    in which a revoke commits in between and the old bearer still writes, so the
    exact capability row is locked and re-resolved *inside* the transaction that
    will write the pause.  Lock order matches pairing and revocation: LiveState,
    then the capability row, then the autopilot/runtime/evidence rows.

    Only a row still bound to the requested session may act, and only
    ``EXPIRED``/``RECOVERY_ONLY``/``VALID``: a ``REVOKED`` row, an unknown
    digest, a re-paired predecessor whose ``active_session_key`` moved on, or
    any session mismatch is a pure no-op.  Current LiveState is deliberately not
    required — a recovery-only capability usually means the session already left
    live, and this path projects no command.

    The scope must genuinely still owe the pause.  If a re-pair, researcher
    takeover, withdrawal or consent revocation already paused it, that pause is
    the governance winner and is never overwritten.  The trusted background
    worker keeps its own token-free helper; only this HTTP-triggered variant
    requires and revalidates a bearer.
    """
    with _LIVE_WRITE_LOCK, device_capability.serialized_mutation():
        with DBSession(db.engine) as terminal_db:
            _live_row_for_update(terminal_db)
            row = terminal_db.exec(select(PatientDeviceCapability).where(
                PatientDeviceCapability.token_hash == token_hash,
            ).with_for_update()).first()
            if row is None or row.session_id != session_id:
                terminal_db.rollback()
                return TerminalPauseOutcome(False, None)
            status, resolved = device_capability.resolve_capability_hash(
                terminal_db, token_hash)
            if (resolved is None or resolved.token_hash != token_hash
                    or status not in allowed
                    or status not in _TERMINAL_PAUSE_ALLOWED_RESOLUTIONS
                    or not _capability_slot_shape_allows_terminal(
                        resolved, session_id=session_id, status=status)):
                terminal_db.rollback()
                return TerminalPauseOutcome(False, status)
            if not autopilot_orchestration.stage_legacy_repeat_recovery_pause(
                    terminal_db, session_id=session_id):
                terminal_db.rollback()
                return TerminalPauseOutcome(False, status)
            _pause_runtime_in_transaction(session_id, terminal_db)
            terminal_db.commit()
            return TerminalPauseOutcome(True, status)


def _run_legacy_repeat_recovery_worker(session_id: str) -> None:
    """Finish the one recording a pre-protocol session left outstanding.

    A session admitted before the repeat protocol was frozen cannot run the
    modern flow: every generic gate refuses it, and its single ``received``
    capture would otherwise stay stuck forever.  This worker is the only path
    that resolves it, and it deliberately ends the scope: exactly one ordinary
    AttemptEvent, then a terminal pause.  It never classifies the transcript as
    an explicit repeat, never issues a command, and never routes onward — a
    patient asking to hear the prompt again under a protocol the session never
    froze is simply the ordinary answer this batch of data always recorded.

    The three stages are separated by commits, so a crash, a restart or a lease
    takeover resumes at the stage the database actually reached: a transcript is
    never produced twice, and a completed Attempt is never judged twice.  The
    target is re-frozen at every stage boundary, because the evidence identity
    legitimately grows as each checkpoint lands.
    """
    try:
        # A crash between the judgement commit and the pause leaves only the
        # pause owed. Finishing it needs no provider, no device and no content
        # resolution, so it is attempted first: routing that re-entry through
        # the full verifier would let a capability that expired afterwards
        # strand the scope in processing_attempt for good.
        if _commit_legacy_recovery_pause(session_id):
            return
        with DBSession(db.engine) as worker_db:
            with _LIVE_WRITE_LOCK, device_capability.serialized_mutation():
                worker_db.rollback()
                worker_db.expire_all()
                _live_row_for_update(worker_db)
                resolved = (
                    autopilot_orchestration.verify_legacy_pre_repeat_recovery(
                        worker_db, session_id=session_id))
                bank = content.load_item_bank_for_week(2)
                blob = audio_store.find_blob(
                    resolved.target.attempt_input.raw_audio_id)
                capture_claim = attempt_claim = None
                if resolved.stage == autopilot_orchestration.LEGACY_STAGE_ASR:
                    if not _legacy_blob_matches(blob, resolved.target):
                        worker_db.rollback()
                        return
                    capture_claim = _claim_legacy_recovery_capture(
                        worker_db, resolved.target)
                    if capture_claim is None:
                        worker_db.rollback()
                        return
                elif (resolved.stage
                        == autopilot_orchestration.LEGACY_STAGE_JUDGEMENT):
                    # A judgement re-entry owes the same physical proof as the
                    # ASR entry: without it a vanished recording would still
                    # earn a fresh claim and go on to be judged.
                    if not _legacy_blob_matches(blob, resolved.target):
                        worker_db.rollback()
                        return
                    attempt_claim = _claim_legacy_recovery_attempt(
                        worker_db, resolved.attempt_id)
                    if attempt_claim is None:
                        worker_db.rollback()
                        return
                    rebound = _rebind_legacy_stage(
                        worker_db, session_id=session_id,
                        expect_stage=(
                            autopilot_orchestration.LEGACY_STAGE_JUDGEMENT),
                        previous=resolved.target,
                        expect_attempt_id=attempt_claim.attempt_id)
                    if rebound is None:
                        worker_db.rollback()
                        return
                    resolved = rebound
            # Release every control lock before provider I/O, exactly as the
            # modern worker does.
            worker_db.rollback()

            if resolved.stage == autopilot_orchestration.LEGACY_STAGE_ASR:
                target = resolved.target
                asr_result = _legacy_recovery_asr(target, blob, bank)
                if asr_result is None:
                    return
                if not _legacy_recovery_fence_holds(
                        worker_db, target=target, blob=blob,
                        expect_stage=autopilot_orchestration.LEGACY_STAGE_ASR,
                        claim_check=lambda s: evidence_ledger.confirm_capture_claim(
                            s, capture_claim.capture_id,
                            owner=capture_claim.owner,
                            generation=capture_claim.generation)):
                    return
                attempt_claim = _commit_legacy_recovery_asr(
                    worker_db, target=target, claim=capture_claim,
                    asr_result=asr_result)
                if attempt_claim is None:
                    return
                worker_db.rollback()
                # Checkpoint 1 changed the evidence identity, so the judgement
                # stage runs against a freshly frozen target — bound to the
                # same capture, record command and Attempt as before.
                rebound = _rebind_legacy_stage(
                    worker_db, session_id=session_id,
                    expect_stage=(
                        autopilot_orchestration.LEGACY_STAGE_JUDGEMENT),
                    previous=target,
                    expect_attempt_id=attempt_claim.attempt_id)
                worker_db.rollback()
                if rebound is None:
                    return
                resolved = rebound

            if resolved.stage == autopilot_orchestration.LEGACY_STAGE_JUDGEMENT:
                target = resolved.target
                attempt = worker_db.get(AttemptEvent, attempt_claim.attempt_id)
                asr_text = attempt.asr_text
                worker_db.rollback()
                # Never open the second provider boundary after the scope
                # changed hands: a late worker must not send patient text to a
                # judge a committed governance action has already fenced out.
                if not _legacy_recovery_fence_holds(
                        worker_db, target=target, blob=blob,
                        expect_stage=(
                            autopilot_orchestration.LEGACY_STAGE_JUDGEMENT),
                        claim_check=lambda s: evidence_ledger.confirm_attempt_claim(
                            s, attempt_claim.attempt_id,
                            owner=attempt_claim.owner,
                            generation=attempt_claim.generation,
                            stage="asr_completed")):
                    return
                judgement = _legal_legacy_judgement(
                    _legacy_recovery_judgement(target, asr_text, bank))
                if judgement is None:
                    return
                if not _commit_legacy_recovery_judgement(
                        worker_db, target=target, claim=attempt_claim,
                        judgement=judgement):
                    return
        _commit_legacy_recovery_pause(session_id)
    except autopilot_orchestration.AutopilotOrchestrationError:
        # Every legacy refusal is observation-only: the modern safe-pause path
        # exists for provider/device failures inside a bound scope and must not
        # be repurposed to write control facts against pre-protocol data.
        return
    except Exception:
        return


def _manual_interaction_payload(body: InteractionAppendIn) -> dict:
    if body.event_type == "cue_selected":
        if body.prompt_level is None:
            raise HTTPException(422, "cue_selected 必须提供 prompt_level")
        return {"prompt_level": body.prompt_level, "cue_type": body.cue_type}
    if body.event_type == "feedback_selected":
        if body.feedback_key is None:
            raise HTTPException(422, "feedback_selected 必须提供 feedback_key")
        return {"feedback_key": body.feedback_key}
    if body.event_type == "technical_pause":
        if body.error_code is None:
            raise HTTPException(422, "technical_pause 必须提供 error_code")
        return {"error_code": body.error_code}
    if body.reason_code is None:
        raise HTTPException(422, "researcher_takeover 必须提供 reason_code")
    return {"reason_code": body.reason_code}


def _prepare_manual_interaction_locked(
        session_id: str, body: InteractionAppendIn,
        s: DBSession,
) -> tuple[TrainSession, AttemptEvent | None, str | None, int | None, dict]:
    """Validate a manual interaction without committing its evidence row."""
    sess = s.get(TrainSession, session_id)
    if not sess:
        raise HTTPException(404, "场次不存在")
    if body.event_type == "researcher_takeover":
        raise HTTPException(409, "人工接管旁路尚未开放；当前不能替代 AI attempt 证据")
    if body.event_type in {
            "cue_selected", "feedback_selected", "technical_pause"}:
        _ensure_manual_plane_writable(
            session_id, s, "从旧操作端写入人工提示、反馈或技术暂停")
    _ensure_runtime_writable(session_id, s, "追加交互证据")
    if body.event_type in {"cue_selected", "feedback_selected"}:
        _ensure_recording_allowed_for_session(
            session_id, s, is_simulation=sess.is_simulation)

    attempt = s.get(AttemptEvent, body.attempt_id) if body.attempt_id else None
    if (body.attempt_id
            and (attempt is None or attempt.session_id != session_id)):
        raise HTTPException(404, "attempt 不存在")
    item_id = body.item_id or (attempt.item_id if attempt else None)
    turn_seq = body.turn_seq or (attempt.turn_seq if attempt else None)
    if attempt is not None:
        if body.item_id is not None and body.item_id != attempt.item_id:
            raise HTTPException(409, "item_id 与 attempt 不一致")
        if body.turn_seq is not None and body.turn_seq != attempt.turn_seq:
            raise HTTPException(409, "turn_seq 与 attempt 不一致")
    if (item_id is None) != (turn_seq is None):
        raise HTTPException(422, "item_id 与 turn_seq 必须同时提供")
    if item_id is not None and turn_seq is not None:
        _frozen_plan_turn(sess, item_id, turn_seq)

    return sess, attempt, item_id, turn_seq, _manual_interaction_payload(body)


@app.post("/sessions/{session_id}/interaction-presentations")
def append_interaction_presentation(
        session_id: str, body: InteractionPresentationIn,
        request: Request, s: DBSession = Depends(get_session)):
    """Commit evidence and the patient-visible cursor in one transaction.

    A pause/wrap-up that wins the LiveState lock rejects the whole action.  If
    this action wins, both the immutable interaction and its presentation
    pointer become authoritative together; a refresh can therefore never
    consume a cue that the server did not make available to the patient.
    """
    with _LIVE_WRITE_LOCK:
        live = _live_row_for_update(s)
        sess = s.get(TrainSession, session_id)
        if sess is None:
            raise HTTPException(404, "场次不存在")
        _require_session_operator(
            request, sess, s, "提交并呈现床旁提示或反馈", mutation=True)

        cursor, request_hash = _interaction_presentation_cursor_and_hash(
            session_id, body)
        replay = _interaction_presentation_replay(
            s,
            session_id=session_id,
            idempotency_key=body.idempotency_key,
            request_hash=request_hash,
        )
        if replay is not None:
            return replay

        if live is None or _live_session_id(live) != session_id:
            raise HTTPException(status_code=409, detail={
                "code": "interaction_presentation_live_session_changed",
                "message": "当前床旁场次已切换；交互证据与呈现均未提交",
            })

        interaction = body.interaction
        sess, attempt, item_id, turn_seq, event_payload = \
            _prepare_manual_interaction_locked(session_id, interaction, s)
        if item_id is None or turn_seq is None:
            raise HTTPException(422, "床旁提示或反馈必须绑定冻结题目与环节")

        # Both values describe the exact server snapshot the operator saw.
        # They are mandatory on this route and are never copied into the new
        # command; a successful commit receives a fresh server wseq/revision.
        expected_revision = cursor.pop("expected_revision")
        expected_wseq = cursor.pop("wseq")

        state = _runtime_row(session_id, s)
        if state.status != "active":
            raise HTTPException(status_code=409, detail={
                "code": "interaction_presentation_runtime_inactive",
                "message": f"场次状态为 {state.status}；交互证据与呈现均未提交",
            })
        if expected_revision != state.revision:
            raise HTTPException(status_code=409, detail={
                "code": "interaction_presentation_revision_changed",
                "message": "场次运行版本已变化；交互证据与呈现均未提交",
                "current_revision": state.revision,
            })
        _validate_cursor(sess, cursor, s)
        if (cursor.get("screen") != "present"
                or cursor.get("recording", "idle") != "idle"
                or cursor.get("rawAudioId") is not None):
            raise HTTPException(
                422, "原子提示/反馈只能提交无音频指针的 present + idle 游标")

        plan = _session_plan_for_runtime(sess)
        planned_item = plan.items[cursor["itemIdx"]]
        planned_turn = planned_item.turns[cursor["turnIdx"]]
        if planned_item.item_id != item_id or planned_turn.turn_seq != turn_seq:
            raise HTTPException(status_code=409, detail={
                "code": "interaction_presentation_frozen_position_mismatch",
                "message": "交互证据与呈现游标不是同一冻结环节",
            })

        current = _json_load(live.cursor_json)
        if (current is None
                or _payload_session_id(current) != session_id
                or current.get("itemIdx") != cursor["itemIdx"]
                or current.get("turnIdx") != cursor["turnIdx"]
                or current.get("screen") in {"thanks", "done", "paused"}):
            raise HTTPException(status_code=409, detail={
                "code": "interaction_presentation_position_changed",
                "message": "床旁位置已推进、暂停或结束；交互证据与呈现均未提交",
            })
        current_wseq = _wseq_from(current)
        if current_wseq != expected_wseq:
            raise HTTPException(status_code=409, detail={
                "code": "interaction_presentation_wseq_changed",
                "message": "床旁呈现序列已变化；交互证据与呈现均未提交",
                "current_wseq": current_wseq,
            })
        current_level = current.get("cueLevel", 0)
        if (not isinstance(current_level, int) or isinstance(current_level, bool)
                or current_level < 0 or current_level > 3):
            raise HTTPException(409, "当前床旁提示等级无效")
        if attempt is not None:
            if attempt.processing_status != "completed":
                raise HTTPException(status_code=409, detail={
                    "code": "interaction_presentation_attempt_incomplete",
                    "message": "只有已完成的逐次 AI 证据才能驱动提示或反馈呈现",
                })
            if attempt.prompt_level != current_level:
                raise HTTPException(status_code=409, detail={
                    "code": "interaction_presentation_attempt_context_changed",
                    "message": "逐次 AI 证据的提示上下文已不是当前床旁等级",
                })
            existing_branch = s.exec(select(InteractionEvent).where(
                InteractionEvent.attempt_id == attempt.id,
                InteractionEvent.event_type.in_((
                    "cue_selected", "feedback_selected")),
            ).order_by(InteractionEvent.event_seq)).first()
            if existing_branch is not None:
                raise HTTPException(status_code=409, detail={
                    "code": "interaction_presentation_attempt_already_resolved",
                    "message": "该 completed attempt 已绑定唯一提示/反馈分支",
                })

        if interaction.event_type == "cue_selected":
            if current_level >= 3:
                raise HTTPException(status_code=409, detail={
                    "code": "interaction_presentation_cue_not_next",
                    "message": "当前已是最高提示等级，不得重复告知答案",
                })
            expected_level = current_level + 1
            if (interaction.prompt_level != expected_level
                    or cursor.get("cueLevel") != expected_level):
                raise HTTPException(status_code=409, detail={
                    "code": "interaction_presentation_cue_not_next",
                    "message": "提示必须按冻结协议逐级升级，且证据等级必须与呈现等级一致",
                })
            if (cursor.get("fbKey") is not None
                    or cursor.get("fbItemId") is not None):
                raise HTTPException(422, "提示呈现不得夹带反馈指针")
        else:
            if attempt is None:
                raise HTTPException(
                    422, "feedback_selected 必须绑定已完成的 attempt")
            if (cursor.get("cueLevel", 0) != current_level
                    or cursor.get("fbKey") != interaction.feedback_key
                    or cursor.get("fbItemId") != item_id):
                raise HTTPException(status_code=409, detail={
                    "code": "interaction_presentation_feedback_mismatch",
                    "message": "反馈证据、冻结题位与呈现指针必须完全一致",
                })

        protocol = content.load_autopilot_protocol(
            content.CONTENT_DIR / "autopilot_protocol_v1.json")
        try:
            cue_text, feedback_text = patient_presentation.resolve_task_texts(
                item_id=planned_item.item_id,
                task_type=planned_item.task_type,
                display=planned_item.display,
                response_role=planned_turn.response_role,
                cue_level=cursor.get("cueLevel", 0),
                protocol=protocol,
                feedback_key=cursor.get("fbKey"),
                feedback_item_id=cursor.get("fbItemId"),
            )
        except ValueError as exc:
            raise HTTPException(status_code=409, detail={
                "code": "interaction_presentation_content_unavailable",
                "message": str(exc),
            }) from exc
        selected_text = (
            cue_text if interaction.event_type == "cue_selected"
            else feedback_text)
        if not isinstance(selected_text, str) or not selected_text.strip():
            raise HTTPException(status_code=409, detail={
                "code": "interaction_presentation_content_unavailable",
                "message": "当前冻结题位没有可呈现的对应提示/反馈话术",
            })

        _lock_evidence_session(session_id, s)
        try:
            event = _append_interaction(
                s, sess, interaction.event_type, event_payload,
                item_id=item_id, turn_seq=turn_seq, attempt=attempt)
            command_wseq = _allocate_live_wseq(live)
            cursor["wseq"] = command_wseq
            if interaction.event_type == "feedback_selected":
                # One server clock drives both cross-device ordering and the
                # patient player's feedback de-duplication trigger.
                cursor["fbSeq"] = command_wseq
            live.cursor_json = _json.dumps(cursor, ensure_ascii=False)
            live.seq += 1
            live.updated_at = datetime.now()
            state.cursor_json = _json.dumps(_safe_cursor(cursor), ensure_ascii=False)
            state.revision += 1
            state.updated_at = datetime.now()
            receipt = InteractionPresentationReceipt(
                session_id=session_id,
                interaction_event_id=event.id,
                attempt_id=attempt.id if attempt is not None else None,
                idempotency_key=body.idempotency_key,
                request_hash=request_hash,
                cursor_json=_json.dumps(cursor, ensure_ascii=False),
                live_seq=live.seq,
                command_wseq=command_wseq,
                runtime_revision=state.revision,
            )
            s.add(live)
            s.add(state)
            s.add(receipt)
            s.commit()
            s.refresh(event)
            s.refresh(live)
            s.refresh(state)
            s.refresh(receipt)
        except IntegrityError:
            s.rollback()
            raced_replay = _interaction_presentation_replay(
                s,
                session_id=session_id,
                idempotency_key=body.idempotency_key,
                request_hash=request_hash,
            )
            if raced_replay is not None:
                return raced_replay
            if attempt is not None and s.exec(select(
                    InteractionPresentationReceipt).where(
                        InteractionPresentationReceipt.attempt_id == attempt.id,
                    )).first() is not None:
                raise HTTPException(status_code=409, detail={
                    "code": "interaction_presentation_attempt_already_resolved",
                    "message": "该 completed attempt 已并发绑定另一提示/反馈分支",
                })
            raise HTTPException(status_code=409, detail={
                "code": "interaction_presentation_concurrent_conflict",
                "message": "原子呈现并发冲突；交互证据与呈现均未提交",
            })
    return _interaction_presentation_receipt_payload(
        receipt, event, idempotent=False)


@app.post("/sessions/{session_id}/technical-pause")
def commit_technical_pause(
        session_id: str, body: TechnicalPauseIn,
        request: Request, s: DBSession = Depends(get_session)):
    """Atomically fence work, append one pause fact, and stop the bedside.

    A fresh command consumes the exact active runtime revision and live cursor
    wseq supplied by the console.  An exact retry may read the durable receipt
    only while that paused snapshot is still current; it is never a command to
    replay an obsolete stop after resume, switch, or another transition.
    """
    with _LIVE_WRITE_LOCK, device_capability.serialized_mutation():
        s.rollback()
        s.expire_all()
        sess = s.exec(select(TrainSession).where(
            TrainSession.session_id == session_id,
        ).with_for_update()).first()
        if sess is None:
            raise HTTPException(404, "场次不存在")
        operator = _require_session_operator(
            request, sess, s, "提交原子技术暂停", mutation=True)
        _require_started_visit_plan_session(session_id, s, sess=sess)
        # Match pause/abort across worker processes: the process-local mutex is
        # advisory only, while these database locks are authoritative.
        live = _live_row_for_update(s)
        request_hash = _technical_pause_request_hash(session_id, body)
        replay = _technical_pause_replay(
            s, session_id=session_id, body=body,
            request_hash=request_hash)
        if replay is not None:
            return replay

        state = s.get(SessionRuntimeState, session_id)
        # Preflight before staging any mutation. The exact same full cursor/CAS
        # contract is re-proved after the authoritative lock order below.
        _technical_pause_active_position(sess, live, state, body, s)

        try:
            staged_autopilot_pause = (
                autopilot_service.pause_autonomous_scope_for_researcher(
                    s,
                    session_id=session_id,
                    actor_id=operator,
                    reason_code=body.error_code,
                    source="atomic_technical_pause",
                )
            )
            if not staged_autopilot_pause:
                # Manual/non-autonomous processing can still have a provider
                # call in flight.  Its generation must lose before evidence and
                # the bedside stop become visible.
                evidence_ledger.invalidate_processing_claims(
                    s, session_id=session_id)
                evidence_ledger.invalidate_capture_processing_claims(
                    s, session_id=session_id)
            # Match the worker/control lock order: runtime is acquired only
            # after autopilot and recoverable attempt generations are fenced.
            s.expire(state)
            state = s.exec(select(SessionRuntimeState).where(
                SessionRuntimeState.session_id == session_id,
            ).with_for_update()).first()
            planned_item, planned_turn = _technical_pause_active_position(
                sess, live, state, body, s)

            attempt = None
            if body.attempt_id is not None:
                attempt = s.exec(select(AttemptEvent).where(
                    AttemptEvent.id == body.attempt_id,
                ).with_for_update()).first()
                if (attempt is None
                        or attempt.session_id != session_id
                        or attempt.item_id != planned_item.item_id
                        or attempt.turn_seq != planned_turn.turn_seq
                        or attempt.response_role != planned_turn.response_role
                        or attempt.is_simulation != sess.is_simulation):
                    raise HTTPException(status_code=409, detail={
                        "code": "technical_pause_attempt_context_mismatch",
                        "message": "attempt 不属于当前场次的冻结环节；技术证据与暂停均未提交",
                    })
            _lock_evidence_session(session_id, s)
            event = _append_interaction(
                s, sess, "technical_pause",
                {"error_code": body.error_code},
                item_id=planned_item.item_id,
                turn_seq=planned_turn.turn_seq,
                attempt=attempt,
            )
            state = _pause_runtime_in_transaction(session_id, s)
            paused_cursor = _json_load(live.cursor_json)
            paused_wseq = _wseq_from(paused_cursor)
            if (paused_cursor is None
                    or paused_cursor.get("screen") != "paused"
                    or paused_cursor.get("recording") != "stopped"
                    or paused_wseq is None):
                raise RuntimeError("原子技术暂停未生成老人端停止投影")
            receipt = TechnicalPauseReceipt(
                session_id=session_id,
                interaction_event_id=event.id,
                idempotency_key=body.idempotency_key,
                request_hash=request_hash,
                expected_runtime_revision=body.expected_revision,
                expected_live_wseq=body.expected_live_wseq,
                runtime_revision=state.revision,
                paused_cursor_wseq=paused_wseq,
                live_seq=live.seq,
                cursor_json=_json.dumps(paused_cursor, ensure_ascii=False),
            )
            s.add(receipt)
            s.commit()
            s.refresh(event)
            s.refresh(state)
            s.refresh(live)
            s.refresh(receipt)
        except autopilot_service.AutopilotServiceError as exc:
            _autopilot_write_failure(s, exc)
        except IntegrityError:
            s.rollback()
            raced_replay = _technical_pause_replay(
                s, session_id=session_id, body=body,
                request_hash=request_hash)
            if raced_replay is not None:
                return raced_replay
            source_winner = s.exec(select(TechnicalPauseReceipt).where(
                TechnicalPauseReceipt.session_id == session_id,
                TechnicalPauseReceipt.expected_runtime_revision
                == body.expected_revision,
                TechnicalPauseReceipt.expected_live_wseq
                == body.expected_live_wseq,
            )).first()
            if source_winner is not None:
                raise HTTPException(status_code=409, detail={
                    "code": "technical_pause_snapshot_already_committed",
                    "message": "该运行快照已由另一原子技术暂停命令消费",
                })
            raise HTTPException(status_code=409, detail={
                "code": "technical_pause_concurrent_conflict",
                "message": "原子技术暂停并发冲突；请刷新场次状态",
            })
        except Exception:
            s.rollback()
            raise
    return _technical_pause_receipt_payload(
        receipt, event, body, state, idempotent=False)


@app.post("/sessions/{session_id}/interactions", response_model=InteractionEvent)
def append_interaction(session_id: str, body: InteractionAppendIn,
                       request: Request,
                       s: DBSession = Depends(get_session)):
    with _LIVE_WRITE_LOCK:
        _live_row_for_update(s)
        sess = s.get(TrainSession, session_id)
        if sess is None:
            raise HTTPException(404, "场次不存在")
        _require_session_operator(
            request, sess, s, "追加床旁交互证据", mutation=True)
        if body.event_type == "technical_pause":
            raise HTTPException(status_code=409, detail={
                "code": "technical_pause_atomic_required",
                "message": "技术暂停必须使用原子停止命令；旧交互入口永不单独记账",
            })
        if body.event_type in {"cue_selected", "feedback_selected"}:
            # First preserve the server-owned-plane and strict event-specific
            # validation contracts; only an otherwise admissible legacy write
            # receives the atomic-route instruction.
            _prepare_manual_interaction_locked(session_id, body, s)
            raise HTTPException(status_code=409, detail={
                "code": "interaction_presentation_atomic_required",
                "message": "提示或反馈必须与患者端呈现指令原子提交",
            })
        return _append_interaction_locked(session_id, body, s)


def _append_interaction_locked(
        session_id: str, body: InteractionAppendIn,
        s: DBSession) -> InteractionEvent:
    sess, attempt, item_id, turn_seq, payload = \
        _prepare_manual_interaction_locked(session_id, body, s)
    _lock_evidence_session(session_id, s)
    try:
        row = _append_interaction(
            s, sess, body.event_type, payload, item_id=item_id,
            turn_seq=turn_seq, attempt=attempt)
        s.commit()
        s.refresh(row)
    except IntegrityError:
        s.rollback()
        raise HTTPException(409, "interaction event_seq 并发冲突，请重试")
    return row


@app.get("/sessions/{session_id}/attempts")
def list_attempts(session_id: str, request: Request, response: Response,
                  s: DBSession = Depends(get_session)):
    sess = s.get(TrainSession, session_id)
    if not sess:
        raise HTTPException(404, "场次不存在")
    _require_session_read_operator_if_admitted(
        request, sess, s, "读取场次 AI attempt 证据")
    response.headers["Cache-Control"] = "private, no-store"
    response.headers["Pragma"] = "no-cache"
    restriction_reason = _session_read_restriction_reason(sess, s)
    if restriction_reason is not None:
        counts = {
            "attempts": int(s.exec(select(func.count(AttemptEvent.id)).where(
                AttemptEvent.session_id == session_id)).one() or 0),
            "interactions": int(s.exec(select(func.count(InteractionEvent.id)).where(
                InteractionEvent.session_id == session_id)).one() or 0),
        }
        tombstone = _withdrawn_session_tombstone(
            sess, s, reason_code=restriction_reason, counts=counts)
        return {
            "attempts": [],
            "interactions": [],
            "truth_scope": "withdrawn_tombstone",
            "tombstone": tombstone,
        }
    attempts = list(s.exec(select(AttemptEvent).where(
        AttemptEvent.session_id == session_id).order_by(
            AttemptEvent.item_id, AttemptEvent.turn_seq, AttemptEvent.attempt_seq)))
    interactions = list(s.exec(select(InteractionEvent).where(
        InteractionEvent.session_id == session_id).order_by(InteractionEvent.event_seq)))
    return {"attempts": [_research_attempt_projection(row) for row in attempts],
            "interactions": interactions,
            "truth_scope": "operational_only"}


def _allowed_lock_values(task_type: str, response_role: str) -> set[float]:
    if task_type == "双要素" and response_role == "关系识别":
        return {0.0, 0.5, 1.0}
    return {0.0, 1.0}


@app.patch("/turns/{turn_id}/lock", response_model=TurnEvent)
def lock_turn(turn_id: int, body: LockIn, request: Request, s: DBSession = Depends(get_session)):
    """人工锁定评分（研究数据真值）。一旦锁定不得重复锁。"""
    with _LIVE_WRITE_LOCK:
        _live_row_for_update(s)
        t = _load_turn(turn_id, s)
        ie = s.get(ItemEvent, t.item_event_id)
        sess = s.get(TrainSession, ie.session_id) if ie else None
        if not ie or not sess:
            raise HTTPException(409, "环节缺少可追溯的题目/场次，禁止锁分")
        operator = _require_session_operator(
            request, sess, s, "锁定场次研究评分", mutation=True,
            not_found_detail="环节不存在")
        return _lock_turn_locked(t, ie, sess, body, operator, request, s)


def _lock_turn_locked(
        t: TurnEvent, ie: ItemEvent, sess: TrainSession, body: LockIn,
        operator: str, request: Request, s: DBSession) -> TurnEvent:
    _ensure_manual_plane_writable(
        sess.session_id, s, "在自动干预期间锁定研究评分",
        allow_after_intervention=True)
    _ensure_post_intervention_review_writable(
        sess.session_id, s, "锁定研究评分")
    if t.score_locked:
        raise HTTPException(409, "该环节已锁分，不可重复锁定")

    if t.source_attempt_id is None or not (t.raw_audio_id or "").strip():
        raise HTTPException(status_code=409, detail={
            "code": "research_review_audio_evidence_required",
            "message": "缺少绑定 source attempt 或原始录音引用，禁止锁定研究真值",
        })
    source_attempt = s.get(AttemptEvent, t.source_attempt_id)
    if (source_attempt is None
            or source_attempt.session_id != sess.session_id
            or source_attempt.item_id != ie.item_id
            or source_attempt.turn_seq != t.turn_seq
            or source_attempt.response_role != (t.response_role or "")
            or source_attempt.raw_audio_id != t.raw_audio_id
            or source_attempt.processing_status != "completed"
            or source_attempt.is_simulation != sess.is_simulation
            or source_attempt.judge_portrait_used
            or t.prompt_level != source_attempt.prompt_level):
        raise HTTPException(status_code=409, detail={
            "code": "research_review_attempt_evidence_mismatch",
            "message": "source attempt 与当前场次/题目/环节/角色/录音或数据边界不一致，禁止锁分",
        })
    audio_row = s.get(AudioAssetRow, t.raw_audio_id)
    if (audio_row is None
            or audio_row.session_id != sess.session_id
            or audio_row.turn_key != f"{ie.item_id}#{t.turn_seq}"
            or audio_row.is_simulation != sess.is_simulation
            or audio_row.data_classification != sess.data_classification):
        raise HTTPException(status_code=409, detail={
            "code": "research_review_audio_evidence_mismatch",
            "message": "录音的场次/题位/数据分类与当前研究环节不一致，禁止锁分",
        })
    _ensure_audio_read_allowed(audio_row, s)
    if not _audio_capture_evidence_is_verified(
            audio_row, s,
            require_capture_receipt=bool((sess.visit_plan_id or "").strip())):
        raise HTTPException(status_code=409, detail={
            "code": "research_review_audio_bytes_unavailable",
            "message": "服务器无法证明本场原始录音字节与上传收据一致，禁止锁分",
        })

    if t.confirmed_response_text is None:
        raise HTTPException(409, "须先人工确认 confirmed_response_text 才能锁分")
    phase = sess.phase_type.value if hasattr(sess.phase_type, "value") else str(sess.phase_type)
    event = sess.event_line.value if hasattr(sess.event_line, "value") else str(sess.event_line)
    if not 2 <= sess.week_no <= 8 or phase != "正式训练" or event != "正式训练":
        raise HTTPException(409, "仅允许在第2–8周正式训练事件中锁定评分")

    prompt_level = source_attempt.prompt_level
    if prompt_level not in (0, 1, 2, 3):
        raise HTTPException(409, "source attempt 的 prompt_level 无效，禁止锁分")
    if body.prompt_level is not None and body.prompt_level != prompt_level:
        raise HTTPException(status_code=409, detail={
            "code": "research_review_prompt_evidence_mismatch",
            "message": "客户端提示等级与 source attempt 权威证据不一致，禁止锁分",
        })

    bank = _load_bank_for_session(sess)
    if sess.week_no not in bank.supported_training_weeks:
        raise HTTPException(409, f"第{sess.week_no}周不在场次绑定题库的支持范围内，禁止锁分")
    found = _task_type_for_bank_item(bank, ie.item_id)
    if not found:
        raise HTTPException(409, f"题目 {ie.item_id} 不在场次绑定题库中")
    expected_task_type, _bank_item = found
    actual_task_type = ie.task_type.value if hasattr(ie.task_type, "value") else str(ie.task_type)
    if actual_task_type != expected_task_type:
        raise HTTPException(409, "题目事件 task_type 与绑定题库不一致")

    response_role = t.response_role or ""
    if expected_task_type == "单要素":
        valid_role = t.turn_seq == 1 and response_role == "命名"
    elif expected_task_type == "双要素":
        valid_role = (1 <= t.turn_seq <= len(runtime.DOUBLE_ROLES)
                      and response_role == runtime.DOUBLE_ROLES[t.turn_seq - 1])
    else:
        try:
            plan = runtime.build_session_plan(bank, sess.week_no, event)
        except ValueError as e:
            raise HTTPException(409, str(e))
        plan_item = next((item for item in plan.items if item.item_id == ie.item_id), None)
        plan_turn = next((turn for turn in (plan_item.turns if plan_item else ())
                          if turn.turn_seq == t.turn_seq), None)
        valid_role = plan_turn is not None and response_role == plan_turn.response_role
    if not valid_role:
        raise HTTPException(422, "turn_seq/response_role 与题库规定的评分环节不一致")

    allowed = _allowed_lock_values(expected_task_type, response_role)
    if (body.reviewed_score is not None
            and body.reviewed_score != body.element_value):
        raise HTTPException(status_code=409, detail={
            "code": "research_review_score_contract_mismatch",
            "message": "reviewed_score 必须与唯一研究评分 element_value 一致",
        })
    if not math.isfinite(body.element_value) or body.element_value not in allowed:
        raise HTTPException(422, f"该环节评分只允许 {sorted(allowed)}")

    t.reviewer_id = operator
    t.element_value = body.element_value
    t.reviewed_score = body.element_value
    t.prompt_level = prompt_level
    t.score_locked = True
    s.add(t)
    s.commit()
    s.refresh(t)
    _audit(s, request, "score_lock",
           f"锁分 第{sess.week_no}周 {ie.item_id} {response_role or ''} = {t.element_value}(提示级{prompt_level})",
           patient_id=sess.patient_id, session_id=sess.session_id, turn_id=t.id)
    return t


# ---------------- 异常/介入（phase 感知）----------------
class AbnormalIn(BaseModel):
    item_event_id: int | None = None
    abnormal_type: str | None = None
    intervention_type: str | None = None
    affects_scoring_validity: bool = False
    note: str | None = None


_CUE_INTERVENTIONS = {"代说物品名", "代说称呼"}


@app.post("/sessions/{session_id}/abnormal", response_model=AbnormalEvent)
def record_abnormal(session_id: str, body: AbnormalIn, request: Request, s: DBSession = Depends(get_session)):
    """记异常/介入。正式训练周的代说物品名/称呼 → 自动判为线索性介入且影响判分有效性。"""
    _require_account_identity(
        request, "写入场次异常或介入记录", roles={"researcher", "admin"},
        allow_local_m0=True)
    with _LIVE_WRITE_LOCK:
        _live_row_for_update(s)
        sess = s.get(TrainSession, session_id)
        if not sess:
            raise HTTPException(404, "场次不存在")
        _require_session_operator(
            request, sess, s, "写入场次异常或介入记录", mutation=True)
        if body.item_event_id is not None:
            # Validate both columns in one predicate.  A bare primary-key lookup
            # followed by trusting the path session would let a caller attach a
            # foreign session's item to this clinical event.  Missing and foreign
            # ids intentionally share one response to avoid an item-id oracle.
            linked_item = s.exec(select(ItemEvent).where(
                ItemEvent.id == body.item_event_id,
                ItemEvent.session_id == session_id,
            ).with_for_update()).first()
            if linked_item is None:
                raise HTTPException(
                    status_code=409,
                    detail={
                        "code": "abnormal_item_session_mismatch",
                        "message": "item_event_id 不属于路径指定场次",
                    },
                )
        _ensure_manual_plane_writable(
            session_id, s, "在自动干预期间写入人工异常或介入记录",
            allow_after_intervention=True)
        _ensure_research_review_writable(session_id, s, "写入场次异常或介入记录")
        data = body.model_dump()
        phase = sess.phase_type.value if hasattr(sess.phase_type, "value") else sess.phase_type
        if data.get("intervention_type") in _CUE_INTERVENTIONS and phase == "正式训练":
            data["abnormal_type"] = data.get("abnormal_type") or "线索性介入"
            data["affects_scoring_validity"] = True
        ev = AbnormalEvent(session_id=session_id, phase_type=sess.phase_type,
                           created_at=datetime.now(), **data)
        s.add(ev)
        s.commit()
        s.refresh(ev)
    # 只记类型/是否影响判分有效性,不写 note 自由文本(可能含识别信息)。
    _audit(s, request, "abnormal",
           f"异常/介入 {ev.abnormal_type or '?'}/{ev.intervention_type or '?'} 影响判分有效性={ev.affects_scoring_validity}",
           patient_id=sess.patient_id, session_id=session_id)
    return ev


# ---------------- 历史量表兼容（ScaleResult 只读；旧自由写入口永久关闭）----------------
class ScaleIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    phase_type: Literal["前测", "后测", "随访"]
    scale_name: str = PydanticField(
        min_length=1, max_length=160, pattern=r"^[^\r\n\x00]+$")
    subscale: str | None = PydanticField(
        default=None, min_length=1, max_length=160,
        pattern=r"^[^\r\n\x00]+$")
    score: float | None = None
    assessor_id: str | None = PydanticField(default=None, max_length=128)

    @model_validator(mode="after")
    def finite_score_only(self):
        if self.score is not None and not math.isfinite(self.score):
            raise ValueError("score 必须是有限数值")
        return self


@app.post("/patients/{patient_id}/scales", response_model=ScaleResult)
def create_scale(patient_id: str, body: ScaleIn, request: Request, s: DBSession = Depends(get_session)):
    _require_account_identity(
        request, "录入临床量表", roles={"researcher", "admin"},
        allow_local_m0=True)
    patient = s.get(Patient, patient_id)
    if not patient:
        raise HTTPException(404, "患者不存在,先建档")
    if ((patient.withdrawal_status or "").strip()
            or (patient.consent_status or "").strip().casefold()
            in _CONSENT_DENIED_STATUSES):
        raise HTTPException(409, "受试者已撤回或拒绝，禁止写入量表结果")
    readiness = scale_protocol.scale_protocol_readiness()
    if readiness.get("definition_ready") is not True:
        raise HTTPException(status_code=409, detail={
            "code": "scale_protocol_not_frozen",
            "message": "两类结局工具尚未由 PI 冻结具体量表、版本、条目与计分规则；自由填写结果不能进入研究数据",
            "readiness_status": readiness["status"],
        })
    if readiness.get("formal_result_contract_ready") is not True:
        raise HTTPException(status_code=409, detail={
            "code": "formal_scale_result_contract_unavailable",
            "message": "量表定义虽已冻结，但逐条目作答、计分证据与正式结果实例合同尚未实现；旧自由名称/分数容器不能进入研究数据",
            "readiness_status": readiness["status"],
        })
    if readiness.get("workflow_ready") is not True:
        raise HTTPException(status_code=409, detail={
            "code": "formal_scale_workflow_unavailable",
            "message": "正式量表结果合同虽已就绪，但应测、完成或批准延期、收尾与切换受试者的工作流尚未开放",
            "readiness_status": readiness["status"],
        })
    raise HTTPException(status_code=409, detail={
        "code": "legacy_scale_container_forbidden",
        "message": "正式量表必须走版本锁定的逐条目实例与计分合同；旧自由名称/分数容器永远不能自动晋升为正式研究结局",
        "readiness_status": readiness["status"],
    })


@app.get("/patients/{patient_id}/scales")
def list_scales(patient_id: str, request: Request, response: Response,
                s: DBSession = Depends(get_session)):
    _require_account_identity(
        request, "读取量表结果", roles={"researcher", "data_steward", "admin"},
        allow_local_m0=True)
    patient = s.get(Patient, patient_id)
    if not patient:
        raise HTTPException(404, "患者不存在")
    response.headers["Cache-Control"] = "private, no-store"
    response.headers["Pragma"] = "no-cache"
    if ((patient.withdrawal_status or "").strip()
            or (patient.consent_status or "").strip().casefold()
            in _CONSENT_DENIED_STATUSES):
        raise HTTPException(status_code=409, detail={
            "code": "subject_withdrawn_content_unavailable",
            "message": "受试者已撤回；量表内容读取已关闭",
            "patient_id": patient_id,
        })
    return list(s.exec(_select(ScaleResult, ScaleResult.patient_id == patient_id)))


# ---------------- 评分重建（只读）+ 去标识化导出 ----------------
@app.get("/sessions/{session_id}/audio-receipts")
def session_audio_receipts(
        session_id: str, request: Request, response: Response,
        after_seq: int = 0, limit: int = 500,
        s: DBSession = Depends(get_session)):
    """具名账号按服务端单调序号增量拉取录音收据；BroadcastChannel 仅作唤醒。"""
    _require_account_identity(
        request, "读取服务端录音收据", roles={"researcher", "data_steward", "admin"})
    if after_seq < 0:
        raise HTTPException(422, "after_seq 必须是非负整数")
    if limit < 1 or limit > 1_000:
        raise HTTPException(422, "limit 必须在 1..1000")
    sess = s.get(TrainSession, session_id)
    if not sess:
        raise HTTPException(404, "场次不存在")
    _require_session_read_operator_if_admitted(
        request, sess, s, "读取场次录音收据")
    response.headers["Cache-Control"] = "private, no-store"
    response.headers["Pragma"] = "no-cache"
    restriction_reason = _session_read_restriction_reason(sess, s)
    if restriction_reason is not None:
        _raise_withdrawn_session_read_conflict(
            sess, resource="audio_receipts", reason_code=restriction_reason)
    rows = audio_capture.list_receipts(
        s, session_id, after_seq=after_seq, limit=limit)
    return {
        "session_id": session_id,
        "after_seq": after_seq,
        "last_seq": rows[-1].server_seq if rows else after_seq,
        "receipts": rows,
    }


@app.get("/sessions/{session_id}/journal")
def session_journal(session_id: str, request: Request, response: Response,
                    s: DBSession = Depends(get_session)):
    """只读场次日志：同时返回研究真值链与独立的 AI operational 证据链。"""
    sess = s.get(TrainSession, session_id)
    if not sess:
        raise HTTPException(404, "场次不存在")
    _require_session_read_operator_if_admitted(
        request, sess, s, "读取或恢复场次日志")
    response.headers["Cache-Control"] = "private, no-store"
    response.headers["Pragma"] = "no-cache"
    restriction_reason = _session_read_restriction_reason(sess, s)
    if restriction_reason is not None:
        tombstone = _withdrawn_session_tombstone(
            sess, s, reason_code=restriction_reason)
        return {
            "session": {
                "session_id": sess.session_id,
                "data_classification": sess.data_classification,
                "is_simulation": sess.is_simulation,
                "content_state": "withdrawn_tombstone",
            },
            "items": [], "turns": [], "audios": [], "abnormal": [],
            "attempts": [], "interactions": [], "audio_receipts": [],
            "tts_serves": [], "confirmation_revisions": [],
            "tombstone": tombstone,
        }
    items = list(s.exec(select(ItemEvent)
                        .where(ItemEvent.session_id == session_id)
                        .order_by(ItemEvent.presentation_order, ItemEvent.id)))
    item_ids = [item.id for item in items if item.id is not None]
    turns = []
    if item_ids:
        turns = list(s.exec(select(TurnEvent)
                            .where(TurnEvent.item_event_id.in_(item_ids))
                            .order_by(TurnEvent.item_event_id, TurnEvent.turn_seq, TurnEvent.id)))
    audios = list(s.exec(select(AudioAssetRow)
                         .where(AudioAssetRow.session_id == session_id)
                         .order_by(AudioAssetRow.raw_audio_id)))
    abnormal = list(s.exec(select(AbnormalEvent)
                           .where(AbnormalEvent.session_id == session_id)
                           .order_by(AbnormalEvent.id)))
    attempts = list(s.exec(select(AttemptEvent)
                           .where(AttemptEvent.session_id == session_id)
                           .order_by(AttemptEvent.item_id, AttemptEvent.turn_seq,
                                     AttemptEvent.attempt_seq)))
    interactions = list(s.exec(select(InteractionEvent)
                               .where(InteractionEvent.session_id == session_id)
                               .order_by(InteractionEvent.event_seq)))
    audio_receipts = list(s.exec(select(AudioCaptureReceipt).where(
        AudioCaptureReceipt.session_id == session_id,
    ).order_by(AudioCaptureReceipt.server_seq)))
    tts_serves = list(s.exec(select(TtsServeEvidence).where(
        TtsServeEvidence.session_id == session_id,
    ).order_by(TtsServeEvidence.id)))
    # 修订账本的内容自由投影：who/when/revision，不带哈希与文本。
    turn_ids = [t.id for t in turns if t.id is not None]
    confirmation_revisions = []
    if turn_ids:
        for row in s.exec(select(TurnConfirmationRevision).where(
                TurnConfirmationRevision.turn_id.in_(turn_ids),
        ).order_by(TurnConfirmationRevision.turn_id,
                   TurnConfirmationRevision.revision)):
            confirmation_revisions.append({
                "turn_id": row.turn_id,
                "revision": row.revision,
                "actor_display_id": row.actor_display_id,
                "changed_at": row.changed_at,
            })
    return {"session": sess, "items": items, "turns": turns,
            "audios": audios, "abnormal": abnormal,
            "attempts": [_research_attempt_projection(row) for row in attempts],
            "interactions": interactions,
            "audio_receipts": audio_receipts,
            "tts_serves": tts_serves,
            "confirmation_revisions": confirmation_revisions}


@app.get("/sessions/{session_id}/ai-usage")
def session_ai_usage(session_id: str, request: Request, response: Response,
                     s: DBSession = Depends(get_session)):
    """本场次 AI 实际使用汇总，只聚合服务端账本。

    三类证据必须分开呈现，不能互相替代：探针通过（/ai/provider-readiness）
    只证明配置可达；本端点聚合的 TtsServeEvidence/AttemptEvent 才是"实际
    使用"；autopilot 控制状态另见 /sessions/{id}/autopilot/status。
    前端自报的引擎信息从不入账。
    """
    sess = s.get(TrainSession, session_id)
    if not sess:
        raise HTTPException(404, "场次不存在")
    _require_session_read_operator_if_admitted(
        request, sess, s, "读取场次 AI 使用证据")
    response.headers["Cache-Control"] = "private, no-store"
    response.headers["Pragma"] = "no-cache"
    restriction_reason = _session_read_restriction_reason(sess, s)
    if restriction_reason is not None:
        _raise_withdrawn_session_read_conflict(
            sess, resource="ai_usage", reason_code=restriction_reason)
    tts_engines: dict[str, dict[str, int]] = {}
    for row in s.exec(select(TtsServeEvidence).where(
            TtsServeEvidence.session_id == session_id)):
        agg = tts_engines.setdefault(row.engine_version, {
            "served": 0, "cache_hits": 0, "degraded": 0})
        if row.result == "served":
            agg["served"] += 1
            if row.cache_hit:
                agg["cache_hits"] += 1
        else:
            agg["degraded"] += 1
    asr_engines: dict[str, int] = {}
    asr_degraded = 0
    judge_modes: dict[tuple[str, str], int] = {}
    for attempt in s.exec(select(AttemptEvent).where(
            AttemptEvent.session_id == session_id)):
        if attempt.asr_engine_version:
            asr_engines[attempt.asr_engine_version] = (
                asr_engines.get(attempt.asr_engine_version, 0) + 1)
        if attempt.error_code == "asr_degraded":
            asr_degraded += 1
        if attempt.judge_mode:
            key = (attempt.judge_mode, attempt.judge_engine_version or "")
            judge_modes[key] = judge_modes.get(key, 0) + 1
    return {
        "session_id": session_id,
        "tts": {
            "engines": [
                {"engine_version": version, **agg}
                for version, agg in sorted(tts_engines.items())
            ],
        },
        "asr": {
            "engines": [
                {"engine_version": version, "attempts": count}
                for version, count in sorted(asr_engines.items())
            ],
            "degraded_attempts": asr_degraded,
        },
        "judge": {
            "modes": [
                {"judge_mode": mode, "judge_engine_version": engine,
                 "attempts": count}
                for (mode, engine), count in sorted(judge_modes.items())
            ],
        },
    }


@app.get("/sessions/{session_id}/scores")
def session_scores(session_id: str, request: Request, response: Response,
                   s: DBSession = Depends(get_session)):
    """从已锁定分环节值重建综合指标（单一事实源，不落库）。只读，不触发导出。"""
    sess = s.get(TrainSession, session_id)
    if not sess:
        raise HTTPException(404, "场次不存在")
    _require_session_read_operator_if_admitted(
        request, sess, s, "读取场次评分")
    response.headers["Cache-Control"] = "private, no-store"
    response.headers["Pragma"] = "no-cache"
    restriction_reason = _session_read_restriction_reason(sess, s)
    if restriction_reason is not None:
        _raise_withdrawn_session_read_conflict(
            sess, resource="scores", reason_code=restriction_reason)
    items = list(s.exec(_select(ItemEvent, ItemEvent.session_id == session_id)))
    tbi = {it.id: list(s.exec(_select(TurnEvent, TurnEvent.item_event_id == it.id))) for it in items}
    return export._reconstruct_scores(items, tbi)


class SessionExportIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    idempotency_key: str = PydanticField(min_length=32, max_length=200)

    @model_validator(mode="after")
    def validate_high_entropy_key(self):
        try:
            export.validate_export_idempotency_key(self.idempotency_key)
        except ValueError as exc:
            raise ValueError(str(exc)) from exc
        return self


@app.post("/sessions/{session_id}/export")
def session_export(
        session_id: str, body: SessionExportIn, request: Request,
        deidentify: bool = True, s: DBSession = Depends(get_session)):
    """场次收尾去标识化导出（默认走去标识通道，不带直接标识符）。触发音频导出闸门第一关。"""
    actor = _require_account_identity(
        request, "导出研究数据", roles={"admin", "data_steward"})
    actor_role = getattr(request.state, "actor_role", None)
    if deidentify is not True:
        raise HTTPException(403, "普通导出 API 仅开放去标识化通道")
    sess = s.get(TrainSession, session_id)
    if not sess:
        raise HTTPException(404, "场次不存在")
    try:
        res = export.export_session_bundle(
            s, session_id, deidentify=deidentify,
            idempotency_key=body.idempotency_key,
            actor_display_id=actor, actor_role=actor_role,
        )
    except export.ExportIdempotencyConflict as e:
        raise HTTPException(409, {
            "code": "export_idempotency_conflict", "message": str(e),
        })
    except (ValueError, RuntimeError) as e:
        raise HTTPException(409, str(e))
    _audit(s, request, "data_export",
           f"导出场次 batch={res['batch_id']} 去标识={res['deidentified']} 触及音频={res['audio_touched']}",
           # Export audit is batch-scoped.  Raw patient/session identifiers are
           # deliberately absent; the identifier-free ExportBatch ledger is the
           # authoritative linkage for this governance action.
           )
    return {"batch_id": res["batch_id"], "status": res["status"],
            "deidentified": res["deidentified"], "files": res["files"],
            "artifacts": res["artifacts"],
            "audio_touched": res["audio_touched"],
            "excluded_items": res["excluded_items"],
            "sheet_counts": res["sheet_counts"]}


@app.get("/exports/{batch_id}")
def export_batch_read(batch_id: str, request: Request,
                      s: DBSession = Depends(get_session)):
    """Verify and return one published batch without exposing storage roots."""
    _require_account_identity(
        request, "读取导出批次", roles={"admin", "data_steward"})
    try:
        return export.get_export_batch_result(s, batch_id)
    except export.LegacyExportBatchError as exc:
        raise HTTPException(409, {
            "code": "legacy_export_batch_unverified", "message": str(exc),
        })
    except ValueError as exc:
        raise HTTPException(404, str(exc))
    except RuntimeError as exc:
        raise HTTPException(409, str(exc))


@app.get("/exports/{batch_id}/{unknown_path:path}")
def export_unknown_subresource(
        batch_id: str, unknown_path: str, request: Request):
    """Keep unknown export API paths out of the SPA static-file fallback."""
    _require_account_identity(
        request, "读取导出治理资源", roles={"admin", "data_steward"})
    raise HTTPException(404, "导出子资源不存在")


def _select(model, where):
    from sqlmodel import select
    return select(model).where(where)


# ---------------- 生产静态托管(离线部署)----------------
# 前端构建为 web/dist(纯静态,无 node 运行时);存在即由本服务同源托管,医院机器只需 Python。
# 仅当 dist 存在时挂载 → 测试/纯后端环境零影响。SPA 客户端路由(/console /patient)回退 index.html。
def _safe_spa_candidate(dist: Path, full_path: str) -> Path | None:
    """Return a static file only when every decoded path stays inside ``dist``.

    Starlette has already URL-decoded the path parameter once, but a proxy or
    client can leave another encoded ``..`` layer behind.  Check bounded
    recursive decodings as well as backslash spellings, and resolve symlinks,
    before ``FileResponse`` ever sees the path.
    """
    root = dist.resolve()
    variants: list[str] = []
    current = full_path
    # A deeply re-encoded path is never a legitimate build artifact.  Decode
    # until stable, with a finite ceiling, and fail closed if another layer is
    # still present after the ceiling instead of accepting a proxy-dependent
    # interpretation.
    for _ in range(8):
        variants.append(current)
        decoded = unquote(current)
        if decoded == current:
            break
        current = decoded
    else:
        raise HTTPException(404, "静态资源不存在")
    try:
        for variant in variants:
            normalized = variant.replace("\\", "/")
            segments = normalized.split("/")
            # Do not merely rely on Path.resolve(): ``foo/../img`` resolves
            # inside dist and used to alias the permanently private /img
            # namespace.  Dot segments are therefore invalid at every decoded
            # layer, even when the final filesystem target remains in-root.
            if any(segment in {".", ".."} for segment in segments):
                raise HTTPException(404, "静态资源不存在")
            checked = (root / normalized).resolve()
            relative = checked.relative_to(root)
            if relative.parts and relative.parts[0].lower() == "img":
                raise HTTPException(404, "静态题图不公开")
            if access_policy.protected_api_namespace_alias(
                    "/" + normalized.lstrip("/")):
                raise HTTPException(404, "受保护 API 命名空间不是静态资源")
        candidate = (root / full_path.replace("\\", "/")).resolve()
        relative_candidate = candidate.relative_to(root)
        # Defense in depth for aliases that a future normalization change may
        # admit: the resolved first directory is the final namespace authority.
        if (relative_candidate.parts
                and relative_candidate.parts[0].lower() == "img"):
            raise HTTPException(404, "静态题图不公开")
        if (relative_candidate.parts
                and access_policy.protected_api_namespace_alias(
                    "/" + "/".join(relative_candidate.parts))):
            raise HTTPException(404, "受保护 API 命名空间不是静态资源")
        return candidate if full_path and candidate.is_file() else None
    except (OSError, RuntimeError, ValueError):
        raise HTTPException(404, "静态资源不存在") from None


def _mount_spa(target_app: FastAPI = app, dist_override: Path | None = None) -> None:
    from fastapi.responses import FileResponse
    from fastapi.staticfiles import StaticFiles

    dist = dist_override or (Path(__file__).resolve().parent.parent / "web" / "dist")
    if not dist.exists():
        return
    assets = dist / "assets"
    if assets.exists():
        target_app.mount("/assets", StaticFiles(directory=assets), name="assets")

    @target_app.api_route(
        "/{full_path:path}", methods=["GET", "HEAD"], include_in_schema=False)
    def spa_fallback(full_path: str):
        # 显式 API 路由已在上方优先匹配;此处只兜非 API 路径 → 返回 SPA 外壳。
        # 历史版本曾把题图放在 dist/img，只用 opaque 文件名不能
        # 阻止预览/枚举未来题。即使旧构建产物尚未清理，/img 也永久
        # 留在服务器私有边界之外；只有 current-only API 可返回字节。
        normalized = full_path.replace("\\", "/").lstrip("/")
        first_segment = normalized.split("/", 1)[0].lower()
        if first_segment == "img":
            raise HTTPException(404, "静态题图不公开")
        if access_policy.protected_api_namespace_alias("/" + full_path):
            raise HTTPException(404, "受保护 API 命名空间不是静态资源")
        candidate = _safe_spa_candidate(dist, full_path)
        if candidate is not None:
            return FileResponse(candidate)
        index = _safe_spa_candidate(dist, "index.html")
        if index is None:
            raise HTTPException(404, "静态资源不存在")
        return FileResponse(index)


_mount_spa()
