"""研究者账号 + 老人端设备 PIN 的认证层（M1-D 公网部署）。

设计口径（与院内单机模式并存，向后兼容）：
  * 认证是否生效由环境决定，不查库：REQUIRE_AUTH=1 或设了 CONSOLE_PIN → 生效。
    回环单机开发两者都不设 → 全开，保持原行为（老人端麦克风 secure context 走 localhost）。
  * 生效时，研究数据/控制接口必须带具名账号会话；X-Console-Pin 只用于
    床旁配对，配对后老人端使用短时、绑定单场次的设备 capability。
  * 密码只存 pbkdf2_sha256 派生值；会话表只存 token 的 sha256。明文都不落库。
  * 登录失败/错 PIN 计入同一个进程内限速器，连败即锁定该 IP 一段时间，挡暴力破解。

红线：本模块永不记录明文密码、明文 token、PIN。
"""
from __future__ import annotations

import hashlib
import hmac
import math
import os
import re
import secrets
import threading
import time
from collections import OrderedDict
from datetime import datetime, timedelta
from typing import Optional

from sqlalchemy import func
from sqlmodel import Session as DBSession, select

from .models import AuthSession, ResearchUser

COOKIE_NAME = "nmu_session"
CSRF_COOKIE_NAME = "nmu_csrf"
_PBKDF2_ITERATIONS = 600_000
_SESSION_TTL_HOURS = 12
_PASSWORD_MAX_BYTES = 1024
_CSRF_CONTEXT = b"nmu-csrf-v1\0account-write"
_PAIRING_PIN_PATTERN = re.compile(r"^[0-9]{6,32}$")


# ---------------- 口令散列（stdlib pbkdf2，无第三方依赖）----------------
def hash_password(password: str) -> str:
    encoded = password.encode("utf-8")
    if not password or len(password) < 8:
        raise ValueError("密码至少 8 位")
    if len(encoded) > _PASSWORD_MAX_BYTES:
        raise ValueError("密码过长")
    salt = secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", encoded, salt, _PBKDF2_ITERATIONS)
    return f"pbkdf2_sha256${_PBKDF2_ITERATIONS}${salt.hex()}${dk.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        encoded = password.encode("utf-8")
        # PBKDF2 会按输入长度做额外工作；先给口令字节数设硬上限，避免公开登录口
        # 被超大请求体放大成 CPU/内存拒绝服务。
        if len(encoded) > _PASSWORD_MAX_BYTES:
            return False
        algo, iters, salt_hex, hash_hex = stored.split("$")
        if algo != "pbkdf2_sha256":
            return False
        dk = hashlib.pbkdf2_hmac("sha256", encoded,
                                 bytes.fromhex(salt_hex), int(iters))
        return hmac.compare_digest(dk.hex(), hash_hex)
    except (ValueError, AttributeError):
        return False


# 用户不存在时也要做与正常路径等价成本的散列,抹平计时旁路(用户名枚举)。
# 导入时预生成一次(真实迭代数),之后复用——占位散列迭代=1 会快 ~150 倍,等于没抹。
_DUMMY_HASH = hash_password("timing-equalizer-placeholder")


# ---------------- 会话（服务端，可撤销）----------------
def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def csrf_token(token: str) -> str:
    """由不可读的会话 bearer 派生当前会话的写请求证明，不另存服务端秘密。"""
    return hmac.new(token.encode("utf-8"), _CSRF_CONTEXT, hashlib.sha256).hexdigest()


def verify_csrf(token: Optional[str], supplied: Optional[str]) -> bool:
    if not token or not supplied:
        return False
    expected = csrf_token(token)
    try:
        return hmac.compare_digest(expected.encode("ascii"), supplied.encode("ascii"))
    except UnicodeEncodeError:
        return False


def create_session(s: DBSession, username: str, ttl_hours: int = _SESSION_TTL_HOURS) -> str:
    """新建会话，返回明文 token（只此一次可见，交给 httponly cookie）。"""
    token = secrets.token_urlsafe(32)
    now = datetime.now()
    s.add(AuthSession(token_hash=_token_hash(token), username=username,
                      created_at=now, expires_at=now + timedelta(hours=ttl_hours),
                      last_seen_at=now))
    s.commit()
    return token


def resolve_session(s: DBSession, token: Optional[str]) -> Optional[ResearchUser]:
    """校验 cookie token → 返回启用中的用户；过期/停用/未知一律 None。"""
    if not token:
        return None
    row = s.get(AuthSession, _token_hash(token))
    now = datetime.now()
    if not row or row.expires_at <= now:
        return None
    user = s.get(ResearchUser, row.username)
    if not user or user.disabled:
        return None
    # last_seen 每请求都写会与训练期高频游标写抢 SQLite 锁；节流到 5 分钟一次。
    if row.last_seen_at is None or (now - row.last_seen_at) > timedelta(minutes=5):
        row.last_seen_at = now
        s.add(row)
        s.commit()
    return user


def revoke_session(s: DBSession, token: Optional[str]) -> None:
    if not token:
        return
    row = s.get(AuthSession, _token_hash(token))
    if row:
        s.delete(row)
        s.commit()


def cleanup_expired_sessions(s: DBSession) -> int:
    rows = list(s.exec(select(AuthSession).where(AuthSession.expires_at <= datetime.now())))
    for r in rows:
        s.delete(r)
    if rows:
        s.commit()
    return len(rows)


def authenticate(s: DBSession, username: str, password: str) -> Optional[ResearchUser]:
    # FastAPI 会把同步登录处理器放入线程池。分布式攻击者可以换 IP
    # 绕过每 IP 锁定，同时占满线程池执行 PBKDF2。全局闸门在查账号前
    # 即拒绝超额请求：无论用户存在与否都返回同一个认证失败，不形成枚举旁路。
    if not _begin_password_verify():
        return None
    try:
        user = s.get(ResearchUser, username)
        if not user or user.disabled:
            verify_password(password, _DUMMY_HASH)   # 等价成本,抹平"用户是否存在"的时延差
            return None
        if not verify_password(password, user.password_hash):
            return None
    finally:
        _end_password_verify()
    user.last_login_at = datetime.now()
    s.add(user)
    s.commit()
    mark_users_present(True)
    return user


# ---------------- 生效判定 / 启动 fail-closed ----------------
def pin_configured() -> bool:
    raw = os.environ.get("CONSOLE_PIN")
    if raw is None:
        return False
    if not _PAIRING_PIN_PATTERN.fullmatch(raw):
        raise RuntimeError("CONSOLE_PIN 必须是 6–32 位 ASCII 数字；空值、空白、过短或非数字均拒绝启动")
    return True


def require_auth_env() -> bool:
    return os.environ.get("REQUIRE_AUTH", "").strip() in ("1", "true", "yes")


# 一旦库里存在账号，认证即视为生效（避免"建了账号却忘设 REQUIRE_AUTH → 裸奔"）。
# 启动时按库置位（见 main lifespan）；登录成功也置位。进程内粘滞，测试用 reset_for_tests 清。
_USERS_PRESENT = False


def mark_users_present(present: bool) -> None:
    global _USERS_PRESENT
    _USERS_PRESENT = _USERS_PRESENT or bool(present)


def auth_active() -> bool:
    """认证是否生效：设了 REQUIRE_AUTH/CONSOLE_PIN，或库里已有账号。回环纯开发三者皆无 → 全开。"""
    return require_auth_env() or pin_configured() or _USERS_PRESENT


def has_any_user(s: DBSession) -> bool:
    return s.exec(select(ResearchUser).limit(1)).first() is not None


def validate_display_id(display_id: str) -> str:
    """审计身份会持久进历史记录；拒绝空值、边界空白和控制字符。"""
    if not display_id or not display_id.strip():
        raise ValueError("审计身份 display_id 不得为空")
    if display_id != display_id.strip():
        raise ValueError("审计身份 display_id 首尾不得包含空白")
    if any(ord(char) < 32 or ord(char) == 127 for char in display_id):
        raise ValueError("审计身份 display_id 不得包含控制字符")
    return display_id


def assert_unique_audit_identities(s: DBSession) -> None:
    """旧库即使漏跑 Alembic，也不得带着可混淆审计归属启动。"""
    duplicate = s.exec(
        select(ResearchUser.display_id)
        .group_by(ResearchUser.display_id)
        .having(func.count(ResearchUser.username) > 1)
        .limit(1)
    ).first()
    if duplicate is not None:
        raise RuntimeError(
            "researchuser.display_id 存在重复，审计身份归属不唯一，"
            "拒绝启动。请由数据库管理员先核对并手工消除重复身份，"
            "再执行 Alembic 升级；系统不会自动改名。"
        )


def assert_deploy_credentials(s: DBSession) -> None:
    """受保护的完整平台必须同时具备账号与床旁配对 PIN。

    账号 cookie 不会再静默升级老人端权限；因此只有账号、没有 PIN 的
    配置即使 console 与 patient 在同一浏览器，老人端也无法安全配对。
    纯回环开发（无 REQUIRE_AUTH、无账号、无 PIN）仍保持开放模式。
    """
    has_user = has_any_user(s)
    if has_user:
        assert_unique_audit_identities(s)
    if not require_auth_env() and not has_user and not pin_configured():
        return
    if not has_user:
        raise RuntimeError(
            "REQUIRE_AUTH=1 但无任何研究者账号——操作端无法具名审计，"
            "拒绝启动。先建账号: python scripts/manage_users.py create <用户名>")
    if not pin_configured():
        raise RuntimeError(
            "已启用研究者账号但未配置 CONSOLE_PIN——老人端无法获取绑定场次的"
            "设备能力，即使同机双窗也会失败，拒绝启动。")


# ---------------- 失败限速 / 锁定（进程内）----------------
# 单 worker 部署足够；多 worker 时各自计数（部署手册要求单 worker 或前置代理限速）。
# OrderedDict 同时是 LRU 顺序；_TRACKED_KEYS 给失败窗口和锁定表的并集设
# 唯一硬上限，防止攻击者持续更换伪造 IP 造成无界内存增长。
_LOCK = threading.Lock()
_FAILURES: OrderedDict[str, list[float]] = OrderedDict()
_LOCKED_UNTIL: OrderedDict[str, float] = OrderedDict()
_TRACKED_KEYS: OrderedDict[str, None] = OrderedDict()
_GLOBAL_FAILURES: dict[str, list[float]] = {}
_GLOBAL_LOCKED_UNTIL: dict[str, float] = {}
_NEXT_SWEEP_AT = 0.0
_PASSWORD_VERIFY_IN_FLIGHT = 0

_TRACKED_KEYS_DEFAULT = 4096
_TRACKED_KEYS_HARD_MAX = 16_384
_PER_KEY_FAILURE_HARD_MAX = 256
_GLOBAL_FAILURE_HARD_MAX = 4096
_PASSWORD_VERIFY_CONCURRENCY_DEFAULT = 2
_PASSWORD_VERIFY_CONCURRENCY_HARD_MAX = 32


def _positive_float(name: str, default: float) -> float:
    try:
        value = float(os.environ.get(name, str(default)) or default)
    except (TypeError, ValueError):
        return default
    return value if math.isfinite(value) and value > 0 else default


def _positive_int(name: str, default: int, *, hard_max: int) -> int:
    try:
        value = int(os.environ.get(name, str(default)) or default)
    except (TypeError, ValueError):
        return default
    if value <= 0:
        return default
    return min(value, hard_max)


def _limit_window() -> float:
    return _positive_float("AUTH_FAIL_WINDOW_SECONDS", 300.0)


def _limit_max() -> int:
    return _positive_int(
        "AUTH_FAIL_MAX", 8, hard_max=_PER_KEY_FAILURE_HARD_MAX)


def _lock_seconds() -> float:
    return _positive_float("AUTH_LOCK_SECONDS", 300.0)


def _tracked_keys_max() -> int:
    return _positive_int(
        "AUTH_TRACKED_KEYS_MAX", _TRACKED_KEYS_DEFAULT,
        hard_max=_TRACKED_KEYS_HARD_MAX,
    )


def _global_limit_window() -> float:
    return _positive_float("AUTH_GLOBAL_FAIL_WINDOW_SECONDS", _limit_window())


def _global_limit_max() -> int:
    return _positive_int(
        "AUTH_GLOBAL_FAIL_MAX", 64, hard_max=_GLOBAL_FAILURE_HARD_MAX)


def _global_lock_seconds() -> float:
    # 全局锁定只是分布式失败的 CPU/PIN 保底，默认比单 IP 锁定短，
    # 减少攻击者利用它影响正常研究人员的可用性。
    return _positive_float("AUTH_GLOBAL_LOCK_SECONDS", 30.0)


def _password_verify_concurrency() -> int:
    return _positive_int(
        "AUTH_PBKDF2_MAX_CONCURRENCY", _PASSWORD_VERIFY_CONCURRENCY_DEFAULT,
        hard_max=_PASSWORD_VERIFY_CONCURRENCY_HARD_MAX,
    )


def _failure_scope(key: str) -> str:
    prefix, separator, _rest = key.partition(":")
    if separator and prefix in {"login", "pair"}:
        return prefix
    return "generic"


def _drop_tracked_key(key: str) -> None:
    _FAILURES.pop(key, None)
    _LOCKED_UNTIL.pop(key, None)
    _TRACKED_KEYS.pop(key, None)


def _touch_tracked_key(key: str) -> None:
    if key in _TRACKED_KEYS:
        _TRACKED_KEYS.move_to_end(key)
        return
    capacity = _tracked_keys_max()
    while len(_TRACKED_KEYS) >= capacity:
        oldest, _ = _TRACKED_KEYS.popitem(last=False)
        _FAILURES.pop(oldest, None)
        _LOCKED_UNTIL.pop(oldest, None)
    _TRACKED_KEYS[key] = None


def _sweep(now: float) -> None:
    """Amortized time-driven expiry; exact-key reads still expire immediately."""
    global _NEXT_SWEEP_AT
    if now < _NEXT_SWEEP_AT:
        return
    failure_cutoff = now - _limit_window()
    for key, hits in list(_FAILURES.items()):
        current = [hit for hit in hits if hit > failure_cutoff]
        if current:
            _FAILURES[key] = current
        else:
            _FAILURES.pop(key, None)
    for key, until in list(_LOCKED_UNTIL.items()):
        if now >= until:
            _LOCKED_UNTIL.pop(key, None)
    for key in list(_TRACKED_KEYS):
        if key not in _FAILURES and key not in _LOCKED_UNTIL:
            _TRACKED_KEYS.pop(key, None)

    global_cutoff = now - _global_limit_window()
    for scope, hits in list(_GLOBAL_FAILURES.items()):
        current = [hit for hit in hits if hit > global_cutoff]
        if current:
            _GLOBAL_FAILURES[scope] = current
        else:
            _GLOBAL_FAILURES.pop(scope, None)
    for scope, until in list(_GLOBAL_LOCKED_UNTIL.items()):
        if now >= until:
            _GLOBAL_LOCKED_UNTIL.pop(scope, None)

    # 默认每 10 秒最多做一次 O(cap) 全表扫描，避免攻击者把清理本身
    # 放大为 CPU DoS；短窗口配置下仍保证至少一个窗口内清理。
    _NEXT_SWEEP_AT = now + min(10.0, _limit_window(), _lock_seconds())


def _begin_password_verify() -> bool:
    global _PASSWORD_VERIFY_IN_FLIGHT
    with _LOCK:
        if _PASSWORD_VERIFY_IN_FLIGHT >= _password_verify_concurrency():
            return False
        _PASSWORD_VERIFY_IN_FLIGHT += 1
        return True


def _end_password_verify() -> None:
    global _PASSWORD_VERIFY_IN_FLIGHT
    with _LOCK:
        # finally 配对调用；max 是防御性保护，避免测试替身导致负数。
        _PASSWORD_VERIFY_IN_FLIGHT = max(0, _PASSWORD_VERIFY_IN_FLIGHT - 1)


def is_locked(key: str, *, now: float | None = None) -> bool:
    current = time.monotonic() if now is None else now
    with _LOCK:
        _sweep(current)
        scope = _failure_scope(key)
        global_until = _GLOBAL_LOCKED_UNTIL.get(scope)
        if global_until is not None:
            if current < global_until:
                return True
            _GLOBAL_LOCKED_UNTIL.pop(scope, None)

        until = _LOCKED_UNTIL.get(key)
        if until is None:
            if key in _FAILURES:
                _touch_tracked_key(key)
            return False
        if current >= until:
            _drop_tracked_key(key)
            return False
        _LOCKED_UNTIL.move_to_end(key)
        _touch_tracked_key(key)
        return True


def record_failure(key: str, *, now: float | None = None) -> None:
    current = time.monotonic() if now is None else now
    window = _limit_window()
    with _LOCK:
        _sweep(current)
        scope = _failure_scope(key)
        global_hits = [
            hit for hit in _GLOBAL_FAILURES.get(scope, [])
            if current - hit < _global_limit_window()
        ]
        global_hits.append(current)
        if len(global_hits) >= _global_limit_max():
            _GLOBAL_LOCKED_UNTIL[scope] = current + _global_lock_seconds()
            _GLOBAL_FAILURES.pop(scope, None)
        else:
            _GLOBAL_FAILURES[scope] = global_hits

        _touch_tracked_key(key)
        hits = [t for t in _FAILURES.get(key, []) if current - t < window]
        hits.append(current)
        _FAILURES[key] = hits
        _FAILURES.move_to_end(key)
        if len(hits) >= _limit_max():
            _LOCKED_UNTIL[key] = current + _lock_seconds()
            _LOCKED_UNTIL.move_to_end(key)
            _FAILURES.pop(key, None)


def record_success(key: str) -> None:
    with _LOCK:
        _drop_tracked_key(key)


def reset_for_tests() -> None:
    """测试隔离：清限速器 + 进程内"已有账号"粘滞位。"""
    global _USERS_PRESENT, _NEXT_SWEEP_AT, _PASSWORD_VERIFY_IN_FLIGHT
    with _LOCK:
        _FAILURES.clear()
        _LOCKED_UNTIL.clear()
        _TRACKED_KEYS.clear()
        _GLOBAL_FAILURES.clear()
        _GLOBAL_LOCKED_UNTIL.clear()
        _NEXT_SWEEP_AT = 0.0
        _PASSWORD_VERIFY_IN_FLIGHT = 0
    _USERS_PRESENT = False
