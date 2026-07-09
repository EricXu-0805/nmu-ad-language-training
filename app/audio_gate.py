"""音频删除闸门状态机（护栏1）。

硬约束（开发计划§6）：原始音频【导出成功 + 校验通过】（信度样本还需【人工复核完成】）
之后平台才准删；**禁止无条件到期自动删**。受试者撤回须履行删除（覆盖闸门）。

状态流转：
    recorded ──export──▶ exported ──checksum──▶ checksum_verified
        非信度样本： checksum_verified ─────────────────────────▶ deletable
        信度样本：   checksum_verified ──reliability_review──▶ reliability_review_done ─▶ deletable
    deletable ──delete──▶ deleted
    （任意状态）withdrawn=True ⇒ 允许删除（合规撤回优先）
"""
from __future__ import annotations

from dataclasses import dataclass

from .enums import AudioStatus


class AudioGateError(RuntimeError):
    """非法状态流转或违规删除。"""


@dataclass
class AudioAsset:
    raw_audio_id: str
    status: AudioStatus = AudioStatus.recorded
    is_reliability_sample: bool = False
    withdrawn: bool = False


def mark_exported(a: AudioAsset) -> None:
    if a.status != AudioStatus.recorded:
        raise AudioGateError(f"{a.raw_audio_id}: 只有 recorded 才能标记 exported（当前 {a.status.value}）")
    a.status = AudioStatus.exported


def mark_checksum_verified(a: AudioAsset) -> None:
    if a.status != AudioStatus.exported:
        raise AudioGateError(f"{a.raw_audio_id}: 只有 exported 才能校验（当前 {a.status.value}）")
    # 非信度样本校验通过即可删；信度样本还须人工复核
    a.status = (AudioStatus.checksum_verified if a.is_reliability_sample
                else AudioStatus.deletable)


def mark_reliability_review_done(a: AudioAsset) -> None:
    if not a.is_reliability_sample:
        raise AudioGateError(f"{a.raw_audio_id}: 非信度样本无需人工复核这一步")
    if a.status != AudioStatus.checksum_verified:
        raise AudioGateError(f"{a.raw_audio_id}: 复核前须先校验通过（当前 {a.status.value}）")
    a.status = AudioStatus.deletable


def mark_withdrawn(a: AudioAsset) -> None:
    a.withdrawn = True


def can_delete(a: AudioAsset) -> tuple[bool, str]:
    """返回 (是否可删, 原因)。撤回优先；否则须达 deletable。"""
    if a.status == AudioStatus.deleted:
        return False, "已删除"
    if a.withdrawn:
        return True, "受试者撤回，须履行删除"
    if a.status == AudioStatus.deletable:
        tail = "+信度样本人工复核完成" if a.is_reliability_sample else ""
        return True, f"已导出+校验通过{tail}"
    need = "导出成功+校验通过" + ("+信度样本须人工复核完成" if a.is_reliability_sample else "")
    return False, f"未达可删状态（当前 {a.status.value}）：{need} 之前不得删除"


def request_delete(a: AudioAsset, *, source: str = "auto") -> None:
    """执行删除。source 仅用于审计（'auto' 定时清理 / 'manual' 人工 / 'withdrawal' 撤回）。
    未达闸门条件即抛错——这正是「禁止无条件到期自动删」的强制点。"""
    ok, reason = can_delete(a)
    if not ok:
        raise AudioGateError(f"{a.raw_audio_id}: 拒绝删除（{source}）：{reason}")
    a.status = AudioStatus.deleted


def guard_time_based_purge(a: AudioAsset) -> None:
    """定时清理任务在删除前必须先过这道守卫：非可删且未撤回则抛错，杜绝到期盲删。"""
    ok, reason = can_delete(a)
    if not ok:
        raise AudioGateError(f"{a.raw_audio_id}: 定时清理被闸门拦截：{reason}")
