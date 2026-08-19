from __future__ import annotations

from contextlib import contextmanager
from dataclasses import FrozenInstanceError
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from app import autopilot_plan_profiles as profiles
from app import content, runtime


PROFILE_KEYS = {
    "profile_schema_version",
    "profile_version_id",
    "simulation_only",
    "usage_scope",
    "week_no",
    "phase_type",
    "event_line",
    "parent_item_bank_version_id",
    "parent_item_bank_definition_digest",
    "parent_autopilot_protocol_version_id",
    "parent_autopilot_protocol_definition_digest",
    "parent_source_document_sha256",
    "parent_source_normalized_text_sha256",
    "ordered_position_keys",
    "allowed_task_types",
    "plan_position_count",
    "completion_scope",
    "profile_definition_digest",
    "qc_status",
}


def _bank() -> content.ItemBank:
    return content.load_item_bank(content.CONTENT_DIR / "item_bank_v1.json")


def _protocol() -> dict:
    return content.load_autopilot_protocol(
        content.CONTENT_DIR / "autopilot_protocol_v1.json")


def _manifest() -> dict:
    return content._load_strict_json_object(  # noqa: SLF001
        content.CONTENT_DIR / "autopilot_demo_profiles_v1.json",
        label="test profile",
    )


@contextmanager
def _assert_code(code: str):
    with pytest.raises(profiles.PlanProfileError, match=".") as caught:
        yield
    assert caught.value.code == code


def _activate_definition(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    definition: dict,
    *,
    pinned_digest: str | None = None,
) -> Path:
    path = tmp_path / "profile.json"
    path.write_text(
        json.dumps(definition, ensure_ascii=False),
        encoding="utf-8",
    )
    monkeypatch.setitem(
        profiles._PROFILE_REGISTRY,  # noqa: SLF001
        profiles.WEEK2_SINGLE20_DEMO_VERSION,
        (
            path,
            pinned_digest
            if pinned_digest is not None
            else definition["profile_definition_digest"],
        ),
    )
    return path


def _resolve_requested(**updates):
    values = {
        "profile_version_id": profiles.WEEK2_SINGLE20_DEMO_VERSION,
        "bank": _bank(),
        "protocol": _protocol(),
        "is_simulation": True,
        "week_no": 2,
        "phase_type": "正式训练",
        "event_line": "正式训练",
    }
    values.update(updates)
    profile_version_id = values.pop("profile_version_id")
    return profiles.resolve_requested_profile(profile_version_id, **values)


def test_packaged_profile_has_exact_schema_and_self_digest() -> None:
    definition = _manifest()
    assert set(definition) == PROFILE_KEYS
    assert definition["profile_version_id"] == (
        profiles.WEEK2_SINGLE20_DEMO_VERSION)
    assert definition["profile_definition_digest"] == (
        profiles.WEEK2_SINGLE20_DEMO_DIGEST)
    assert profiles._canonical_profile_digest(definition) == (  # noqa: SLF001
        profiles.WEEK2_SINGLE20_DEMO_DIGEST)
    assert definition["ordered_position_keys"] == [
        f"{row['item_id']}#1" for row in _bank().single_element
    ]


# --------------------------------------------------------------------------
# Immutable historical binding boundary
#
# ``resolve_registered_binding`` is the only door a stored VisitPlan/Session
# pair goes through.  It must answer from the registry alone so a row frozen
# against an older parent keeps resolving after canonical content moves on.
# --------------------------------------------------------------------------


def _forbid_current_content(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make any touch of today's bank/protocol an outright failure."""
    def boom(*_args, **_kwargs):
        raise AssertionError(
            "historical binding resolution must not read current content")

    monkeypatch.setattr(profiles, "_default_bank", boom)  # noqa: SLF001
    monkeypatch.setattr(profiles, "_default_protocol", boom)  # noqa: SLF001
    monkeypatch.setattr(content, "load_item_bank", boom)
    monkeypatch.setattr(content, "load_autopilot_protocol", boom)


def test_registered_binding_resolves_without_touching_current_content(
        monkeypatch: pytest.MonkeyPatch) -> None:
    _forbid_current_content(monkeypatch)

    definition = profiles.resolve_registered_binding(
        profiles.WEEK2_SINGLE20_DEMO_VERSION,
        profiles.WEEK2_SINGLE20_DEMO_DIGEST,
    )

    assert definition is not None
    assert definition.profile_version_id == (
        profiles.WEEK2_SINGLE20_DEMO_VERSION)
    assert definition.profile_definition_digest == (
        profiles.WEEK2_SINGLE20_DEMO_DIGEST)
    assert len(definition.ordered_position_keys) == 20
    assert definition.simulation_only is True


def test_paired_null_binding_resolves_to_none(
        monkeypatch: pytest.MonkeyPatch) -> None:
    _forbid_current_content(monkeypatch)

    assert profiles.resolve_registered_binding(None, None) is None


@pytest.mark.parametrize(
    "version,digest",
    [
        (profiles.WEEK2_SINGLE20_DEMO_VERSION, None),
        (None, profiles.WEEK2_SINGLE20_DEMO_DIGEST),
    ],
)
def test_half_pair_binding_is_incomplete(version, digest) -> None:
    with _assert_code("plan_profile_binding_incomplete"):
        profiles.resolve_registered_binding(version, digest)


@pytest.mark.parametrize(
    "version",
    [
        "no-such-demo-v9",
        "",
        " week2-single20-demo-v1",
        "week2-single20-demo-v1 ",
    ],
)
def test_unregistered_or_padded_binding_version_is_unknown(version) -> None:
    with _assert_code("plan_profile_unknown"):
        profiles.resolve_registered_binding(
            version, profiles.WEEK2_SINGLE20_DEMO_DIGEST)


@pytest.mark.parametrize(
    "digest",
    [
        "f" * 64,                                        # well formed, wrong
        "0123456789abcdef" * 3 + "0123456789abcde",      # 63
        ("0123456789abcdef" * 4).upper(),                # uppercase
        "g" * 64,                                        # non-hex
    ],
)
def test_binding_digest_must_match_the_registered_definition(digest) -> None:
    with _assert_code("plan_profile_digest_mismatch"):
        profiles.resolve_registered_binding(
            profiles.WEEK2_SINGLE20_DEMO_VERSION, digest)


def test_registry_pin_still_governs_the_historical_binding(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """An edited file that recomputes a valid self-digest is still refused."""
    tampered = _manifest()
    # Schema-valid but genuinely different content, so the recomputed
    # self-digest is internally consistent yet no longer the approved one.
    tampered["parent_item_bank_version_id"] = "wk2-v1-tampered"
    tampered["profile_definition_digest"] = (
        profiles._canonical_profile_digest(tampered))  # noqa: SLF001
    assert tampered["profile_definition_digest"] != (
        profiles.WEEK2_SINGLE20_DEMO_DIGEST)
    _activate_definition(
        monkeypatch, tmp_path, tampered,
        pinned_digest=profiles.WEEK2_SINGLE20_DEMO_DIGEST)

    with _assert_code("plan_profile_digest_mismatch"):
        profiles.resolve_registered_binding(
            profiles.WEEK2_SINGLE20_DEMO_VERSION,
            tampered["profile_definition_digest"],
        )


def test_demo_resolution_reuses_exact_canonical_plan_items(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: list[runtime.SessionPlan] = []
    original = runtime.build_session_plan

    def capture(*args, **kwargs):
        plan = original(*args, **kwargs)
        captured.append(plan)
        return plan

    monkeypatch.setattr(profiles.runtime, "build_session_plan", capture)
    resolved = _resolve_requested()

    assert len(captured) == 2
    assert resolved.profile_version_id == profiles.WEEK2_SINGLE20_DEMO_VERSION
    assert resolved.profile_definition_digest == (
        profiles.WEEK2_SINGLE20_DEMO_DIGEST)
    assert resolved.resolved_position_count == 20
    assert resolved.resolved_position_content_ready is True
    assert resolved.unsupported_position_count == 0
    assert resolved.structured_readiness_gaps == ()
    assert resolved.source_unstructured_gaps == ()
    assert resolved.source_protocol_position_count == 78
    assert [position.position_key for position in resolved.positions] == [
        f"{row['item_id']}#1" for row in _bank().single_element
    ]
    assert all(
        selected is captured[-1].items[index]
        for index, selected in enumerate(resolved.session_plan.items)
    )


def test_resolution_is_frozen_but_plan_item_display_remains_shallow() -> None:
    resolved = _resolve_requested()
    with pytest.raises(FrozenInstanceError):
        resolved.resolved_position_count = 21  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        resolved.session_plan.items = ()  # type: ignore[misc]
    assert isinstance(resolved.session_plan.items[0].display, dict)


def test_paired_null_resolution_preserves_full_canonical_gap_accounting() -> None:
    bank = _bank()
    resolved = profiles.resolve_requested_profile(
        None,
        bank=bank,
        protocol=_protocol(),
        is_simulation=True,
        week_no=2,
        phase_type="正式训练",
        event_line="正式训练",
    )
    assert resolved.profile_version_id is None
    assert resolved.profile_definition_digest is None
    assert resolved.completion_scope == "canonical_full_source"
    # 2026-08-19 内容交付后源协议全量结构化:78 位置全入 canonical 计划,
    # 无 source-only 缺口;58 个剩余缺口(双要素 10×5 + 多要素 2×4)全部结构化。
    assert resolved.resolved_position_count == 78
    assert len(resolved.structured_readiness_gaps) == 58
    assert len(resolved.source_unstructured_gaps) == 0
    assert resolved.unsupported_position_count == 58
    assert resolved.source_protocol_position_count == 78
    assert resolved.resolved_position_content_ready is False
    assert resolved.source_unstructured_gaps == tuple(
        row["source_position_key"]
        for row in bank.meta["source_unstructured_positions"]
    )
    assert len(bank.double_element) == 10
    assert [
        (gap.position.item_id, gap.position.turn_seq)
        for gap in resolved.structured_readiness_gaps
        if gap.position.task_type == "双要素"
    ] == [
        (row["item_id"], turn_seq)
        for row in bank.double_element
        for turn_seq in range(1, 6)
    ]


@pytest.mark.parametrize(
    ("version", "digest", "code"),
    [
        ("week2-single20-demo-v1", None, "plan_profile_binding_incomplete"),
        (None, profiles.WEEK2_SINGLE20_DEMO_DIGEST,
         "plan_profile_binding_incomplete"),
        ("unknown-profile", "0" * 64, "plan_profile_unknown"),
        ("week2-single20-demo-v1", "0" * 64,
         "plan_profile_digest_mismatch"),
    ],
)
def test_bound_profile_requires_exact_registered_pair(
    version: str | None,
    digest: str | None,
    code: str,
) -> None:
    with _assert_code(code):
        profiles.resolve_bound_profile(
            version,
            digest,
            bank=_bank(),
            protocol=_protocol(),
            is_simulation=True,
            week_no=2,
            phase_type="正式训练",
            event_line="正式训练",
        )


def test_subject_boundary_requires_explicit_simulation() -> None:
    with _assert_code("plan_profile_simulation_required"):
        _resolve_requested(is_simulation=False)


@pytest.mark.parametrize(
    "update",
    [
        {"week_no": 3},
        {"phase_type": "关系建立"},
        {"event_line": "关系建立环节"},
    ],
)
def test_profile_context_is_exact(update: dict[str, object]) -> None:
    with _assert_code("plan_profile_context_mismatch"):
        _resolve_requested(**update)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda value: value.__setitem__("unexpected", True),
        lambda value: value.pop("qc_status"),
        lambda value: value.__setitem__("simulation_only", 1),
        lambda value: value.__setitem__("plan_position_count", "20"),
    ],
)
def test_manifest_schema_is_closed_and_non_coercing(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    mutate,
) -> None:
    definition = _manifest()
    mutate(definition)
    _activate_definition(monkeypatch, tmp_path, definition)
    with _assert_code("plan_profile_invalid"):
        profiles.resolve_requested_definition(
            profiles.WEEK2_SINGLE20_DEMO_VERSION,
            bank=_bank(),
            protocol=_protocol(),
        )


@pytest.mark.parametrize(
    "raw",
    [
        '{"profile_schema_version":"autopilot-demo-profile.v1",'
        '"profile_schema_version":"autopilot-demo-profile.v1"}',
        '{"profile_schema_version":"autopilot-demo-profile.v1","x":NaN}',
        '{"profile_schema_version":"autopilot-demo-profile.v1","x":Infinity}',
        '[]',
    ],
)
def test_manifest_uses_strict_json_parser(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    raw: str,
) -> None:
    path = tmp_path / "profile.json"
    path.write_text(raw, encoding="utf-8")
    monkeypatch.setitem(
        profiles._PROFILE_REGISTRY,  # noqa: SLF001
        profiles.WEEK2_SINGLE20_DEMO_VERSION,
        (path, profiles.WEEK2_SINGLE20_DEMO_DIGEST),
    )
    with _assert_code("plan_profile_invalid"):
        profiles.resolve_requested_definition(
            profiles.WEEK2_SINGLE20_DEMO_VERSION,
            bank=_bank(),
            protocol=_protocol(),
        )


def test_malformed_declared_self_digest_is_invalid(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    definition = _manifest()
    definition["profile_definition_digest"] = "not-a-lowerhex-digest"
    _activate_definition(monkeypatch, tmp_path, definition)
    with _assert_code("plan_profile_invalid"):
        profiles.resolve_requested_definition(
            profiles.WEEK2_SINGLE20_DEMO_VERSION,
            bank=_bank(),
            protocol=_protocol(),
        )


def test_declared_self_digest_drift_is_not_accepted_as_a_registry_pin(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    definition = _manifest()
    definition["profile_definition_digest"] = "0" * 64
    _activate_definition(monkeypatch, tmp_path, definition)
    with _assert_code("plan_profile_digest_mismatch"):
        profiles.resolve_requested_definition(
            profiles.WEEK2_SINGLE20_DEMO_VERSION,
            bank=_bank(),
            protocol=_protocol(),
        )


def test_registry_pin_rejects_internally_rehashed_file_drift(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    definition = _manifest()
    definition["qc_status"] = "draft"
    definition["ordered_position_keys"] = list(
        reversed(definition["ordered_position_keys"]))
    definition["profile_definition_digest"] = (
        profiles._canonical_profile_digest(definition))  # noqa: SLF001
    _activate_definition(
        monkeypatch,
        tmp_path,
        definition,
        pinned_digest=profiles.WEEK2_SINGLE20_DEMO_DIGEST,
    )
    with _assert_code("plan_profile_digest_mismatch"):
        profiles.resolve_requested_definition(
            profiles.WEEK2_SINGLE20_DEMO_VERSION,
            bank=_bank(),
            protocol=_protocol(),
        )


@pytest.mark.parametrize(
    "keys",
    [
        lambda current: current[:-1],
        lambda current: current + ["SE_extra#1"],
        lambda current: list(reversed(current)),
        lambda current: [*current[:-1], "SE_unknown#1"],
        lambda current: [current[0], *current[:-1]],
        lambda current: [*current[:-1], "DE_烟灰缸+烟#1"],
        lambda current: [f"{current[0].split('#', 1)[0]}#2", *current[1:]],
    ],
)
def test_activated_definition_must_be_exact_single20_order(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    keys,
) -> None:
    definition = _manifest()
    definition["ordered_position_keys"] = keys(
        definition["ordered_position_keys"])
    definition["plan_position_count"] = len(
        definition["ordered_position_keys"])
    definition["profile_definition_digest"] = (
        profiles._canonical_profile_digest(definition))  # noqa: SLF001
    _activate_definition(monkeypatch, tmp_path, definition)
    with _assert_code("plan_profile_invalid"):
        profiles.resolve_requested_definition(
            profiles.WEEK2_SINGLE20_DEMO_VERSION,
            bank=_bank(),
            protocol=_protocol(),
        )


@pytest.mark.parametrize(
    "field",
    [
        "parent_item_bank_definition_digest",
        "parent_autopilot_protocol_definition_digest",
        "parent_source_document_sha256",
        "parent_source_normalized_text_sha256",
    ],
)
def test_definition_rejects_manifest_parent_digest_drift(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    field: str,
) -> None:
    definition = _manifest()
    definition[field] = "0" * 64
    definition["profile_definition_digest"] = (
        profiles._canonical_profile_digest(definition))  # noqa: SLF001
    _activate_definition(monkeypatch, tmp_path, definition)
    with _assert_code("plan_profile_parent_mismatch"):
        profiles.resolve_requested_definition(
            profiles.WEEK2_SINGLE20_DEMO_VERSION,
            bank=_bank(),
            protocol=_protocol(),
        )


def test_definition_rejects_item_bank_parent_drift() -> None:
    bank = _bank()
    drifted = content.ItemBank(
        version_id="same-content-new-version",
        single_element=bank.single_element,
        double_element=bank.double_element,
        multi_element=bank.multi_element,
        errata_fixed=bank.errata_fixed,
        meta=bank.meta,
    )
    with _assert_code("plan_profile_parent_mismatch"):
        profiles.resolve_requested_definition(
            profiles.WEEK2_SINGLE20_DEMO_VERSION,
            bank=drifted,
            protocol=_protocol(),
        )


def test_definition_rejects_protocol_parent_drift() -> None:
    protocol = _protocol()
    protocol["protocol_version_id"] = "future-protocol"
    with _assert_code("plan_profile_parent_mismatch"):
        profiles.resolve_requested_definition(
            profiles.WEEK2_SINGLE20_DEMO_VERSION,
            bank=_bank(),
            protocol=protocol,
        )


def test_visit_plan_and_session_helpers_use_row_facts() -> None:
    pair = {
        "autopilot_profile_version_id": (
            profiles.WEEK2_SINGLE20_DEMO_VERSION),
        "autopilot_profile_definition_digest": (
            profiles.WEEK2_SINGLE20_DEMO_DIGEST),
        "is_simulation": True,
        "week_no": 2,
        "phase_type": "正式训练",
        "event_line": "正式训练",
    }
    plan = SimpleNamespace(**pair)
    session = SimpleNamespace(**pair)
    assert profiles.resolve_for_visit_plan(plan).resolved_position_count == 20
    assert profiles.resolve_for_session(session).resolved_position_count == 20
    profiles.assert_plan_session_profile_binding(plan, session)

    session.autopilot_profile_definition_digest = "0" * 64
    with _assert_code("plan_profile_digest_mismatch"):
        profiles.assert_plan_session_profile_binding(plan, session)


def test_binding_helper_rejects_half_pair() -> None:
    plan = SimpleNamespace(
        autopilot_profile_version_id=None,
        autopilot_profile_definition_digest=None,
    )
    session = SimpleNamespace(
        autopilot_profile_version_id=profiles.WEEK2_SINGLE20_DEMO_VERSION,
        autopilot_profile_definition_digest=None,
    )
    with _assert_code("plan_profile_binding_incomplete"):
        profiles.assert_plan_session_profile_binding(plan, session)
