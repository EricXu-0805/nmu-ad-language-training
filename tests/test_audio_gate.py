import pytest

from app.audio_gate import (
    AudioAsset, AudioGateError,
    mark_exported, mark_checksum_verified, mark_reliability_review_done, mark_withdrawn,
    can_delete, request_delete, guard_time_based_purge,
)
from app.enums import AudioStatus


# ---------- 非信度样本：导出+校验通过即可删 ----------
def test_normal_path_to_deletable():
    a = AudioAsset("r1")
    assert can_delete(a)[0] is False          # recorded 不能删
    mark_exported(a)
    assert a.status == AudioStatus.exported
    assert can_delete(a)[0] is False          # 仅导出、未校验，仍不能删
    mark_checksum_verified(a)
    assert a.status == AudioStatus.deletable  # 非信度样本校验通过即可删
    assert can_delete(a)[0] is True
    request_delete(a, source="auto")
    assert a.status == AudioStatus.deleted


def test_refuse_delete_before_export():
    a = AudioAsset("r2")
    with pytest.raises(AudioGateError):
        request_delete(a, source="auto")


def test_guard_blocks_time_purge_until_deletable():
    a = AudioAsset("r3")
    with pytest.raises(AudioGateError):      # recorded 阶段定时清理必须被拦
        guard_time_based_purge(a)
    mark_exported(a)
    mark_checksum_verified(a)
    guard_time_based_purge(a)                # 到 deletable 才放行，不抛错


# ---------- 信度样本：还须人工复核完成 ----------
def test_reliability_sample_needs_review():
    a = AudioAsset("rel1", is_reliability_sample=True)
    mark_exported(a)
    mark_checksum_verified(a)
    assert a.status == AudioStatus.checksum_verified   # 信度样本校验后仍未可删
    ok, reason = can_delete(a)
    assert ok is False and "人工复核" in reason
    with pytest.raises(AudioGateError):                # 复核前删除被拒
        request_delete(a, source="auto")
    mark_reliability_review_done(a)
    assert a.status == AudioStatus.deletable
    assert can_delete(a)[0] is True


def test_reliability_review_requires_flag_and_order():
    a = AudioAsset("rel2", is_reliability_sample=False)
    with pytest.raises(AudioGateError):      # 非信度样本无此步
        mark_reliability_review_done(a)
    b = AudioAsset("rel3", is_reliability_sample=True)
    with pytest.raises(AudioGateError):      # 未校验就复核
        mark_reliability_review_done(b)


# ---------- 撤回：合规优先，覆盖闸门 ----------
def test_withdrawal_allows_deletion_even_when_recorded():
    a = AudioAsset("w1")
    mark_withdrawn(a)
    ok, reason = can_delete(a)
    assert ok is True and "撤回" in reason
    request_delete(a, source="withdrawal")
    assert a.status == AudioStatus.deleted


# ---------- 非法流转 ----------
def test_illegal_transitions():
    a = AudioAsset("i1")
    with pytest.raises(AudioGateError):       # 未导出不能校验
        mark_checksum_verified(a)
    mark_exported(a)
    with pytest.raises(AudioGateError):       # 已 exported 不能再 export
        mark_exported(a)
