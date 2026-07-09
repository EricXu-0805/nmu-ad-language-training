from app import enums
from app.enums import FROZEN_MEMBERS, PromptLevel, AudioStatus


def test_frozen_members_present():
    for enum_name, expected in FROZEN_MEMBERS.items():
        cls = getattr(enums, enum_name)
        actual = {m.value for m in cls}
        missing = expected - actual
        assert not missing, f"{enum_name} 缺少冻结成员 {missing}"


def test_prompt_level_values():
    assert PromptLevel.自发正确 == 0
    assert PromptLevel.告知答案 == 3
    assert [PromptLevel(i).value for i in (0, 1, 2, 3)] == [0, 1, 2, 3]


def test_audio_status_lifecycle_members():
    order = ["recorded", "exported", "checksum_verified",
             "reliability_review_done", "deletable", "deleted"]
    assert [s.value for s in AudioStatus] == order
