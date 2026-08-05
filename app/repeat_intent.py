"""Versioned explicit-repeat intent protocol registry and its exact detector.

A patient may ask the server to say the question again.  That request is not an
answer: it must never consume ``attempt_seq``, a cue, or a prompt level.  The
only way to recognise it is an exact, closed, approved phrase list — never a
substring, fuzzy or LLM decision.

Every approved version stays on disk as its own file.  A capture is bound to
one ``(version_id, definition_digest)`` pair at admission time, and a recovery
worker resolves that historical pair here rather than silently adopting
whatever version the current deployment ships.  Raw transcripts never reach
this module's output: a match yields only the frozen ``phrase_key`` and the
SHA-256 of the normalized string.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal
import hashlib
import json
import re
import unicodedata

from pydantic import BaseModel, ConfigDict, Field

from . import content


REPEAT_INTENT_DIR = (
    Path(__file__).resolve().parent.parent / "content" / "repeat_intent_protocols")
# The one definition new plans may freeze, pinned by BOTH identity halves.  A
# version id alone is not an identity: an in-place edit of the same file would
# keep the id and silently activate unapproved phrases, so the approved digest
# is pinned here too and checked on every activation.
ACTIVE_REPEAT_INTENT_VERSION_ID = "repeat-intent-v1-20260730-proposal"
ACTIVE_REPEAT_INTENT_DEFINITION_DIGEST = (
    "51e8ce30d6273df52fc25011ed00ebc5fba15b30c9ed98b4ccc146b72e05484f"
)

_HEX64_RE = re.compile(r"^[0-9a-f]{64}$")
NormalizationStep = Literal[
    "unicode_nfkc",
    "strip_unicode_whitespace",
    "strip_boundary_chars_repeatedly",
]


class _RepeatPhraseSchema(BaseModel):
    model_config = ConfigDict(extra="forbid")

    phrase_key: str = Field(
        min_length=1, max_length=64, pattern=r"^[a-z][a-z0-9_]{0,63}$")
    text: str = Field(min_length=1, max_length=64)


class _RepeatNormalizationSchema(BaseModel):
    model_config = ConfigDict(extra="forbid")

    steps: list[NormalizationStep] = Field(min_length=1, max_length=8)
    boundary_chars: str = Field(min_length=1, max_length=64)
    inner_text_policy: Literal["preserve_exactly"]
    matching: Literal["full_string_exact"]
    normalized_text_hash: Literal["sha256_utf8_lower_hex"]


class _RepeatGovernanceSchema(BaseModel):
    model_config = ConfigDict(extra="forbid")

    clinical_answer_journal: Literal["exclude"]
    score_export: Literal["exclude"]
    controlled_audio_manifest: Literal["metadata_only"]
    audio_manifest_fields: list[str] = Field(min_length=1, max_length=8)
    forbidden_projection_fields: list[str] = Field(min_length=1, max_length=16)


class _RepeatIntentProtocolSchema(BaseModel):
    model_config = ConfigDict(extra="forbid")

    protocol_schema_version: Literal["repeat-intent-protocol.v1"]
    protocol_version_id: str = Field(min_length=1, max_length=128)
    qc_status: Literal["draft", "reviewed", "frozen"]
    simulation_only: bool
    normalization: _RepeatNormalizationSchema
    phrases: list[_RepeatPhraseSchema] = Field(min_length=1, max_length=64)
    max_replay_count_per_logical_slot: Literal[1]
    # Machine value is frozen and must not change: "exact" here means the
    # replay copies the predecessor command's canonical payload_json
    # byte-for-byte, so the frozen speech_text is identical.  It does not
    # assert identical audio — the replay is synthesized again downstream.
    first_request_outcome: Literal["replay_exact_predecessor_tts"]
    second_request_outcome: Literal["safe_pause"]
    second_request_reason_code: Literal["explicit_repeat_limit"]
    attempt_policy: Literal["do_not_consume_attempt_prompt_or_cue"]
    raw_transcript_persistence: Literal["forbidden_in_repeat_ledger"]
    governance: _RepeatGovernanceSchema


@dataclass(frozen=True)
class RepeatIntentProtocol:
    """One frozen, digest-identified repeat-intent definition."""

    version_id: str
    definition_digest: str
    steps: tuple[str, ...]
    boundary_chars: str
    phrase_by_normalized_text: dict[str, str]
    max_replay_count_per_logical_slot: int
    second_request_reason_code: str
    simulation_only: bool


@dataclass(frozen=True)
class RepeatMatch:
    """A confirmed explicit-repeat request; carries no patient transcript."""

    phrase_key: str
    normalized_text_sha256: str
    protocol_version_id: str
    protocol_definition_digest: str


def definition_digest(definition: dict) -> str:
    """Canonical compact UTF-8 sorted JSON plus a trailing newline, SHA-256.

    This deliberately differs from :func:`content.autopilot_protocol_definition_digest`:
    the repeat-intent definition has no self-referential digest field to exclude,
    and the approved artifact identity includes the trailing newline.
    """
    if not isinstance(definition, dict):
        raise TypeError("repeat-intent definition 必须是字典")
    canonical = json.dumps(
        definition,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8") + b"\n"
    return hashlib.sha256(canonical).hexdigest()


def _apply_step(value: str, step: str, boundary_chars: str) -> str:
    if step == "unicode_nfkc":
        return unicodedata.normalize("NFKC", value)
    if step == "strip_unicode_whitespace":
        return value.strip()
    if step == "strip_boundary_chars_repeatedly":
        # ``str.strip(chars)`` already removes every leading/trailing member of
        # the closed set repeatedly, and never touches inner characters.
        return value.strip(boundary_chars)
    raise ValueError(f"未知的 repeat-intent normalization 步骤: {step}")


def normalize(protocol: RepeatIntentProtocol, text: str) -> str:
    """Run the frozen normalization steps in their exact recorded order."""
    value = text
    for step in protocol.steps:
        value = _apply_step(value, step, protocol.boundary_chars)
    return value


def normalized_text_sha256(normalized: str) -> str:
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def detect(protocol: RepeatIntentProtocol, asr_text: object) -> RepeatMatch | None:
    """Return a match only for a full-string exact hit on the frozen phrases.

    ``None`` and the empty string are never matches: silence has its own
    response path and a failed transcription is a technical failure.  A
    transcript that merely contains a phrase (``"没听清，不过我觉得是苹果"``)
    keeps its inner characters after normalization and therefore cannot equal
    any frozen phrase.
    """
    if not isinstance(asr_text, str):
        return None
    normalized = normalize(protocol, asr_text)
    if not normalized:
        return None
    phrase_key = protocol.phrase_by_normalized_text.get(normalized)
    if phrase_key is None:
        return None
    return RepeatMatch(
        phrase_key=phrase_key,
        normalized_text_sha256=normalized_text_sha256(normalized),
        protocol_version_id=protocol.version_id,
        protocol_definition_digest=protocol.definition_digest,
    )


def _build_protocol(data: dict) -> RepeatIntentProtocol:
    try:
        parsed = _RepeatIntentProtocolSchema.model_validate(data)
    except ValueError as exc:
        raise ValueError(f"重复请求协议结构不合法：{exc}") from exc
    digest = definition_digest(data)
    steps = tuple(parsed.normalization.steps)
    boundary_chars = parsed.normalization.boundary_chars
    phrase_by_normalized_text: dict[str, str] = {}
    for phrase in parsed.phrases:
        normalized = phrase.text
        for step in steps:
            normalized = _apply_step(normalized, step, boundary_chars)
        if not normalized:
            raise ValueError(
                f"重复请求协议短语 {phrase.phrase_key} 规范化后为空")
        if normalized in phrase_by_normalized_text:
            raise ValueError(
                f"重复请求协议短语 {phrase.phrase_key} 规范化后与已有短语冲突")
        phrase_by_normalized_text[normalized] = phrase.phrase_key
    return RepeatIntentProtocol(
        version_id=parsed.protocol_version_id,
        definition_digest=digest,
        steps=steps,
        boundary_chars=boundary_chars,
        phrase_by_normalized_text=phrase_by_normalized_text,
        max_replay_count_per_logical_slot=(
            parsed.max_replay_count_per_logical_slot),
        second_request_reason_code=parsed.second_request_reason_code,
        simulation_only=parsed.simulation_only,
    )


def load_registry(
        directory: str | Path | None = None) -> dict[str, RepeatIntentProtocol]:
    """Load every historical repeat-intent version, indexed by version id."""
    source = Path(directory) if directory is not None else REPEAT_INTENT_DIR
    try:
        if not source.is_dir():
            raise ValueError("重复请求协议目录不存在")
        registry: dict[str, RepeatIntentProtocol] = {}
        for path in sorted(source.glob("*.json")):
            data = content._load_strict_json_object(  # noqa: SLF001 - shared 冻结定义装载契约
                path, label="重复请求协议")
            protocol = _build_protocol(data)
            if protocol.version_id in registry:
                raise ValueError(
                    f"重复请求协议版本 {protocol.version_id} 在注册表中重复")
            registry[protocol.version_id] = protocol
        if not registry:
            raise ValueError("重复请求协议注册表为空")
        return registry
    except content.FrozenContentUnavailable:
        raise
    except (OSError, TypeError, ValueError) as exc:
        raise content.FrozenContentUnavailable(str(exc)) from exc


def active_protocol(
        directory: str | Path | None = None) -> RepeatIntentProtocol:
    """The exact approved definition a newly created VisitPlan may freeze.

    Both halves of the identity are pinned.  If the file carrying the active
    version id no longer hashes to the approved digest, activation fails closed
    — an edited definition never becomes the active one just by keeping its
    version id.  Historical resolution is unaffected: rows already bound to an
    older version keep resolving through :func:`protocol_for_binding`.
    """
    registry = load_registry(directory)
    protocol = registry.get(ACTIVE_REPEAT_INTENT_VERSION_ID)
    if protocol is None:
        raise content.FrozenContentUnavailable(
            "当前生效的重复请求协议版本不在注册表中")
    if protocol.definition_digest != ACTIVE_REPEAT_INTENT_DEFINITION_DIGEST:
        raise content.FrozenContentUnavailable(
            "当前生效的重复请求协议内容与已批准摘要不一致，禁止启用")
    return protocol


def protocol_for_binding(
    version_id: object,
    definition_digest_value: object,
    *,
    directory: str | Path | None = None,
) -> RepeatIntentProtocol:
    """Resolve exactly the historical definition a row was frozen against."""
    if (not isinstance(version_id, str) or not version_id.strip()
            or not isinstance(definition_digest_value, str)
            or _HEX64_RE.fullmatch(definition_digest_value) is None):
        raise content.FrozenContentUnavailable("重复请求协议绑定格式非法")
    protocol = load_registry(directory).get(version_id)
    if protocol is None:
        raise content.FrozenContentUnavailable(
            "重复请求协议版本不在历史注册表中")
    if protocol.definition_digest != definition_digest_value:
        raise content.FrozenContentUnavailable(
            "重复请求协议内容已变化，绑定摘要与历史定义不一致")
    return protocol
