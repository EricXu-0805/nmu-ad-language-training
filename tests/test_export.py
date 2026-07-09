import pytest
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine

from app import export
from app.export import DIRECT_IDENTIFIER_COLUMNS, mask_text, pseudonymize
from app.models import (
    AudioAssetRow, ItemEvent, Patient, ScaleResult, Session as TrainSession, TurnEvent,
)


@pytest.fixture
def db():
    eng = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    SQLModel.metadata.create_all(eng)
    with Session(eng) as s:
        yield s


def _seed(s):
    s.add(Patient(patient_id="P77", dementia_severity="轻度", mandarin_eligible=True))
    s.add(TrainSession(session_id="S9", patient_id="P77", week_no=2,
                       phase_type="正式训练", event_line="正式训练", item_bank_version_id="wk2-v1-20260707"))
    # 单要素：1 环节，锁定 final_correct=1，自发（prompt_level=0）
    se = ItemEvent(session_id="S9", item_id="SE_锚", task_type="单要素", item_set_type="训练集")
    s.add(se); s.commit(); s.refresh(se)
    s.add(TurnEvent(item_event_id=se.id, turn_seq=1, response_role="命名",
                    asr_text="锚", confirmed_response_text="锚", prompt_level=0,
                    element_value=1, reviewed_score=1, score_locked=True, reviewer_id="R1"))
    # 双要素：5 环节全锁定，全对 → de_total=1.0
    de = ItemEvent(session_id="S9", item_id="DE_斧子+树", task_type="双要素", item_set_type="训练集")
    s.add(de); s.commit(); s.refresh(de)
    for seq, role in enumerate(["左命名", "左作用", "右命名", "右作用", "关系识别"], start=1):
        s.add(TurnEvent(item_event_id=de.id, turn_seq=seq, response_role=role,
                        element_value=1, reviewed_score=1, score_locked=True, reviewer_id="R1"))
    # 含直接标识符的音频（第1周自我介绍类）——转写须被红线
    s.add(AudioAssetRow(raw_audio_id="a_id", session_id="S9", contains_direct_identifier=True))
    s.add(AudioAssetRow(raw_audio_id="a_plain", session_id="S9"))
    s.add(ScaleResult(patient_id="P77", phase_type="前测", scale_name="CETI", score=42.0, assessor_id="A1"))
    s.commit()


def test_pseudonym_is_stable_and_not_raw_id():
    assert pseudonymize("P77") == pseudonymize("P77")
    assert "P77" not in pseudonymize("P77")


def test_mask_redacts_direct_identifier_and_digits():
    assert mask_text("我叫张三今年85岁", True) == "〔含直接标识·已去标识〕"
    assert mask_text("电话13800001111", False) == "电话##"
    assert mask_text("锚", False) == "锚"


def test_deidentified_export_has_no_direct_identifiers(db, tmp_path):
    _seed(db)
    res = export.export_session_bundle(db, "S9", deidentify=True, write_dir=tmp_path)
    # 任一表都不得出现直接标识列
    for name, rows in res["sheets"].items():
        for r in rows:
            assert not (DIRECT_IDENTIFIER_COLUMNS & set(r)), f"{name} 泄露直接标识"
    # patient_id 不出现在任何值里
    flat = str(res["sheets"])
    assert "P77" not in flat
    assert pseudonymize("P77") in flat
    assert "crosswalk" not in res["sheets"]


def test_export_reconstructs_scores_from_locked_turns(db, tmp_path):
    _seed(db)
    res = export.export_session_bundle(db, "S9", deidentify=True, write_dir=tmp_path)
    scores = export._reconstruct_scores(
        list(db.exec(export.select(ItemEvent).where(ItemEvent.session_id == "S9"))),
        {it.id: list(db.exec(export.select(TurnEvent).where(TurnEvent.item_event_id == it.id)))
         for it in db.exec(export.select(ItemEvent).where(ItemEvent.session_id == "S9"))})
    assert scores["double"]["weekly_de_score_percentile"] == 100.0
    assert scores["single"]["naming_accuracy"] == 1.0
    assert scores["single"]["spontaneous_naming_accuracy"] == 1.0
    assert scores["excluded_items"] == []


def test_export_masks_identifier_transcript_and_triggers_audio_gate(db, tmp_path):
    _seed(db)
    res = export.export_session_bundle(db, "S9", deidentify=True, write_dir=tmp_path)
    # 含标识音频对应的转写被红线（此处两条 turn 无 raw_audio_id 关联，验证 mask 规则由上一测试覆盖）
    # 音频闸门：recorded → exported，且打上批次；绝不删除
    a = db.get(AudioAssetRow, "a_id")
    assert a.status.value == "exported" and a.export_batch_id == res["batch_id"]
    assert set(res["audio_touched"]) == {"a_id", "a_plain"}


def test_unlocked_turns_excluded_from_scoring(db, tmp_path):
    _seed(db)
    # 追加一道未锁定的单要素题
    ie = ItemEvent(session_id="S9", item_id="SE_花", task_type="单要素", item_set_type="训练集")
    db.add(ie); db.commit(); db.refresh(ie)
    db.add(TurnEvent(item_event_id=ie.id, turn_seq=1, response_role="命名", element_value=0, score_locked=False))
    db.commit()
    scores = export._reconstruct_scores(
        list(db.exec(export.select(ItemEvent).where(ItemEvent.session_id == "S9"))),
        {it.id: list(db.exec(export.select(TurnEvent).where(TurnEvent.item_event_id == it.id)))
         for it in db.exec(export.select(ItemEvent).where(ItemEvent.session_id == "S9"))})
    assert any("SE_花" in x for x in scores["excluded_items"])
    # 已锁定的单要素仍只算 1 题（未锁定的被排除）
    assert scores["single"]["n"] == 1
