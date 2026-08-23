"""按受试者派生配对码 + 无状态设备绑定令牌(零新表、零迁移)。

设计口径:
  * 密钥没有独立部署项——由 CONSOLE_PIN 经 PBKDF2 域分隔拉伸而来。未配置
    CONSOLE_PIN 时整套"按受试者配对"功能 fail-closed 不可用;轮换 CONSOLE_PIN
    即同时作废全部已派发配对码与绑定令牌(这是特性,不是缺陷)。
  * 配对码 = 6 位数字,HMAC(key, "patient-pin-v1:"+patient_id+"#counter") 截断;
    counter 只用来确定性地跳过与 CONSOLE_PIN 撞值的候选,避免受试者配对码
    静默变成全局配对码。
  * 绑定令牌 = "pb1." + b64url(claims) + "." + b64url(HMAC),claims 只含
    patient_id/device_id/issued_at,不含姓名等 PII。令牌只能换当前属于本人
    的 live 场次能力(/device/attach),不是任何其他接口的凭据。
  * 本模块永不记录 PIN、配对码或令牌。
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from functools import lru_cache
from typing import Iterable

_PIN_DOMAIN = b"patient-pin-v1:"
_BIND_DOMAIN = b"patient-bind-v1"
_KEY_SALT = b"nmu-patient-pairing-v1"
# CONSOLE_PIN 本身熵低(6-32 位数字)。拉伸不是为了把它变成强密钥,而是把
# "拿到一枚绑定令牌→离线穷举出 CONSOLE_PIN"的成本抬高六个数量级。
_KEY_ITERATIONS = 600_000
_TOKEN_PREFIX = "pb1."
_TOKEN_MAX_LENGTH = 1024
_PATIENT_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_DEVICE_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{16,128}$")


class PatientPairingUnavailable(RuntimeError):
    """CONSOLE_PIN 未配置;按受试者配对功能整体不可用(fail-closed)。"""


def pairing_enabled() -> bool:
    return bool(os.environ.get("CONSOLE_PIN"))


@lru_cache(maxsize=4)
def _stretched_key(console_pin: str) -> bytes:
    return hashlib.pbkdf2_hmac(
        "sha256", console_pin.encode("utf-8"), _KEY_SALT, _KEY_ITERATIONS)


def _key() -> bytes:
    pin = os.environ.get("CONSOLE_PIN")
    if not pin:
        raise PatientPairingUnavailable(
            "未配置 CONSOLE_PIN,按受试者配对功能不可用")
    return _stretched_key(pin)


def derive_patient_pin(patient_id: str) -> str:
    """确定性 6 位配对码;绝不返回与 CONSOLE_PIN 相同的值。"""
    key = _key()
    console_pin = (os.environ.get("CONSOLE_PIN") or "").encode("utf-8")
    for counter in range(16):
        digest = hmac.new(
            key,
            _PIN_DOMAIN + patient_id.encode("utf-8") + b"#%d" % counter,
            hashlib.sha256,
        ).digest()
        candidate = f"{int.from_bytes(digest[:4], 'big') % 1_000_000:06d}"
        if not hmac.compare_digest(candidate.encode("ascii"), console_pin):
            return candidate
    raise PatientPairingUnavailable(  # pragma: no cover - 16 连撞不可达
        "无法为该受试者派生配对码")


def match_patient_pin(supplied_pin: str, patient_ids: Iterable[str]) -> list[str]:
    """返回配对码等于 supplied_pin 的全部受试者;逐个恒定时间比较,不提前退出。"""
    supplied = supplied_pin.encode("utf-8")
    matches: list[str] = []
    for patient_id in patient_ids:
        expected = derive_patient_pin(patient_id).encode("ascii")
        if hmac.compare_digest(expected, supplied):
            matches.append(patient_id)
    return matches


@dataclass(frozen=True)
class BindingClaims:
    patient_id: str
    device_id: str
    issued_at: int


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _b64url_decode(text: str) -> bytes:
    padded = text + "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(padded.encode("ascii"))


def _sign(payload: bytes) -> bytes:
    return hmac.new(_key(), _BIND_DOMAIN + b"\x00" + payload, hashlib.sha256).digest()


def issue_binding_token(patient_id: str, device_id: str, *,
                        now: datetime | None = None) -> str:
    if not _PATIENT_ID_PATTERN.fullmatch(patient_id):
        raise ValueError("patient_id 格式非法")
    if not _DEVICE_ID_PATTERN.fullmatch(device_id):
        raise ValueError("device_id 格式非法")
    issued = now or datetime.now(timezone.utc)
    payload = json.dumps(
        {"p": patient_id, "d": device_id, "t": int(issued.timestamp())},
        separators=(",", ":"), sort_keys=True,
    ).encode("utf-8")
    return _TOKEN_PREFIX + _b64url(payload) + "." + _b64url(_sign(payload))


def verify_binding_token(token: str) -> BindingClaims | None:
    """验签并解析;任何一处不合法一律返回 None,不区分失败原因。"""
    if (not isinstance(token, str) or len(token) > _TOKEN_MAX_LENGTH
            or not token.startswith(_TOKEN_PREFIX)):
        return None
    body = token[len(_TOKEN_PREFIX):]
    payload_part, separator, signature_part = body.partition(".")
    if not separator or not payload_part or not signature_part:
        return None
    try:
        payload = _b64url_decode(payload_part)
    except (ValueError, UnicodeEncodeError):
        return None
    # 签名按规范化 base64url 文本恒定时间比较:解码后比字节会因 base64 末位
    # 冗余比特把"改了一个字符的令牌"当成合法,非规范编码一律拒绝。
    if not hmac.compare_digest(
            _b64url(_sign(payload)).encode("ascii"),
            signature_part.encode("ascii", errors="replace")):
        return None
    try:
        claims = json.loads(payload.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return None
    if not isinstance(claims, dict) or set(claims) != {"p", "d", "t"}:
        return None
    patient_id, device_id, issued_at = claims["p"], claims["d"], claims["t"]
    if (not isinstance(patient_id, str)
            or not _PATIENT_ID_PATTERN.fullmatch(patient_id)
            or not isinstance(device_id, str)
            or not _DEVICE_ID_PATTERN.fullmatch(device_id)
            or not isinstance(issued_at, int) or issued_at < 0):
        return None
    return BindingClaims(
        patient_id=patient_id, device_id=device_id, issued_at=issued_at)
