"""技术失败后的两条出口,按控制台的真实顺序走 API:
①转人工 → 人工做完这一题 → 切回 AI;②「跳过本题」→ AI 从下一题接着带练。

单测此前只各覆盖一半(worker 技术失败暂停;失败证明可收麦接管;人工态切回 AI),
三段从没在同一场里连起来走过;截至 2026-10-02 生产 570 次 attempt 零技术失败,现场也没人走过。
"""
import json

from sqlmodel import Session, select

from app import asr
from app.main import _run_p0a_attempt_worker
from app.models import (
    AttemptEvent, AuditLog, AutopilotControlEvent, AutopilotPositionAdjudication,
    InteractionEvent, ItemEvent, LiveState, RuntimeCommand, SessionAutopilotState,
    SessionOutcomeSummary, SessionRuntimeState, TurnEvent,
)
from test_autopilot_api import (
    SESSION_ID, TWO_ONLY_BANK, _EmptyTranscriptAsr, _WorkerAsr, _ack_body, _adjudicate,
    _bind_api_bank, _device_next, _drive_followup_processing_attempt_via_current_cue,
    _drive_issued_question_to_processing_attempt, _drive_to_processing_attempt,
    _enable_p0a, _resume, api_clients as api_clients,
)

FIRST, SECOND = TWO_ONLY_BANK.single_element
MANUAL_AUDIO_ID = "manual-after-technical-failure-0001"


def _snapshot(engine) -> dict:
    with Session(engine) as db:
        def rows(model):
            return [row.model_dump() for row in db.exec(select(model).order_by(model.id))]
        return {
            "control": db.get(SessionAutopilotState, SESSION_ID).model_dump(),
            "runtime": db.get(SessionRuntimeState, SESSION_ID).model_dump(),
            "live": db.get(LiveState, 1).model_dump(),
            "commands": rows(RuntimeCommand),
            "attempts": rows(AttemptEvent),
            "control_events": rows(AutopilotControlEvent),
            "adjudications": rows(AutopilotPositionAdjudication),
            "interactions": rows(InteractionEvent),
            "items": rows(ItemEvent),
            "turns": rows(TurnEvent),
        }


def _assert_ai_exits_refused(clients, revision: int, *, suffix: str) -> None:
    """失败那一题 AI 接不下去,也没有可确认的回答:继续 AI 自动带练、老人已答对都拒。"""
    resumed = _resume(clients, key=f"resume-after-asr-failure-{suffix}",
                      expected_revision=revision)
    assert resumed.status_code == 409, resumed.text
    assert resumed.json()["detail"]["code"] == "autopilot_attempt_failed"
    refused = _adjudicate(clients, key=f"adjudicate-confirmed_correct-{suffix}",
                          expected_revision=revision, kind="confirmed_correct",
                          reason_code="asr_misrecognized")
    assert refused.status_code == 409, refused.text
    assert refused.json()["detail"]["code"] == "autopilot_attempt_failed"


def _put_manual_cursor(clients, **overrides) -> None:
    cursor = {"sessionId": SESSION_ID, "screen": "present", "itemIdx": 0, "turnIdx": 0,
              "responseRole": "命名", "cueLevel": 0, "recording": "idle", "recSeq": 1,
              "selfStart": False}
    cursor.update(overrides)
    written = clients.account.put("/live/state", json={"kind": "cursor", "payload": cursor})
    assert written.status_code == 200, written.text


def _device_records_manual_answer(clients, raw_audio_id: str) -> None:
    """老人端人工录音:授权 → 开麦 → 自选 id 登记 → 传字节 → audioSaved → 收麦。"""
    turn_ref = "itm-0001#1"
    headers = clients.device_headers
    authorized = clients.device.post(
        f"/sessions/{SESSION_ID}/recording-authorization", headers=headers)
    assert authorized.status_code == 200, authorized.text
    mic_on = clients.device.put("/live/state", headers=headers, json={
        "kind": "patientRec",
        "payload": {"active": True, "turnKey": turn_ref, "sessionId": SESSION_ID}})
    assert mic_on.status_code == 200, mic_on.text
    registered = clients.device.post("/audio", headers=headers, json={
        "raw_audio_id": raw_audio_id, "session_id": SESSION_ID, "turn_key": turn_ref,
        "contains_direct_identifier": False})
    assert registered.status_code == 200, registered.text
    uploaded = clients.device.put(
        f"/audio/{raw_audio_id}/blob",
        headers={**headers, "content-type": "audio/webm"},
        content=b"\x1a\x45\xdf\xa3manual-answer-after-technical-failure")
    assert uploaded.status_code == 200, uploaded.text
    saved = clients.device.put("/live/state", headers=headers, json={
        "kind": "audioSaved", "payload": {
            "rawAudioId": raw_audio_id, "durationSeconds": 1.5,
            "byteCount": uploaded.json()["bytes"], "checksum": uploaded.json()["checksum"],
            "turnKey": turn_ref, "sessionId": SESSION_ID, "containsDirectIdentifier": False}})
    assert saved.status_code == 200, saved.text
    mic_off = clients.device.put("/live/state", headers=headers, json={
        "kind": "patientRec",
        "payload": {"active": False, "turnKey": turn_ref, "sessionId": SESSION_ID}})
    assert mic_off.status_code == 200, mic_off.text


def test_technical_failure_exit_takeover_manual_completion_then_ai_resumes_next_item(
        api_clients, monkeypatch):
    _enable_p0a(monkeypatch)
    _bind_api_bank(api_clients, monkeypatch, TWO_ONLY_BANK)
    capture = _drive_to_processing_attempt(api_clients)
    monkeypatch.setattr(asr, "get_engine", lambda: _WorkerAsr(fails=True))
    _run_p0a_attempt_worker(SESSION_ID)

    # 1. 第一题第一段录音判分技术失败:AI 安全暂停,没发提示、没消耗提示等级、没造环节记录。
    with Session(api_clients.engine) as db:
        failed = db.exec(select(AttemptEvent)).one()
        assert (failed.item_id, failed.turn_seq, failed.attempt_seq, failed.prompt_level) == (
            FIRST["item_id"], 1, 1, 0)
        assert (failed.processing_status, failed.error_code) == (
            "technical_failure", "asr_exception")
        assert failed.operational_answer_type is None
        failed_row = failed.model_dump()
        assert db.get(SessionRuntimeState, SESSION_ID).status == "paused"
        assert [(c.kind, c.item_id, c.state, json.loads(c.payload_json).get("purpose"))
                for c in db.exec(select(RuntimeCommand).order_by(RuntimeCommand.command_seq))
                ] == [("tts", FIRST["item_id"], "succeeded", "question"),
                      ("record", FIRST["item_id"], "succeeded", None)]
        assert list(db.exec(select(ItemEvent))) == []
        assert list(db.exec(select(TurnEvent))) == []
    journal = api_clients.account.get(f"/sessions/{SESSION_ID}/journal")
    assert journal.status_code == 200, journal.text
    # 控制台按 TurnEvent 与 cue_selected 恢复这一环节的提示等级:两者都没有 → 仍是 0 级。
    assert journal.json()["turns"] == []
    assert [row["event_type"] for row in journal.json()["interactions"]] == [
        "attempt_received", "asr_failed", "technical_pause"]
    status = api_clients.account.get(f"/sessions/{SESSION_ID}/autopilot/status")
    assert status.status_code == 200, status.text
    assert {key: status.json()[key] for key in (
        "mode", "status", "takeover_ready", "position_item_id", "last_error_code")} == {
        "mode": "autonomous", "status": "paused", "takeover_ready": True,
        "position_item_id": FIRST["item_id"], "last_error_code": "asr_exception"}
    revision = status.json()["state_revision"]

    # 2. 两个 AI 出口写前拒绝,一行都不落(「跳过本题」另有专测)。
    before = _snapshot(api_clients.engine)
    _assert_ai_exits_refused(api_clients, revision, suffix="0001")
    assert _snapshot(api_clients.engine) == before

    # 3. 失败本身就是收麦证明,drain-ack 只是重放;重放后这两个 AI 出口照样关着。这里走转人工。
    drained = api_clients.device.post(
        f"/sessions/{SESSION_ID}/autopilot/commands/{capture['record']['command_key']}/drain-ack",
        headers=api_clients.device_headers)
    assert drained.status_code == 200, drained.text
    assert drained.json() == {"replayed": True, "state_revision": revision}
    _assert_ai_exits_refused(api_clients, revision, suffix="0002")
    assert _snapshot(api_clients.engine) == before
    taken = api_clients.account.post(
        f"/sessions/{SESSION_ID}/autopilot/takeover",
        json={"idempotency_key": "takeover-after-asr-failure-0001",
              "expected_revision": revision})
    assert taken.status_code == 200, taken.text
    assert {key: taken.json()[key] for key in (
        "mode", "status", "server_owned", "position_item_id", "position_turn_seq")} == {
        "mode": "manual", "status": "paused", "server_owned": False,
        "position_item_id": FIRST["item_id"], "position_turn_seq": 1}
    manual_revision = taken.json()["state_revision"]

    # 人工这一题还没做完就点「切回 AI」:同一个码拒绝,状态不动。
    before_manual = _snapshot(api_clients.engine)
    too_early = _resume(api_clients, key="resume-before-manual-completion-0001",
                        expected_revision=manual_revision)
    assert too_early.status_code == 409, too_early.text
    assert too_early.json()["detail"]["code"] == "autopilot_attempt_failed"
    assert _snapshot(api_clients.engine) == before_manual

    # 4. 人工把失败的这一题做完:继续场次 → 示意录音 → 老人端录一段 → 收回示意 → 保存转写。
    runtime = api_clients.account.post(f"/sessions/{SESSION_ID}/resume")
    assert runtime.status_code == 200, runtime.text
    assert runtime.json()["status"] == "active"
    _put_manual_cursor(api_clients, screen="record", recording="armed")
    _device_records_manual_answer(api_clients, MANUAL_AUDIO_ID)
    # 控制台收到 audioSaved 就把游标写回 idle;留着 armed,切回 AI 会被当成热麦拒绝。
    _put_manual_cursor(api_clients)
    monkeypatch.setattr(asr, "get_engine", lambda: _WorkerAsr())
    processed = api_clients.account.post(f"/sessions/{SESSION_ID}/attempts/process", json={
        "item_id": FIRST["item_id"], "turn_seq": 1, "response_role": "命名",
        "raw_audio_id": MANUAL_AUDIO_ID, "prompt_level": 0, "duration_seconds": 1.5})
    assert processed.status_code == 200, processed.text
    manual_attempt = processed.json()["attempt"]
    assert (manual_attempt["processing_status"], manual_attempt["attempt_seq"],
            manual_attempt["prompt_level"], manual_attempt["operational_answer_type"]) == (
        "completed", 2, 0, "正确")
    item = api_clients.account.post(f"/sessions/{SESSION_ID}/items", json={
        "item_id": FIRST["item_id"], "task_type": "单要素", "image_id": FIRST["image_id"]})
    assert item.status_code == 200, item.text
    assert item.json()["presentation_order"] == 1
    turn = api_clients.account.post(f"/items/{item.json()['id']}/turns", json={
        "turn_seq": 1, "response_role": "命名", "raw_audio_id": MANUAL_AUDIO_ID})
    assert turn.status_code == 200, turn.text
    assert (turn.json()["source_attempt_id"], turn.json()["prompt_level"],
            turn.json()["ai_answer_type"]) == (manual_attempt["id"], 0, "正确")

    # 5. 切回 AI:从下一道未完成的题接着弹,不回到失败的那一题。
    resumed = _resume(api_clients, key="resume-after-manual-completion-0001",
                      expected_revision=manual_revision)
    assert resumed.status_code == 200, resumed.text
    assert {key: resumed.json()[key] for key in (
        "mode", "status", "position_item_id", "position_turn_seq", "last_error_code")} == {
        "mode": "autonomous", "status": "waiting_tts", "position_item_id": SECOND["item_id"],
        "position_turn_seq": 1, "last_error_code": None}
    question = _device_next(api_clients)
    assert question is not None
    assert (question["kind"], question["payload"]["purpose"], question["item_ref"],
            question["turn_seq"], question["attempt_seq"], question["prompt_level"]) == (
        "tts", "question", "itm-0002", 1, 1, 0)
    assert question["payload"]["speech_text"] == SECOND["initial_prompt"]
    assert question["control_generation"] == capture["record"]["control_generation"] + 1

    # 6. 账面:失败那条 attempt 原样保留;这一题唯一的环节记录来自人工那段录音,仍是 0 级。
    with Session(api_clients.engine) as db:
        assert db.get(AttemptEvent, failed_row["id"]).model_dump() == failed_row
        assert [(a.item_id, a.attempt_seq, a.prompt_level, a.processing_status)
                for a in db.exec(select(AttemptEvent).order_by(AttemptEvent.id))] == [
            (FIRST["item_id"], 1, 0, "technical_failure"),
            (FIRST["item_id"], 2, 0, "completed")]
        turns = list(db.exec(select(TurnEvent)))
        assert [(t.source_attempt_id, t.raw_audio_id) for t in turns] == [
            (manual_attempt["id"], MANUAL_AUDIO_ID)]
        assert [(c.kind, c.item_id, c.prompt_level, c.attempt_seq)
                for c in db.exec(select(RuntimeCommand).order_by(RuntimeCommand.command_seq))
                ] == [("tts", FIRST["item_id"], 0, 1), ("record", FIRST["item_id"], 0, 1),
                      ("tts", SECOND["item_id"], 0, 1)]
        assert [e.event_type for e in db.exec(select(AutopilotControlEvent).order_by(
            AutopilotControlEvent.event_seq))] == ["start", "failure", "takeover", "resume"]

    # 7. AI 把第二题练完,场次照常自动收尾;汇总如实记下那次技术失败。
    _drive_issued_question_to_processing_attempt(
        api_clients, ack_prefix="ack-after-exit", first_device_event_seq=3)
    monkeypatch.setattr(asr, "get_engine", lambda: _WorkerAsr(text=SECOND["target_word"]))
    _run_p0a_attempt_worker(SESSION_ID)
    feedback = _device_next(api_clients)
    assert feedback is not None and feedback["payload"]["purpose"] == "feedback"
    ended = api_clients.device.post(
        f"/sessions/{SESSION_ID}/autopilot/commands/{feedback['command_key']}/acks",
        headers=api_clients.device_headers,
        json=_ack_body(feedback, ack_type="tts_ended", ack_key="ack-after-exit-feedback-0001",
                       device_event_seq=5, media_ended=True, media_duration_ms=900))
    assert ended.status_code == 200, ended.text
    assert ended.json()["status"] == "scope_completed"
    with Session(api_clients.engine) as db:
        assert db.get(SessionRuntimeState, SESSION_ID).status == "intervention_completed"
        summary = db.get(SessionOutcomeSummary, SESSION_ID)
        assert summary is not None
        assert (summary.expected_turns, summary.matched_turns, summary.completed_attempt_turns,
                summary.audio_evidenced_turns) == (2, 2, 2, 2)
        assert (summary.total_attempts, summary.completed_attempts,
                summary.technical_failure_attempts, summary.prompt_level_0_count,
                summary.technical_pause_count) == (3, 2, 1, 3, 1)
        # 接管只记在控制账本里;汇总改数账本之前这里恒为 0(收尾面板「人工接管」跟着显示 0)。
        assert summary.researcher_takeover_count == 1


def _fail_current_attempt(clients, monkeypatch) -> int:
    monkeypatch.setattr(asr, "get_engine", lambda: _WorkerAsr(fails=True))
    _run_p0a_attempt_worker(SESSION_ID)
    status = clients.account.get(f"/sessions/{SESSION_ID}/autopilot/status")
    assert status.status_code == 200, status.text
    assert (status.json()["status"], status.json()["last_error_code"]) == (
        "paused", "asr_exception")
    return status.json()["state_revision"]


def _finish_with_feedback(clients, monkeypatch, *, target_word: str, ack_key: str,
                          device_event_seq: int):
    monkeypatch.setattr(asr, "get_engine", lambda: _WorkerAsr(text=target_word))
    _run_p0a_attempt_worker(SESSION_ID)
    feedback = _device_next(clients)
    assert feedback is not None and feedback["payload"]["purpose"] == "feedback"
    return clients.device.post(
        f"/sessions/{SESSION_ID}/autopilot/commands/{feedback['command_key']}/acks",
        headers=clients.device_headers,
        json=_ack_body(feedback, ack_type="tts_ended", ack_key=ack_key,
                       device_event_seq=device_event_seq, media_ended=True,
                       media_duration_ms=900))


def test_technical_failure_skip_item_records_a_named_skip_and_ai_continues_with_next_item(
        api_clients, monkeypatch):
    _enable_p0a(monkeypatch)
    _bind_api_bank(api_clients, monkeypatch, TWO_ONLY_BANK)
    capture = _drive_to_processing_attempt(api_clients)
    revision = _fail_current_attempt(api_clients, monkeypatch)
    before = _snapshot(api_clients.engine)
    _assert_ai_exits_refused(api_clients, revision, suffix="skip-0001")
    assert _snapshot(api_clients.engine) == before

    skipped = _adjudicate(api_clients, key="adjudicate-skip-after-failure-0001",
                          expected_revision=revision, kind="skip_item",
                          reason_code="asr_repeatedly_failed")
    assert skipped.status_code == 200, skipped.text
    assert {key: skipped.json()[key] for key in (
        "mode", "status", "position_item_id", "position_turn_seq", "last_error_code")} == {
        "mode": "autonomous", "status": "waiting_tts", "position_item_id": SECOND["item_id"],
        "position_turn_seq": 1, "last_error_code": None}
    question = _device_next(api_clients)
    assert (question["payload"]["purpose"], question["item_ref"], question["attempt_seq"],
            question["prompt_level"]) == ("question", "itm-0002", 1, 0)
    assert question["control_generation"] == capture["record"]["control_generation"] + 1

    # 只追加一条具名跳过收据:不造环节记录,失败那段 attempt 与交互账一字不动。
    with Session(api_clients.engine) as db:
        rows = list(db.exec(select(AutopilotPositionAdjudication)))
        assert [(r.kind, r.item_id, r.turn_seq, r.reason_code, r.source_attempt_id,
                 r.turn_event_id, r.idempotency_key) for r in rows] == [
            ("skipped", FIRST["item_id"], 1, "asr_repeatedly_failed", None, None,
             "adjudicate-skip-after-failure-0001")]
        assert rows[0].actor_id and rows[0].is_simulation is True
        assert [a.model_dump() for a in db.exec(
            select(AttemptEvent).order_by(AttemptEvent.id))] == before["attempts"]
        assert [i.model_dump() for i in db.exec(
            select(InteractionEvent).order_by(InteractionEvent.id))] == before["interactions"]
        assert list(db.exec(select(ItemEvent))) == []
        assert list(db.exec(select(TurnEvent))) == []
        assert [e.event_type for e in db.exec(select(AutopilotControlEvent).order_by(
            AutopilotControlEvent.event_seq))] == ["start", "failure", "resume"]
        audits = list(db.exec(select(AuditLog).where(
            AuditLog.action == "autopilot_position_adjudicated")))
        assert len(audits) == 1 and "kind=skip_item" in audits[0].summary

    replay = _adjudicate(api_clients, key="adjudicate-skip-after-failure-0001",
                         expected_revision=revision, kind="skip_item",
                         reason_code="asr_repeatedly_failed")
    assert replay.status_code == 200, replay.text
    assert replay.json()["position_item_id"] == SECOND["item_id"]
    with Session(api_clients.engine) as db:
        assert len(list(db.exec(select(AutopilotPositionAdjudication)))) == 1

    # AI 把第二题练完,场次照常自动收尾;汇总不替跳过的题声称完成,但照实记下那次失败。
    _drive_issued_question_to_processing_attempt(
        api_clients, ack_prefix="ack-after-skip", first_device_event_seq=3)
    ended = _finish_with_feedback(api_clients, monkeypatch, target_word=SECOND["target_word"],
                                  ack_key="ack-after-skip-feedback-0001", device_event_seq=5)
    assert ended.status_code == 200, ended.text
    assert ended.json()["status"] == "scope_completed"
    with Session(api_clients.engine) as db:
        assert db.get(SessionRuntimeState, SESSION_ID).status == "intervention_completed"
        summary = db.get(SessionOutcomeSummary, SESSION_ID)
        assert (summary.expected_turns, summary.matched_turns, summary.completed_attempt_turns,
                summary.audio_evidenced_turns) == (1, 1, 1, 1)
        assert (summary.total_attempts, summary.completed_attempts,
                summary.technical_failure_attempts, summary.technical_pause_count,
                summary.researcher_takeover_count) == (2, 1, 1, 1, 0)
        assert summary.generator_version == "server-authoritative-closeout.v3"


def test_technical_failure_skip_of_the_last_item_leaves_wrapup_to_the_researcher(
        api_clients, monkeypatch):
    _enable_p0a(monkeypatch)
    _bind_api_bank(api_clients, monkeypatch, TWO_ONLY_BANK)
    _drive_to_processing_attempt(api_clients)
    first_done = _finish_with_feedback(
        api_clients, monkeypatch, target_word=FIRST["target_word"],
        ack_key="ack-first-feedback-0001", device_event_seq=3)
    assert first_done.status_code == 200, first_done.text
    _drive_issued_question_to_processing_attempt(
        api_clients, ack_prefix="ack-second", first_device_event_seq=4)
    revision = _fail_current_attempt(api_clients, monkeypatch)

    skipped = _adjudicate(api_clients, key="adjudicate-skip-last-failure-0001",
                          expected_revision=revision, kind="skip_item", reason_code="other",
                          note="识别服务报错")
    assert skipped.status_code == 200, skipped.text
    assert (skipped.json()["status"], skipped.json()["last_error_code"]) == (
        "paused", "autopilot_scope_completed")
    finished = api_clients.account.post(f"/sessions/{SESSION_ID}/finish-intervention")
    assert finished.status_code == 200, finished.text
    with Session(api_clients.engine) as db:
        assert db.get(SessionRuntimeState, SESSION_ID).status == "intervention_completed"
        summary = db.get(SessionOutcomeSummary, SESSION_ID)
        assert (summary.expected_turns, summary.completed_attempt_turns,
                summary.total_attempts, summary.technical_failure_attempts) == (1, 1, 2, 1)


def test_technical_failure_after_a_cue_is_skipped_without_reusing_the_earlier_answer(
        api_clients, monkeypatch):
    """第二段(提示后)识别失败:第一段虽已判完,也不拿它替这一环节收口。"""
    _enable_p0a(monkeypatch)
    _bind_api_bank(api_clients, monkeypatch, TWO_ONLY_BANK)
    _drive_to_processing_attempt(api_clients)
    monkeypatch.setattr(asr, "get_engine", lambda: _EmptyTranscriptAsr())
    _run_p0a_attempt_worker(SESSION_ID)
    _drive_followup_processing_attempt_via_current_cue(
        api_clients, suffix="cue-then-failure-0001",
        tts_device_event_seq=3, record_device_event_seq=4)
    revision = _fail_current_attempt(api_clients, monkeypatch)
    before = _snapshot(api_clients.engine)
    assert [(a["attempt_seq"], a["prompt_level"], a["processing_status"])
            for a in before["attempts"]] == [(1, 0, "completed"), (2, 1, "technical_failure")]
    _assert_ai_exits_refused(api_clients, revision, suffix="cue-0001")
    assert _snapshot(api_clients.engine) == before

    skipped = _adjudicate(api_clients, key="adjudicate-skip-after-cue-failure-0001",
                          expected_revision=revision, kind="skip_item",
                          reason_code="asr_repeatedly_failed")
    assert skipped.status_code == 200, skipped.text
    assert skipped.json()["position_item_id"] == SECOND["item_id"]
    with Session(api_clients.engine) as db:
        assert [(r.kind, r.source_attempt_id, r.turn_event_id) for r in db.exec(
            select(AutopilotPositionAdjudication))] == [("skipped", None, None)]
        assert list(db.exec(select(TurnEvent))) == []
        assert [a.model_dump() for a in db.exec(
            select(AttemptEvent).order_by(AttemptEvent.id))] == before["attempts"]


def test_technical_failure_skip_during_provider_outage_is_saved_and_resume_moves_on(
        api_clients, monkeypatch):
    from app import provider_readiness
    from test_autopilot_adjudication_outage import _unavailable
    _enable_p0a(monkeypatch)
    _bind_api_bank(api_clients, monkeypatch, TWO_ONLY_BANK)
    _drive_to_processing_attempt(api_clients)
    revision = _fail_current_attempt(api_clients, monkeypatch)
    real_readiness = provider_readiness.require_resume_ready
    monkeypatch.setattr(provider_readiness, "require_resume_ready", _unavailable)
    saved = _adjudicate(api_clients, key="adjudicate-skip-outage-0001",
                        expected_revision=revision, kind="skip_item",
                        reason_code="asr_repeatedly_failed")
    assert saved.status_code == 200, saved.text
    assert (saved.json()["status"], saved.json()["last_error_code"]) == (
        "paused", "autopilot_adjudication_saved")
    monkeypatch.setattr(provider_readiness, "require_resume_ready", real_readiness)
    resumed = _resume(api_clients, key="resume-after-saved-skip-0001",
                      expected_revision=saved.json()["state_revision"])
    assert resumed.status_code == 200, resumed.text
    assert (resumed.json()["status"], resumed.json()["position_item_id"]) == (
        "waiting_tts", SECOND["item_id"])
