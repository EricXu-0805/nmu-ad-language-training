"""老人端绑定令牌必须会过期。

2026-08-27 审查坐实：`verify_binding_token` 解出 claims 里的 `t` 只校验它是非负整数，
全仓没有任何地方拿它比过时间；`web/src/security/patientBinding.ts` 又把它长期存在
localStorage。唯一的作废手段是轮换 CONSOLE_PIN，而那会同时作废所有受试者的配对码。

后果：平板从张阿姨换给李阿姨、送修、被家属带走，旧令牌照样有效。持有者轮询
`/device/attach` 就能先于真平板拿到设备能力，读到这一场的全部临床呈现内容、
以「老人端设备」身份 POST /audio 往研究记录里写字节、POST patient-pause 打断训练；
真平板此时被 `allow_rotation=False` 拒成 device_attach_device_busy，
现场看到的是「配对不上」。

寿命定在一个训练周期（8 周）加缓冲：整个干预就 8 周，跨周期还在用的令牌一定是
该重新配对的那种。
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app import patient_pairing


def _issued(days_ago: float, monkeypatch) -> str:
    monkeypatch.setenv("CONSOLE_PIN", "24681357")
    when = datetime.now(timezone.utc) - timedelta(days=days_ago)
    return patient_pairing.issue_binding_token("P-001", "dev-abcdefghijklmnop", now=when)


def test_a_fresh_token_verifies(monkeypatch):
    token = _issued(0.0, monkeypatch)
    claims = patient_pairing.verify_binding_token(token)
    assert claims is not None and claims.patient_id == "P-001"


def test_a_token_from_within_the_training_cycle_still_verifies(monkeypatch):
    token = _issued(patient_pairing.BINDING_TOKEN_MAX_AGE_DAYS - 1, monkeypatch)
    assert patient_pairing.verify_binding_token(token) is not None


def test_a_token_older_than_the_training_cycle_is_refused(monkeypatch):
    token = _issued(patient_pairing.BINDING_TOKEN_MAX_AGE_DAYS + 1, monkeypatch)
    assert patient_pairing.verify_binding_token(token) is None, (
        "过期令牌仍然验得过——平板换了人，旧令牌照样能拿设备能力")


def test_a_token_dated_in_the_future_is_refused(monkeypatch):
    """时钟回拨或伪造的未来时间戳不能换来无限寿命。"""
    token = _issued(-400.0, monkeypatch)
    assert patient_pairing.verify_binding_token(token) is None


def test_the_lifetime_covers_a_whole_eight_week_intervention():
    assert patient_pairing.BINDING_TOKEN_MAX_AGE_DAYS >= 8 * 7


def test_an_expired_token_lands_on_the_code_the_frontend_drops_on(monkeypatch):
    """链路：过期 → verify 返 None → /device/attach 401 device_binding_invalid
    → `classifyAttachOutcome` 判 drop_binding → 前端清 localStorage 回配对界面。

    少了最后这一段，老人端会带着一个永远换不到能力的令牌静默空转。
    """
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    backend = (root / "app/main.py").read_text(encoding="utf-8")
    assert "device_binding_invalid" in backend
    policy = (root / "web/src/patient/bindingAttachPolicy.ts").read_text(encoding="utf-8")
    assert 'code === "device_binding_invalid"' in policy
    assert '"drop_binding"' in policy
