"""研究者账号 + PIN 保底的认证层（M1-D 公网部署）。

设计口径（与院内单机模式并存，向后兼容）：
  * 认证是否生效由环境决定，不查库：REQUIRE_AUTH=1 或设了 CONSOLE_PIN → 生效。
    回环单机开发两者都不设 → 全开，保持原行为（老人端麦克风 secure context 走 localhost）。
  * 生效时，受保护接口需满足其一：带有效会话 cookie（真实账号，携带审计身份），
    或带正确 X-Console-Pin（共享 PIN 保底，无身份）。
  * 密码只存 pbkdf2_sha256 派生值；会话表只存 token 的 sha256。明文都不落库。
  * 登录失败/错 PIN 计入同一个进程内限速器，连败即锁定该 IP 一段时间，挡暴力破解。

红线：本模块永不记录明文密码、明文 token、PIN。
"""
from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import threading
import time
from datetime import datetime, timedelta
from typing import Optional

from sqlmodel import Session as DBSession, select

from .models import AuthSession, ResearchUser

COOKIE_NAME = "nmu_session"
_PBKDF2_ITERATIONS = 600_000
_SESSION_TTL_HOURS = 12


# ---------------- 口令散列（stdlib pbkdf2，无第三方依赖）----------------
def hash_password(password: str) -> str:
    if not password or len(password) < 8:
        raise ValueError("密码至少 8 位")
    salt = secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, _PBKDF2_ITERATIONS)
    return f"pbkdf2_sha256${_PBKDF2_ITERATIONS}${salt.hex()}${dk.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, iters, salt_hex, hash_hex = stored.split("$")
        if algo != "pbkdf2_sha256":
            return False
        dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"),
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
        s.add(row); s.commit()
    return user


def revoke_session(s: DBSession, token: Optional[str]) -> None:
    if not token:
        return
    row = s.get(AuthSession, _token_hash(token))
    if row:
        s.delete(row); s.commit()


def cleanup_expired_sessions(s: DBSession) -> int:
    rows = list(s.exec(select(AuthSession).where(AuthSession.expires_at <= datetime.now())))
    for r in rows:
        s.delete(r)
    if rows:
        s.commit()
    return len(rows)


def authenticate(s: DBSession, username: str, password: str) -> Optional[ResearchUser]:
    user = s.get(ResearchUser, username)
    if not user or user.disabled:
        verify_password(password, _DUMMY_HASH)   # 等价成本,抹平"用户是否存在"的时延差
        return None
    if not verify_password(password, user.password_hash):
        return None
    user.last_login_at = datetime.now()
    s.add(user); s.commit()
    mark_users_present(True)
    return user


# ---------------- 生效判定 / 启动 fail-closed ----------------
def pin_configured() -> bool:
    return bool(os.environ.get("CONSOLE_PIN"))


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


def assert_deploy_credentials(s: DBSession) -> None:
    """公网部署 fail-closed：声明了 REQUIRE_AUTH 却没有任何可用凭据 → 拒绝启动。

    杜绝"上了公网却谁都能进"。回环开发不设 REQUIRE_AUTH，本检查跳过。
    """
    if not require_auth_env():
        return
    if not pin_configured() and not has_any_user(s):
        raise RuntimeError(
            "REQUIRE_AUTH=1 但既无 CONSOLE_PIN 也无任何研究者账号——"
            "公网部署缺认证，拒绝启动。先建账号: python scripts/manage_users.py create <用户名>")


# ---------------- 失败限速 / 锁定（进程内）----------------
# 单 worker 部署足够；多 worker 时各自计数（部署手册要求单 worker 或前置代理限速）。
_LOCK = threading.Lock()
_FAILURES: dict[str, list[float]] = {}
_LOCKED_UNTIL: dict[str, float] = {}


def _limit_window() -> float:
    return float(os.environ.get("AUTH_FAIL_WINDOW_SECONDS", "300") or 300)


def _limit_max() -> int:
    return int(os.environ.get("AUTH_FAIL_MAX", "8") or 8)


def _lock_seconds() -> float:
    return float(os.environ.get("AUTH_LOCK_SECONDS", "300") or 300)


def is_locked(ip: str) -> bool:
    with _LOCK:
        until = _LOCKED_UNTIL.get(ip)
        if until is None:
            return False
        if time.monotonic() >= until:
            _LOCKED_UNTIL.pop(ip, None)
            _FAILURES.pop(ip, None)
            return False
        return True


def record_failure(ip: str) -> None:
    now = time.monotonic()
    window = _limit_window()
    with _LOCK:
        hits = [t for t in _FAILURES.get(ip, []) if now - t < window]
        hits.append(now)
        _FAILURES[ip] = hits
        if len(hits) >= _limit_max():
            _LOCKED_UNTIL[ip] = now + _lock_seconds()
            _FAILURES.pop(ip, None)


def record_success(ip: str) -> None:
    with _LOCK:
        _FAILURES.pop(ip, None)
        _LOCKED_UNTIL.pop(ip, None)


def reset_for_tests() -> None:
    """测试隔离：清限速器 + 进程内"已有账号"粘滞位。"""
    global _USERS_PRESENT
    with _LOCK:
        _FAILURES.clear()
        _LOCKED_UNTIL.clear()
    _USERS_PRESENT = False
