"""两床并行的选槽纯函数:表驱动用例 + 性质断言。

选槽顺序:本场已占 → 同受试者 → 同 trainer → 空或终态取最小编号 → 遗弃槽(只在握手时)
→ N=1 走今天的新鲜保护 / N=2 回 409 bedside_slots_full。
「空」= 没有行、没有 sessionId,或持有的场次已不存在;「终态」= completed/aborted/failed。
选中槽之后的动作(keep/vacant/terminal_takeover/stale_takeover/protected)必须恒等于
今天单槽握手和自动场切回对这个持有者走的分支,变的只是选哪一槽。
"""
from __future__ import annotations

import itertools
import json

import pytest
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

from app import main, session_admission
from app.models import LiveState
from app.session_admission import BedsideSlotError, BedsideSlotView, choose_bedside_slot


R, P, T = "S-REQ", "P-REQ", "T-REQ"   # 请求的场次 / 它的受试者 / 它的 trainer
_TERMINAL = frozenset({"completed", "aborted", "failed"})
_PURPOSES = ("handshake", "autopilot_return")


def _choose(*views, n=2, purpose="handshake"):
    return choose_bedside_slot(
        views, session_id=R, patient_id=P, trainer_id=T,
        slot_count=n, purpose=purpose)


def _outcome(*views, n=2, purpose="handshake"):
    try:
        choice = _choose(*views, n=n, purpose=purpose)
    except BedsideSlotError as exc:
        return exc.code
    return (choice.slot_id, choice.action)


def _mine(slot, *, stale=False):
    return BedsideSlotView(slot_id=slot, session_id=R, session_exists=True,
                           status="active", stale=stale, patient_id=P, trainer_id=T)


def _other(slot, sid="S-X", *, patient="P-X", trainer="T-X", status="active", stale=False):
    return BedsideSlotView(slot_id=slot, session_id=sid, session_exists=True,
                           status=status, stale=stale, patient_id=patient, trainer_id=trainer)


def _gone(slot, sid="S-GONE"):
    return BedsideSlotView(slot_id=slot, session_id=sid, session_exists=False)


def _second(slot, **kwargs):
    return _other(slot, "S-Y", **{"patient": "P-Y", "trainer": "T-Y", **kwargs})


_TWO_SLOT_CASES = [
    ("两槽都空", (), "handshake", (1, "vacant")),
    ("1 号新鲜他场、2 号没有行:B 进 2 号槽", (_other(1),), "handshake", (2, "vacant")),
    ("1 号空、2 号新鲜他场", (_other(2),), "handshake", (1, "vacant")),
    ("1 号终态、2 号没有行:空和终态同档取最小编号,单场仍回 1 号槽",
     (_other(1, status="completed"),), "handshake", (1, "terminal_takeover")),
    ("1 号空、2 号终态", (_other(2, status="aborted"),), "handshake", (1, "vacant")),
    ("1 号新鲜他场、2 号终态", (_other(1), _second(2, status="failed")), "handshake",
     (2, "terminal_takeover")),
    ("本场在 2 号槽、1 号空:回本场那一槽", (_mine(2),), "handshake", (2, "keep")),
    ("本场在 1 号槽、2 号新鲜他场", (_mine(1), _other(2)), "handshake", (1, "keep")),
    ("本场所在槽遗弃也照样回它", (_mine(2, stale=True), _other(1)), "handshake", (2, "keep")),
    ("同受试者新鲜场在 2 号、1 号空:回受试者那一槽并受保护",
     (_other(2, patient=P),), "handshake", (2, "protected")),
    ("同受试者遗弃场在 2 号:握手按遗弃让位",
     (_other(2, patient=P, stale=True),), "handshake", (2, "stale_takeover")),
    ("同受试者遗弃场在 2 号:自动场切回不按遗弃,只走保护",
     (_other(2, patient=P, stale=True),), "autopilot_return", (2, "protected")),
    ("同受试者的终态场不吸槽", (_other(2, patient=P, status="completed"),), "handshake",
     (1, "vacant")),
    ("同 trainer 新鲜场在 2 号、1 号空:同账号原地换场",
     (_other(2, trainer=T),), "handshake", (2, "protected")),
    ("同 trainer 遗弃场在 2 号", (_other(2, trainer=T, stale=True),), "handshake",
     (2, "stale_takeover")),
    ("同受试者排在同 trainer 前面",
     (_other(1, trainer=T), _second(2, patient=P)), "handshake", (2, "protected")),
    ("同 trainer 占两槽取最小编号",
     (_other(1, trainer=T), _second(2, trainer=T)), "handshake", (1, "protected")),
    ("空槽排在遗弃槽前面", (_other(1, stale=True),), "handshake", (2, "vacant")),
    ("终态槽排在遗弃槽前面", (_other(1, stale=True), _second(2, status="completed")),
     "handshake", (2, "terminal_takeover")),
    ("两槽都新鲜他场:满员", (_other(1), _second(2)), "handshake", "bedside_slots_full"),
    ("两槽都新鲜他场:自动场切回同样满员", (_other(1), _second(2)), "autopilot_return",
     "bedside_slots_full"),
    ("1 号遗弃、2 号新鲜:握手接管 1 号", (_other(1, stale=True), _second(2)), "handshake",
     (1, "stale_takeover")),
    ("1 号新鲜、2 号遗弃:握手接管 2 号", (_other(1), _second(2, stale=True)), "handshake",
     (2, "stale_takeover")),
    ("两槽都遗弃:取最小编号", (_other(1, stale=True), _second(2, stale=True)), "handshake",
     (1, "stale_takeover")),
    ("自动场切回不选遗弃槽:两槽都遗弃也是满员",
     (_other(1, stale=True), _second(2, stale=True)), "autopilot_return", "bedside_slots_full"),
    ("暂停与待收尾的他场同样占着槽",
     (_other(1, status="paused"), _second(2, status="intervention_completed")), "handshake",
     "bedside_slots_full"),
    ("没有 runtime 行的他场按 active 占槽",
     (_other(1, status=None), _second(2, status=None)), "handshake", "bedside_slots_full"),
    ("受试者为空不当作同一位受试者",
     (_other(1, patient=None), _second(2, patient=None)), "handshake", "bedside_slots_full"),
    ("受试者为空白也不当作同一位受试者",
     (_other(1, patient=" "), _second(2, patient=" ")), "handshake", "bedside_slots_full"),
    ("持有的场次已不存在算空", (_gone(1), _second(2)), "handshake", (1, "vacant")),
    ("自动场切回:持有的场次已不存在也算空,由调用方照今天回 bedside_busy",
     (_gone(1), _second(2)), "autopilot_return", (1, "vacant")),
    ("有行但没有 sessionId 算空", (BedsideSlotView(slot_id=1), _second(2)), "handshake",
     (1, "vacant")),
    ("自动场切回:1 号新鲜他场、2 号空", (_other(1),), "autopilot_return", (2, "vacant")),
    ("本场同时命中两槽:fail closed", (_mine(1), _mine(2, stale=True)), "handshake",
     "bedside_slot_conflict"),
    # 违例只拦牵涉到的那一场和那位受试者;别人的违例槽照普通占用处理,不连坐所有握手。
    ("他场同时在两槽、一新一旧:旧的那份照常按遗弃接管(那一场的平板和握手本就 409)",
     (_other(1), _other(2, stale=True)), "handshake", (2, "stale_takeover")),
    ("他场同时在两槽:自动场切回不选遗弃槽,满员",
     (_other(1), _other(2, stale=True)), "autopilot_return", "bedside_slots_full"),
    ("另一位受试者的两场同时占两槽、都新鲜:满员",
     (_other(1), _second(2, patient="P-X")), "handshake", "bedside_slots_full"),
    ("另一位受试者的两场同时占两槽、2 号那场遗弃:照常接管 2 号",
     (_other(1), _second(2, patient="P-X", stale=True)), "handshake", (2, "stale_takeover")),
    ("本场在 1 号、同受试者另一场在 2 号", (_mine(1), _other(2, patient=P)), "handshake",
     "bedside_slot_conflict"),
    ("本场在 1 号、同受试者另一场在 2 号:自动场切回同样拒绝",
     (_mine(1), _other(2, patient=P)), "autopilot_return", "bedside_slot_conflict"),
    ("同受试者另两场各占一槽:不知道接哪一槽,fail closed",
     (_other(1, patient=P), _second(2, patient=P, stale=True)), "handshake",
     "bedside_slot_conflict"),
    ("终态场的影子不算占槽", (_other(1, status="completed"), _other(2, status="completed")),
     "handshake", (1, "terminal_takeover")),
    ("已不存在场次的影子不算占槽", (_gone(1), _gone(2)), "handshake", (1, "vacant")),
]


@pytest.mark.parametrize(
    ("views", "purpose", "expected"),
    [case[1:] for case in _TWO_SLOT_CASES],
    ids=[case[0] for case in _TWO_SLOT_CASES],
)
def test_two_slot_selection_table(views, purpose, expected):
    assert _outcome(*views, purpose=purpose) == expected
    assert _outcome(*reversed(views), purpose=purpose) == expected


_ONE_SLOT_CASES = [
    ("没有行", (), "handshake", (1, "vacant")),
    ("本场", (_mine(1),), "handshake", (1, "keep")),
    ("终态让位", (_other(1, status="completed"),), "handshake", (1, "terminal_takeover")),
    ("遗弃让位", (_other(1, stale=True),), "handshake", (1, "stale_takeover")),
    ("新鲜保护", (_other(1),), "handshake", (1, "protected")),
    ("同 trainer 新鲜场仍走保护(操作权由调用方核)", (_other(1, trainer=T),), "handshake",
     (1, "protected")),
    ("同受试者遗弃场", (_other(1, patient=P, stale=True),), "handshake", (1, "stale_takeover")),
    ("持有的场次已不存在", (_gone(1),), "handshake", (1, "vacant")),
    ("自动场切回:遗弃不让位,只走保护", (_other(1, stale=True),), "autopilot_return",
     (1, "protected")),
    ("自动场切回:终态", (_other(1, status="aborted"),), "autopilot_return",
     (1, "terminal_takeover")),
    ("2 号行被忽略:本场只在 2 号也照样落 1 号", (_mine(2),), "handshake", (1, "vacant")),
    ("2 号行被忽略:不因 2 号的影子判违例", (_other(1), _other(2, stale=True)), "handshake",
     (1, "protected")),
]


@pytest.mark.parametrize(
    ("views", "purpose", "expected"),
    [case[1:] for case in _ONE_SLOT_CASES],
    ids=[case[0] for case in _ONE_SLOT_CASES],
)
def test_one_slot_selection_table(views, purpose, expected):
    assert _outcome(*views, n=1, purpose=purpose) == expected


def test_unknown_purpose_is_refused():
    with pytest.raises(ValueError):
        _choose(n=2, purpose="autopilot-return")


def test_terminal_statuses_match_main():
    assert session_admission.BEDSIDE_TERMINAL_STATUSES == main._TERMINAL_RUNTIME_STATUSES


def test_slot_errors_carry_only_the_closed_code():
    for views, code in (((_other(1), _second(2)), "bedside_slots_full"),
                        ((_mine(1), _other(2, patient=P)), "bedside_slot_conflict")):
        with pytest.raises(BedsideSlotError) as caught:
            _choose(*views)
        assert caught.value.code == code
        assert caught.value.args == (code,)


# ── 性质断言 ───────────────────────────────────────────────────────────────

def _today_action(view: BedsideSlotView | None, purpose: str) -> str:
    """照抄今天 main.py 单槽的分支:握手 live_put(kind=session)与 autopilot_activate。"""
    previous = view.session_id if view is not None else None
    if previous == R:
        return "keep"
    if not previous or not view.session_exists:
        return "vacant"
    if view.status in _TERMINAL:
        return "terminal_takeover"
    if purpose == "handshake" and view.stale:
        return "stale_takeover"
    return "protected"


def _session(sid, patient, trainer, status):
    return {"session_id": sid, "patient_id": patient, "trainer_id": trainer, "status": status}


def _place(slot, option):
    if option is None:
        return None
    if option == "gone":
        return _gone(slot)
    if option == "empty":
        return BedsideSlotView(slot_id=slot)
    holder, stale = option
    return BedsideSlotView(slot_id=slot, session_exists=True, stale=stale, **holder)


_REQUESTER = _session(R, P, T, "active")
# paused 与 active、aborted 与 completed 对选槽等价,只留一个代表;None(没有 runtime 行)单列。
_STATUSES = (None, "active", "intervention_completed", "completed", "failed")
_OTHERS = [
    (_session("S-X", px, tx, sx), _session("S-Y", py, ty, sy))
    for px, tx, sx in itertools.product((P, "P-O"), (T, "T-X"), _STATUSES)
    for py, ty, sy in itertools.product((P, "P-O", "P-Y"), (T, "T-Y"), _STATUSES)
]


def _states():
    """两槽各取:没有行 / 有行无 sessionId / 场次已不存在 / 本场 / X / Y,后三者再分新旧。
    同一场次放进两槽时属性一致,只有 stale 按槽不同。"""
    for x, y in _OTHERS:
        options = [None, "empty", "gone",
                   *((holder, stale) for holder in (_REQUESTER, x, y) for stale in (False, True))]
        for o1, o2 in itertools.product(options, repeat=2):
            yield tuple(v for v in (_place(1, o1), _place(2, o2)) if v is not None)


def _holding(views):
    return [v for v in views
            if v.session_id and (v.session_id == R
                                 or (v.session_exists and v.status not in _TERMINAL))]


def _invariant_holds(views) -> bool:
    """一场最多占一槽、一位受试者最多占一槽;终态场和已不存在的场不算占槽。"""
    holding = _holding(views)
    sessions = [v.session_id for v in holding]
    patients = [v.patient_id for v in holding]
    return len(set(sessions)) == len(sessions) and len(set(patients)) == len(patients)


def _requester_invariant_holds(views) -> bool:
    """只看请求的这一场和它的受试者:各自最多占一槽。"""
    holding = _holding(views)
    return (sum(v.session_id == R for v in holding) <= 1
            and sum(v.session_id == R or v.patient_id == P for v in holding) <= 1)


def _apply(views, slot_id):
    kept = [v for v in views if v.slot_id != slot_id]
    return (*kept, BedsideSlotView(slot_id=slot_id, session_exists=True, **_REQUESTER))


def _slot(views, slot_id):
    return next((v for v in views if v.slot_id == slot_id), None)


def _is_live_other(view):
    return (view is not None and view.session_id not in {None, R} and view.session_exists
            and view.status not in _TERMINAL)


def test_one_slot_always_slot_one_and_matches_today_branch():
    seen = set()
    for views in _states():
        for purpose in _PURPOSES:
            choice = _choose(*views, n=1, purpose=purpose)
            assert choice.slot_id == 1
            assert choice.action == _today_action(_slot(views, 1), purpose)
            seen.add((purpose, choice.action))
    assert seen == {(purpose, action) for purpose in _PURPOSES
                    for action in ("keep", "vacant", "terminal_takeover", "protected")
                    } | {("handshake", "stale_takeover")}


def test_two_slots_keep_the_invariant_and_refuse_only_broken_or_full_states():
    outcomes = set()
    for views in _states():
        for purpose in _PURPOSES:
            try:
                choice = _choose(*views, purpose=purpose)
            except BedsideSlotError as exc:
                outcomes.add(exc.code)
                # 只有牵涉到本场或本场受试者的违例才拒;别人的违例不连坐。
                if exc.code == "bedside_slot_conflict":
                    assert not _requester_invariant_holds(views)
                    continue
                assert exc.code == "bedside_slots_full"
                assert _requester_invariant_holds(views)
                for slot_id in (1, 2):
                    view = _slot(views, slot_id)
                    assert _is_live_other(view)
                    assert view.patient_id != P and view.trainer_id != T
                    assert purpose == "autopilot_return" or not view.stale
                continue
            outcomes.add(choice.action)
            assert _requester_invariant_holds(views)
            assert choice.slot_id in (1, 2)
            chosen = _slot(views, choice.slot_id)
            # 槽内动作恒为今天单槽对这个持有者的分支,变的只是选哪一槽。
            assert choice.action == _today_action(chosen, purpose)
            after = _apply(views, choice.slot_id)
            assert _requester_invariant_holds(after)
            if _invariant_holds(views):
                assert _invariant_holds(after)
            assert [v.slot_id for v in after if v.session_id == R] == [choice.slot_id]
            if _is_live_other(chosen):
                # 只在同受试者/同 trainer,或已经没有空闲槽时,才动别人占着的槽。
                other = _slot(views, 3 - choice.slot_id)
                assert (chosen.patient_id == P or chosen.trainer_id == T
                        or _is_live_other(other))
    assert outcomes == {"keep", "vacant", "terminal_takeover", "stale_takeover", "protected",
                        "bedside_slot_conflict", "bedside_slots_full"}


def test_two_slots_with_a_single_session_always_use_slot_one():
    checked = 0
    for views in _states():
        if any(_is_live_other(v) for v in views):
            continue
        if any(v.session_id == R and v.slot_id == 2 for v in views):
            continue
        for purpose in _PURPOSES:
            assert _choose(*views, purpose=purpose).slot_id == 1
            checked += 1
    assert checked > 0


# ── 槽数开关与按场次找行 ──────────────────────────────────────────────────

@pytest.mark.parametrize(("raw", "expected"), [
    (None, 1), ("", 1), ("1", 1), ("2", 2), (" 2 ", 2), ("2\n", 2), ("\t2", 2),
    ("3", 1), ("02", 1), ("2.0", 1), ("two", 1), ("-2", 1), ("0", 1),
])
def test_slot_count_is_two_only_for_exact_two(monkeypatch, raw, expected):
    if raw is None:
        monkeypatch.delenv("NMU_BEDSIDE_SLOTS", raising=False)
    else:
        monkeypatch.setenv("NMU_BEDSIDE_SLOTS", raw)
    assert session_admission.bedside_slot_count() == expected


def test_slot_count_is_read_on_every_call(monkeypatch):
    monkeypatch.setenv("NMU_BEDSIDE_SLOTS", "2")
    assert session_admission.bedside_slot_count() == 2
    monkeypatch.setenv("NMU_BEDSIDE_SLOTS", "1")
    assert session_admission.bedside_slot_count() == 1


@pytest.fixture
def db_session():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    SQLModel.metadata.create_all(engine)
    with Session(engine) as session:
        yield session


def _seed(session, **holders):
    for slot_id, sid in holders.items():
        session.add(LiveState(
            id=int(slot_id.removeprefix("slot")),
            session_json=None if sid is None else json.dumps({"sessionId": sid})))
    session.commit()


def _rows(session):
    return session.exec(select(LiveState.id).order_by(LiveState.id)).all()


@pytest.mark.parametrize("raw", [None, "1", "3"])
def test_lookup_with_one_slot_is_always_the_anchor(db_session, monkeypatch, raw):
    if raw is None:
        monkeypatch.delenv("NMU_BEDSIDE_SLOTS", raising=False)
    else:
        monkeypatch.setenv("NMU_BEDSIDE_SLOTS", raw)
    assert session_admission.live_row_for_session(db_session, "S-A") is None
    _seed(db_session, slot1="S-A", slot2="S-B")
    anchor = db_session.get(LiveState, 1)
    for sid in ("S-A", "S-B", "S-NONE", None, ""):
        assert session_admission.live_row_for_session(db_session, sid) is anchor


def test_lookup_with_two_slots_finds_own_row_or_falls_back_to_anchor(db_session, monkeypatch):
    monkeypatch.setenv("NMU_BEDSIDE_SLOTS", "2")
    assert session_admission.live_row_for_session(db_session, "S-A") is None
    _seed(db_session, slot2="S-B")
    assert session_admission.live_row_for_session(db_session, "S-B").id == 2
    assert session_admission.live_row_for_session(db_session, "S-A") is None
    _seed(db_session, slot1="S-A")
    assert session_admission.live_row_for_session(db_session, "S-A").id == 1
    assert session_admission.live_row_for_session(db_session, "S-B").id == 2
    for sid in ("S-NONE", None, ""):
        assert session_admission.live_row_for_session(db_session, sid).id == 1


def test_lookup_with_two_slots_refuses_a_session_in_both(db_session, monkeypatch):
    monkeypatch.setenv("NMU_BEDSIDE_SLOTS", "2")
    _seed(db_session, slot1="S-A", slot2="S-A")
    with pytest.raises(BedsideSlotError) as caught:
        session_admission.live_row_for_session(db_session, "S-A")
    assert caught.value.args == ("bedside_slot_conflict",)
    assert session_admission.live_row_for_session(db_session, "S-B").id == 1


@pytest.mark.parametrize("raw", [None, "2"])
def test_lookup_is_read_only(db_session, monkeypatch, raw):
    if raw is None:
        monkeypatch.delenv("NMU_BEDSIDE_SLOTS", raising=False)
    else:
        monkeypatch.setenv("NMU_BEDSIDE_SLOTS", raw)
    for sid in ("S-A", None):
        session_admission.live_row_for_session(db_session, sid)
    assert not db_session.new and not db_session.dirty
    assert _rows(db_session) == []
    _seed(db_session, slot1="S-A")
    session_admission.live_row_for_session(db_session, "S-B")
    assert not db_session.new and not db_session.dirty
    assert _rows(db_session) == [1]
