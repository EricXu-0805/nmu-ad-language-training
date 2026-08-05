"""Synthetic, patient-free provider readiness for server autopilot.

Possessing an API key is not evidence that any model is reachable or enabled.
This module probes the exact runtime engines with a fixed allow-listed phrase
and persists metadata-only results.  Generated audio never enters the research
audio store or ledger; an ASR provider may use its existing self-deleting,
process-private scratch adapter while making the provider request.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import json
import math
import os
import re
import secrets
from typing import Any

from pydantic import BaseModel, ConfigDict
from sqlmodel import Session as DBSession, select

from . import asr, content, judging, llm_judge, tts
from .enums import AnswerType
from .models import ProviderReadinessProbe


SCHEMA_VERSION = "provider-readiness.v2"
RUNTIME_CONTRACT = "p0a_sim_first_single_v1"
PROBE_TEXT = "您好"
TTL_ENV = "PROVIDER_READINESS_TTL_MINUTES"
CREDENTIAL_FINGERPRINT_KEY_ENV = "PROVIDER_READINESS_FINGERPRINT_KEY"
DEFAULT_TTL_MINUTES = 30
_MAX_VERSION_LENGTH = 200
_MIN_FINGERPRINT_KEY_BYTES = 32
_MIN_FINGERPRINT_KEY_DISTINCT_CHARACTERS = 12
_NON_REUSABLE_SECRET_ENV_NAMES = (
    "DASHSCOPE_API_KEY",
    "DEIDENTIFICATION_KEY",
    "CONSOLE_PIN",
)
# Narrow public request-shape contract: only the currently supported
# non-secret cache_params shapes (Qwen/CosyVoice speech_rate, Piper
# length_scale, the synthetic test-double contract voice=synthetic, and the
# harness stand-in engine's versioned literal harness-synthetic-v1).
# Anything outside this allowlist is not trusted as safe/non-secret by
# construction -- see _cache_params().  Deliberately not "or hash it": a
# hash of a low-entropy secret is offline-guessable, so an unrecognized
# value is rejected outright, never digested.
_CACHE_PARAMS_ALLOWLIST = re.compile(
    r"^(?:(?:speech_rate|length_scale)=\d{1,3}(?:\.\d{1,3})?"
    r"|voice=synthetic|harness-synthetic-v1)$")
_UNAPPROVED_CACHE_PARAMS_MESSAGE = (
    "engine cache_params does not match the approved public request-shape "
    "allowlist; refusing to fingerprint an unrecognized/unapproved value")


def _utc_now_naive() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def ttl_minutes() -> int:
    raw = os.environ.get(TTL_ENV, str(DEFAULT_TTL_MINUTES)).strip()
    try:
        value = int(raw)
    except ValueError as exc:
        raise RuntimeError(f"{TTL_ENV} 必须是整数") from exc
    if not 5 <= value <= 1440:
        raise RuntimeError(f"{TTL_ENV} 必须在 5..1440 之间")
    return value


def _clean_version(value: object) -> str:
    text = str(value or "unknown").strip() or "unknown"
    return text[:_MAX_VERSION_LENGTH]


def _boundary(engine: object) -> str:
    explicit = getattr(engine, "data_boundary", None)
    if explicit in {"local", "cloud", "unknown"}:
        return str(explicit)
    cloud = getattr(engine, "cloud", None)
    return "cloud" if cloud is True else "local" if cloud is False else "unknown"


def _provider_id(engine: object) -> str | None:
    value = getattr(engine, "provider_id", None)
    if isinstance(value, str) and value.strip():
        return value.strip()[:120]
    # Existing TTS providers pre-date the shared provider boundary protocol.
    if getattr(engine, "cloud", False) is True and _clean_version(
            getattr(engine, "version", "")).startswith("dashscope/"):
        return "aliyun-dashscope"
    return None


def _cache_params(engine: object) -> str:
    """Public, non-secret request-shape descriptor (e.g. speech_rate=0.9).

    Only the narrow allowlisted shapes are ever embedded, verbatim and
    untruncated (so two approved values can never collide regardless of
    length).  An unrecognized value is not assumed safe/non-secret and is
    never embedded, truncated, hashed, persisted, or printed in any form:
    a low-entropy secret run through SHA-256 is still offline-guessable, so
    "digest it instead" is not a safe fallback.  The only correct response
    to an unapproved shape is to refuse before fingerprint construction.
    """
    value = getattr(engine, "cache_params", None)
    if not value:
        return ""
    text = str(value)
    if not _CACHE_PARAMS_ALLOWLIST.fullmatch(text):
        raise ValueError(_UNAPPROVED_CACHE_PARAMS_MESSAGE)
    return text


def _engine_descriptor(engine: object) -> dict[str, object]:
    return {
        "version": _clean_version(getattr(engine, "version", None)),
        "boundary": _boundary(engine),
        "provider_id": _provider_id(engine),
        "cache_params": _cache_params(engine),
    }


@dataclass(frozen=True)
class ProviderConfiguration:
    fingerprint: str
    runtime_contract: str
    tts_engine_version: str
    asr_engine_version: str
    llm_engine_version: str
    llm_required: bool
    llm_configured: bool
    credential_fingerprint_ready: bool
    credential_failure_code: str | None
    # Provider objects are intentionally excluded from repr.  Current engines
    # do not retain credentials, but this prevents a future adapter from
    # accidentally placing secret-bearing state in diagnostics.
    tts_engine: object = field(repr=False)
    asr_engine: object = field(repr=False)
    llm_engine: object = field(repr=False)


def _fingerprint_key_failure_code(
        provider_key: str, fingerprint_key: str | None) -> str | None:
    if not fingerprint_key:
        return "provider_credential_fingerprint_key_missing"
    encoded = fingerprint_key.encode("utf-8")
    if (len(encoded) < _MIN_FINGERPRINT_KEY_BYTES
            or any(character.isspace() for character in fingerprint_key)
            or len(set(fingerprint_key))
            < _MIN_FINGERPRINT_KEY_DISTINCT_CHARACTERS
            or _has_short_exact_period(fingerprint_key)):
        return "provider_credential_fingerprint_key_invalid"
    for name in _NON_REUSABLE_SECRET_ENV_NAMES:
        other = os.environ.get(name)
        if (other is not None and name != CREDENTIAL_FINGERPRINT_KEY_ENV
                and hmac.compare_digest(encoded, other.encode("utf-8"))):
            return "provider_credential_fingerprint_key_reused"
    # Keep the explicit parameter comparison even if provider env names change
    # in a later adapter.
    if hmac.compare_digest(encoded, provider_key.encode("utf-8")):
        return "provider_credential_fingerprint_key_reused"
    return None


def _has_short_exact_period(value: str) -> bool:
    """Reject conspicuously repeated human-made patterns, not estimate entropy.

    True entropy cannot be proven from one configured string.  This catches the
    dangerous operational mistakes we can identify without persisting or
    logging the secret, while deployment instructions still require a CSPRNG.
    """
    for width in range(1, min(16, len(value) // 2) + 1):
        if len(value) % width == 0 and value == value[:width] * (len(value) // width):
            return True
    return False


def capture_configuration(*, tts_engine: object | None = None,
                          asr_engine: object | None = None,
                          llm_engine: object | None = None) -> ProviderConfiguration:
    """Capture the engines the current process would actually use.

    The persisted fingerprint never contains the API key.  When a DashScope key
    is configured, a separate deployment secret derives an HMAC marker inside
    the in-memory canonical document.  Only the final configuration SHA-256 is
    persisted.  Rotating either credential therefore invalidates an old probe,
    while neither credential nor an offline-verifiable key digest reaches the
    readiness ledger.
    """
    # The exact/autopilot channel is Qwen-only (tts.get_autopilot_engine);
    # the generic tts.engine() can be Piper/CosyVoice/Null depending on
    # config.  Probing the generic engine would fingerprint and ready-check
    # a provider that autopilot never actually calls.  `is None` (not
    # truthiness) so a supplied test double is never silently replaced.
    selected_tts = tts_engine if tts_engine is not None else tts.get_autopilot_engine()
    selected_asr = asr_engine or asr.get_engine()
    selected_llm = llm_engine or llm_judge.get_engine()
    llm_version = _clean_version(getattr(selected_llm, "version", None))
    llm_configured = llm_version != "off"
    # The current P0a contract always has deterministic rule/rubric fallback.
    # LLM is therefore probed when configured, but explicitly not a required
    # capability and never silently counted as "all ready" when it fails.
    llm_required = False
    provider_key = os.environ.get("DASHSCOPE_API_KEY")
    fingerprint_key = os.environ.get(CREDENTIAL_FINGERPRINT_KEY_ENV)
    credential_fingerprint_ready = True
    credential_failure_code: str | None = None
    credential_marker: str | None = None
    if provider_key:
        credential_failure_code = _fingerprint_key_failure_code(
            provider_key, fingerprint_key)
        if credential_failure_code is not None:
            credential_fingerprint_ready = False
            credential_marker = "unavailable"
        else:
            assert fingerprint_key is not None
            credential_marker = hmac.new(
                fingerprint_key.encode("utf-8"),
                b"nmu-provider-readiness-credential-v1\x00"
                + provider_key.encode("utf-8"),
                hashlib.sha256,
            ).hexdigest()
    document = {
        "schema_version": SCHEMA_VERSION,
        "runtime_contract": RUNTIME_CONTRACT,
        "key_present": bool(provider_key),
        "credential_marker": credential_marker,
        "tts": _engine_descriptor(selected_tts),
        "asr": _engine_descriptor(selected_asr),
        "llm": {
            **_engine_descriptor(selected_llm),
            "configured": llm_configured,
            "required": llm_required,
        },
    }
    canonical = json.dumps(
        document, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    return ProviderConfiguration(
        fingerprint=hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
        runtime_contract=RUNTIME_CONTRACT,
        tts_engine_version=str(document["tts"]["version"]),
        asr_engine_version=str(document["asr"]["version"]),
        llm_engine_version=llm_version,
        llm_required=llm_required,
        llm_configured=llm_configured,
        credential_fingerprint_ready=credential_fingerprint_ready,
        credential_failure_code=credential_failure_code,
        tts_engine=selected_tts,
        asr_engine=selected_asr,
        llm_engine=selected_llm,
    )


@dataclass(frozen=True)
class CapabilityResult:
    required: bool
    success: bool
    failure_code: str | None


@dataclass(frozen=True)
class SyntheticProbeResult:
    configuration: ProviderConfiguration
    tts: CapabilityResult
    asr: CapabilityResult
    llm: CapabilityResult
    required_capabilities_ready: bool
    all_configured_capabilities_ready: bool
    probe_failure_code: str | None = None


def _tts_probe(configuration: ProviderConfiguration) -> tuple[CapabilityResult, bytes | None]:
    if not tts.cloud_text_allowed(PROBE_TEXT) or PROBE_TEXT not in content.FIXED_TTS_LINES:
        return CapabilityResult(True, False, "tts_probe_text_not_allowlisted"), None
    engine = configuration.tts_engine
    if _boundary(engine) == "unknown":
        return CapabilityResult(True, False, "tts_provider_boundary_unknown"), None
    try:
        audio = engine.synthesize(PROBE_TEXT)
    except Exception:
        return CapabilityResult(True, False, "tts_provider_exception"), None
    if not isinstance(audio, (bytes, bytearray)) or len(audio) <= 44:
        return CapabilityResult(True, False, "tts_audio_invalid"), None
    return CapabilityResult(True, True, None), bytes(audio)


def _asr_probe(configuration: ProviderConfiguration,
               audio: bytes | None) -> CapabilityResult:
    if audio is None:
        return CapabilityResult(True, False, "asr_probe_audio_unavailable")
    engine = configuration.asr_engine
    if _boundary(engine) == "unknown":
        return CapabilityResult(True, False, "asr_provider_boundary_unknown")
    try:
        result = engine.transcribe(audio, [PROBE_TEXT])
    except Exception:
        return CapabilityResult(True, False, "asr_provider_exception")
    text = getattr(result, "asr_text", None)
    if not isinstance(text, str) or not text.strip():
        return CapabilityResult(True, False, "asr_text_empty")
    return CapabilityResult(True, True, None)


def _llm_probe(configuration: ProviderConfiguration) -> CapabilityResult:
    required = configuration.llm_required
    if not configuration.llm_configured:
        return CapabilityResult(required, False, "llm_not_required_not_configured")
    engine = configuration.llm_engine
    if _boundary(engine) == "unknown":
        return CapabilityResult(required, False, "llm_provider_boundary_unknown")
    ji = judging.build_judge_input(
        item_id="synthetic-provider-probe",
        task_type="单要素",
        target_word="您好",
        acceptable_expressions=("你好",),
        asr_text="您好",
    )
    try:
        result = engine.judge(ji)
    except Exception:
        return CapabilityResult(required, False, "llm_provider_exception")
    if result is None:
        return CapabilityResult(required, False, "llm_result_empty")
    answer_type = getattr(result, "answer_type", None)
    score = getattr(result, "ai_score", None)
    needs_review = getattr(result, "ai_needs_review", None)
    if (not isinstance(answer_type, AnswerType)
            or not isinstance(score, (int, float))
            or isinstance(score, bool)
            or not math.isfinite(float(score))
            or float(score) not in {0.0, 0.5, 1.0}
            or not isinstance(needs_review, bool)):
        return CapabilityResult(required, False, "llm_result_invalid")
    return CapabilityResult(required, True, None)


def run_synthetic_probe(configuration: ProviderConfiguration | None = None) -> SyntheticProbeResult:
    """Perform the provider calls without reading a Patient, Turn or Audio row."""
    config = configuration or capture_configuration()
    if not config.credential_fingerprint_ready:
        failure = config.credential_failure_code or "provider_credential_fingerprint_unavailable"
        return SyntheticProbeResult(
            configuration=config,
            tts=CapabilityResult(True, False, failure),
            asr=CapabilityResult(True, False, failure),
            llm=CapabilityResult(
                config.llm_required, False,
                failure if config.llm_configured else "llm_not_required_not_configured",
            ),
            required_capabilities_ready=False,
            all_configured_capabilities_ready=False,
            probe_failure_code=failure,
        )
    tts_result, probe_audio = _tts_probe(config)
    try:
        asr_result = _asr_probe(config, probe_audio)
    finally:
        # Release the in-memory reference immediately.  The ASR adapter deletes
        # any provider-required scratch file in its own finally block; the
        # readiness ledger below has no audio/text column.
        probe_audio = None
    llm_result = _llm_probe(config)
    capabilities = (tts_result, asr_result, llm_result)
    required_ready = all(item.success for item in capabilities if item.required)
    configured_ready = (
        tts_result.success and asr_result.success
        and (not config.llm_configured or llm_result.success)
    )
    return SyntheticProbeResult(
        configuration=config,
        tts=tts_result,
        asr=asr_result,
        llm=llm_result,
        required_capabilities_ready=required_ready,
        all_configured_capabilities_ready=configured_ready,
        probe_failure_code=None,
    )


def persist_probe(session: DBSession, *, result: SyntheticProbeResult,
                  actor_display_id: str, checked_at: datetime | None = None,
                  config_changed_during_probe: bool = False) -> ProviderReadinessProbe:
    now = checked_at or _utc_now_naive()
    required_ready = result.required_capabilities_ready and not config_changed_during_probe
    all_configured_ready = (
        result.all_configured_capabilities_ready and not config_changed_during_probe)
    row = ProviderReadinessProbe(
        probe_id=f"prb_{secrets.token_hex(16)}",
        schema_version=SCHEMA_VERSION,
        runtime_contract=result.configuration.runtime_contract,
        config_fingerprint=result.configuration.fingerprint,
        tts_engine_version=result.configuration.tts_engine_version,
        asr_engine_version=result.configuration.asr_engine_version,
        llm_engine_version=result.configuration.llm_engine_version,
        tts_required=result.tts.required,
        tts_success=result.tts.success,
        tts_failure_code=result.tts.failure_code,
        asr_required=result.asr.required,
        asr_success=result.asr.success,
        asr_failure_code=result.asr.failure_code,
        llm_required=result.llm.required,
        llm_configured=result.configuration.llm_configured,
        llm_success=result.llm.success,
        llm_failure_code=result.llm.failure_code,
        required_capabilities_ready=required_ready,
        all_configured_capabilities_ready=all_configured_ready,
        probe_failure_code=(
            "provider_config_changed_during_probe"
            if config_changed_during_probe else result.probe_failure_code),
        checked_at=now,
        expires_at=now + timedelta(minutes=ttl_minutes()),
        actor_display_id=actor_display_id,
    )
    session.add(row)
    session.flush()
    return row


def latest_probe(session: DBSession) -> ProviderReadinessProbe | None:
    return session.exec(select(ProviderReadinessProbe).order_by(
        ProviderReadinessProbe.checked_at.desc(),
        ProviderReadinessProbe.probe_id.desc(),
    )).first()


class CapabilityProjection(BaseModel):
    model_config = ConfigDict(extra="forbid")
    required: bool
    configured: bool
    success: bool
    engine_version: str
    failure_code: str | None


class ReadinessProjection(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema_version: str
    runtime_contract: str
    status: str
    start_allowed: bool
    required_capabilities_ready: bool
    all_configured_capabilities_ready: bool
    matches_current_config: bool
    tts: CapabilityProjection
    asr: CapabilityProjection
    llm: CapabilityProjection
    checked_at: datetime | None
    expires_at: datetime | None
    actor_display_id: str | None
    probe_failure_code: str | None


def readiness_projection(session: DBSession, *,
                         configuration: ProviderConfiguration | None = None,
                         now: datetime | None = None) -> ReadinessProjection:
    current = configuration or capture_configuration()
    checked_now = now or _utc_now_naive()
    row = latest_probe(session)
    if row is None:
        return ReadinessProjection(
            schema_version=SCHEMA_VERSION,
            runtime_contract=current.runtime_contract,
            status="missing",
            start_allowed=False,
            required_capabilities_ready=False,
            all_configured_capabilities_ready=False,
            matches_current_config=False,
            tts=CapabilityProjection(required=True, configured=True, success=False,
                                     engine_version=current.tts_engine_version,
                                     failure_code="probe_missing"),
            asr=CapabilityProjection(required=True, configured=True, success=False,
                                     engine_version=current.asr_engine_version,
                                     failure_code="probe_missing"),
            llm=CapabilityProjection(required=current.llm_required,
                                     configured=current.llm_configured, success=False,
                                     engine_version=current.llm_engine_version,
                                     failure_code="probe_missing"),
            checked_at=None, expires_at=None, actor_display_id=None,
            probe_failure_code="probe_missing",
        )
    matches = (
        row.schema_version == SCHEMA_VERSION
        and row.config_fingerprint == current.fingerprint
        and row.runtime_contract == current.runtime_contract)
    expired = row.expires_at <= checked_now
    if not matches:
        status = "config_mismatch"
    elif expired:
        status = "expired"
    elif not row.required_capabilities_ready:
        status = "required_capability_failed"
    else:
        status = "ready"
    return ReadinessProjection(
        # The response schema describes THIS projection's structure, so it is
        # always the current constant.  A historical row's own schema version
        # is an input to `matches` above and nothing else: it downgrades the
        # row to config_mismatch rather than changing the wire contract.
        schema_version=SCHEMA_VERSION,
        # Returned verbatim on purpose.  Substituting the current constant here
        # would let an old runtime contract masquerade as the current one; the
        # strict client fails such a row closed instead.
        runtime_contract=row.runtime_contract,
        status=status,
        start_allowed=status == "ready",
        required_capabilities_ready=row.required_capabilities_ready,
        all_configured_capabilities_ready=row.all_configured_capabilities_ready,
        matches_current_config=matches,
        tts=CapabilityProjection(
            required=row.tts_required, configured=True, success=row.tts_success,
            engine_version=row.tts_engine_version,
            failure_code=row.tts_failure_code),
        asr=CapabilityProjection(
            required=row.asr_required, configured=True, success=row.asr_success,
            engine_version=row.asr_engine_version,
            failure_code=row.asr_failure_code),
        llm=CapabilityProjection(
            required=row.llm_required, configured=row.llm_configured,
            success=row.llm_success, engine_version=row.llm_engine_version,
            failure_code=row.llm_failure_code),
        checked_at=row.checked_at, expires_at=row.expires_at,
        actor_display_id=row.actor_display_id,
        probe_failure_code=row.probe_failure_code,
    )


_STATUS_TO_CONFLICT_CODE = {
    "missing": "provider_readiness_missing",
    "expired": "provider_readiness_expired",
    "config_mismatch": "provider_readiness_config_mismatch",
    "required_capability_failed": "provider_readiness_required_capability_failed",
}


class ProviderReadinessConflict(RuntimeError):
    def __init__(self, projection: ReadinessProjection):
        self.projection = projection
        self.code = _STATUS_TO_CONFLICT_CODE.get(
            projection.status, "provider_readiness_unavailable")
        super().__init__(self.code)


def require_start_ready(session: DBSession) -> ReadinessProjection:
    projection = readiness_projection(session)
    if not projection.start_allowed:
        raise ProviderReadinessConflict(projection)
    return projection


def conflict_detail(exc: ProviderReadinessConflict) -> dict[str, Any]:
    return {
        "code": exc.code,
        "message": "AI 服务能力尚未通过当前配置的有效合成检查，禁止取得控制权",
        "readiness": exc.projection.model_dump(mode="json"),
    }
