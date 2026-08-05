"""认证开启时的集中访问策略。

共享 PIN 只代表“已配对的老人端设备”，不代表具名研究人员。所以它只能访问
完成当前场次所需的最小接口；患者、场次、评分、证据账本和数据治理等接口一律
要求具名账号。

路由策略放在一个模块中，避免中间件和处理器各自维护宽松正则而漏保护。
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import re
from typing import Pattern
from urllib.parse import unquote


class AccessKind(str, Enum):
    PUBLIC = "public"
    DEVICE_PAIR = "device_pair"
    DEVICE = "device"
    DEVICE_LIVE_WRITE = "device_live_write"
    ACCOUNT = "account"


KNOWN_ACCOUNT_ROLES = frozenset({"researcher", "data_steward", "admin"})
DATA_GOVERNANCE_ROLES = frozenset({"data_steward", "admin"})
TRAINING_OPERATION_ROLES = frozenset({"researcher", "admin"})
ADMIN_ROLES = frozenset({"admin"})
DEVICE_LIVE_WRITE_KINDS = frozenset({"audioSaved", "patientRec"})


@dataclass(frozen=True)
class AccessRule:
    kind: AccessKind
    roles: frozenset[str] = KNOWN_ACCOUNT_ROLES
    label: str = "访问该资源"


@dataclass(frozen=True)
class _RouteRule:
    methods: frozenset[str]
    pattern: Pattern[str]
    access: AccessRule

    def matches(self, method: str, path: str) -> bool:
        return method in self.methods and self.pattern.fullmatch(path) is not None


def _route(methods: set[str], pattern: str, kind: AccessKind, *,
           roles: frozenset[str] = KNOWN_ACCOUNT_ROLES,
           label: str) -> _RouteRule:
    return _RouteRule(frozenset(methods), re.compile(pattern), AccessRule(kind, roles, label))


# 顺序有意义：精确的老人端白名单和高敏感角色规则先于通用 API 兜底。
_ROUTE_RULES = (
    # 公开路径：登录簇自管认证。TTS 即使云端有白名单，本地引擎仍会消耗
    # CPU/磁盘缓存，因此只开放给已配对老人端设备或具名账号。
    _route({"POST"}, r"/auth/logout", AccessKind.ACCOUNT, label="退出研究账号"),
    _route({"POST"}, r"/auth/login", AccessKind.PUBLIC, label="登录研究账号"),
    _route({"GET", "HEAD"}, r"/auth/(?:config|me)", AccessKind.PUBLIC, label="读取认证状态"),
    _route({"GET", "HEAD"}, r"/health", AccessKind.PUBLIC, label="读取健康状态"),

    # 完整题库/脚本只供具名工作人员端使用。它们由服务器固定路由返回，
    # 不再复制进 SPA 静态目录；患者 capability 永远不能读取答案整包。
    _route(
        {"GET", "HEAD"},
        r"/content/(?:item-bank-bundle|week1-script|autopilot-protocol)",
        AccessKind.ACCOUNT,
        label="读取工作人员端冻结内容包",
    ),

    # PIN 只能进入这个配对口；配对后 PIN 不再是设备 API 凭据。
    _route({"POST"}, r"/device/pair", AccessKind.DEVICE_PAIR, label="配对受试者端设备"),

    # 老人端设备最小权限。具名账号可以进入这些双用途路由的认证层，但这
    # 不是场次对象授权：处理器仍须按 trainer owner / admin / 终态 steward
    # 规则重新核对目标场次。匿名始终不可访问。
    _route({"POST"}, r"/tts/speak", AccessKind.DEVICE,
           roles=TRAINING_OPERATION_ROLES, label="合成老人端话术"),
    _route({"GET", "HEAD"}, r"/live/state", AccessKind.DEVICE, label="读取老人端呈现状态"),
    _route({"GET", "HEAD"}, r"/sessions/[^/]+/patient-plan", AccessKind.DEVICE,
           label="读取老人端冻结训练计划"),
    _route({"GET", "HEAD"}, r"/sessions/[^/]+/patient-asset/current",
           AccessKind.DEVICE, label="读取当前老人端题图片"),
    _route({"GET", "HEAD"}, r"/sessions/[^/]+/plan", AccessKind.DEVICE, label="读取冻结训练计划"),
    _route({"GET", "HEAD"}, r"/sessions/[^/]+/patient-presentation", AccessKind.DEVICE,
           label="读取当前老人端呈现文本"),
    _route({"GET", "HEAD"}, r"/sessions/[^/]+/autopilot/next", AccessKind.DEVICE,
           label="读取当前自动驾驶命令"),
    _route({"GET", "HEAD"}, r"/sessions/[^/]+/autopilot/drain-target",
           AccessKind.DEVICE, label="恢复当前自动驾驶收麦目标"),
    _route({"POST"}, r"/sessions/[^/]+/autopilot/commands/[^/]+/tts",
           AccessKind.DEVICE, roles=TRAINING_OPERATION_ROLES,
           label="合成当前自动驾驶话术"),
    _route(
        {"POST"},
        r"/sessions/[^/]+/autopilot/commands/[^/]+/recording-authorization",
        AccessKind.DEVICE,
        roles=TRAINING_OPERATION_ROLES,
        label="申请当前自动驾驶录音授权",
    ),
    _route({"POST"}, r"/sessions/[^/]+/autopilot/commands/[^/]+/acks",
           AccessKind.DEVICE, roles=TRAINING_OPERATION_ROLES,
           label="回执当前自动驾驶命令"),
    _route({"POST"}, r"/sessions/[^/]+/autopilot/commands/[^/]+/drain-ack",
           AccessKind.DEVICE, roles=TRAINING_OPERATION_ROLES,
           label="回执当前自动驾驶收麦状态"),
    _route({"POST"}, r"/sessions/[^/]+/recording-authorization", AccessKind.DEVICE,
           roles=TRAINING_OPERATION_ROLES, label="检查当次录音授权"),
    _route({"POST"}, r"/audio", AccessKind.DEVICE,
           roles=TRAINING_OPERATION_ROLES, label="登记当次录音"),
    _route({"PUT"}, r"/audio/[^/]+/blob", AccessKind.DEVICE,
           roles=TRAINING_OPERATION_ROLES, label="上传当次录音"),
    _route({"POST"}, r"/live/patient-heartbeat", AccessKind.DEVICE,
           roles=TRAINING_OPERATION_ROLES, label="上报老人端在场状态"),
    _route({"PUT"}, r"/live/state", AccessKind.DEVICE_LIVE_WRITE,
           roles=TRAINING_OPERATION_ROLES, label="上报老人端录音状态"),

    # 服务端录音收据是后台权威证据源；BroadcastChannel/共享 PIN 只可唤醒，
    # 不可直接读取跨轮次账本。
    _route({"GET", "HEAD"}, r"/sessions/[^/]+/audio-receipts", AccessKind.ACCOUNT,
           label="读取服务端录音收据"),
    _route({"GET", "HEAD"}, r"/sessions/[^/]+/(?:outcome-summary|closeout)",
           AccessKind.ACCOUNT, label="读取场次自动汇总或现场收尾"),
    _route({"GET", "HEAD"}, r"/sessions/[^/]+/ai-usage", AccessKind.ACCOUNT,
           label="读取场次 AI 实际使用证据"),
    _route({"GET", "HEAD"}, r"/sessions/[^/]+/autopilot/status", AccessKind.ACCOUNT,
           label="读取自动驾驶所有权状态"),
    _route({"GET", "HEAD"}, r"/ai/provider-readiness", AccessKind.ACCOUNT,
           label="读取 AI 服务合成检查状态"),
    _route({"GET", "HEAD"}, r"/quality/ai-metrics", AccessKind.ACCOUNT,
           roles=KNOWN_ACCOUNT_ROLES, label="读取去标识 AI 质量汇总"),
    _route({"POST"}, r"/ai/provider-readiness/probe", AccessKind.ACCOUNT,
           roles=ADMIN_ROLES, label="执行 AI 服务合成检查"),
    _route({"POST"}, r"/sessions/[^/]+/autopilot/start", AccessKind.ACCOUNT,
           roles=TRAINING_OPERATION_ROLES, label="启动限定自动驾驶范围"),
    _route({"POST"}, r"/sessions/[^/]+/autopilot/takeover", AccessKind.ACCOUNT,
           roles=TRAINING_OPERATION_ROLES, label="显式接管自动驾驶"),

    # 训练安排是测试前的具名账号工作流。数据管理员可读取溯源，
    # 但不能建立、审批、启动或取消临床训练安排。
    _route({"GET", "HEAD"}, r"/visit-plans(?:/today)?", AccessKind.ACCOUNT,
           label="读取训练安排"),
    _route({"POST"}, r"/visit-plans", AccessKind.ACCOUNT,
           roles=TRAINING_OPERATION_ROLES, label="创建训练安排"),
    _route({"POST"}, r"/visit-plans/[^/]+/(?:approve|start|cancel)",
           AccessKind.ACCOUNT, roles=TRAINING_OPERATION_ROLES,
           label="变更训练安排状态"),

    # 一个部署工作区当前只对应一个获批课题组。Patient roster
    # 与 patient-level 量表是具名课题组的共享记录；场次内容仍由
    # 处理器按 trainer_id / terminal governance 做对象级授权。这些读口
    # 必须显式分类，不依赖已知 API 首段的通用 ACCOUNT 兜底。
    _route({"GET", "HEAD"}, r"/patients", AccessKind.ACCOUNT,
           roles=KNOWN_ACCOUNT_ROLES, label="读取课题组受试者名单"),
    _route({"GET", "HEAD"}, r"/patients/[^/]+", AccessKind.ACCOUNT,
           roles=KNOWN_ACCOUNT_ROLES, label="读取受试者档案"),
    _route({"GET", "HEAD"}, r"/patients/[^/]+/scales", AccessKind.ACCOUNT,
           roles=KNOWN_ACCOUNT_ROLES, label="读取受试者量表记录"),
    _route({"GET", "HEAD"}, r"/patients/[^/]+/assessment-events",
           AccessKind.ACCOUNT, roles=KNOWN_ACCOUNT_ROLES,
           label="读取受试者正式评估事件"),
    _route({"GET", "HEAD"}, r"/assessment-events/today",
           AccessKind.ACCOUNT, roles=TRAINING_OPERATION_ROLES,
           label="读取今日正式评估待办"),
    _route({"GET", "HEAD"}, r"/assessment-events/[^/]+",
           AccessKind.ACCOUNT, roles=KNOWN_ACCOUNT_ROLES,
           label="读取正式评估事件"),

    # 临床事实写入只属于 researcher/admin。data_steward 负责审计、完整性
    # 核验和去标识导出，不能借通用账号兜底修改档案、授权、场次、判定或量表。
    _route({"POST"}, r"/patients", AccessKind.ACCOUNT,
           roles=TRAINING_OPERATION_ROLES, label="登记受试者档案"),
    _route({"POST"}, r"/patients/[^/]+/withdrawal", AccessKind.ACCOUNT,
           roles=ADMIN_ROLES, label="登记研究撤回"),
    _route({"GET", "HEAD"}, r"/patients/[^/]+/withdrawal", AccessKind.ACCOUNT,
           roles=DATA_GOVERNANCE_ROLES, label="读取研究撤回回执"),
    _route({"PATCH"}, r"/patients/[^/]+/cloud-processing", AccessKind.ACCOUNT,
           roles=TRAINING_OPERATION_ROLES, label="更新受试者云处理授权"),
    _route({"POST"}, r"/patients/[^/]+/scales", AccessKind.ACCOUNT,
           roles=TRAINING_OPERATION_ROLES, label="录入临床量表"),
    _route({"POST"}, r"/patients/[^/]+/assessment-events", AccessKind.ACCOUNT,
           roles=TRAINING_OPERATION_ROLES, label="建立正式评估事件"),
    _route({"POST"}, r"/assessment-events/[^/]+/(?:start|close|cancel)",
           AccessKind.ACCOUNT, roles=TRAINING_OPERATION_ROLES,
           label="变更正式评估事件状态"),
    _route({"PUT"}, r"/assessment-instances/[^/]+/responses/[^/]+",
           AccessKind.ACCOUNT, roles=TRAINING_OPERATION_ROLES,
           label="保存正式评估条目响应"),
    _route({"POST"}, r"/assessment-instances/[^/]+/complete",
           AccessKind.ACCOUNT, roles=TRAINING_OPERATION_ROLES,
           label="服务端完成正式评估计分"),
    _route({"POST"}, r"/assessment-instances/[^/]+/approve-defer",
           AccessKind.ACCOUNT, roles=ADMIN_ROLES,
           label="批准正式评估延期"),
    _route({"POST"}, r"/sessions", AccessKind.ACCOUNT,
           roles=TRAINING_OPERATION_ROLES, label="建立训练场次"),
    _route({"PUT"}, r"/sessions/[^/]+/runtime/cursor", AccessKind.ACCOUNT,
           roles=TRAINING_OPERATION_ROLES, label="写入训练进度"),
    _route({"POST"}, r"/sessions/[^/]+/(?:pause|resume|finish-intervention|complete|abort)",
           AccessKind.ACCOUNT, roles=TRAINING_OPERATION_ROLES,
           label="变更训练场次状态"),
    _route({"PUT"}, r"/sessions/[^/]+/closeout", AccessKind.ACCOUNT,
           roles=TRAINING_OPERATION_ROLES, label="保存现场收尾记录"),
    _route({"POST"},
           r"/sessions/[^/]+/(?:items|attempts/process|interactions|interaction-presentations|technical-pause|abnormal)",
           AccessKind.ACCOUNT, roles=TRAINING_OPERATION_ROLES,
           label="写入训练或临床事实"),
    _route({"POST"}, r"/items/[^/]+/turns", AccessKind.ACCOUNT,
           roles=TRAINING_OPERATION_ROLES, label="写入训练回答环节"),
    _route({"PATCH"}, r"/turns/[^/]+/(?:confirm|lock)", AccessKind.ACCOUNT,
           roles=TRAINING_OPERATION_ROLES, label="确认或锁定训练判定"),
    _route({"POST"}, r"/turns/[^/]+/ai-judge", AccessKind.ACCOUNT,
           roles=TRAINING_OPERATION_ROLES, label="执行训练判定"),
    _route({"POST"}, r"/(?:asr/transcribe/[^/]+|judge/classify)", AccessKind.ACCOUNT,
           roles=TRAINING_OPERATION_ROLES, label="处理受试者回答"),
    _route({"POST"}, r"/(?:score/(?:single|double|multi)|judge/build-input)",
           AccessKind.ACCOUNT, roles=TRAINING_OPERATION_ROLES,
           label="计算训练判定输入或分值"),

    # 数据治理/完整性动作：需具名账号，且不开放给普通 researcher。
    _route({"GET", "HEAD"}, r"/audit(?:/.*)?", AccessKind.ACCOUNT,
           roles=DATA_GOVERNANCE_ROLES, label="读取或验证研究审计账本"),
    _route({"POST"}, r"/sessions/[^/]+/export", AccessKind.ACCOUNT,
           roles=DATA_GOVERNANCE_ROLES, label="导出去标识化研究数据"),
    _route({"GET", "HEAD"}, r"/exports/[^/]+", AccessKind.ACCOUNT,
           roles=DATA_GOVERNANCE_ROLES, label="读取并验证去标识导出批次"),
    # Future export sub-resources must never fall through to the generic API
    # account role (which includes researcher).  They remain governance-only
    # until deliberately classified.
    _route({"GET", "HEAD"}, r"/exports(?:/.*)?", AccessKind.ACCOUNT,
           roles=DATA_GOVERNANCE_ROLES, label="读取导出治理资源"),
    _route({"POST"}, r"/audio/[^/]+/(?:export|checksum|reliability-review)", AccessKind.ACCOUNT,
           roles=DATA_GOVERNANCE_ROLES, label="执行录音数据治理动作"),
    _route({"DELETE"}, r"/audio/[^/]+", AccessKind.ACCOUNT,
           roles=ADMIN_ROLES, label="物理删除录音"),
    _route({"GET", "HEAD"}, r"/governance/withdrawn-audio", AccessKind.ACCOUNT,
           roles=ADMIN_ROLES, label="读取撤回录音治理列表"),
)


# 这些首段是 API；若未被上面显式白名单命中，则默认要求具名账号。
_API_ROOTS = frozenset({
    "audit", "patients", "sessions", "content", "score", "judge", "audio",
    "live", "asr", "items", "turns", "cloud-processing", "auth", "tts", "device",
    "visit-plans",
    "ai", "quality", "exports", "governance", "assessment-events",
    "assessment-instances",
})

# These roots are permanently reserved for server-controlled APIs.  Static SPA
# fallback must not reinterpret case, backslashes, or another URL-decoding layer
# as a public file alias.  The docs roots are included because their canonical
# spellings are protected even though they are not ordinary API resource roots.
_PROTECTED_NAMESPACE_ROOTS = _API_ROOTS | frozenset({
    "docs", "redoc", "openapi.json",
})
_MAX_PATH_DECODE_LAYERS = 8

# These exact account endpoints replace the historical answer-bearing files in
# ``web/public/content``.  Keep the canonical paths in the authorization layer
# so the HTTP middleware can reject alternate spellings *before* credential
# handling or SPA fallback makes their behavior proxy/filesystem-dependent.
STAFF_CONTENT_BUNDLE_PATHS = frozenset({
    "/content/item-bank-bundle",
    "/content/week1-script",
    "/content/autopilot-protocol",
})
_LEGACY_ANSWER_BUNDLE_PATHS = frozenset({
    "/content/item_bank_v1.json",
    "/content/week1_script.json",
    "/content/autopilot_protocol_v1.json",
})


def _bounded_path_variants(path: str) -> tuple[tuple[str, ...], bool]:
    """Return recursive URL-decoding variants and whether decoding stabilized."""
    variants: list[str] = []
    current = path
    for _ in range(_MAX_PATH_DECODE_LAYERS):
        variants.append(current)
        decoded = unquote(current)
        if decoded == current:
            return tuple(variants), True
        current = decoded
    return tuple(variants), False


def protected_api_namespace_alias(path: str) -> bool:
    """Return whether any bounded decoded spelling enters a protected root.

    ASGI servers and reverse proxies can disagree about how many times a path is
    decoded, while Windows separators and case-insensitive filesystems can map a
    spelling that did not match a FastAPI route onto a real static file.  Treat
    every such spelling as protected.  Excessively nested encoding is not a
    legitimate build path and therefore also fails closed.
    """
    variants, stabilized = _bounded_path_variants(path)
    for current in variants:
        normalized = current.replace("\\", "/").lstrip("/")
        first_segment = normalized.split("/", 1)[0].lower()
        if first_segment in _PROTECTED_NAMESPACE_ROOTS:
            return True
    return not stabilized


def noncanonical_protected_namespace_alias(path: str) -> bool:
    """Detect alternate spellings of a server-reserved first path segment.

    This is intentionally separate from :func:`access_rule`: policy still
    classifies aliases as protected when used in isolation, while the live HTTP
    boundary can conceal case/backslash/encoded aliases with a uniform 404
    before checking whether a caller has credentials.
    """
    if not protected_api_namespace_alias(path):
        return False
    if not path.startswith("/") or path.startswith("//"):
        return True
    raw_first_segment = path[1:].split("/", 1)[0]
    return raw_first_segment not in _PROTECTED_NAMESPACE_ROOTS


def answer_bundle_alias_must_be_hidden(path: str) -> bool:
    """Return whether ``path`` is a legacy or non-exact answer bundle URL.

    Only the three canonical hyphenated account routes may reach FastAPI.
    Historical JSON filenames are permanently gone, and case, separator,
    repeated-slash, dot-segment, trailing-slash, and recursively encoded
    spellings of the replacements all resolve to 404 for every caller.
    """
    variants, stabilized = _bounded_path_variants(path)
    canonical_by_fold = {
        value.casefold(): value for value in STAFF_CONTENT_BUNDLE_PATHS
    }
    legacy_folds = {value.casefold() for value in _LEGACY_ANSWER_BUNDLE_PATHS}

    for current in variants:
        normalized = "/" + current.replace("\\", "/").lstrip("/")
        # Collapse spellings only for detection; this never authorizes or
        # resolves a file.  If the collapsed path names a protected bundle, the
        # non-exact raw request must be hidden.
        segments: list[str] = []
        for segment in normalized.split("/"):
            if not segment or segment == ".":
                continue
            if segment == "..":
                if segments:
                    segments.pop()
                continue
            segments.append(segment)
        collapsed = "/" + "/".join(segments)
        folded = collapsed.rstrip("/").casefold()
        if folded in legacy_folds:
            return True
        canonical = canonical_by_fold.get(folded)
        if canonical is not None and path != canonical:
            return True
    # An excessively nested encoding inside /content is not a legitimate
    # endpoint spelling.  Fail closed before auth/fallback disagreement.
    return not stabilized and protected_api_namespace_alias(path)


def access_rule(method: str, path: str) -> AccessRule:
    """返回请求所需的主体类型/角色。

    未知的静态 GET 保持公开，以便登录页和老人端 SPA 外壳能加载；未知的写请求
    默认要账号。已知 API 首段则一律 fail-closed。
    """
    normalized_method = method.upper()
    if normalized_method == "OPTIONS":
        return AccessRule(AccessKind.PUBLIC, label="完成预检请求")
    for rule in _ROUTE_RULES:
        if rule.matches(normalized_method, path):
            return rule.access

    if protected_api_namespace_alias(path):
        return AccessRule(AccessKind.ACCOUNT, label="访问研究平台 API")
    if normalized_method in {"GET", "HEAD"}:
        return AccessRule(AccessKind.PUBLIC, label="读取静态应用资源")
    # New mutations are unsafe until somebody deliberately classifies them.
    # Admin-only is the fail-closed fallback; route-coverage tests should then
    # force the author to choose a narrower product role before release.
    return AccessRule(
        AccessKind.ACCOUNT,
        roles=ADMIN_ROLES,
        label="执行尚未分类的管理写操作",
    )


def mutation_route_is_classified(method: str, path_template: str) -> bool:
    """Return whether a FastAPI mutation template has an explicit policy rule."""
    if method.upper() in {"GET", "HEAD", "OPTIONS"}:
        return True
    sample_path = re.sub(r"\{[^}]+\}", "X", path_template)
    return any(rule.matches(method.upper(), sample_path) for rule in _ROUTE_RULES)


def autopilot_next_session_id(method: str, path: str) -> str | None:
    """The session id of an exact ``GET /sessions/{id}/autopilot/next``, else None.

    Deliberately its own matcher rather than an entry in
    :func:`device_recovery_candidate`: that list grants a recovery-only bearer
    real route access, while this one only identifies the single route whose
    authentication failure may still finish an already-durable terminal pause.
    """
    # GET only. A HEAD (or any other verb) must never reach a write path.
    if method.upper() != "GET":
        return None
    matched = re.fullmatch(r"/sessions/([^/]+)/autopilot/next", path)
    return matched.group(1) if matched is not None else None


def device_recovery_candidate(method: str, path: str) -> bool:
    """Routes that may prove an already-persisted audio ACK after a session switch.

    Handlers must still verify exact immutable facts; this is not permission for a
    new registration, new upload, receipt creation, heartbeat, read, or microphone.
    """
    normalized_method = method.upper()
    return (
        (normalized_method == "POST" and path == "/audio")
        or (normalized_method == "PUT"
            and re.fullmatch(r"/audio/[^/]+/blob", path) is not None)
        or (normalized_method == "PUT" and path == "/live/state")
    )
