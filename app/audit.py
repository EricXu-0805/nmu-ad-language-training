"""研究审计账本(M1-E)——只追加、防篡改(tamper-evident)。

用途:给研究真值的每次变更(锁分/量表/异常介入/删录音/导出)和账号安全事件(登录)
留下"谁在何时做了什么"的可核查记录,供论文数据溯源与伦理核查。

完整性设计(边界诚实标注,勿夸大):
  * 哈希链:entry_hash = sha256(prev_hash | ts | actor | action | patient | session | turn | summary)。
    改动/删除任一【中间】行 → 从该行起断链,verify 检出。
  * 高水位锚点(AuditAnchor 单行):每次追加在【同一事务】里更新 (count, tip_hash);
    verify 比对锚点 → 尾部截断/整表清空可检出(裸哈希链单靠自身检不出的盲区)。
  * prev_hash 唯一约束:并发追加撞链(多 worker/多副本)在 DB 层被挡 → 不会静默分叉;
    进程内 _APPEND_LOCK 只是快路径,真正的防线在约束 + 重试。
  * 【已知残余】sha256 无密钥、锚点与账本同库——掌握 DB 写权且懂方案者可重算链+改锚点整体伪造。
    强保证需外部密钥 HMAC/把链尾定期锚定到外部不可变存储,留待后续;本工具威胁模型是
    可信研究者小组的诚实溯源 + 意外损坏检出 + 阻吓随手篡改,不承诺抗全权内部人。

其余口径:
  * 红线:summary 只写元数据(研究编号 + 动作 + 分值/类型),【绝不含患者回答文本或姓名】。
  * 记账失败绝不拖垮临床写(锁分不能因审计库抖动而失败)——调用方 best-effort 包裹。
  * ts 沿用库内约定的本地 datetime.now();哈希取 isoformat(timespec='microseconds') 固定精度,
    要求 DB DateTime 往返保留微秒(SQLite/Postgres 均满足;接新后端前须确认,否则 verify 会假阳性报断)。
"""
from __future__ import annotations

import hashlib
import threading
from datetime import datetime
from typing import Optional

from sqlalchemy.exc import IntegrityError
from sqlmodel import Session as DBSession, select

from .models import AuditAnchor, AuditLog

_GENESIS = "0" * 64
_APPEND_LOCK = threading.Lock()
_APPEND_RETRIES = 3


def _chain_hash(prev_hash: str, ts: datetime, actor: str, action: str,
                patient_id: Optional[str], session_id: Optional[str],
                turn_id: Optional[int], summary: str) -> str:
    payload = "|".join([
        prev_hash, ts.isoformat(timespec="microseconds"), actor, action,
        patient_id or "", session_id or "", "" if turn_id is None else str(turn_id), summary,
    ])
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def record(session: DBSession, *, actor: str, action: str, summary: str,
           patient_id: Optional[str] = None, session_id: Optional[str] = None,
           turn_id: Optional[int] = None) -> None:
    """追加一条审计并原子更新高水位锚点。summary 务必只含元数据。

    用【独立会话】(绑定调用方同一引擎)提交,绝不 commit 调用方的 session——否则会把调用方
    刚锁好的 turn/量表对象 expire 掉,导致接口 response_model 序列化出空对象。
    并发撞链(多 worker 同 prev_hash)由唯一约束挡下 → 回滚重读重试。
    """
    bind = session.get_bind()
    with _APPEND_LOCK:
        for attempt in range(_APPEND_RETRIES):
            with DBSession(bind) as s:
                try:
                    last = s.exec(select(AuditLog).order_by(AuditLog.id.desc()).limit(1)).first()
                    prev = last.entry_hash if last else _GENESIS
                    ts = datetime.now()
                    entry_hash = _chain_hash(prev, ts, actor, action, patient_id, session_id, turn_id, summary)
                    s.add(AuditLog(ts=ts, actor=actor, action=action, patient_id=patient_id,
                                   session_id=session_id, turn_id=turn_id, summary=summary,
                                   prev_hash=prev, entry_hash=entry_hash))
                    anchor = s.get(AuditAnchor, 1) or AuditAnchor(id=1)
                    anchor.count += 1
                    anchor.tip_hash = entry_hash
                    anchor.updated_at = ts
                    s.add(anchor)
                    s.commit()          # 账本行 + 锚点同事务:要么都落,要么都不落
                    return
                except IntegrityError:
                    s.rollback()        # 撞链(并发同 prev_hash)→ 重读末行重试
                    if attempt == _APPEND_RETRIES - 1:
                        raise


def verify_chain(s: DBSession) -> dict:
    """从头重算哈希链并比对高水位锚点。

    返回 problem ∈ {None, 'chain_broken', 'truncated', 'anchor_behind'}:
      chain_broken  —— 某行被改/中间被删(broken_at=首个对不上的行 id);
      truncated     —— 链自洽但条数/链尾低于锚点 → 尾部被删或整表清空;
      anchor_behind —— 锚点落后于账本(理论上同事务不该出现,报出来供排查)。
    """
    rows = list(s.exec(select(AuditLog).order_by(AuditLog.id)))
    prev = _GENESIS
    for r in rows:
        expect = _chain_hash(prev, r.ts, r.actor, r.action, r.patient_id, r.session_id, r.turn_id, r.summary)
        if r.prev_hash != prev or r.entry_hash != expect:
            return {"ok": False, "problem": "chain_broken", "count": len(rows), "broken_at": r.id}
        prev = r.entry_hash
    anchor = s.get(AuditAnchor, 1)
    expected_count = anchor.count if anchor else 0
    expected_tip = anchor.tip_hash if anchor else _GENESIS
    if len(rows) < expected_count or (expected_count and prev != expected_tip and len(rows) <= expected_count):
        return {"ok": False, "problem": "truncated", "count": len(rows),
                "expected_count": expected_count, "broken_at": None}
    if len(rows) > expected_count:
        return {"ok": False, "problem": "anchor_behind", "count": len(rows),
                "expected_count": expected_count, "broken_at": None}
    return {"ok": True, "problem": None, "count": len(rows), "broken_at": None}
