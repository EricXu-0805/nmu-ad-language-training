"""Recover item numbers only from the exact content bound to a session.

Legacy manual ItemEvents may have no number.  These read projections must not
rewrite historical rows or guess a number from an unverified current bank.
"""
from __future__ import annotations

from . import autopilot_plan_profiles, content, runtime
from .models import ItemEvent, Session


class FrozenNumberingUnavailable(ValueError):
    pass


def frozen_items(session: Session) -> dict[str, runtime.PlanItem]:
    try:
        bank = content.load_item_bank_for_week(session.week_no)
        if (session.item_bank_version_id != bank.version_id
                or session.item_bank_definition_digest
                != content.item_bank_definition_digest(bank)):
            raise FrozenNumberingUnavailable("session bank binding unavailable")
        if (session.autopilot_profile_version_id is not None
                or session.autopilot_profile_definition_digest is not None):
            protocol = content.load_autopilot_protocol(
                content.CONTENT_DIR / "autopilot_protocol_v1.json")
            if (session.autopilot_protocol_version_id != protocol["protocol_version_id"]
                    or session.autopilot_protocol_definition_digest
                    != content.autopilot_protocol_definition_digest(protocol)):
                raise FrozenNumberingUnavailable("session protocol binding unavailable")
            plan = autopilot_plan_profiles.resolve_for_session(
                session, bank=bank, protocol=protocol).session_plan
        else:
            event = str(getattr(session.event_line, "value", session.event_line))
            plan = runtime.build_session_plan(bank, session.week_no, event)
        return {item.item_id: item for item in plan.items}
    except (OSError, ValueError, TypeError) as exc:
        raise FrozenNumberingUnavailable("frozen item numbering unavailable") from exc


def historical_items(session: Session) -> dict[str, runtime.PlanItem]:
    """Unavailable historical definitions leave the missing number explicit."""
    try:
        return frozen_items(session)
    except FrozenNumberingUnavailable:
        return {}


def presentation_order(item: ItemEvent, plan_items: dict[str, runtime.PlanItem]) -> int | None:
    if item.presentation_order is not None:
        return item.presentation_order
    planned = plan_items.get(item.item_id)
    if (planned is not None
            and str(getattr(item.task_type, "value", item.task_type)) == planned.task_type):
        return planned.presentation_order
    return None
