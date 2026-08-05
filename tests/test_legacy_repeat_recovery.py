"""Legacy ``legacy_pre_repeat`` recovery: finish one old recording, then stop.

Every fixture here is a genuine pre-protocol chain: it is inserted into a
database at revision ``c7d4f9a1e603`` and then upgraded, so the
``legacy_pre_repeat`` marker can only ever come from the ``d3f8b5c1a704``
migration itself.  Nothing writes that marker by hand at head, and no test
weakens a production gate to make a scenario reachable.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import json
import threading

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event
from sqlmodel import Session, select

from app import (asr, audio_store, auth, autopilot_orchestration,
                 autopilot_service, cloud_processing, db, device_capability,
                 evidence_ledger, llm_judge)
from app import main as main_module
from app.main import _run_legacy_repeat_recovery_worker
from app.models import (
    AttemptCaptureProcessing,
    AttemptEvent,
    AudioAssetRow,
    AutopilotControlEvent,
    AutopilotRepeatRequest,
    InteractionEvent,
    LiveState,
    PatientDeviceCapability,
    ResearchUser,
    RuntimeCommand,
    RuntimeCommandAck,
    Session as TrainSession,
    SessionAutopilotState,
    SessionRuntimeState,
    VisitPlan,
)

from test_repeat_intent_protocol import (
    APPROVED_DIGEST,
    APPROVED_VERSION_ID,
    HEAD,
    PARENT,
    LegacyChain,
    _config,
    _insert_c7_populated_capture_chain,
)


TARGET_WORD = "胡萝卜"


class _CountingAsr:
    """A local ASR provider that records how many times it really ran."""

    version = "legacy-recovery-asr-v1"
    data_boundary = "local"
    provider_id = None

    def __init__(self, text: str = TARGET_WORD, *, before=None):
        self.text = text
        self.calls = 0
        self._before = before

    def transcribe(self, _audio_bytes, _hotwords):
        self.calls += 1
        if self._before is not None:
            self._before()
        return asr.AsrResult(self.text, 0.91, self.version)


class _CountingJudge:
    """A local judge provider that records how many times it really ran.

    Returning ``None`` is the provider contract's "I decline"; production then
    falls through to the closed deterministic rules, which is the branch this
    frozen simulation item actually uses.
    """

    version = "legacy-recovery-judge-v1"
    data_boundary = "local"
    provider_id = None

    def __init__(self, result=None, *, before=None):
        self.calls = 0
        self.result = result
        self._before = before

    def judge(self, _judge_input):
        self.calls += 1
        if self._before is not None:
            self._before()
        return self.result


@dataclass
class LegacyEnv:
    engine: object
    chain: LegacyChain
    asr_engine: _CountingAsr
    judge_engine: _CountingJudge
    db_path: object


def _install_engine(monkeypatch, db_path):
    engine = create_engine(
        f"sqlite:///{db_path}", connect_args={"check_same_thread": False})

    @event.listens_for(engine, "connect")
    def _foreign_keys(dbapi_connection, _record):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    monkeypatch.setattr(db, "engine", engine)
    return engine


@pytest.fixture
def legacy_env(monkeypatch, tmp_path) -> LegacyEnv:
    monkeypatch.setenv("ENABLE_AUTOPILOT_P0A_SIMULATION", "1")
    monkeypatch.setenv("ALLOW_SIMULATION_DATA", "1")
    # The production authentication middleware must really run: without
    # REQUIRE_AUTH the loopback M0 bypass skips capability parsing entirely and
    # every device request would fail as device_capability_required, which
    # would make the route-level acceptance below meaningless.
    monkeypatch.setenv("REQUIRE_AUTH", "1")
    monkeypatch.setenv("CONSOLE_PIN", "246810")

    db_path = tmp_path / "legacy-pre-repeat.sqlite"
    config = _config(db_path)
    from alembic import command as alembic_command

    alembic_command.upgrade(config, PARENT)
    chain = _insert_c7_populated_capture_chain(db_path)
    alembic_command.upgrade(config, HEAD)

    engine = _install_engine(monkeypatch, db_path)
    # REQUIRE_AUTH refuses to serve without a named researcher account to audit
    # against. This row is not part of the legacy chain and carries no repeat
    # semantics, so it is created at head rather than in the c7 fixture.
    with Session(engine) as session:
        session.add(ResearchUser(
            username="legacy-researcher",
            display_id="ACTOR-LEGACY",
            password_hash=auth.hash_password("password1"),
            role="researcher",
            created_at=datetime(2026, 7, 30, 12, 0, 0),
        ))
        session.commit()
    audio_store.save_blob(chain.raw_audio_id, chain.audio_bytes, "audio/webm")

    asr_engine = _CountingAsr()
    judge_engine = _CountingJudge()
    monkeypatch.setattr(asr, "get_engine", lambda: asr_engine)
    monkeypatch.setattr(llm_judge, "get_engine", lambda: judge_engine)
    monkeypatch.setattr(
        cloud_processing, "provider_boundary",
        lambda _provider: cloud_processing.DataBoundary.LOCAL)
    return LegacyEnv(engine=engine, chain=chain, asr_engine=asr_engine,
                     judge_engine=judge_engine, db_path=db_path)


def _rows(env: LegacyEnv, model, **where):
    with Session(env.engine) as session:
        statement = select(model)
        for name, value in where.items():
            statement = statement.where(getattr(model, name) == value)
        return list(session.exec(statement))


def _state(env: LegacyEnv) -> SessionAutopilotState:
    with Session(env.engine) as session:
        return session.get(SessionAutopilotState, env.chain.session_id)


def _capture(env: LegacyEnv) -> AttemptCaptureProcessing:
    with Session(env.engine) as session:
        return session.get(AttemptCaptureProcessing, env.chain.capture_id)


def _counts(env: LegacyEnv) -> dict:
    """Every ledger a legacy recovery is allowed — or forbidden — to touch."""
    return {
        "attempts": len(_rows(env, AttemptEvent)),
        "interactions": len(_rows(env, InteractionEvent)),
        "repeat_requests": len(_rows(env, AutopilotRepeatRequest)),
        "commands": len(_rows(env, RuntimeCommand)),
        "control_events": len(_rows(env, AutopilotControlEvent)),
    }


def _pause_events(env: LegacyEnv) -> list[AutopilotControlEvent]:
    return [row for row in _rows(env, AutopilotControlEvent)
            if row.event_type == "pause"]


_AUTHORITY_MODELS = (
    PatientDeviceCapability,
    LiveState,
    SessionAutopilotState,
    SessionRuntimeState,
    RuntimeCommand,
    RuntimeCommandAck,
    AttemptCaptureProcessing,
    AttemptEvent,
    InteractionEvent,
    AutopilotControlEvent,
    AutopilotRepeatRequest,
)


def _all_model_columns_snapshot(session: Session, model) -> dict:
    """Every column of every row of one table, discovered from the model.

    The column list comes from ``__table__.columns`` at call time, so a column
    added later is covered automatically and no hand-maintained list can drift
    out of date and false-green. Rows are ordered by ``repr`` so ``None``,
    datetimes and mixed types sort without raising.
    """
    column_names = tuple(column.name for column in model.__table__.columns)
    rows = [tuple(getattr(row, name) for name in column_names)
            for row in session.exec(select(model))]
    return {"columns": column_names, "rows": sorted(rows, key=repr)}


def _authority_snapshot(env: LegacyEnv) -> dict:
    """Whole-row state of every authoritative table this path can touch.

    Counts or selected fields alone would let a mutated revision, lease,
    generation, cursor or capability slot pass as "no change".
    """
    with Session(env.engine) as session:
        return {model.__name__: _all_model_columns_snapshot(session, model)
                for model in _AUTHORITY_MODELS}


def test_legacy_marker_really_came_from_the_migration(legacy_env):
    """夹具前提：标记只能由 c7→d3 升级产生，且旧链没有任何 repeat 绑定。"""
    capture = _capture(legacy_env)
    assert capture.repeat_admission_semantics == "legacy_pre_repeat"
    assert capture.repeat_protocol_version_id is None
    assert capture.repeat_protocol_definition_digest is None
    assert capture.repeat_request_id is None
    assert capture.processing_status == "received"
    with Session(legacy_env.engine) as session:
        train_session = session.get(TrainSession, legacy_env.chain.session_id)
        plan = session.get(VisitPlan, legacy_env.chain.plan_id)
        assert train_session.repeat_protocol_version_id is None
        assert plan.repeat_protocol_version_id is None
        assert plan.status == "started"
        for command in session.exec(select(RuntimeCommand)):
            assert command.repeat_protocol_version_id is None
            assert command.replay_source_command_id is None


def test_legacy_verifier_accepts_only_the_needs_asr_stage_first(legacy_env):
    with Session(legacy_env.engine) as session:
        resolved = autopilot_orchestration.verify_legacy_pre_repeat_recovery(
            session, session_id=legacy_env.chain.session_id)
    assert resolved.stage == autopilot_orchestration.LEGACY_STAGE_ASR
    assert resolved.attempt_id is None
    assert resolved.target.capture_id == legacy_env.chain.capture_id
    assert resolved.target.patient_id == legacy_env.chain.patient_id
    assert resolved.target.audio_checksum == legacy_env.chain.checksum
    assert resolved.target.attempt_input.item_id == legacy_env.chain.item_id
    assert resolved.target.attempt_input.prompt_level == 0
    assert resolved.target.attempt_input.cue_type is None


def test_explicit_repeat_phrase_is_recovered_as_one_ordinary_attempt(legacy_env):
    """旧协议场次里说"再说一遍"仍然只是普通回答，绝不进重播路径。"""
    legacy_env.asr_engine.text = "再说一遍"

    _run_legacy_repeat_recovery_worker(legacy_env.chain.session_id)

    assert legacy_env.asr_engine.calls == 1
    attempts = _rows(legacy_env, AttemptEvent)
    assert len(attempts) == 1
    attempt = attempts[0]
    assert attempt.processing_status == "completed"
    assert attempt.asr_text == "再说一遍"
    assert attempt.attempt_seq == 1
    assert attempt.prompt_level == 0
    assert attempt.raw_audio_id == legacy_env.chain.raw_audio_id
    assert _rows(legacy_env, AutopilotRepeatRequest) == []
    # No command of any kind was issued: no replay, no cue, no new recording.
    assert len(_rows(legacy_env, RuntimeCommand)) == 2

    capture = _capture(legacy_env)
    assert capture.processing_status == "asr_completed"
    assert capture.disposition == "answer_candidate"
    assert capture.final_attempt_id == attempt.id
    assert capture.repeat_request_id is None
    assert capture.repeat_admission_semantics == "legacy_pre_repeat"

    state = _state(legacy_env)
    assert state.status == "paused"
    assert state.current_command_id is None
    assert state.lease_owner is None
    assert state.last_error_code == "legacy_repeat_recovery_complete"
    pauses = _pause_events(legacy_env)
    assert len(pauses) == 1
    assert pauses[0].reason_code == "legacy_repeat_recovery_complete"
    assert pauses[0].actor_type == "system"
    assert pauses[0].actor_id is None
    assert pauses[0].from_status == "processing_attempt"
    assert pauses[0].to_status == "paused"
    with Session(legacy_env.engine) as session:
        runtime_state = session.get(
            SessionRuntimeState, legacy_env.chain.session_id)
        assert runtime_state.status == "paused"


def test_rerunning_the_worker_after_completion_only_observes(legacy_env):
    _run_legacy_repeat_recovery_worker(legacy_env.chain.session_id)
    before = _counts(legacy_env)
    asr_calls = legacy_env.asr_engine.calls

    _run_legacy_repeat_recovery_worker(legacy_env.chain.session_id)
    _run_legacy_repeat_recovery_worker(legacy_env.chain.session_id)

    assert legacy_env.asr_engine.calls == asr_calls
    assert _counts(legacy_env) == before
    assert len(_pause_events(legacy_env)) == 1


def test_a_completed_legacy_scope_never_resumes_the_old_protocol(legacy_env):
    """完成后必须停住：旧协议场次不能继续拿到任何命令。"""
    _run_legacy_repeat_recovery_worker(legacy_env.chain.session_id)

    with Session(legacy_env.engine) as session:
        with pytest.raises(autopilot_service.AutopilotServiceError) as excinfo:
            autopilot_service.get_next_command(
                session,
                session_id=legacy_env.chain.session_id,
                capability_token_hash=legacy_env.chain.capability_token_hash,
            )
    assert excinfo.value.code == "autopilot_repeat_binding_missing"
    assert len(_rows(legacy_env, RuntimeCommand)) == 2


def _raw(env: LegacyEnv, sql: str, params: tuple = ()):
    """Direct SQL, used only to simulate corruption a governance API cannot."""
    import sqlite3

    connection = sqlite3.connect(env.db_path)
    try:
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute(sql, params)
        connection.commit()
    finally:
        connection.close()


def _run_to_asr_checkpoint(env: LegacyEnv) -> None:
    """Drive exactly the first durable checkpoint, then stop before judgement."""
    original = main_module._legacy_recovery_judgement

    def _stop(*_args, **_kwargs):
        return None

    main_module._legacy_recovery_judgement = _stop
    try:
        _run_legacy_repeat_recovery_worker(env.chain.session_id)
    finally:
        main_module._legacy_recovery_judgement = original


# --------------------------------------------------------------------------
# A. durable stage machine: every crash point resumes, never re-runs a provider
# --------------------------------------------------------------------------


def test_crash_after_asr_checkpoint_resumes_at_judgement_without_new_asr(
        legacy_env):
    _run_to_asr_checkpoint(legacy_env)

    capture = _capture(legacy_env)
    assert capture.processing_status == "asr_completed"
    assert capture.disposition == "answer_candidate"
    attempts = _rows(legacy_env, AttemptEvent)
    assert len(attempts) == 1
    assert attempts[0].processing_status == "asr_completed"
    assert legacy_env.asr_engine.calls == 1
    assert _pause_events(legacy_env) == []

    with Session(legacy_env.engine) as session:
        resolved = autopilot_orchestration.verify_legacy_pre_repeat_recovery(
            session, session_id=legacy_env.chain.session_id)
    assert resolved.stage == autopilot_orchestration.LEGACY_STAGE_JUDGEMENT
    assert resolved.attempt_id == attempts[0].id

    # A fresh worker takes over only after the attempt lease really expires.
    _expire_attempt_lease(legacy_env, attempts[0].id)
    _run_legacy_repeat_recovery_worker(legacy_env.chain.session_id)

    assert legacy_env.asr_engine.calls == 1        # never transcribed twice
    attempts = _rows(legacy_env, AttemptEvent)
    assert len(attempts) == 1
    assert attempts[0].processing_status == "completed"
    assert len(_pause_events(legacy_env)) == 1
    assert _rows(legacy_env, AutopilotRepeatRequest) == []
    assert len(_rows(legacy_env, RuntimeCommand)) == 2


def _expire_attempt_lease(env: LegacyEnv, attempt_id: int) -> None:
    _raw(env,
         "UPDATE attemptevent SET processing_lease_expires_at = ? WHERE id = ?",
         ("2020-01-01 00:00:00", attempt_id))


def _expire_capture_lease(env: LegacyEnv) -> None:
    _raw(env,
         "UPDATE attemptcaptureprocessing "
         "SET processing_lease_expires_at = ? WHERE id = ?",
         ("2020-01-01 00:00:00", env.chain.capture_id))


def test_crash_after_judgement_only_adds_the_one_missing_pause(legacy_env):
    """判分已持久、pause 未提交时重入：provider=0，只补一次 pause。"""
    original = main_module._commit_legacy_recovery_pause
    calls = {"n": 0}

    def _fault(session_id: str) -> bool:
        # Call 1 is the worker's provider-free entry probe (nothing owed yet);
        # call 2 is the real terminal pause, and that is the one we crash.
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("simulated crash before the pause commits")
        return original(session_id)

    main_module._commit_legacy_recovery_pause = _fault
    try:
        _run_legacy_repeat_recovery_worker(legacy_env.chain.session_id)
    finally:
        main_module._commit_legacy_recovery_pause = original

    attempts = _rows(legacy_env, AttemptEvent)
    assert len(attempts) == 1
    assert attempts[0].processing_status == "completed"
    assert _pause_events(legacy_env) == []
    assert _state(legacy_env).status == "processing_attempt"
    asr_calls, judge_calls = (legacy_env.asr_engine.calls,
                              legacy_env.judge_engine.calls)

    _run_legacy_repeat_recovery_worker(legacy_env.chain.session_id)

    assert legacy_env.asr_engine.calls == asr_calls
    assert legacy_env.judge_engine.calls == judge_calls
    assert len(_rows(legacy_env, AttemptEvent)) == 1
    assert len(_pause_events(legacy_env)) == 1
    assert _state(legacy_env).last_error_code == "legacy_repeat_recovery_complete"


def test_pause_projection_fault_rolls_back_the_whole_transaction(legacy_env):
    """暂停投影失败必须整事务回滚：不留半暂停，也不留第二条事件。"""
    original = main_module._pause_runtime_in_transaction

    def _fault(session_id, s):
        raise RuntimeError("simulated LiveState projection fault")

    main_module._pause_runtime_in_transaction = _fault
    try:
        _run_legacy_repeat_recovery_worker(legacy_env.chain.session_id)
    finally:
        main_module._pause_runtime_in_transaction = original

    assert _pause_events(legacy_env) == []
    state = _state(legacy_env)
    assert state.status == "processing_attempt"
    assert state.current_command_id == legacy_env.chain.record_command_id
    assert state.last_error_code is None
    with Session(legacy_env.engine) as session:
        runtime_state = session.get(
            SessionRuntimeState, legacy_env.chain.session_id)
        assert runtime_state.status == "active"

    _run_legacy_repeat_recovery_worker(legacy_env.chain.session_id)

    assert len(_pause_events(legacy_env)) == 1
    assert _state(legacy_env).status == "paused"
    assert len(_rows(legacy_env, AttemptEvent)) == 1


# --------------------------------------------------------------------------
# provider contract and blocked-provider drift
# --------------------------------------------------------------------------


@pytest.mark.parametrize("result", [
    pytest.param(asr.AsrResult(TARGET_WORD, 0.9, ""), id="blank-engine"),
    pytest.param(asr.AsrResult(TARGET_WORD, 0.9, None), id="null-engine"),
    pytest.param(asr.AsrResult(TARGET_WORD, "0.9", "v1"), id="string-confidence"),
    pytest.param(asr.AsrResult(TARGET_WORD, True, "v1"), id="bool-confidence"),
    pytest.param(asr.AsrResult(TARGET_WORD, float("nan"), "v1"),
                 id="nonfinite-confidence"),
    pytest.param(asr.AsrResult(TARGET_WORD, 1.5, "v1"), id="out-of-range-confidence"),
    pytest.param(asr.AsrResult(TARGET_WORD, 0.9, "v1", "yes"), id="non-bool-hotword"),
    pytest.param(asr.AsrResult(None, 0.9, "v1"), id="null-text"),
])
def test_malformed_asr_result_leaves_no_durable_change(legacy_env, result):
    class _Bad(_CountingAsr):
        def transcribe(self, _audio_bytes, _hotwords):
            self.calls += 1
            return result

    bad = _Bad()
    import app.asr as asr_module
    original = asr_module.get_engine
    asr_module.get_engine = lambda: bad
    try:
        _run_legacy_repeat_recovery_worker(legacy_env.chain.session_id)
    finally:
        asr_module.get_engine = original

    assert bad.calls == 1
    assert legacy_env.judge_engine.calls == 0
    assert _rows(legacy_env, AttemptEvent) == []
    assert _rows(legacy_env, InteractionEvent) == []
    assert _capture(legacy_env).processing_status == "received"
    assert _state(legacy_env).status == "processing_attempt"


def test_a_valid_retry_after_a_malformed_result_still_succeeds(legacy_env):
    class _Bad(_CountingAsr):
        def transcribe(self, _audio_bytes, _hotwords):
            self.calls += 1
            return asr.AsrResult(TARGET_WORD, 2.0, "v1")

    bad = _Bad()
    import app.asr as asr_module
    original = asr_module.get_engine
    asr_module.get_engine = lambda: bad
    try:
        _run_legacy_repeat_recovery_worker(legacy_env.chain.session_id)
    finally:
        asr_module.get_engine = original
    assert _rows(legacy_env, AttemptEvent) == []

    # The abandoned claim keeps its lease; a retry legitimately waits for it.
    _expire_capture_lease(legacy_env)
    _run_legacy_repeat_recovery_worker(legacy_env.chain.session_id)

    assert len(_rows(legacy_env, AttemptEvent)) == 1
    assert len(_pause_events(legacy_env)) == 1


# --------------------------------------------------------------------------
# the provider-free terminal closure: exact, and independent of today's content
# --------------------------------------------------------------------------


def _drain_target_accepts(env: LegacyEnv) -> bool:
    with Session(env.engine) as session:
        state = session.get(SessionAutopilotState, env.chain.session_id)
        command = session.get(RuntimeCommand, env.chain.record_command_id)
        event = session.exec(select(AutopilotControlEvent).where(
            AutopilotControlEvent.session_id == env.chain.session_id,
            AutopilotControlEvent.event_type == "pause",
        )).first()
        if event is None:
            return False
        return autopilot_service._legacy_recovery_pause_matches(
            session, event, state=state, command=command)


def _terminal_paths_accept(env: LegacyEnv) -> dict:
    """The three provider-free terminal readers, evaluated on current data."""
    with Session(env.engine) as session:
        owed = autopilot_orchestration.legacy_terminal_pause_owed(
            session, session_id=env.chain.session_id)
    return {"pause_owed": owed, "drain": _drain_target_accepts(env)}


def test_terminal_pause_survives_a_content_file_change_after_judgement(
        legacy_env, monkeypatch):
    """判分已完成后即使题库/协议解析变了，仍必须能补上 provider-free 暂停。"""
    original = main_module._commit_legacy_recovery_pause
    calls = {"n": 0}

    def _fault(session_id: str) -> bool:
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("simulated crash before the pause commits")
        return original(session_id)

    main_module._commit_legacy_recovery_pause = _fault
    try:
        _run_legacy_repeat_recovery_worker(legacy_env.chain.session_id)
    finally:
        main_module._commit_legacy_recovery_pause = original
    assert _pause_events(legacy_env) == []

    # Today's content resolution now fails outright; the terminal guarantee
    # must not depend on it.
    def _gone(_path):
        raise RuntimeError("item bank file replaced after the judgement landed")

    monkeypatch.setattr(main_module.content, "load_item_bank", _gone)
    monkeypatch.setattr(
        autopilot_orchestration.content, "load_item_bank", _gone)
    monkeypatch.setattr(
        autopilot_service.content, "load_item_bank", _gone)

    assert _terminal_paths_accept(legacy_env)["pause_owed"] is True
    asr_calls, judge_calls = (legacy_env.asr_engine.calls,
                              legacy_env.judge_engine.calls)

    assert main_module._commit_legacy_recovery_pause(
        legacy_env.chain.session_id) is True

    assert legacy_env.asr_engine.calls == asr_calls
    assert legacy_env.judge_engine.calls == judge_calls
    assert len(_pause_events(legacy_env)) == 1
    assert _state(legacy_env).last_error_code == "legacy_repeat_recovery_complete"


@pytest.mark.parametrize("column,value", [
    pytest.param("response_role", "复述", id="response-role"),
    pytest.param("cue_type", "语义", id="cue-type-appears-at-level-0"),
    pytest.param("duration_seconds", 9.0, id="duration"),
    pytest.param("item_id", "SE_OTHER", id="item"),
    pytest.param("prompt_level", 1, id="prompt-level"),
])
def test_attempt_fact_mutation_closes_every_terminal_path(
        legacy_env, column, value):
    """判分后篡改 Attempt 的任一权威事实：探针、暂停、drain 三处都必须拒绝。"""
    _run_legacy_repeat_recovery_worker(legacy_env.chain.session_id)
    assert _terminal_paths_accept(legacy_env)["drain"] is True
    attempt_id = _rows(legacy_env, AttemptEvent)[0].id

    _raw(legacy_env, f"UPDATE attemptevent SET {column} = ? WHERE id = ?",
         (value, attempt_id))

    assert _terminal_paths_accept(legacy_env) == {
        "pause_owed": False, "drain": False}


@pytest.mark.parametrize("prompt_level,wrong_cue", [
    pytest.param(1, "排除式", id="level1-labelled-as-level2"),
    pytest.param(2, "语义", id="level2-labelled-as-level1"),
])
def test_synchronized_downstream_rewrite_cannot_relabel_an_issued_turn(
        legacy_env, prompt_level, wrong_cue):
    """录音/采集/Attempt/交互一起改写，但已发出的前置 TTS 与回执不可变。

    这正是"换靶"最像合法的形状：下游每一行都自洽。前置 TTS、其载荷与回执仍停在
    原提示等级，所以三个终态读取者都必须拒绝——已发出的环节身份不可改写。
    """
    _run_legacy_repeat_recovery_worker(legacy_env.chain.session_id)
    attempt_id = _rows(legacy_env, AttemptEvent)[0].id
    interaction = [row for row in _rows(legacy_env, InteractionEvent)
                   if row.event_type == "attempt_received"][0]

    # Move the whole chain to a cued level, then mislabel only the cue type —
    # every other fact stays internally consistent.
    _raw(legacy_env, "UPDATE runtimecommand SET prompt_level = ? WHERE id = ?",
         (prompt_level, legacy_env.chain.record_command_id))
    _raw(legacy_env,
         "UPDATE attemptcaptureprocessing SET proof_prompt_level = ? WHERE id = ?",
         (prompt_level, legacy_env.chain.capture_id))
    _raw(legacy_env,
         "UPDATE attemptevent SET prompt_level = ?, cue_type = ? WHERE id = ?",
         (prompt_level, wrong_cue, attempt_id))
    payload = json.loads(interaction.payload_json)
    payload["prompt_level"] = prompt_level
    payload["cue_type"] = wrong_cue
    _raw(legacy_env, "UPDATE interactionevent SET payload_json = ? WHERE id = ?",
         (evidence_ledger.encode_event_payload("attempt_received", payload),
          interaction.id))

    assert _terminal_paths_accept(legacy_env) == {
        "pause_owed": False, "drain": False}


# --------------------------------------------------------------------------
# the judgement contract itself, closed against real production output
# --------------------------------------------------------------------------


def _rewrite_judgement(env: LegacyEnv, *, attempt_only: bool = False,
                       **columns) -> None:
    """Mutate the Attempt and its judgement payload together, consistently.

    A forger controls both rows, so an inconsistency between them is not what
    makes these cases interesting: the point is that a fully self-consistent
    rewrite must still be rejected by the frozen output contract.
    """
    attempt = _rows(env, AttemptEvent)[0]
    interaction = [row for row in _rows(env, InteractionEvent)
                   if row.event_type == "judgement_completed"][0]
    assignments = ", ".join(f"{name} = ?" for name in columns)
    _raw(env, f"UPDATE attemptevent SET {assignments} WHERE id = ?",
         (*columns.values(), attempt.id))
    payload_by_column = {
        "operational_answer_type": "answer_type",
        "operational_score": "score",
        "operational_needs_review": "needs_review",
        "judge_mode": "judge_mode",
        "judge_engine_version": "judge_engine_version",
        "matched_on": "matched_on",
        "contains_target": "contains_target",
    }
    if attempt_only:
        # The canonical encoder rightly refuses a non-finite number, so that
        # corruption can only ever exist on the Attempt column itself.
        return
    payload = json.loads(interaction.payload_json)
    for column, value in columns.items():
        key = payload_by_column.get(column)
        if key is not None:
            payload[key] = value
    _raw(env, "UPDATE interactionevent SET payload_json = ? WHERE id = ?",
         (evidence_ledger.encode_event_payload("judgement_completed", payload),
          interaction.id))


FORGED_JUDGEMENTS = [
    pytest.param(
        {"operational_answer_type": "伪造答案类型", "operational_score": 37.0,
         "operational_needs_review": False, "judge_mode": "规则确定式",
         "judge_engine_version": "rule-1", "matched_on": "invented-match",
         "contains_target": True, "judge_portrait_used": True},
        id="fabricated-type-score-and-portrait"),
    pytest.param({"operational_score": 37.0}, id="score-off-the-mapping"),
    pytest.param({"operational_score": float("nan")}, id="nan-score"),
    pytest.param({"contains_target": False}, id="rule-target-hit-without-target"),
    pytest.param({"judge_portrait_used": True}, id="portrait-used"),
    pytest.param({"matched_on": "invented-match"}, id="invented-rule-match"),
    pytest.param({"operational_needs_review": True},
                 id="rule-needs-review-contradicts-match"),
    pytest.param({"judge_mode": "人工"}, id="unknown-judge-mode"),
    pytest.param({"judge_engine_version": "rule-2"}, id="wrong-rule-engine"),
    pytest.param({"judge_mode": "LLM辅助"},
                 id="llm-mode-still-claiming-a-rule-match"),
    pytest.param({"operational_answer_type": "沉默", "operational_score": 0.0,
                  "judge_mode": "LLM辅助", "matched_on": None,
                  "judge_engine_version": "legacy-recovery-judge-v1"},
                 id="llm-mode-emitting-an-interaction-state"),
]


@pytest.mark.parametrize("columns", FORGED_JUDGEMENTS)
def test_a_forged_judgement_closes_every_terminal_path(legacy_env, columns):
    """判分结果被改成产线永远不会输出的形状：三个终态读取者都必须拒绝。"""
    _run_legacy_repeat_recovery_worker(legacy_env.chain.session_id)
    assert _terminal_paths_accept(legacy_env)["drain"] is True

    attempt_only = any(
        isinstance(value, float) and value != value for value in columns.values())
    _rewrite_judgement(legacy_env, attempt_only=attempt_only, **columns)

    assert _terminal_paths_accept(legacy_env) == {
        "pause_owed": False, "drain": False}


def test_the_real_rule_judgement_this_run_produced_is_accepted(legacy_env):
    """正例：本次运行真实产出的规则判分必须被接受，负例才有意义。"""
    _run_legacy_repeat_recovery_worker(legacy_env.chain.session_id)

    attempt = _rows(legacy_env, AttemptEvent)[0]
    assert attempt.judge_mode == "规则确定式"
    assert attempt.judge_engine_version == "rule-1"
    assert attempt.judge_reason is None
    assert attempt.judge_portrait_used is False
    key = (attempt.operational_answer_type, attempt.matched_on,
           attempt.operational_needs_review)
    assert key in autopilot_service.LEGACY_RULE_JUDGEMENTS
    expected_contains = autopilot_service.LEGACY_RULE_JUDGEMENTS[key]
    assert expected_contains is None or attempt.contains_target is expected_contains
    assert attempt.operational_score == (
        autopilot_service.LEGACY_JUDGEMENT_SCORE_BY_ANSWER_TYPE[
            attempt.operational_answer_type])
    assert _terminal_paths_accept(legacy_env) == {
        "pause_owed": False, "drain": True}


def test_a_legal_llm_assisted_judgement_is_accepted(legacy_env):
    """LLM 分支合法输出 matched_on=None，绝不能被当成"字段缺失"拒绝。"""
    from app.enums import AnswerType
    from app.llm_judge import LlmJudgement

    legacy_env.judge_engine.result = LlmJudgement(
        answer_type=AnswerType.正确, ai_score=1.0, ai_needs_review=False,
        reason="目标词完整出现")

    _run_legacy_repeat_recovery_worker(legacy_env.chain.session_id)

    attempt = _rows(legacy_env, AttemptEvent)[0]
    assert legacy_env.judge_engine.calls == 1
    assert attempt.judge_mode == "LLM辅助"
    assert attempt.matched_on is None
    assert attempt.judge_reason == "目标词完整出现"
    assert attempt.judge_engine_version == "legacy-recovery-judge-v1"
    assert attempt.processing_status == "completed"
    assert len(_pause_events(legacy_env)) == 1
    assert _terminal_paths_accept(legacy_env)["drain"] is True


# --------------------------------------------------------------------------
# D-AUTH: the terminal pause through the real authentication middleware
# --------------------------------------------------------------------------


def _client() -> TestClient:
    return TestClient(main_module.app)


def _next(client: TestClient, env: LegacyEnv, token: str | None = None,
          session_id: str | None = None):
    headers = ({} if token is None
               else {"X-Device-Capability": token or env.chain.device_token})
    return client.get(
        f"/sessions/{session_id or env.chain.session_id}/autopilot/next",
        headers=headers)


def _capability_row(env: LegacyEnv):
    with Session(env.engine) as session:
        return session.get(
            PatientDeviceCapability, env.chain.capability_token_hash)


def _db_snapshot(env: LegacyEnv) -> dict:
    """Everything an unauthorized request must be unable to move."""
    with Session(env.engine) as session:
        capability = session.get(
            PatientDeviceCapability, env.chain.capability_token_hash)
        state = session.get(SessionAutopilotState, env.chain.session_id)
        runtime_state = session.get(SessionRuntimeState, env.chain.session_id)
        return {
            **_counts(env),
            "capability": (capability.last_seen_at, capability.revoked_at,
                           capability.recovery_only_at,
                           capability.active_session_key, capability.expires_at),
            "state": (state.status, state.revision, state.current_command_id,
                      state.last_error_code),
            "runtime": (runtime_state.status, runtime_state.revision),
            "pause_reasons": sorted(
                (row.reason_code or "") for row in _pause_events(env)),
        }


def _judged_but_unpaused(env: LegacyEnv) -> None:
    """Drive to a durable judgement, then fault exactly the terminal pause."""
    original = main_module._commit_legacy_recovery_pause
    calls = {"n": 0}

    def _fault(session_id: str) -> bool:
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("simulated crash before the pause commits")
        return original(session_id)

    main_module._commit_legacy_recovery_pause = _fault
    try:
        _run_legacy_repeat_recovery_worker(env.chain.session_id)
    finally:
        main_module._commit_legacy_recovery_pause = original
    assert _pause_events(env) == []
    assert _rows(env, AttemptEvent)[0].processing_status == "completed"


def _last_evidence_moment(env: LegacyEnv) -> str:
    """Just after the newest ACK this chain produced, and still in the past.

    An expiry (or recovery-only demotion) stamped *before* the ACKs a
    capability already produced is not an expiry — it is a rewritten history,
    and ``verify_immutable_record_capture`` rightly refuses it. A faithful
    lapse happens after the device finished acknowledging and before now.
    """
    with Session(env.engine) as session:
        latest = max(row.received_at for row in session.exec(
            select(RuntimeCommandAck)))
    return (latest + timedelta(seconds=1)).isoformat(sep=" ")


def _expire_capability(env: LegacyEnv) -> None:
    _raw(env, "UPDATE patientdevicecapability SET expires_at = ? "
         "WHERE token_hash = ?",
         (_last_evidence_moment(env), env.chain.capability_token_hash))


def _make_last_seen_stale(env: LegacyEnv) -> None:
    _raw(env, "UPDATE patientdevicecapability SET last_seen_at = ? "
         "WHERE token_hash = ?",
         ("2020-01-01 00:00:00", env.chain.capability_token_hash))


def test_resolving_a_capability_never_writes_to_the_database(legacy_env):
    """认证解析必须是纯读：合法 token 打错场次也不能留下任何写入。"""
    _make_last_seen_stale(legacy_env)
    before = _db_snapshot(legacy_env)

    with _client() as client:
        response = _next(client, legacy_env, legacy_env.chain.device_token,
                         session_id="S-NOT-THIS-ONE")

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "device_session_mismatch"
    assert _db_snapshot(legacy_env) == before
    assert _capability_row(legacy_env).last_seen_at == before["capability"][0]


def test_an_active_bearer_schedules_the_legacy_worker(legacy_env):
    """活跃 token 的 /next 不返回任何命令，但会调度专用 worker。"""
    scheduled: list[str] = []
    import app.autopilot_orchestration as orchestration
    original = orchestration.submit
    orchestration.submit = lambda session_id, worker: (
        scheduled.append(worker.__name__) or True)
    try:
        with _client() as client:
            response = _next(client, legacy_env, legacy_env.chain.device_token)
    finally:
        orchestration.submit = original

    assert response.status_code == 200
    assert response.json() is None
    assert scheduled == ["_run_legacy_repeat_recovery_worker"]
    assert _rows(legacy_env, AttemptEvent) == []


def test_an_exact_expired_bearer_finishes_the_owed_terminal_pause(legacy_env):
    """判分已持久、能力已过期：仍必须补上唯一暂停，并保持原 401。"""
    _judged_but_unpaused(legacy_env)
    _expire_capability(legacy_env)
    asr_calls, judge_calls = (legacy_env.asr_engine.calls,
                              legacy_env.judge_engine.calls)

    with _client() as client:
        response = _next(client, legacy_env, legacy_env.chain.device_token)

    assert response.status_code == 401
    assert response.json()["code"] == "device_capability_expired"
    assert legacy_env.asr_engine.calls == asr_calls
    assert legacy_env.judge_engine.calls == judge_calls
    pauses = _pause_events(legacy_env)
    assert len(pauses) == 1
    assert pauses[0].reason_code == "legacy_repeat_recovery_complete"
    assert json.loads(pauses[0].payload_json)["source"] == "legacy_repeat_recovery"
    assert _state(legacy_env).status == "paused"
    assert len(_rows(legacy_env, RuntimeCommand)) == 2


def test_a_recovery_only_bearer_after_the_session_left_live(legacy_env):
    """RECOVERY_ONLY 通常意味着场次已离开 live；终态路径不得要求当前 live。"""
    _judged_but_unpaused(legacy_env)
    _raw(legacy_env,
         "UPDATE patientdevicecapability SET recovery_only_at = ?, "
         "active_session_key = NULL WHERE token_hash = ?",
         (_last_evidence_moment(legacy_env),
          legacy_env.chain.capability_token_hash))
    _raw(legacy_env, "UPDATE livestate SET session_json = ? WHERE id = 1",
         (json.dumps({"sessionId": "S-SOMEWHERE-ELSE", "weekNo": 2,
                      "eventLine": "正式训练", "mode": "task",
                      "itemBankVersionId": legacy_env.chain.item_bank_version_id},
                     ensure_ascii=False),))

    with _client() as client:
        response = _next(client, legacy_env, legacy_env.chain.device_token)

    assert response.status_code == 401
    assert response.json()["code"] == "device_capability_recovery_only"
    assert len(_pause_events(legacy_env)) == 1
    assert _state(legacy_env).last_error_code == "legacy_repeat_recovery_complete"


@pytest.mark.parametrize("mutation,expected_code", [
    pytest.param(
        "UPDATE patientdevicecapability SET revoked_at = '2026-07-30 00:00:00'",
        "device_capability_revoked", id="revoked"),
])
def test_a_revoked_bearer_can_never_trigger_the_terminal_pause(
        legacy_env, mutation, expected_code):
    _judged_but_unpaused(legacy_env)
    _raw(legacy_env, f"{mutation} WHERE token_hash = ?",
         (legacy_env.chain.capability_token_hash,))
    before = _db_snapshot(legacy_env)

    with _client() as client:
        response = _next(client, legacy_env, legacy_env.chain.device_token)

    assert response.status_code == 401
    assert response.json()["code"] == expected_code
    assert _db_snapshot(legacy_env) == before
    assert _pause_events(legacy_env) == []


@pytest.mark.parametrize("token", [
    pytest.param("not-a-real-capability-token-000000000000000", id="unknown"),
    pytest.param("!!bad!!", id="malformed"),
])
def test_an_invalid_bearer_writes_nothing_at_all(legacy_env, token):
    _judged_but_unpaused(legacy_env)
    before = _db_snapshot(legacy_env)

    with _client() as client:
        response = _next(client, legacy_env, token)

    assert response.status_code == 401
    assert response.json()["code"] == "device_capability_invalid"
    assert _db_snapshot(legacy_env) == before


def test_an_expired_bearer_addressing_another_session_writes_nothing(legacy_env):
    """过期 token 打别的场次：中间件先按过期 401，且绝不触发任何终态写入。"""
    _judged_but_unpaused(legacy_env)
    _expire_capability(legacy_env)
    before = _db_snapshot(legacy_env)

    with _client() as client:
        response = _next(client, legacy_env, legacy_env.chain.device_token,
                         session_id="S-NOT-THIS-ONE")

    assert response.status_code == 401
    assert response.json()["code"] == "device_capability_expired"
    assert _db_snapshot(legacy_env) == before
    assert _pause_events(legacy_env) == []


@pytest.mark.parametrize("mutation", [
    pytest.param("active_session_key = NULL, recovery_only_at = NULL",
                 id="orphan-slot"),
    pytest.param("recovery_only_at = '2026-07-30 00:00:00'",
                 id="demoted-yet-still-active"),
    pytest.param("active_session_key = 'S-SOMEWHERE-ELSE'",
                 id="foreign-active-slot"),
])
def test_a_contradictory_capability_slot_shape_writes_nothing(
        legacy_env, mutation):
    """枚举通过还不够：active/recovery 槽位形状不合法一律 no-op。"""
    _judged_but_unpaused(legacy_env)
    _expire_capability(legacy_env)
    _raw(legacy_env,
         f"UPDATE patientdevicecapability SET {mutation} WHERE token_hash = ?",
         (legacy_env.chain.capability_token_hash,))
    before = _db_snapshot(legacy_env)

    with _client() as client:
        response = _next(client, legacy_env, legacy_env.chain.device_token)

    assert response.status_code == 401
    assert response.json()["code"] == "device_capability_expired"
    assert _db_snapshot(legacy_env) == before
    assert _pause_events(legacy_env) == []


def test_an_incomplete_terminal_proof_keeps_the_error_and_writes_nothing(
        legacy_env):
    """判分尚未完成时，过期 token 不得借终态例外写入任何东西。"""
    _expire_capability(legacy_env)
    before = _db_snapshot(legacy_env)

    with _client() as client:
        response = _next(client, legacy_env, legacy_env.chain.device_token)

    assert response.status_code == 401
    assert response.json()["code"] == "device_capability_expired"
    assert _db_snapshot(legacy_env) == before
    assert _rows(legacy_env, AttemptEvent) == []


# --------------------------------------------------------------------------
# D-ASR/JUDGE: one closed contract on both sides of the commit
# --------------------------------------------------------------------------


def _rewrite_asr_facts(env: LegacyEnv, *, text=None, confidence=None) -> None:
    """Mutate Attempt, Capture and the asr_completed payload consistently.

    A forger controls every row, so an inconsistency between them is not what
    makes this interesting: the point is that a fully synchronised rewrite must
    still be refused by the same contract the prewrite gate applies.
    """
    attempt = _rows(env, AttemptEvent)[0]
    interaction = [row for row in _rows(env, InteractionEvent)
                   if row.event_type == "asr_completed"][0]
    if text is not None:
        _raw(env, "UPDATE attemptevent SET asr_text = ? WHERE id = ?",
             (text, attempt.id))
    if confidence is not None:
        _raw(env, "UPDATE attemptevent SET asr_confidence = ? WHERE id = ?",
             (confidence, attempt.id))
        _raw(env, "UPDATE attemptcaptureprocessing SET asr_confidence = ? "
             "WHERE id = ?", (confidence, env.chain.capture_id))
        payload = json.loads(interaction.payload_json)
        payload["asr_confidence"] = confidence
        _raw(env, "UPDATE interactionevent SET payload_json = ? WHERE id = ?",
             (evidence_ledger.encode_event_payload("asr_completed", payload),
              interaction.id))


FORGED_ASR_FACTS = [
    pytest.param({"text": "话" * 3000}, id="transcript-far-over-the-limit"),
    pytest.param({"confidence": 37.0}, id="confidence-far-out-of-range"),
]


@pytest.mark.parametrize("facts", FORGED_ASR_FACTS)
def test_the_asr_contract_rejects_the_same_shape_on_both_sides(facts):
    """先独立证明：这些形状在预写门禁处本来就非法。"""
    legal = {"text": TARGET_WORD, "confidence": 0.9,
             "engine_version": "v1", "hotword_hit": False}
    assert autopilot_service.legacy_asr_facts_are_legal(**legal) is True
    assert autopilot_service.legacy_asr_facts_are_legal(
        **{**legal, **facts}) is False
    assert main_module._legacy_asr_result_is_well_formed(
        asr.AsrResult(legal["text"], legal["confidence"],
                      legal["engine_version"])) is True
    assert main_module._legacy_asr_result_is_well_formed(
        asr.AsrResult(facts.get("text", legal["text"]),
                      facts.get("confidence", legal["confidence"]),
                      legal["engine_version"])) is False


@pytest.mark.parametrize("facts", FORGED_ASR_FACTS)
def test_forged_asr_facts_block_the_owed_terminal_pause(legacy_env, facts):
    """判分已持久但尚未暂停时同步伪造 ASR 事实：终态探针与提交都必须拒绝。"""
    _judged_but_unpaused(legacy_env)
    assert _pause_events(legacy_env) == []
    with Session(legacy_env.engine) as session:
        assert autopilot_orchestration.legacy_terminal_pause_owed(
            session, session_id=legacy_env.chain.session_id) is True

    _rewrite_asr_facts(legacy_env, **facts)

    with Session(legacy_env.engine) as session:
        assert autopilot_orchestration.legacy_terminal_pause_owed(
            session, session_id=legacy_env.chain.session_id) is False
    assert main_module._commit_legacy_recovery_pause(
        legacy_env.chain.session_id) is False
    assert _pause_events(legacy_env) == []
    assert _state(legacy_env).status == "processing_attempt"


@pytest.mark.parametrize("facts", FORGED_ASR_FACTS)
def test_forged_asr_facts_also_close_the_drain_matcher(legacy_env, facts):
    """已完成并暂停的场次同样不能靠伪造 ASR 事实继续拿到 drain target。"""
    _run_legacy_repeat_recovery_worker(legacy_env.chain.session_id)
    # The scope really is paused and the matcher really does accept it first,
    # so the refusal below cannot be an artefact of a not-yet-paused state.
    assert len(_pause_events(legacy_env)) == 1
    assert _terminal_paths_accept(legacy_env)["drain"] is True

    _rewrite_asr_facts(legacy_env, **facts)

    assert _terminal_paths_accept(legacy_env)["drain"] is False


def _legal_judgement_result() -> dict:
    return {
        "answer_type": "正确",
        "ai_score": 1.0,
        "needs_review": False,
        "judge_mode": "规则确定式",
        "judge_engine_version": "rule-1",
        "judge_reason": None,
        "matched_on": "target",
        "contains_target": True,
        "judge_portrait_used": False,
        "truth_scope": "operational_only",
    }


def test_the_judgement_result_shape_is_an_exact_key_set():
    """判分结果的键集必须精确闭合：少一个、多一个、口径错都不算合法。"""
    legal = _legal_judgement_result()
    assert autopilot_service.legacy_judgement_result_is_legal(legal) is True

    missing = {key: value for key, value in legal.items() if key != "matched_on"}
    assert autopilot_service.legacy_judgement_result_is_legal(missing) is False

    extra = {**legal, "confirmed_text": "胡萝卜"}
    assert autopilot_service.legacy_judgement_result_is_legal(extra) is False

    drifted = {**legal, "truth_scope": "research_truth"}
    assert autopilot_service.legacy_judgement_result_is_legal(drifted) is False


def test_a_whitespace_only_llm_reason_is_normalised_to_none(legacy_env):
    """LLM 只给空白理由时写入 NULL，而不是留下一个终态永远拒绝的形状。"""
    from app.enums import AnswerType
    from app.llm_judge import LlmJudgement

    legacy_env.judge_engine.result = LlmJudgement(
        answer_type=AnswerType.正确, ai_score=1.0, ai_needs_review=False,
        reason="   ")

    _run_legacy_repeat_recovery_worker(legacy_env.chain.session_id)

    attempts = _rows(legacy_env, AttemptEvent)
    assert len(attempts) == 1
    assert attempts[0].judge_mode == "LLM辅助"
    assert attempts[0].judge_reason is None
    assert attempts[0].processing_status == "completed"
    assert len(_pause_events(legacy_env)) == 1
    assert legacy_env.asr_engine.calls == 1
    assert legacy_env.judge_engine.calls == 1


MALFORMED_LLM_COMPLETIONS = [
    pytest.param({"ai_score": 37.0}, None, id="score-off-the-mapping"),
    pytest.param({"ai_score": True}, None, id="bool-score"),
    pytest.param({}, "   ", id="blank-judge-engine-version"),
    # A falsey non-string reason must survive normalisation intact so the
    # validator can refuse it; ``or None`` would have laundered it into a
    # perfectly legal missing reason.
    pytest.param({"reason": 0}, None, id="falsey-non-string-reason"),
]


@pytest.mark.parametrize("overrides,blank_engine", MALFORMED_LLM_COMPLETIONS)
def test_a_malformed_llm_completion_keeps_the_asr_checkpoint_retryable(
        legacy_env, overrides, blank_engine):
    """非法判分绝不落库：Attempt 停在 asr_completed，重试不再调用 ASR。"""
    from app.enums import AnswerType
    from app.llm_judge import LlmJudgement

    fields = {"answer_type": AnswerType.正确, "ai_score": 1.0,
              "ai_needs_review": False, "reason": "目标词完整出现"}
    fields.update(overrides)
    legacy_env.judge_engine.result = LlmJudgement(**fields)
    if blank_engine is not None:
        legacy_env.judge_engine.version = blank_engine

    _run_legacy_repeat_recovery_worker(legacy_env.chain.session_id)

    assert legacy_env.asr_engine.calls == 1
    assert legacy_env.judge_engine.calls == 1
    attempts = _rows(legacy_env, AttemptEvent)
    assert len(attempts) == 1
    assert attempts[0].processing_status == "asr_completed"
    assert attempts[0].operational_answer_type is None
    assert attempts[0].judge_mode is None
    assert _pause_events(legacy_env) == []
    assert {row.event_type for row in _rows(legacy_env, InteractionEvent)} == {
        "attempt_received", "asr_completed"}

    # A valid provider and an expired attempt lease let the retry finish, and
    # the durable ASR checkpoint means it never transcribes a second time.
    legacy_env.judge_engine.version = "legacy-recovery-judge-v1"
    legacy_env.judge_engine.result = None
    _expire_attempt_lease(legacy_env, attempts[0].id)

    _run_legacy_repeat_recovery_worker(legacy_env.chain.session_id)

    assert legacy_env.asr_engine.calls == 1
    attempts = _rows(legacy_env, AttemptEvent)
    assert len(attempts) == 1
    assert attempts[0].processing_status == "completed"
    assert len(_pause_events(legacy_env)) == 1


# --------------------------------------------------------------------------
# D-CRASH/LEASE: crash points, lease takeover and late-result fencing
# --------------------------------------------------------------------------


class _TwoCallGate:
    """Pause the first two calls of one production function, independently.

    Test-only. Each call gets its own ``entered``/``release`` Event so the test
    controls happens-before exactly, with no sleeps and no production timeout
    monkeypatching. The real callable still runs after release, and its return
    value or exception is recorded per call.
    """

    def __init__(self, module, name: str):
        self._module = module
        self._name = name
        self._original = getattr(module, name)
        self._lock = threading.Lock()
        self._next = 0
        self.entered = [threading.Event(), threading.Event()]
        self.release = [threading.Event(), threading.Event()]
        self.results: dict[int, object] = {}
        self.errors: dict[int, BaseException] = {}
        setattr(module, name, self._wrapped)

    def _wrapped(self, *args, **kwargs):
        with self._lock:
            index = self._next
            self._next += 1
        if index < len(self.entered):
            self.entered[index].set()
            assert self.release[index].wait(30), f"gate {index} never released"
        try:
            outcome = self._original(*args, **kwargs)
        except BaseException as exc:      # noqa: BLE001 - recorded, then re-raised
            self.errors[index] = exc
            raise
        self.results[index] = outcome
        return outcome

    @property
    def calls(self) -> int:
        with self._lock:
            return self._next

    def release_all(self) -> None:
        for gate in self.release:
            gate.set()

    def restore(self) -> None:
        setattr(self._module, self._name, self._original)


def _start_worker(workers: list, env: LegacyEnv) -> threading.Thread:
    """A daemon worker thread, registered for bounded cleanup."""
    thread = threading.Thread(
        target=_run_legacy_repeat_recovery_worker,
        args=(env.chain.session_id,), daemon=True)
    workers.append(thread)
    thread.start()
    return thread


def _drain_workers(gate: "_TwoCallGate", workers: list) -> None:
    """Bounded teardown that also holds on the failure path.

    Every join is timed, so a wedged production worker fails the run instead of
    hanging the whole regression. The gate wrapper is restored only *after* the
    threads have exited: a worker that has not yet reached the gate must still
    meet the wrapper, never slip past a half-removed one.
    """
    gate.release_all()
    still_alive = []
    for thread in workers:
        thread.join(timeout=30)
        if thread.is_alive():
            still_alive.append(thread.name)
    gate.restore()
    assert not still_alive, f"worker threads did not exit: {still_alive}"


def _capture_claim_state(env: LegacyEnv) -> tuple:
    with Session(env.engine) as session:
        row = session.get(AttemptCaptureProcessing, env.chain.capture_id)
        return (row.processing_owner, row.processing_generation,
                row.processing_status)


def _attempt_claim_state(env: LegacyEnv) -> tuple:
    with Session(env.engine) as session:
        row = session.exec(select(AttemptEvent)).first()
        return (row.processing_owner, row.processing_generation,
                row.processing_status)


def test_capture_claim_crash_before_asr_provider_waits_for_lease_then_recovers(
        legacy_env):
    """claim 已提交、provider 未调用就崩溃：活跃租约内不得重试，过期后接管。"""
    entered: list[str] = []
    original = main_module._legacy_recovery_asr

    def _crash(*_args, **_kwargs):
        entered.append("called")
        raise RuntimeError("simulated crash after the claim, before ASR")

    main_module._legacy_recovery_asr = _crash
    try:
        _run_legacy_repeat_recovery_worker(legacy_env.chain.session_id)

        assert len(entered) == 1
        assert legacy_env.asr_engine.calls == 0
        owner, generation, status = _capture_claim_state(legacy_env)
        assert owner is not None
        assert generation == 1            # advanced past the admitted 0
        assert status == "received"
        assert _rows(legacy_env, AttemptEvent) == []
        assert _rows(legacy_env, InteractionEvent) == []
        assert _pause_events(legacy_env) == []
        assert _rows(legacy_env, AutopilotRepeatRequest) == []
        assert len(_rows(legacy_env, RuntimeCommand)) == 2
        after_crash = _authority_snapshot(legacy_env)

        # Still inside the live lease: a second worker must not even reach the
        # provider wrapper, and must move nothing.
        _run_legacy_repeat_recovery_worker(legacy_env.chain.session_id)
        assert len(entered) == 1
        assert _authority_snapshot(legacy_env) == after_crash
    finally:
        main_module._legacy_recovery_asr = original

    _expire_capture_lease(legacy_env)

    # Read the claim *while the recoverer holds it*. Reading only after the run
    # would compare against a terminal row whose owner has been cleared, so
    # ``!= old_owner`` would be satisfied by ``None`` and would prove nothing.
    gate = _TwoCallGate(main_module, "_legacy_recovery_asr")
    workers: list[threading.Thread] = []
    try:
        recoverer = _start_worker(workers, legacy_env)
        assert gate.entered[0].wait(30), "recoverer never reached the provider"
        with Session(legacy_env.engine) as session:
            held = session.get(
                AttemptCaptureProcessing, legacy_env.chain.capture_id)
            held_owner = held.processing_owner
            held_generation = held.processing_generation
            held_status = held.processing_status
            held_claimed_at = held.processing_claimed_at
            held_lease = held.processing_lease_expires_at
        assert owner is not None
        assert held_owner is not None
        assert held_owner != owner
        assert held_generation == generation + 1
        assert held_claimed_at is not None
        assert held_lease is not None
        assert held_status == "received"
        assert legacy_env.asr_engine.calls == 0

        gate.release[0].set()
        recoverer.join(timeout=30)
        assert not recoverer.is_alive()
    finally:
        _drain_workers(gate, workers)

    owner_after, generation_after, status_after = _capture_claim_state(legacy_env)
    assert generation_after == held_generation
    assert owner_after is None            # the terminal update clears the lease
    assert status_after == "asr_completed"
    assert legacy_env.asr_engine.calls == 1
    attempts = _rows(legacy_env, AttemptEvent)
    assert len(attempts) == 1
    assert attempts[0].processing_status == "completed"
    assert sorted(row.event_type for row in _rows(legacy_env, InteractionEvent)) == [
        "asr_completed", "attempt_received", "judgement_completed"]
    assert len(_pause_events(legacy_env)) == 1
    assert len(_rows(legacy_env, RuntimeCommand)) == 2
    assert _rows(legacy_env, AutopilotRepeatRequest) == []


def test_attempt_claim_crash_before_judge_waits_for_lease_then_recovers(
        legacy_env):
    """判分 claim 已提交、judge 未调用就崩溃：同样只能等租约过期后接管。"""
    _run_to_asr_checkpoint(legacy_env)
    assert legacy_env.asr_engine.calls == 1
    assert legacy_env.judge_engine.calls == 0
    attempt_id = _rows(legacy_env, AttemptEvent)[0].id
    assert _pause_events(legacy_env) == []
    _expire_attempt_lease(legacy_env, attempt_id)

    entered: list[str] = []
    original = main_module._legacy_recovery_judgement

    def _crash(*_args, **_kwargs):
        entered.append("called")
        raise RuntimeError("simulated crash after the claim, before judging")

    main_module._legacy_recovery_judgement = _crash
    try:
        _run_legacy_repeat_recovery_worker(legacy_env.chain.session_id)

        assert len(entered) == 1
        assert legacy_env.judge_engine.calls == 0
        owner, generation, status = _attempt_claim_state(legacy_env)
        assert owner is not None
        assert status == "asr_completed"
        assert _pause_events(legacy_env) == []
        after_crash = _authority_snapshot(legacy_env)

        _run_legacy_repeat_recovery_worker(legacy_env.chain.session_id)
        assert len(entered) == 1
        assert _authority_snapshot(legacy_env) == after_crash
    finally:
        main_module._legacy_recovery_judgement = original

    _expire_attempt_lease(legacy_env, attempt_id)

    # Same reasoning as the capture case: prove the new owner while it is
    # actually held, not after the terminal update has cleared it.
    gate = _TwoCallGate(main_module, "_legacy_recovery_judgement")
    workers: list[threading.Thread] = []
    try:
        recoverer = _start_worker(workers, legacy_env)
        assert gate.entered[0].wait(30), "recoverer never reached the judge"
        with Session(legacy_env.engine) as session:
            held = session.get(AttemptEvent, attempt_id)
            held_owner = held.processing_owner
            held_generation = held.processing_generation
            held_status = held.processing_status
            held_claimed_at = held.processing_claimed_at
            held_lease = held.processing_lease_expires_at
        assert owner is not None
        assert held_owner is not None
        assert held_owner != owner
        assert held_generation > generation
        assert held_claimed_at is not None
        assert held_lease is not None
        assert held_status == "asr_completed"
        assert legacy_env.judge_engine.calls == 0

        gate.release[0].set()
        recoverer.join(timeout=30)
        assert not recoverer.is_alive()
    finally:
        _drain_workers(gate, workers)

    owner_after, generation_after, status_after = _attempt_claim_state(legacy_env)
    assert generation_after == held_generation
    assert owner_after is None            # the terminal update clears the lease
    assert status_after == "completed"
    assert legacy_env.asr_engine.calls == 1       # never transcribed again
    assert legacy_env.judge_engine.calls == 1
    attempts = _rows(legacy_env, AttemptEvent)
    assert len(attempts) == 1 and attempts[0].id == attempt_id
    judgements = [row for row in _rows(legacy_env, InteractionEvent)
                  if row.event_type == "judgement_completed"]
    assert len(judgements) == 1
    assert len(_rows(legacy_env, InteractionEvent)) == 3
    assert len(_pause_events(legacy_env)) == 1


def test_stale_asr_result_after_capture_takeover_is_pure_observation(legacy_env):
    """A 的 ASR 已返回但卡在提交前，租约被 B 接管：A 的迟到结果必须零写。"""
    gate = _TwoCallGate(main_module, "_commit_legacy_recovery_asr")
    workers: list[threading.Thread] = []
    try:
        worker_a = _start_worker(workers, legacy_env)
        assert gate.entered[0].wait(30), "A never reached the commit gate"
        assert legacy_env.asr_engine.calls == 1
        owner_a, generation_a, _status = _capture_claim_state(legacy_env)

        # A holds a claim it is about to lose; expiring the lease lets B take
        # the capture over legitimately, with a strictly higher generation.
        _expire_capture_lease(legacy_env)
        worker_b = _start_worker(workers, legacy_env)
        assert gate.entered[1].wait(30), "B never reached the commit gate"
        assert legacy_env.asr_engine.calls == 2
        owner_b, generation_b, status_b = _capture_claim_state(legacy_env)
        assert owner_a is not None
        assert owner_b is not None
        assert generation_b > generation_a
        assert owner_b != owner_a
        assert status_b == "received"

        # Both blocked before any durable judgement work: nothing persisted.
        both_blocked = _authority_snapshot(legacy_env)
        assert _rows(legacy_env, AttemptEvent) == []
        assert _rows(legacy_env, InteractionEvent) == []
        assert _pause_events(legacy_env) == []

        # Release only the stale owner: its real commit must refuse.
        gate.release[0].set()
        worker_a.join(timeout=30)
        assert not worker_a.is_alive()
        assert gate.results[0] is None, "stale commit must return None"
        assert _authority_snapshot(legacy_env) == both_blocked

        gate.release[1].set()
        worker_b.join(timeout=30)
        assert not worker_b.is_alive()
    finally:
        _drain_workers(gate, workers)

    attempts = _rows(legacy_env, AttemptEvent)
    assert len(attempts) == 1
    assert attempts[0].processing_status == "completed"
    assert len(_rows(legacy_env, InteractionEvent)) == 3
    assert len(_pause_events(legacy_env)) == 1
    assert len(_rows(legacy_env, RuntimeCommand)) == 2
    assert _rows(legacy_env, AutopilotRepeatRequest) == []
    # Two owners each legitimately called the provider inside their own lease;
    # only one set of durable facts may exist.
    assert legacy_env.asr_engine.calls == 2
    # Only the winner went on to judge, and only once.
    assert legacy_env.judge_engine.calls == 1


def test_stale_judgement_after_attempt_takeover_is_pure_observation(legacy_env):
    """判分已返回但卡在提交前，Attempt 租约被接管：迟到判分必须零写。"""
    _run_to_asr_checkpoint(legacy_env)
    assert legacy_env.asr_engine.calls == 1
    attempt_id = _rows(legacy_env, AttemptEvent)[0].id
    _expire_attempt_lease(legacy_env, attempt_id)

    gate = _TwoCallGate(main_module, "_commit_legacy_recovery_judgement")
    workers: list[threading.Thread] = []
    try:
        worker_a = _start_worker(workers, legacy_env)
        assert gate.entered[0].wait(30), "A never reached the commit gate"
        assert legacy_env.judge_engine.calls == 1
        owner_a, generation_a, _status = _attempt_claim_state(legacy_env)

        _expire_attempt_lease(legacy_env, attempt_id)
        worker_b = _start_worker(workers, legacy_env)
        assert gate.entered[1].wait(30), "B never reached the commit gate"
        assert legacy_env.judge_engine.calls == 2
        owner_b, generation_b, status_b = _attempt_claim_state(legacy_env)
        assert owner_a is not None
        assert owner_b is not None
        assert generation_b > generation_a
        assert owner_b != owner_a
        assert status_b == "asr_completed"

        both_blocked = _authority_snapshot(legacy_env)
        assert [row.event_type for row in _rows(legacy_env, InteractionEvent)
                if row.event_type == "judgement_completed"] == []
        assert _pause_events(legacy_env) == []

        gate.release[0].set()
        worker_a.join(timeout=30)
        assert not worker_a.is_alive()
        assert gate.results[0] is False, "stale judgement commit must be False"
        assert _authority_snapshot(legacy_env) == both_blocked

        gate.release[1].set()
        worker_b.join(timeout=30)
        assert not worker_b.is_alive()
    finally:
        _drain_workers(gate, workers)

    attempts = _rows(legacy_env, AttemptEvent)
    assert len(attempts) == 1 and attempts[0].id == attempt_id
    assert attempts[0].processing_status == "completed"
    judgements = [row for row in _rows(legacy_env, InteractionEvent)
                  if row.event_type == "judgement_completed"]
    assert len(judgements) == 1
    assert len(_rows(legacy_env, InteractionEvent)) == 3
    assert len(_pause_events(legacy_env)) == 1
    assert legacy_env.asr_engine.calls == 1
    assert legacy_env.judge_engine.calls == 2


def _account_client(env: LegacyEnv) -> TestClient:
    """A real signed-in researcher client, carrying its CSRF token."""
    client = TestClient(main_module.app)
    login = client.post("/auth/login", json={
        "username": "legacy-researcher", "password": "password1"})
    assert login.status_code == 200, login.text
    csrf = client.cookies.get(auth.CSRF_COOKIE_NAME)
    assert csrf
    client.headers["X-CSRF-Token"] = csrf
    return client


def test_a_drained_and_taken_over_legacy_scope_never_resumes_manual_work(
        legacy_env, monkeypatch):
    """旧协议场次收麦+接管后必须永久停住，不能回到旧人工流程。

    收麦只关掉患者屏幕，接管把控制面翻成 manual/paused —— 过去这就足以让
    /resume 与通用录音授权重新放行。本用例证明它们现在都被冻结绑定挡住，
    且每次拒绝后完整权威快照零变化。
    """
    # Run the real production admission and repeat gate, not the pytest escape
    # that conftest enables for historical directly-constructed fixtures.
    monkeypatch.delenv("NMU_TEST_ALLOW_DIRECT_SESSION_CREATE", raising=False)
    _run_legacy_repeat_recovery_worker(legacy_env.chain.session_id)
    assert len(_pause_events(legacy_env)) == 1
    assert _state(legacy_env).status == "paused"

    with _account_client(legacy_env) as account, _client() as device:
        # Before takeover the control plane is still autonomous, so the stable
        # pre-existing error code must be unchanged.
        early_resume = account.post(
            f"/sessions/{legacy_env.chain.session_id}/resume")
        assert early_resume.status_code == 409, early_resume.text
        assert early_resume.json()["detail"]["code"] == (
            "autopilot_manual_control_locked")

        before_drain = _authority_snapshot(legacy_env)
        target = device.get(
            f"/sessions/{legacy_env.chain.session_id}/autopilot/drain-target",
            headers={"X-Device-Capability": legacy_env.chain.device_token})
        assert target.status_code == 200, target.text
        with Session(legacy_env.engine) as session:
            record = session.get(
                RuntimeCommand, legacy_env.chain.record_command_id)
            state_revision = session.get(
                SessionAutopilotState,
                legacy_env.chain.session_id).revision
        assert target.json() == {
            "command_key": record.idempotency_key,
            "state_revision": state_revision,
        }
        assert target.headers["Cache-Control"] == "private, no-store"
        # A projection must not move anything at all.
        assert _authority_snapshot(legacy_env) == before_drain

        drain_url = (
            f"/sessions/{legacy_env.chain.session_id}/autopilot/commands/"
            f"{record.idempotency_key}/drain-ack")
        drained = device.post(
            drain_url,
            headers={"X-Device-Capability": legacy_env.chain.device_token})
        assert drained.status_code == 200, drained.text
        assert drained.json() == {"replayed": False,
                                  "state_revision": state_revision + 1}
        # Captured before the replay: a later legitimate takeover would
        # otherwise mask any extra write the replay smuggled in.
        after_first_drain = _authority_snapshot(legacy_env)

        replayed = device.post(
            drain_url,
            headers={"X-Device-Capability": legacy_env.chain.device_token})
        assert replayed.status_code == 200, replayed.text
        assert replayed.json() == {"replayed": True,
                                   "state_revision": state_revision + 1}
        assert _authority_snapshot(legacy_env) == after_first_drain
        assert len([row for row in _rows(legacy_env, AutopilotControlEvent)
                    if row.event_type == "drain_complete"]) == 1

        taken = account.post(
            f"/sessions/{legacy_env.chain.session_id}/autopilot/takeover",
            json={"idempotency_key": "takeover-legacy-0001",
                  "expected_revision": state_revision + 1})
        assert taken.status_code == 200, taken.text
        assert taken.json()["mode"] == "manual"
        assert taken.json()["status"] == "paused"
        assert taken.json()["server_owned"] is False

        # From here the control plane is manual/paused — the exact state that
        # used to re-open the old manual flow.
        after_takeover = _authority_snapshot(legacy_env)

        resumed = account.post(
            f"/sessions/{legacy_env.chain.session_id}/resume")
        assert resumed.status_code == 409, resumed.text
        assert resumed.json()["detail"]["code"] == "session_repeat_binding_missing"
        assert _authority_snapshot(legacy_env) == after_takeover

        authorized = device.post(
            f"/sessions/{legacy_env.chain.session_id}/recording-authorization",
            headers={"X-Device-Capability": legacy_env.chain.device_token})
        assert authorized.status_code == 409, authorized.text
        assert authorized.json()["detail"]["code"] == (
            "session_repeat_binding_missing")
        assert _authority_snapshot(legacy_env) == after_takeover

    with Session(legacy_env.engine) as session:
        runtime_state = session.get(
            SessionRuntimeState, legacy_env.chain.session_id)
        control = session.get(
            SessionAutopilotState, legacy_env.chain.session_id)
    assert runtime_state.status == "paused"
    assert control.mode == "manual"
    assert len(_rows(legacy_env, RuntimeCommand)) == 2
    assert _rows(legacy_env, AutopilotRepeatRequest) == []
    assert len(_rows(legacy_env, AttemptEvent)) == 1


def test_the_handler_revalidates_the_capability_exactly_once(legacy_env,
                                                             monkeypatch):
    """两次解析之间就是过期窗口：handler 锁内只允许一次 revalidate。"""
    calls: list[str] = []
    original = device_capability.revalidate_active_for_write

    def _spy(s, hashed_token, session_id, **kwargs):
        calls.append(session_id)
        return original(s, hashed_token, session_id, **kwargs)

    monkeypatch.setattr(
        device_capability, "revalidate_active_for_write", _spy)
    import app.main as main_for_spy
    monkeypatch.setattr(
        main_for_spy.device_capability, "revalidate_active_for_write", _spy)

    with _client() as client:
        response = _next(client, legacy_env, legacy_env.chain.device_token)

    assert response.status_code == 200
    # The middleware's own resolution is a pure read and does not count; the
    # handler must lock and revalidate exactly one time.
    assert calls == [legacy_env.chain.session_id]


def test_the_valid_path_keeps_its_visit_plan_and_current_live_admission(
        legacy_env):
    """删掉二次 capability 解析后，原有准入必须原样保留。"""
    checked: list[str] = []
    import app.main as main_module_local
    original_plan = main_module_local._require_started_visit_plan_session
    original_live = main_module_local._require_capability_current_live

    def _plan_spy(session_id, s):
        checked.append("visit_plan")
        return original_plan(session_id, s)

    def _live_spy(request, s, action, **kwargs):
        checked.append("current_live")
        return original_live(request, s, action, **kwargs)

    main_module_local._require_started_visit_plan_session = _plan_spy
    main_module_local._require_capability_current_live = _live_spy
    try:
        with _client() as client:
            response = _next(client, legacy_env, legacy_env.chain.device_token)
    finally:
        main_module_local._require_started_visit_plan_session = original_plan
        main_module_local._require_capability_current_live = original_live

    assert response.status_code == 200
    assert checked == ["visit_plan", "current_live"]


def test_a_live_state_switch_still_refuses_an_active_bearer(legacy_env):
    """current-live 准入仍然有效：场次已离开 live 时活跃 token 不得读取命令。"""
    _raw(legacy_env, "UPDATE livestate SET session_json = ? WHERE id = 1",
         (json.dumps({"sessionId": "S-SOMEWHERE-ELSE", "weekNo": 2,
                      "eventLine": "正式训练", "mode": "task",
                      "itemBankVersionId": legacy_env.chain.item_bank_version_id},
                     ensure_ascii=False),))
    before = _db_snapshot(legacy_env)

    with _client() as client:
        response = _next(client, legacy_env, legacy_env.chain.device_token)

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "device_session_changed"
    assert _db_snapshot(legacy_env) == before


def test_a_head_request_never_reaches_the_terminal_commit(legacy_env):
    """终态例外只对 GET 开放：HEAD 必须停在 GET-only matcher，绝不进提交路径。"""
    _judged_but_unpaused(legacy_env)
    _expire_capability(legacy_env)
    before = _authority_snapshot(legacy_env)
    asr_calls, judge_calls = (legacy_env.asr_engine.calls,
                              legacy_env.judge_engine.calls)
    committed: list[str] = []
    original = main_module._commit_http_legacy_terminal_pause
    main_module._commit_http_legacy_terminal_pause = (
        lambda **kwargs: committed.append("called") or original(**kwargs))
    try:
        with _client() as client:
            response = client.head(
                f"/sessions/{legacy_env.chain.session_id}/autopilot/next",
                headers={"X-Device-Capability": legacy_env.chain.device_token})
    finally:
        main_module._commit_http_legacy_terminal_pause = original

    assert response.status_code == 401
    assert response.headers["content-type"].startswith("application/json")
    # The commit helper is the only thing that can write; a HEAD must never
    # reach it, because autopilot_next_session_id refuses any non-GET verb.
    assert committed == []
    assert _authority_snapshot(legacy_env) == before
    assert _pause_events(legacy_env) == []
    assert legacy_env.asr_engine.calls == asr_calls
    assert legacy_env.judge_engine.calls == judge_calls


def test_the_same_expired_bearer_gets_the_exact_expired_code_on_get(legacy_env):
    """与上一条配对：同一 bearer 的 GET 才拿到 top-level 过期码并完成暂停。"""
    _judged_but_unpaused(legacy_env)
    _expire_capability(legacy_env)

    with _client() as client:
        response = _next(client, legacy_env, legacy_env.chain.device_token)

    assert response.status_code == 401
    assert response.json()["code"] == "device_capability_expired"
    assert len(_pause_events(legacy_env)) == 1


def _fenced(target_module, name: str, *, arrived, release, timeout=20.0):
    """Wrap a production callable so a test can pause it at an exact point."""
    original = getattr(target_module, name)

    def _wrapped(*args, **kwargs):
        arrived.set()
        assert release.wait(timeout), f"{name} fence never released"
        return original(*args, **kwargs)

    setattr(target_module, name, _wrapped)
    return original


def test_a_valid_preflight_demoted_to_recovery_only_still_closes_the_scope(
        legacy_env):
    """中间件按 VALID 放行后场次离开 live：锁内唯一 revalidate 必须看到降级。

    降级不影响已持久的判分，所以终态暂停仍要补上；但绝不能走 current-live、
    命令投影或 provider，且响应必须是降级后的 401。
    """
    _judged_but_unpaused(legacy_env)
    asr_calls, judge_calls = (legacy_env.asr_engine.calls,
                              legacy_env.judge_engine.calls)
    arrived, release = threading.Event(), threading.Event()
    projected: list[str] = []
    commanded: list[str] = []
    original_token = _fenced(
        main_module, "_require_device_capability_token_hash",
        arrived=arrived, release=release)
    original_live = main_module._require_capability_current_live
    main_module._require_capability_current_live = (
        lambda *a, **k: projected.append("current_live") or original_live(*a, **k))
    original_next = autopilot_service.get_next_command
    autopilot_service.get_next_command = (
        lambda *a, **k: commanded.append("get_next") or original_next(*a, **k))
    try:
        with ThreadPoolExecutor(max_workers=1) as pool, _client() as client:
            future = pool.submit(
                _next, client, legacy_env, legacy_env.chain.device_token)
            assert arrived.wait(20), "GET never reached the fence"
            # A real, separately-committed governance transaction in the
            # production lock order: LiveState first, then the capability rows.
            # The session genuinely leaves live here — demoting the capability
            # without moving LiveState would not be the situation under test.
            with main_module._LIVE_WRITE_LOCK, \
                    device_capability.serialized_mutation():
                with Session(legacy_env.engine) as governance:
                    live = main_module._live_row_for_update(governance)
                    live.session_json = json.dumps({
                        "sessionId": "S-SOMEWHERE-ELSE", "weekNo": 2,
                        "eventLine": "正式训练", "mode": "task",
                        "itemBankVersionId": legacy_env.chain.item_bank_version_id,
                    }, ensure_ascii=False)
                    governance.add(live)
                    demoted = device_capability.mark_session_recovery_only(
                        governance, legacy_env.chain.session_id)
                    governance.commit()
            assert demoted == 1
            with Session(legacy_env.engine) as check:
                switched = json.loads(check.get(LiveState, 1).session_json)
            assert switched["sessionId"] == "S-SOMEWHERE-ELSE"
            release.set()
            response = future.result(timeout=30)
    finally:
        release.set()
        main_module._require_device_capability_token_hash = original_token
        main_module._require_capability_current_live = original_live
        autopilot_service.get_next_command = original_next

    assert response.status_code == 401
    assert response.json()["detail"]["code"] == "device_capability_recovery_only"
    assert projected == []            # no current-live gate ...
    assert commanded == []            # ... and no command projection at all
    assert legacy_env.asr_engine.calls == asr_calls
    assert legacy_env.judge_engine.calls == judge_calls
    pauses = _pause_events(legacy_env)
    assert len(pauses) == 1
    assert pauses[0].reason_code == "legacy_repeat_recovery_complete"
    assert json.loads(pauses[0].payload_json)["source"] == "legacy_repeat_recovery"
    assert len(_rows(legacy_env, RuntimeCommand)) == 2


def _pair(client: TestClient, device_id: str = "legacy-repaired-device-0001"):
    return client.post("/device/pair", headers={"X-Console-Pin": "246810"},
                       json={"deviceId": device_id})


def _capability_rows(env: LegacyEnv) -> list:
    with Session(env.engine) as session:
        return [(row.token_hash, row.active_session_key, row.revoked_at)
                for row in session.exec(select(PatientDeviceCapability))]


class _ContendedLock:
    """The real ``_LIVE_WRITE_LOCK``, instrumented to observe contention.

    The underlying RLock is never replaced or bypassed: this only counts
    ``__enter__`` calls and signals when a second one is about to block on the
    genuine ``acquire()``, which is the only honest evidence that the other
    request really is waiting for the lock rather than merely unscheduled.
    """

    def __init__(self, inner, *, second_attempt):
        self._inner = inner
        self._second_attempt = second_attempt
        self._attempts = 0
        self._guard = threading.Lock()

    def __enter__(self):
        with self._guard:
            self._attempts += 1
            attempt = self._attempts
        if attempt == 2:
            self._second_attempt.set()
        self._inner.acquire()
        return self

    def __exit__(self, *exc_info):
        self._inner.release()
        return False


def _assert_pair_response_matches_the_new_capability(
        env: LegacyEnv, response, device_id: str):
    """The plaintext capability handed out must be the one active row."""
    body = response.json()
    assert set(body) == {"capability", "sessionId", "expiresAt"}
    issued_hash = device_capability.token_hash(body["capability"])
    with Session(env.engine) as session:
        rows = list(session.exec(select(PatientDeviceCapability)))
    assert len(rows) == 2, "exactly the superseded and the new capability"
    new = [row for row in rows if row.token_hash == issued_hash]
    assert len(new) == 1
    new = new[0]
    assert body["sessionId"] == new.session_id == env.chain.session_id
    expected_expires_at = new.expires_at.replace(
        tzinfo=timezone.utc).isoformat().replace("+00:00", "Z")
    assert body["expiresAt"] == expected_expires_at
    assert new.active_session_key == env.chain.session_id
    assert new.session_id == env.chain.session_id
    assert new.revoked_at is None
    assert new.device_id_hash == device_capability.device_id_hash(device_id)
    assert new.expires_at > new.created_at
    old = [row for row in rows if row.token_hash != issued_hash][0]
    assert old.token_hash == env.chain.capability_token_hash
    assert old.active_session_key is None
    assert old.revoked_at is not None


def _assert_one_winner_shape(env: LegacyEnv, *, reason: str, source: str):
    """Whichever governance action won, the scope must look exactly like this."""
    pauses = _pause_events(env)
    assert len(pauses) == 1
    assert pauses[0].reason_code == reason
    assert json.loads(pauses[0].payload_json)["source"] == source
    state = _state(env)
    assert state.status == "paused"
    assert state.current_command_id is None
    with Session(env.engine) as session:
        runtime_state = session.get(SessionRuntimeState, env.chain.session_id)
    assert runtime_state.status == "paused"
    attempts = _rows(env, AttemptEvent)
    assert len(attempts) == 1 and attempts[0].processing_status == "completed"
    assert len(_rows(env, InteractionEvent)) == 3
    assert _rows(env, AutopilotRepeatRequest) == []
    assert len(_rows(env, RuntimeCommand)) == 2
    old = [row for row in _capability_rows(env)
           if row[0] == env.chain.capability_token_hash]
    assert old == [(env.chain.capability_token_hash, None, old[0][2])]
    assert old[0][2] is not None, "the superseded capability must be revoked"
    active = [row for row in _capability_rows(env) if row[1] is not None]
    assert len(active) == 1
    assert active[0][0] != env.chain.capability_token_hash


def test_terminal_pause_winning_the_lock_race_completes_before_a_re_pair(
        legacy_env):
    """终态暂停先拿到真实锁：legacy 完成，随后配对才发生。"""
    _judged_but_unpaused(legacy_env)
    asr_calls, judge_calls = (legacy_env.asr_engine.calls,
                              legacy_env.judge_engine.calls)
    _expire_capability(legacy_env)
    before_state = _state(legacy_env)
    with Session(legacy_env.engine) as session:
        before_runtime_revision = session.get(
            SessionRuntimeState, legacy_env.chain.session_id).revision
    arrived, release = threading.Event(), threading.Event()
    second_attempt = threading.Event()
    original = _fenced(autopilot_orchestration,
                       "stage_legacy_repeat_recovery_pause",
                       arrived=arrived, release=release)
    real_lock = main_module._LIVE_WRITE_LOCK
    main_module._LIVE_WRITE_LOCK = _ContendedLock(
        real_lock, second_attempt=second_attempt)
    device_id = "legacy-repaired-device-0001"
    try:
        # Two independent clients: one TestClient serialises requests through a
        # single portal, which would fake the interleaving instead of testing it.
        with ThreadPoolExecutor(max_workers=2) as pool, \
                _client() as stale_client, _client() as pair_client:
            stale = pool.submit(
                _next, stale_client, legacy_env, legacy_env.chain.device_token)
            assert arrived.wait(20), "terminal pause never reached the fence"
            pair_future = pool.submit(_pair, pair_client, device_id)
            # Real contention, not mere scheduling: pairing has reached the
            # genuine acquire() on the same RLock the stale GET is holding.
            assert second_attempt.wait(20), "pair never contended for the lock"
            assert not pair_future.done()
            release.set()
            stale_response = stale.result(timeout=30)
            pair_response = pair_future.result(timeout=30)
    finally:
        release.set()
        main_module._LIVE_WRITE_LOCK = real_lock
        autopilot_orchestration.stage_legacy_repeat_recovery_pause = original

    assert stale_response.status_code == 401
    assert stale_response.json()["code"] == "device_capability_expired"
    assert pair_response.status_code == 200, pair_response.text
    _assert_pair_response_matches_the_new_capability(
        legacy_env, pair_response, device_id)
    _assert_one_winner_shape(
        legacy_env, reason="legacy_repeat_recovery_complete",
        source="legacy_repeat_recovery")
    after_state = _state(legacy_env)
    assert after_state.revision == before_state.revision + 1
    with Session(legacy_env.engine) as session:
        after_runtime = session.get(
            SessionRuntimeState, legacy_env.chain.session_id)
    assert after_runtime.revision == before_runtime_revision + 1
    assert legacy_env.asr_engine.calls == asr_calls
    assert legacy_env.judge_engine.calls == judge_calls


def test_a_re_pair_winning_the_lock_race_is_never_overwritten(legacy_env):
    """配对先赢：中间件已按 VALID 放行的旧请求必须零增量，轮换暂停是赢家。

    旧请求在 handler 取锁前被栅栏挡住（此时它的 bearer 还是 VALID），真实
    ``/device/pair`` 完整提交并轮换设备；释放后 handler 的唯一锁内 revalidate
    只能看到 REVOKED。
    """
    _judged_but_unpaused(legacy_env)
    asr_calls, judge_calls = (legacy_env.asr_engine.calls,
                              legacy_env.judge_engine.calls)
    before_state = _state(legacy_env)
    with Session(legacy_env.engine) as session:
        before_runtime_revision = session.get(
            SessionRuntimeState, legacy_env.chain.session_id).revision
    stale_arrived, stale_release = threading.Event(), threading.Event()
    committed: list[str] = []
    original_token = _fenced(
        main_module, "_require_device_capability_token_hash",
        arrived=stale_arrived, release=stale_release)
    original_commit = main_module._commit_http_legacy_terminal_pause
    main_module._commit_http_legacy_terminal_pause = (
        lambda **kwargs: committed.append("called") or original_commit(**kwargs))
    device_id = "legacy-repaired-device-0002"
    try:
        with ThreadPoolExecutor(max_workers=2) as pool, \
                _client() as stale_client, _client() as pair_client:
            stale = pool.submit(
                _next, stale_client, legacy_env, legacy_env.chain.device_token)
            # The bearer was still VALID when the middleware admitted it; the
            # fence holds it before the handler takes any lock.
            assert stale_arrived.wait(20), "stale GET never reached the fence"
            pair_response = pool.submit(
                _pair, pair_client, device_id).result(timeout=30)
            assert pair_response.status_code == 200, pair_response.text
            after_pair = _authority_snapshot(legacy_env)
            stale_release.set()
            stale_response = stale.result(timeout=30)
    finally:
        stale_release.set()
        main_module._require_device_capability_token_hash = original_token
        main_module._commit_http_legacy_terminal_pause = original_commit

    assert stale_response.status_code == 401
    assert stale_response.json()["detail"]["code"] == "device_capability_revoked"
    # Field-for-field across every authoritative column: the released stale
    # request added nothing at all, and never reached the commit helper.
    assert _authority_snapshot(legacy_env) == after_pair
    assert committed == []
    _assert_pair_response_matches_the_new_capability(
        legacy_env, pair_response, device_id)
    _assert_one_winner_shape(
        legacy_env, reason="autopilot_device_rotated", source="device_rotation")
    after_state = _state(legacy_env)
    assert after_state.revision == before_state.revision + 1
    with Session(legacy_env.engine) as session:
        after_runtime = session.get(
            SessionRuntimeState, legacy_env.chain.session_id)
    assert after_runtime.revision == before_runtime_revision + 1
    assert legacy_env.asr_engine.calls == asr_calls
    assert legacy_env.judge_engine.calls == judge_calls


# --------------------------------------------------------------------------
# D-GOV: real governance winning against a genuinely blocked provider call
# --------------------------------------------------------------------------


def _provider_gate(arrived: threading.Event, release: threading.Event):
    """Hold a real provider call open until the test lets it finish.

    Installed on a counting provider's ``before`` hook, so the worker blocks
    *inside* genuine provider I/O — after it released every control lock and
    before any durable write. That is the only window in which a governance
    request can legitimately race it, and the only one worth proving.
    """
    def _hold() -> None:
        arrived.set()
        assert release.wait(30), "provider gate never released"

    return _hold


def _drain_provider_workers(release: threading.Event, workers: list,
                            restore) -> None:
    """Bounded teardown for provider-gated workers, safe on the failure path.

    Mirrors :func:`_drain_workers`: every join is timed so a wedged worker
    fails the run instead of hanging the whole regression, and the production
    wrappers plus the provider hook are put back only after the threads exit.
    """
    release.set()
    still_alive = []
    for thread in workers:
        thread.join(timeout=30)
        if thread.is_alive():
            still_alive.append(thread.name)
    restore()
    assert not still_alive, f"worker threads did not exit: {still_alive}"


def _control_event_types(env: LegacyEnv) -> list[str]:
    return [row.event_type for row in
            sorted(_rows(env, AutopilotControlEvent),
                   key=lambda row: row.event_seq)]


def test_a_re_pair_during_a_blocking_asr_provider_wins_the_scope(
        legacy_env, monkeypatch):
    """ASR 真卡在 provider 里时的真实重配对：轮换赢，迟到转写一个字节都不写。"""
    # Run the real started-VisitPlan admission, not the conftest escape.
    monkeypatch.delenv("NMU_TEST_ALLOW_DIRECT_SESSION_CREATE", raising=False)
    arrived, release = threading.Event(), threading.Event()
    fence_outcomes: list[object] = []
    committed: list[str] = []
    original_fence = main_module._legacy_recovery_fence_holds
    original_commit = main_module._commit_legacy_recovery_asr

    def _watch_fence(*args, **kwargs):
        outcome = original_fence(*args, **kwargs)
        fence_outcomes.append(outcome)
        return outcome

    def _watch_commit(*args, **kwargs):
        committed.append("entered")
        return original_commit(*args, **kwargs)

    def _restore() -> None:
        legacy_env.asr_engine._before = None
        main_module._legacy_recovery_fence_holds = original_fence
        main_module._commit_legacy_recovery_asr = original_commit

    legacy_env.asr_engine._before = _provider_gate(arrived, release)
    main_module._legacy_recovery_fence_holds = _watch_fence
    main_module._commit_legacy_recovery_asr = _watch_commit
    workers: list[threading.Thread] = []
    device_id = "legacy-repaired-device-0003"
    try:
        worker = _start_worker(workers, legacy_env)
        assert arrived.wait(30), "the worker never reached the ASR provider"
        # Inside real provider I/O: the capture claim is committed, no durable
        # processing fact exists yet, and every control lock has been released.
        assert legacy_env.asr_engine.calls == 1
        assert legacy_env.judge_engine.calls == 0
        owner, generation, status = _capture_claim_state(legacy_env)
        assert owner is not None
        assert generation == 1
        assert status == "received"

        with _client() as pair_client:
            pair_response = _pair(pair_client, device_id)
        assert pair_response.status_code == 200, pair_response.text
        _assert_pair_response_matches_the_new_capability(
            legacy_env, pair_response, device_id)

        state = _state(legacy_env)
        assert (state.mode, state.status, state.revision) == (
            "autonomous", "paused", 4)
        assert state.current_command_id is None
        assert state.last_error_code == "autopilot_device_rotated"
        with Session(legacy_env.engine) as session:
            runtime_state = session.get(
                SessionRuntimeState, legacy_env.chain.session_id)
            rotated = session.get(
                AttemptCaptureProcessing, legacy_env.chain.capture_id)
        assert (runtime_state.status, runtime_state.revision) == ("paused", 1)
        # The rotation fenced the live claim in its own commit: the blocked
        # worker no longer owns the capture it is still transcribing.
        assert rotated.processing_owner is None
        assert rotated.processing_lease_expires_at is None
        assert rotated.processing_claimed_at is None
        assert rotated.processing_generation == 2
        assert rotated.processing_status == "received"
        assert _control_event_types(legacy_env) == ["start", "pause"]
        pauses = _pause_events(legacy_env)
        assert len(pauses) == 1
        assert pauses[0].reason_code == "autopilot_device_rotated"
        assert json.loads(pauses[0].payload_json)["source"] == "device_rotation"
        assert _rows(legacy_env, AttemptEvent) == []
        assert _rows(legacy_env, InteractionEvent) == []
        assert _rows(legacy_env, AutopilotRepeatRequest) == []
        assert len(_rows(legacy_env, RuntimeCommand)) == 2
        after_rotation = _authority_snapshot(legacy_env)

        release.set()
        worker.join(timeout=30)
        assert not worker.is_alive()
    finally:
        _drain_provider_workers(release, workers, _restore)

    # The transcript really came back and really met the re-verification, which
    # refused it before any durable write could happen.
    assert legacy_env.asr_engine.calls == 1
    assert legacy_env.judge_engine.calls == 0
    assert fence_outcomes == [False]
    assert committed == []
    assert _authority_snapshot(legacy_env) == after_rotation
    assert [row.reason_code for row in _pause_events(legacy_env)] == [
        "autopilot_device_rotated"]
    assert _state(legacy_env).last_error_code == "autopilot_device_rotated"


def test_a_pause_drain_takeover_during_a_blocking_judge_wins_the_scope(
        legacy_env, monkeypatch):
    """判分真卡在 provider 里时的真实暂停→收麦→接管：治理赢，迟到判分零写。"""
    monkeypatch.delenv("NMU_TEST_ALLOW_DIRECT_SESSION_CREATE", raising=False)
    _run_to_asr_checkpoint(legacy_env)
    assert legacy_env.asr_engine.calls == 1
    assert legacy_env.judge_engine.calls == 0
    attempts = _rows(legacy_env, AttemptEvent)
    assert len(attempts) == 1
    attempt_id = attempts[0].id
    assert attempts[0].processing_status == "asr_completed"
    assert attempts[0].processing_generation == 1
    assert _pause_events(legacy_env) == []
    _expire_attempt_lease(legacy_env, attempt_id)

    arrived, release = threading.Event(), threading.Event()
    outcomes: list[object] = []
    original_commit = main_module._commit_legacy_recovery_judgement

    def _watch_commit(*args, **kwargs):
        try:
            outcome = original_commit(*args, **kwargs)
        except BaseException as exc:  # noqa: BLE001 - recorded, then re-raised
            outcomes.append(exc)
            raise
        outcomes.append(outcome)
        return outcome

    def _restore() -> None:
        legacy_env.judge_engine._before = None
        main_module._commit_legacy_recovery_judgement = original_commit

    legacy_env.judge_engine._before = _provider_gate(arrived, release)
    main_module._commit_legacy_recovery_judgement = _watch_commit
    workers: list[threading.Thread] = []
    session_id = legacy_env.chain.session_id
    try:
        worker = _start_worker(workers, legacy_env)
        assert arrived.wait(30), "the worker never reached the judge provider"
        assert legacy_env.judge_engine.calls == 1
        held_owner, held_generation, held_status = _attempt_claim_state(
            legacy_env)
        assert held_owner is not None
        assert held_generation == 2
        assert held_status == "asr_completed"

        with _account_client(legacy_env) as account, _client() as device:
            paused = account.post(f"/sessions/{session_id}/pause")
            assert paused.status_code == 200, paused.text
            pauses = _pause_events(legacy_env)
            assert len(pauses) == 1
            assert pauses[0].reason_code == "researcher_requested_pause"
            assert json.loads(pauses[0].payload_json)["source"] == (
                "account_pause_endpoint")
            state = _state(legacy_env)
            assert (state.status, state.revision) == ("paused", 4)
            with Session(legacy_env.engine) as session:
                runtime_state = session.get(SessionRuntimeState, session_id)
                fenced = session.get(AttemptEvent, attempt_id)
            assert (runtime_state.status, runtime_state.revision) == (
                "paused", 1)
            # Same fence as the rotation case, on the attempt claim this time.
            assert fenced.processing_generation == 3
            assert fenced.processing_owner is None
            assert fenced.processing_lease_expires_at is None
            assert fenced.processing_claimed_at is None

            before_target = _authority_snapshot(legacy_env)
            target = device.get(
                f"/sessions/{session_id}/autopilot/drain-target",
                headers={"X-Device-Capability": legacy_env.chain.device_token})
            assert target.status_code == 200, target.text
            assert target.json() == {"command_key": "cmd-legacy-record-0001",
                                     "state_revision": 4}
            assert target.headers["Cache-Control"] == "private, no-store"
            assert _authority_snapshot(legacy_env) == before_target

            drained = device.post(
                f"/sessions/{session_id}/autopilot/commands/"
                "cmd-legacy-record-0001/drain-ack",
                headers={"X-Device-Capability": legacy_env.chain.device_token})
            assert drained.status_code == 200, drained.text
            assert drained.json() == {"replayed": False, "state_revision": 5}

            taken = account.post(
                f"/sessions/{session_id}/autopilot/takeover",
                json={"idempotency_key": "takeover-legacy-gov-0001",
                      "expected_revision": 5})
            assert taken.status_code == 200, taken.text
            assert taken.json()["mode"] == "manual"
            assert taken.json()["status"] == "paused"
            assert taken.json()["server_owned"] is False
            assert taken.json()["state_revision"] == 6

        assert _control_event_types(legacy_env) == [
            "start", "pause", "drain_complete", "takeover"]
        control = _state(legacy_env)
        assert (control.mode, control.status) == ("manual", "paused")
        with Session(legacy_env.engine) as session:
            runtime_state = session.get(SessionRuntimeState, session_id)
        assert runtime_state.status == "paused"
        assert len(_rows(legacy_env, RuntimeCommand)) == 2
        assert _rows(legacy_env, AutopilotRepeatRequest) == []
        after_takeover = _authority_snapshot(legacy_env)

        release.set()
        worker.join(timeout=30)
        assert not worker.is_alive()
    finally:
        _drain_provider_workers(release, workers, _restore)

    # The judgement really came back and really reached the durable commit,
    # which refused it: the whole authority stays exactly as governance left it.
    #
    # The refusal is an exception, not this helper's own ``return False``: the
    # committed governance cleared ``current_command_id`` and flipped the mode
    # to manual, so the re-verification inside the commit fails at the pending
    # command itself. The worker's own AutopilotOrchestrationError handler then
    # swallows it and writes nothing — a strictly earlier and narrower refusal
    # than the target comparison the ``return False`` branch would have made.
    assert len(outcomes) == 1, "the late judgement never reached the real commit"
    refusal = outcomes[0]
    assert isinstance(
        refusal, autopilot_orchestration.AutopilotOrchestrationError)
    assert refusal.code == "autopilot_attempt_not_pending"
    assert refusal.message == "当前没有待处理的 P0a attempt"
    assert str(refusal) == "当前没有待处理的 P0a attempt"
    assert _authority_snapshot(legacy_env) == after_takeover
    assert _rows(legacy_env, AttemptEvent)[0].processing_status == (
        "asr_completed")
    assert sorted(row.event_type
                  for row in _rows(legacy_env, InteractionEvent)) == [
        "asr_completed", "attempt_received"]
    assert [row.reason_code for row in _pause_events(legacy_env)] == [
        "researcher_requested_pause"]
    assert legacy_env.asr_engine.calls == 1
    assert legacy_env.judge_engine.calls == 1


# --------------------------------------------------------------------------
# D-FINGERPRINT: evidence drift under a provider call the worker cannot undo
# --------------------------------------------------------------------------


def _audio_turn_ref_version(env: LegacyEnv) -> int:
    with Session(env.engine) as session:
        return session.get(
            AudioAssetRow, env.chain.raw_audio_id).patient_turn_ref_version


def test_audio_turn_ref_drift_during_asr_closes_the_durable_checkpoint(
        legacy_env, monkeypatch):
    """ASR 阻塞期间音频证据形状漂移：栅栏必须拒绝，且不得把漂移“修回”。"""
    monkeypatch.delenv("NMU_TEST_ALLOW_DIRECT_SESSION_CREATE", raising=False)
    arrived, release = threading.Event(), threading.Event()
    fence_outcomes: list[object] = []
    committed: list[str] = []
    original_fence = main_module._legacy_recovery_fence_holds
    original_commit = main_module._commit_legacy_recovery_asr

    def _watch_fence(*args, **kwargs):
        outcome = original_fence(*args, **kwargs)
        fence_outcomes.append(outcome)
        return outcome

    def _watch_commit(*args, **kwargs):
        committed.append("entered")
        return original_commit(*args, **kwargs)

    def _restore() -> None:
        legacy_env.asr_engine._before = None
        main_module._legacy_recovery_fence_holds = original_fence
        main_module._commit_legacy_recovery_asr = original_commit

    legacy_env.asr_engine._before = _provider_gate(arrived, release)
    main_module._legacy_recovery_fence_holds = _watch_fence
    main_module._commit_legacy_recovery_asr = _watch_commit
    workers: list[threading.Thread] = []
    try:
        worker = _start_worker(workers, legacy_env)
        assert arrived.wait(30), "the worker never reached the ASR provider"
        assert legacy_env.asr_engine.calls == 1
        assert legacy_env.judge_engine.calls == 0
        owner, generation, status = _capture_claim_state(legacy_env)
        assert owner is not None
        assert generation == 1
        assert status == "received"

        # A legally-shaped drift, not a corrupt NULL: still valid, wrong turn.
        assert _audio_turn_ref_version(legacy_env) == 2
        _raw(legacy_env,
             "UPDATE audioassetrow SET patient_turn_ref_version = ? "
             "WHERE raw_audio_id = ?",
             (1, legacy_env.chain.raw_audio_id))
        assert _audio_turn_ref_version(legacy_env) == 1
        after_mutation = _authority_snapshot(legacy_env)

        release.set()
        worker.join(timeout=30)
        assert not worker.is_alive()
    finally:
        _drain_provider_workers(release, workers, _restore)

    assert fence_outcomes == [False]
    assert committed == []
    assert _authority_snapshot(legacy_env) == after_mutation
    # The worker may not repair the drift, and may not close the scope over it.
    assert _audio_turn_ref_version(legacy_env) == 1
    assert _capture_claim_state(legacy_env) == (owner, generation, status)
    assert legacy_env.asr_engine.calls == 1
    assert legacy_env.judge_engine.calls == 0
    assert _rows(legacy_env, AttemptEvent) == []
    assert _rows(legacy_env, InteractionEvent) == []
    assert _pause_events(legacy_env) == []
    assert _rows(legacy_env, AutopilotRepeatRequest) == []
    assert len(_rows(legacy_env, RuntimeCommand)) == 2


def test_transcript_drift_after_judging_closes_the_durable_checkpoint(
        legacy_env, monkeypatch):
    """判分已算完、尚未落库时转写被改写：durable commit 必须拒绝这份判分。"""
    monkeypatch.delenv("NMU_TEST_ALLOW_DIRECT_SESSION_CREATE", raising=False)
    _run_to_asr_checkpoint(legacy_env)
    assert legacy_env.asr_engine.calls == 1
    assert legacy_env.judge_engine.calls == 0
    attempts = _rows(legacy_env, AttemptEvent)
    assert len(attempts) == 1
    attempt_id = attempts[0].id
    assert attempts[0].processing_status == "asr_completed"
    assert attempts[0].processing_generation == 1
    assert attempts[0].asr_text == TARGET_WORD
    _expire_attempt_lease(legacy_env, attempt_id)

    arrived, release = threading.Event(), threading.Event()
    outcomes: list[object] = []
    hold = _provider_gate(arrived, release)
    original_judgement = main_module._legacy_recovery_judgement
    original_commit = main_module._commit_legacy_recovery_judgement

    def _judge_then_hold(*args, **kwargs):
        # The judgement is really produced first — provider and deterministic
        # classifier both done — so the drift below lands on a real decision.
        result = original_judgement(*args, **kwargs)
        hold()
        return result

    def _watch_commit(*args, **kwargs):
        try:
            outcome = original_commit(*args, **kwargs)
        except BaseException as exc:  # noqa: BLE001 - recorded, then re-raised
            outcomes.append(exc)
            raise
        outcomes.append(outcome)
        return outcome

    def _restore() -> None:
        main_module._legacy_recovery_judgement = original_judgement
        main_module._commit_legacy_recovery_judgement = original_commit

    main_module._legacy_recovery_judgement = _judge_then_hold
    main_module._commit_legacy_recovery_judgement = _watch_commit
    workers: list[threading.Thread] = []
    try:
        worker = _start_worker(workers, legacy_env)
        assert arrived.wait(30), "the worker never finished judging"
        assert legacy_env.judge_engine.calls == 1
        held_owner, held_generation, held_status = _attempt_claim_state(
            legacy_env)
        assert held_owner is not None
        assert held_generation == 2
        assert held_status == "asr_completed"

        _raw(legacy_env, "UPDATE attemptevent SET asr_text = ? WHERE id = ?",
             ("苹果", attempt_id))
        with Session(legacy_env.engine) as session:
            assert session.get(AttemptEvent, attempt_id).asr_text == "苹果"
        after_mutation = _authority_snapshot(legacy_env)

        release.set()
        worker.join(timeout=30)
        assert not worker.is_alive()
    finally:
        _drain_provider_workers(release, workers, _restore)

    # Judged against a transcript that no longer exists: the commit must refuse
    # rather than attach that decision to the new text.
    assert len(outcomes) == 1, "the judgement never reached the real commit"
    assert outcomes[0] is False
    assert _authority_snapshot(legacy_env) == after_mutation
    with Session(legacy_env.engine) as session:
        attempt = session.get(AttemptEvent, attempt_id)
    assert attempt.asr_text == "苹果"
    assert attempt.processing_status == "asr_completed"
    assert (attempt.processing_owner, attempt.processing_generation) == (
        held_owner, held_generation)
    assert attempt.operational_answer_type is None
    assert sorted(row.event_type
                  for row in _rows(legacy_env, InteractionEvent)) == [
        "asr_completed", "attempt_received"]
    assert _pause_events(legacy_env) == []
    assert legacy_env.asr_engine.calls == 1
    assert legacy_env.judge_engine.calls == 1
    assert len(_rows(legacy_env, RuntimeCommand)) == 2
    assert _rows(legacy_env, AutopilotRepeatRequest) == []


# --------------------------------------------------------------------------
# D-BLOB: the physical recording, across both sides of the durable checkpoint
# --------------------------------------------------------------------------


def _corrupt_blob_bytes(original: bytes, kind: str) -> bytes:
    """One real recording, altered in exactly one auditable way.

    ``same_length_flip`` keeps the byte count and moves only the hash, so a
    length check alone can never catch it; ``truncate_one`` moves both.
    """
    if kind == "same_length_flip":
        return original[:-1] + bytes([original[-1] ^ 0xFF])
    assert kind == "truncate_one", kind
    return original[:-1]


@pytest.mark.parametrize("kind", ["same_length_flip", "truncate_one"])
def test_a_replaced_recording_never_reaches_the_asr_provider(
        legacy_env, monkeypatch, kind):
    """provider 之前录音已失配：一个字节都不送进 ASR，也不留任何痕迹。"""
    monkeypatch.delenv("NMU_TEST_ALLOW_DIRECT_SESSION_CREATE", raising=False)
    blob = audio_store.find_blob(legacy_env.chain.raw_audio_id)
    assert blob is not None
    original = blob.read_bytes()
    assert audio_store.sha256_hex(original) == legacy_env.chain.checksum
    assert len(original) == legacy_env.chain.byte_count
    before = _authority_snapshot(legacy_env)
    try:
        blob.write_bytes(_corrupt_blob_bytes(original, kind))
        swapped = blob.read_bytes()
        assert audio_store.sha256_hex(swapped) != legacy_env.chain.checksum
        assert (len(swapped) == legacy_env.chain.byte_count) is (
            kind == "same_length_flip")

        _run_legacy_repeat_recovery_worker(legacy_env.chain.session_id)

        assert legacy_env.asr_engine.calls == 0
        assert legacy_env.judge_engine.calls == 0
        assert _authority_snapshot(legacy_env) == before
        assert _capture_claim_state(legacy_env) == (None, 0, "received")
        assert _rows(legacy_env, AttemptEvent) == []
        assert _rows(legacy_env, InteractionEvent) == []
        assert _pause_events(legacy_env) == []
        assert _rows(legacy_env, AutopilotRepeatRequest) == []
        assert len(_rows(legacy_env, RuntimeCommand)) == 2
    finally:
        blob.write_bytes(original)
    restored = blob.read_bytes()
    assert audio_store.sha256_hex(restored) == legacy_env.chain.checksum
    assert len(restored) == legacy_env.chain.byte_count


def test_a_mid_flight_recording_swap_is_refused_then_recovers_exactly_once(
        legacy_env, monkeypatch):
    """ASR 进行中换掉录音：迟到结果零写；换回来后仍能且只能完成一次。"""
    monkeypatch.delenv("NMU_TEST_ALLOW_DIRECT_SESSION_CREATE", raising=False)
    blob = audio_store.find_blob(legacy_env.chain.raw_audio_id)
    assert blob is not None
    original = blob.read_bytes()
    arrived, release = threading.Event(), threading.Event()
    fence_outcomes: list[object] = []
    committed: list[str] = []
    original_fence = main_module._legacy_recovery_fence_holds
    original_commit = main_module._commit_legacy_recovery_asr

    def _watch_fence(*args, **kwargs):
        outcome = original_fence(*args, **kwargs)
        fence_outcomes.append(outcome)
        return outcome

    def _watch_commit(*args, **kwargs):
        committed.append("entered")
        return original_commit(*args, **kwargs)

    def _restore() -> None:
        legacy_env.asr_engine._before = None
        main_module._legacy_recovery_fence_holds = original_fence
        main_module._commit_legacy_recovery_asr = original_commit

    legacy_env.asr_engine._before = _provider_gate(arrived, release)
    main_module._legacy_recovery_fence_holds = _watch_fence
    main_module._commit_legacy_recovery_asr = _watch_commit
    workers: list[threading.Thread] = []
    try:
        try:
            worker = _start_worker(workers, legacy_env)
            assert arrived.wait(30), "the worker never reached the ASR provider"
            # The provider has already read and re-hashed the original bytes.
            assert legacy_env.asr_engine.calls == 1
            owner, generation, status = _capture_claim_state(legacy_env)
            assert owner is not None
            assert (generation, status) == (1, "received")

            blob.write_bytes(_corrupt_blob_bytes(original, "same_length_flip"))
            swapped = blob.read_bytes()
            assert len(swapped) == len(original)
            assert audio_store.sha256_hex(swapped) != legacy_env.chain.checksum
            after_mutation = _authority_snapshot(legacy_env)

            release.set()
            worker.join(timeout=30)
            assert not worker.is_alive()
        finally:
            _drain_provider_workers(release, workers, _restore)
    finally:
        blob.write_bytes(original)
    restored = blob.read_bytes()
    assert audio_store.sha256_hex(restored) == legacy_env.chain.checksum
    assert len(restored) == legacy_env.chain.byte_count

    assert fence_outcomes == [False]
    assert committed == []
    assert _authority_snapshot(legacy_env) == after_mutation
    assert _rows(legacy_env, AttemptEvent) == []
    assert _rows(legacy_env, InteractionEvent) == []
    assert _pause_events(legacy_env) == []
    assert legacy_env.judge_engine.calls == 0
    assert _capture_claim_state(legacy_env) == (owner, generation, status)

    # With the real recording back and the abandoned lease expired, the scope
    # still finishes — and finishes exactly once.
    _expire_capture_lease(legacy_env)
    _run_legacy_repeat_recovery_worker(legacy_env.chain.session_id)

    assert legacy_env.asr_engine.calls == 2
    assert legacy_env.judge_engine.calls == 1
    assert _capture_claim_state(legacy_env) == (None, 2, "asr_completed")
    attempts = _rows(legacy_env, AttemptEvent)
    assert len(attempts) == 1
    assert attempts[0].processing_status == "completed"
    assert len(_rows(legacy_env, InteractionEvent)) == 3
    assert len(_pause_events(legacy_env)) == 1
    assert len(_rows(legacy_env, RuntimeCommand)) == 2
    assert _rows(legacy_env, AutopilotRepeatRequest) == []
    settled = _authority_snapshot(legacy_env)

    _run_legacy_repeat_recovery_worker(legacy_env.chain.session_id)
    assert _authority_snapshot(legacy_env) == settled
    assert legacy_env.asr_engine.calls == 2
    assert legacy_env.judge_engine.calls == 1


def test_a_post_fence_recording_swap_must_not_become_durable_evidence(
        legacy_env, monkeypatch):
    """栅栏已过、durable commit 尚未执行时换掉录音：这份转写不得成为证据。

    已审计出的 TOCTOU：``_commit_legacy_recovery_asr`` 不再收音频参数，因此写入
    前无法复核字节。本用例按现状预期为红，作为真实证据保留，等待窄生产修复。
    """
    monkeypatch.delenv("NMU_TEST_ALLOW_DIRECT_SESSION_CREATE", raising=False)
    blob = audio_store.find_blob(legacy_env.chain.raw_audio_id)
    assert blob is not None
    original = blob.read_bytes()
    gate = _TwoCallGate(main_module, "_commit_legacy_recovery_asr")
    workers: list[threading.Thread] = []
    try:
        try:
            worker = _start_worker(workers, legacy_env)
            assert gate.entered[0].wait(30), "the commit gate was never reached"
            # ASR returned and the fence already re-hashed the real bytes; the
            # durable write itself has not begun.
            assert legacy_env.asr_engine.calls == 1
            assert legacy_env.judge_engine.calls == 0
            owner, generation, status = _capture_claim_state(legacy_env)
            assert owner is not None
            assert (generation, status) == (1, "received")
            assert _rows(legacy_env, AttemptEvent) == []

            blob.write_bytes(_corrupt_blob_bytes(original, "same_length_flip"))
            swapped = blob.read_bytes()
            assert len(swapped) == len(original)
            assert audio_store.sha256_hex(swapped) != legacy_env.chain.checksum
            after_mutation = _authority_snapshot(legacy_env)

            gate.release[0].set()
            worker.join(timeout=30)
            assert not worker.is_alive()
        finally:
            _drain_workers(gate, workers)
    finally:
        blob.write_bytes(original)

    assert gate.results[0] is None, (
        "post-fence swap still produced "
        f"{type(gate.results[0]).__name__}; counts={_counts(legacy_env)}; "
        f"capture={_capture_claim_state(legacy_env)}")
    assert _authority_snapshot(legacy_env) == after_mutation
    assert _rows(legacy_env, AttemptEvent) == []
    assert _rows(legacy_env, InteractionEvent) == []
    assert _pause_events(legacy_env) == []
    assert _capture_claim_state(legacy_env) == (owner, generation, status)


# --------------------------------------------------------------------------
# D-TERMINAL-POISON: session-wide repeat/replay evidence at the terminal stage
# --------------------------------------------------------------------------


_POISON_KINDS = [
    pytest.param("repeat_bound", id="repeat-bound"),
    pytest.param("replayed", id="replayed"),
]


def _insert_noncurrent_repeat_command(env: LegacyEnv, *, poison_kind: str) -> int:
    """Add a third, non-current TTS command that poisons the whole session.

    Every field is copied from the chain's own source TTS command, so the
    issued identity is genuine; only the sequence, the key and the repeat/
    replay bindings differ.  It goes in through the ORM and satisfies every
    database CHECK, because a partial or NULL-shaped forgery would prove
    nothing — the full verifier already refuses those on shape alone.
    """
    current_before = _state(env).current_command_id
    with Session(env.engine) as session:
        source = session.get(RuntimeCommand, env.chain.source_command_id)
        assert source is not None and source.kind == "tts"
        payload_digest = hashlib.sha256(
            source.payload_json.encode("utf-8")).hexdigest()
        fields = source.model_dump()
        fields.pop("id", None)
        fields.update({
            "idempotency_key": f"cmd-legacy-poison-{poison_kind}-0001",
            "command_seq": 3,
            "repeat_protocol_version_id": APPROVED_VERSION_ID,
            "repeat_protocol_definition_digest": APPROVED_DIGEST,
        })
        if poison_kind == "replayed":
            fields.update({
                "replay_source_command_id": source.id,
                "replay_ordinal": 1,
                "replay_source_payload_sha256": payload_digest,
            })
        else:
            assert poison_kind == "repeat_bound", poison_kind
        poison = RuntimeCommand(**fields)
        session.add(poison)
        session.commit()
        session.refresh(poison)
        poison_id = poison.id
        assert poison.kind == "tts"
        assert poison.command_seq == 3
        assert poison.session_id == env.chain.session_id
        assert poison.repeat_protocol_version_id == APPROVED_VERSION_ID
        assert poison.repeat_protocol_definition_digest == APPROVED_DIGEST
        if poison_kind == "replayed":
            assert poison.replay_source_command_id == source.id
            assert poison.replay_ordinal == 1
            assert poison.replay_source_payload_sha256 == payload_digest
        else:
            assert poison.replay_source_command_id is None
            assert poison.replay_ordinal is None
            assert poison.replay_source_payload_sha256 is None
        violations = session.connection().exec_driver_sql(
            "PRAGMA foreign_key_check").fetchall()
    assert violations == []
    assert len(_rows(env, RuntimeCommand)) == 3
    # The poison is a third, non-current command: whatever the scope was
    # pointing at before must be exactly what it points at afterwards.
    assert _state(env).current_command_id == current_before
    return poison_id


@pytest.mark.parametrize("poison_kind", _POISON_KINDS)
def test_session_repeat_poison_before_asr_is_observation_only(
        legacy_env, monkeypatch, poison_kind):
    """同场出现重复/重播证据后，旧协议恢复在 provider 之前就整体拒绝。"""
    monkeypatch.delenv("NMU_TEST_ALLOW_DIRECT_SESSION_CREATE", raising=False)
    _insert_noncurrent_repeat_command(legacy_env, poison_kind=poison_kind)
    before = _authority_snapshot(legacy_env)

    _run_legacy_repeat_recovery_worker(legacy_env.chain.session_id)

    assert legacy_env.asr_engine.calls == 0
    assert legacy_env.judge_engine.calls == 0
    assert _authority_snapshot(legacy_env) == before
    assert _capture_claim_state(legacy_env) == (None, 0, "received")
    assert _rows(legacy_env, AttemptEvent) == []
    assert _rows(legacy_env, InteractionEvent) == []
    assert _rows(legacy_env, AutopilotRepeatRequest) == []
    assert _pause_events(legacy_env) == []
    state = _state(legacy_env)
    assert state.status == "processing_attempt"
    assert state.current_command_id == legacy_env.chain.record_command_id
    assert len(_rows(legacy_env, RuntimeCommand)) == 3


@pytest.mark.parametrize("poison_kind", _POISON_KINDS)
def test_session_repeat_poison_after_judgement_blocks_terminal_pause(
        legacy_env, monkeypatch, poison_kind):
    """判分已落库、pause 未落库时出现同场毒证据：provider-free 终态必须闭合。

    已知红点：``legacy_terminal_pause_owed`` 与它调度的写入没有 full verifier
    那条 session-wide repeat/replay 查询。本用例按现状预期为红。
    """
    monkeypatch.delenv("NMU_TEST_ALLOW_DIRECT_SESSION_CREATE", raising=False)
    _judged_but_unpaused(legacy_env)
    attempts = _rows(legacy_env, AttemptEvent)
    assert len(attempts) == 1 and attempts[0].processing_status == "completed"
    assert len(_rows(legacy_env, InteractionEvent)) == 3
    assert _pause_events(legacy_env) == []
    asr_calls, judge_calls = (legacy_env.asr_engine.calls,
                              legacy_env.judge_engine.calls)

    _insert_noncurrent_repeat_command(legacy_env, poison_kind=poison_kind)
    before = _authority_snapshot(legacy_env)

    # The write is executed before anything is asserted: asserting on ``owed``
    # first would abort the test and destroy the evidence of what the writer
    # actually did.
    with Session(legacy_env.engine) as session:
        owed = autopilot_orchestration.legacy_terminal_pause_owed(
            session, session_id=legacy_env.chain.session_id)
    committed = main_module._commit_legacy_recovery_pause(
        legacy_env.chain.session_id)
    after = _authority_snapshot(legacy_env)
    state = _state(legacy_env)
    with Session(legacy_env.engine) as session:
        runtime_state = session.get(
            SessionRuntimeState, legacy_env.chain.session_id)

    diagnosis = (
        f"poison={poison_kind} owed={owed} committed={committed} "
        f"pauses={len(_pause_events(legacy_env))} "
        f"state=({state.status},rev{state.revision},{state.last_error_code}) "
        f"runtime=({runtime_state.status},rev{runtime_state.revision}) "
        f"snapshot_changed={after != before}")
    assert owed is False, diagnosis
    assert committed is False, diagnosis
    assert after == before, diagnosis
    assert _pause_events(legacy_env) == [], diagnosis
    assert state.status == "processing_attempt", diagnosis
    assert runtime_state.status == "active", diagnosis
    assert legacy_env.asr_engine.calls == asr_calls
    assert legacy_env.judge_engine.calls == judge_calls


@pytest.mark.parametrize("poison_kind", _POISON_KINDS)
def test_session_repeat_poison_closes_existing_legacy_drain_matcher(
        legacy_env, monkeypatch, poison_kind):
    """已 paused 的旧协议场次事后出现毒证据：收麦 matcher 必须随之关闭。

    与写入端的用例相互独立：这里的 paused 基线由正常 worker 产生，所以即使
    终态写入被修好，这个场景仍然造得出来。按现状预期为红。
    """
    monkeypatch.delenv("NMU_TEST_ALLOW_DIRECT_SESSION_CREATE", raising=False)
    _run_legacy_repeat_recovery_worker(legacy_env.chain.session_id)
    assert len(_pause_events(legacy_env)) == 1
    assert _drain_target_accepts(legacy_env) is True

    _insert_noncurrent_repeat_command(legacy_env, poison_kind=poison_kind)
    before = _authority_snapshot(legacy_env)

    accepted = _drain_target_accepts(legacy_env)
    after = _authority_snapshot(legacy_env)

    diagnosis = (f"poison={poison_kind} accepted={accepted} "
                 f"snapshot_changed={after != before}")
    assert accepted is False, diagnosis
    assert after == before, diagnosis


def test_feature_off_after_judgement_still_commits_exactly_one_pause(
        legacy_env, monkeypatch):
    """判分已落库后关掉 P0a：仍必须补上、且只补上那一次 canonical 终态暂停。

    未来把 poison 查询提炼成共享 predicate 时，它不能顺手夹带 feature/content/
    device 门禁 —— 否则这条已判分的场次会被永久卡在 processing_attempt。
    """
    monkeypatch.delenv("NMU_TEST_ALLOW_DIRECT_SESSION_CREATE", raising=False)
    _judged_but_unpaused(legacy_env)
    before_counts = _counts(legacy_env)
    assert before_counts["attempts"] == 1
    assert before_counts["interactions"] == 3
    assert _pause_events(legacy_env) == []
    asr_calls, judge_calls = (legacy_env.asr_engine.calls,
                              legacy_env.judge_engine.calls)
    before_state = _state(legacy_env)
    assert before_state.status == "processing_attempt"
    with Session(legacy_env.engine) as session:
        before_runtime = session.get(
            SessionRuntimeState, legacy_env.chain.session_id)
        before_live = session.get(LiveState, 1)
        before_runtime_revision = before_runtime.revision
        before_live_seq = before_live.seq
        assert before_runtime.status == "active"

    monkeypatch.setenv("ENABLE_AUTOPILOT_P0A_SIMULATION", "0")
    _run_legacy_repeat_recovery_worker(legacy_env.chain.session_id)

    pauses = _pause_events(legacy_env)
    assert len(pauses) == 1
    assert pauses[0].reason_code == "legacy_repeat_recovery_complete"
    state = _state(legacy_env)
    assert state.status == "paused"
    assert state.current_command_id is None
    assert state.revision == before_state.revision + 1
    assert state.last_error_code == "legacy_repeat_recovery_complete"
    with Session(legacy_env.engine) as session:
        runtime_state = session.get(
            SessionRuntimeState, legacy_env.chain.session_id)
        live = session.get(LiveState, 1)
        assert runtime_state.status == "paused"
        assert runtime_state.revision == before_runtime_revision + 1
        assert runtime_state.paused_at is not None
        assert live.seq == before_live_seq + 1
        assert json.loads(live.session_json)["paused"] is True
    after_counts = _counts(legacy_env)
    for ledger in ("attempts", "interactions", "commands", "repeat_requests"):
        assert after_counts[ledger] == before_counts[ledger]
    assert legacy_env.asr_engine.calls == asr_calls
    assert legacy_env.judge_engine.calls == judge_calls
    settled = _authority_snapshot(legacy_env)

    _run_legacy_repeat_recovery_worker(legacy_env.chain.session_id)
    assert _authority_snapshot(legacy_env) == settled
    assert len(_pause_events(legacy_env)) == 1


def test_the_poison_helper_never_flushes_the_callers_session(
        legacy_env, monkeypatch):
    """共享 poison helper 必须是可证明的纯读：脏 Session 也不能被它顺手 flush。

    合同写的是 never flushes, never mutates a row。SQLAlchemy 默认 autoflush 会
    让任何 get/exec 把调用者尚未提交的修改推进数据库，所以这里用 before_flush
    监听器把"零次 flush"变成事实，而不是从 helper 里没写 flush 去推断。
    """
    monkeypatch.delenv("NMU_TEST_ALLOW_DIRECT_SESSION_CREATE", raising=False)
    before = _authority_snapshot(legacy_env)
    flushes: list[int] = []

    def _record_flush(session, _flush_context, _instances):
        flushes.append(len(session.dirty))

    with Session(legacy_env.engine) as session:
        # A dirty, unflushed edit on a row the helper never reads, so the
        # identity map cannot turn this into a semantic change of its answer.
        state = session.get(SessionAutopilotState, legacy_env.chain.session_id)
        state.last_error_code = "poison_harness_dirty_marker"
        assert session.dirty
        event.listen(session, "before_flush", _record_flush)
        try:
            reason = autopilot_service.legacy_session_repeat_poison_reason(
                session, session_id=legacy_env.chain.session_id)
            after = _authority_snapshot(legacy_env)
        finally:
            event.remove(session, "before_flush", _record_flush)
            session.rollback()

    assert reason is None
    assert flushes == [], f"helper autoflushed the caller's session: {flushes}"
    assert after == before
    assert _state(legacy_env).last_error_code != "poison_harness_dirty_marker"


# --------------------------------------------------------------------------
# D-BLOB-JUDGE: the physical recording across the *judgement* checkpoint
# --------------------------------------------------------------------------


def _judgement_stage_facts(env: LegacyEnv) -> dict:
    """Everything the judgement stage is allowed — or forbidden — to move."""
    attempts = _rows(env, AttemptEvent)
    return {
        "asr": env.asr_engine.calls,
        "judge": env.judge_engine.calls,
        "claim": _attempt_claim_state(env),
        "attempt_status": attempts[0].processing_status if attempts else None,
        "answer_type": attempts[0].operational_answer_type if attempts else None,
        "interactions": sorted(row.event_type
                               for row in _rows(env, InteractionEvent)),
        "pauses": len(_pause_events(env)),
    }


def _at_asr_checkpoint(env: LegacyEnv) -> int:
    """Drive to the durable ASR checkpoint and prove the judgement is owed."""
    _run_to_asr_checkpoint(env)
    attempts = _rows(env, AttemptEvent)
    assert len(attempts) == 1
    assert attempts[0].processing_status == "asr_completed"
    assert env.asr_engine.calls == 1
    assert env.judge_engine.calls == 0
    assert sorted(row.event_type for row in _rows(env, InteractionEvent)) == [
        "asr_completed", "attempt_received"]
    assert _pause_events(env) == []
    return attempts[0].id


def test_a_missing_recording_leaves_the_judgement_stage_untouched(
        legacy_env, monkeypatch):
    """判分重入前录音已不在盘上：连一个新的持久 claim 都不该留下。

    已审计缺口：judgement 分支在 claim 之前不做 ``_legacy_blob_matches``，而
    ``_legacy_recovery_fence_holds`` 的字节复核对 ``blob is None`` 直接跳过。
    本用例按现状预期为红。
    """
    monkeypatch.delenv("NMU_TEST_ALLOW_DIRECT_SESSION_CREATE", raising=False)
    attempt_id = _at_asr_checkpoint(legacy_env)
    _expire_attempt_lease(legacy_env, attempt_id)

    blob = audio_store.find_blob(legacy_env.chain.raw_audio_id)
    assert blob is not None
    backup = blob.with_name(f"{legacy_env.chain.raw_audio_id}-red-backup.webm")
    try:
        # Same directory, and a name find_blob's ``<raw_id>.*`` glob cannot see.
        blob.rename(backup)
        assert audio_store.find_blob(legacy_env.chain.raw_audio_id) is None
        before = _authority_snapshot(legacy_env)
        expected = _judgement_stage_facts(legacy_env)

        _run_legacy_repeat_recovery_worker(legacy_env.chain.session_id)

        actual = _judgement_stage_facts(legacy_env)
        after = _authority_snapshot(legacy_env)
        diagnosis = (f"missing-blob judgement reentry: expected={expected} "
                     f"actual={actual} snapshot_changed={after != before}")
        assert actual["judge"] == 0, diagnosis
        assert actual["asr"] == 1, diagnosis
        assert actual["claim"] == expected["claim"], diagnosis
        assert actual["attempt_status"] == "asr_completed", diagnosis
        assert actual["interactions"] == [
            "asr_completed", "attempt_received"], diagnosis
        assert actual["pauses"] == 0, diagnosis
        assert after == before, diagnosis
    finally:
        if backup.exists():
            backup.rename(blob)
    restored = blob.read_bytes()
    assert audio_store.sha256_hex(restored) == legacy_env.chain.checksum
    assert len(restored) == legacy_env.chain.byte_count

    # With the recording back and the abandoned lease expired, the judgement
    # stage finishes — exactly once.
    _expire_attempt_lease(legacy_env, attempt_id)
    _run_legacy_repeat_recovery_worker(legacy_env.chain.session_id)

    assert legacy_env.asr_engine.calls == 1
    assert legacy_env.judge_engine.calls == 1
    attempts = _rows(legacy_env, AttemptEvent)
    assert len(attempts) == 1 and attempts[0].processing_status == "completed"
    assert len(_rows(legacy_env, InteractionEvent)) == 3
    assert len(_pause_events(legacy_env)) == 1
    settled = _authority_snapshot(legacy_env)

    _run_legacy_repeat_recovery_worker(legacy_env.chain.session_id)
    assert _authority_snapshot(legacy_env) == settled
    assert legacy_env.judge_engine.calls == 1


def test_a_post_judge_recording_swap_must_not_become_durable_evidence(
        legacy_env, monkeypatch):
    """判分已真实返回、durable commit 未执行时换包：这份判分不得落库。

    已审计缺口：``_commit_legacy_recovery_judgement`` 不收音频参数，其重验只读
    数据库行，因此写入前无法复核物理字节。本用例按现状预期为红。
    """
    monkeypatch.delenv("NMU_TEST_ALLOW_DIRECT_SESSION_CREATE", raising=False)
    attempt_id = _at_asr_checkpoint(legacy_env)
    _expire_attempt_lease(legacy_env, attempt_id)

    blob = audio_store.find_blob(legacy_env.chain.raw_audio_id)
    assert blob is not None
    original = blob.read_bytes()
    gate = _TwoCallGate(main_module, "_commit_legacy_recovery_judgement")
    workers: list[threading.Thread] = []
    try:
        try:
            worker = _start_worker(workers, legacy_env)
            assert gate.entered[0].wait(30), "the commit gate was never reached"
            # The judge really ran and the claim is really held; the durable
            # write itself has not begun.
            expected = _judgement_stage_facts(legacy_env)
            assert expected["judge"] == 1
            assert expected["claim"][0] is not None
            assert expected["attempt_status"] == "asr_completed"
            assert expected["interactions"] == [
                "asr_completed", "attempt_received"]
            assert expected["pauses"] == 0

            blob.write_bytes(_corrupt_blob_bytes(original, "same_length_flip"))
            swapped = blob.read_bytes()
            assert len(swapped) == len(original)
            assert audio_store.sha256_hex(swapped) != legacy_env.chain.checksum
            before = _authority_snapshot(legacy_env)

            gate.release[0].set()
            worker.join(timeout=30)
            assert not worker.is_alive()
        finally:
            _drain_workers(gate, workers)
    finally:
        blob.write_bytes(original)
    restored = blob.read_bytes()
    assert audio_store.sha256_hex(restored) == legacy_env.chain.checksum
    assert len(restored) == legacy_env.chain.byte_count

    actual = _judgement_stage_facts(legacy_env)
    after = _authority_snapshot(legacy_env)
    diagnosis = (f"post-judge swap: commit_returned={gate.results.get(0)!r} "
                 f"expected={expected} actual={actual} "
                 f"snapshot_changed={after != before}")
    assert gate.results[0] is False, diagnosis
    assert after == before, diagnosis
    assert actual["claim"] == expected["claim"], diagnosis
    assert actual["attempt_status"] == "asr_completed", diagnosis
    assert actual["answer_type"] is None, diagnosis
    assert actual["interactions"] == [
        "asr_completed", "attempt_received"], diagnosis
    assert actual["pauses"] == 0, diagnosis

    # The refusal must be recoverable, not terminal: with the real recording
    # back the scope still closes, judging exactly once more.
    _expire_attempt_lease(legacy_env, attempt_id)
    _run_legacy_repeat_recovery_worker(legacy_env.chain.session_id)

    assert legacy_env.asr_engine.calls == 1
    assert legacy_env.judge_engine.calls == 2
    attempts = _rows(legacy_env, AttemptEvent)
    assert len(attempts) == 1 and attempts[0].processing_status == "completed"
    assert len([row for row in _rows(legacy_env, InteractionEvent)
                if row.event_type == "judgement_completed"]) == 1
    assert len(_rows(legacy_env, InteractionEvent)) == 3
    assert len(_pause_events(legacy_env)) == 1
    settled = _authority_snapshot(legacy_env)

    _run_legacy_repeat_recovery_worker(legacy_env.chain.session_id)
    assert _authority_snapshot(legacy_env) == settled
    assert legacy_env.judge_engine.calls == 2
