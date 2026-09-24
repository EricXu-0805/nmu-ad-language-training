from dataclasses import replace

import pytest

from app import autopilot_plan_profiles, content, item_numbering
from app.models import ItemEvent, Session


def _session(**overrides):
    bank = content.load_item_bank_for_week(2)
    protocol = content.load_autopilot_protocol(content.CONTENT_DIR / "autopilot_protocol_v1.json")
    return Session(**{
        "session_id": "S-NUMBER", "patient_id": "P-NUMBER", "week_no": 2,
        "phase_type": "正式训练", "event_line": "正式训练", "is_simulation": True,
        "data_classification": "simulation", "item_bank_version_id": bank.version_id,
        "item_bank_definition_digest": content.item_bank_definition_digest(bank),
        "autopilot_protocol_version_id": protocol["protocol_version_id"],
        "autopilot_protocol_definition_digest": content.autopilot_protocol_definition_digest(protocol),
        **overrides,
    })


def test_historical_projection_never_overwrites_an_existing_number_or_invents_a_position():
    session = _session()
    plan = item_numbering.historical_items(session)
    for item_id, task_type, order, expected in [
        ("SE_锚", "单要素", None, 3), ("SE_锚", "单要素", 99, 99),
        ("SE_锚", "双要素", None, None), ("unknown", "单要素", None, None),
    ]:
        item = ItemEvent(session_id=session.session_id, item_id=item_id,
                         task_type=task_type, presentation_order=order)
        assert item_numbering.presentation_order(item, plan) == expected
        assert item.presentation_order == order


@pytest.mark.parametrize("field,value", [
    ("item_bank_definition_digest", None), ("item_bank_version_id", "old-version"),
    ("item_bank_definition_digest", "0" * 64), ("week_no", 8),
])
def test_unverified_historical_bank_keeps_numbers_missing(field, value):
    assert item_numbering.historical_items(_session(**{field: value})) == {}


def test_reordered_current_content_is_not_used_as_the_old_frozen_plan(monkeypatch):
    session = _session()
    bank = content.load_item_bank_for_week(2)
    changed = replace(bank, single_element=list(reversed(bank.single_element)))
    monkeypatch.setattr(content, "load_item_bank_for_week", lambda _week: changed)
    assert item_numbering.historical_items(session) == {}


def test_profile_numbering_respects_exact_selected_scope_and_protocol_binding():
    session = _session(
        autopilot_profile_version_id=autopilot_plan_profiles.WEEK2_SINGLE20_DEMO_VERSION,
        autopilot_profile_definition_digest=autopilot_plan_profiles.WEEK2_SINGLE20_DEMO_DIGEST)
    plan = item_numbering.frozen_items(session)
    assert len(plan) == 20 and plan["SE_锚"].presentation_order == 3
    assert "DE_斧子+树" not in plan
    session.autopilot_protocol_definition_digest = "0" * 64
    assert item_numbering.historical_items(session) == {}
