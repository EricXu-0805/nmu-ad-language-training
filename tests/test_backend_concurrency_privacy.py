"""Regression tests for the 2026-09-06 audit, using real HTTP threads and locks.

Only provider output and deterministic scheduling are injected; all authorization,
pause/abort, consent and commit boundaries remain production code.
"""
from concurrent.futures import ThreadPoolExecutor
import threading

from test_rapport_reply_pipeline import (
    pipeline_client as shared_pipeline_client, _seed_scene, _seed_audio,
    _StubAsr, _use_reply_stub, _create_reply,
)
from app import asr, audio_store
import app.main as main
from app.models import RapportUtteranceEvent, SessionRuntimeState, AudioAssetRow
from sqlmodel import Session, select
from datetime import datetime
import json
from app import auth, cloud_processing, questionnaire_ai_draft, questionnaires
from app.models import Patient
from test_auth import _add_user
from test_questionnaires import GDS, _gds_values_payload

# Re-export the existing fixture object without shadowing an unused import.
pipeline_client = shared_pipeline_client

BODY = {"sectionKey":"介绍机构环境", "questionIdx":0,"mode":"auto", "rawAudioId":"audit-audio"}

def setup_scene(client, monkeypatch):
    _seed_scene(client)
    _seed_audio(client, session_id="S-PIPE", raw_id="audit-audio", section="介绍机构环境")
    monkeypatch.setattr(asr,"get_engine", lambda: _StubAsr("我喜欢晒太阳"))

def test_late_reply_is_rejected_after_http_abort(pipeline_client, monkeypatch):
    client=pipeline_client
    setup_scene(client, monkeypatch)
    abort_status=[]
    def late_reply(round_no):
        with Session(client.test_engine) as s:
            rt=s.get(SessionRuntimeState,"S-PIPE")
            revision=rt.revision if rt else 0
        aborted=client.post("/sessions/S-PIPE/abort", json={"reason_code":"clinical_safety", "expected_revision":revision,"idempotency_key":"audit-abort-00000001"})
        abort_status.append(aborted.status_code)
        return "晒太阳舒服呀。"
    _use_reply_stub(monkeypatch, late_reply)
    response=_create_reply(client,"S-PIPE",BODY)
    with Session(client.test_engine) as s:
        rt=s.get(SessionRuntimeState,"S-PIPE")
        rows=list(s.exec(select(RapportUtteranceEvent)))
    assert abort_status==[200]
    assert rt.status=="aborted"
    assert response.status_code==409 and len(rows)==0

def test_concurrent_same_audio_has_one_utterance(pipeline_client,monkeypatch):
    client=pipeline_client
    setup_scene(client,monkeypatch)
    both_generating=threading.Barrier(2)
    def reply_after_both_arrive(_round):
        both_generating.wait(timeout=10)
        return "晒太阳舒服呀。"
    _use_reply_stub(monkeypatch,reply_after_both_arrive)
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures=[pool.submit(_create_reply,client,"S-PIPE",BODY) for _ in range(2)]
        responses=[f.result(timeout=25) for f in futures]
    with Session(client.test_engine) as s:
        rows=list(s.exec(select(RapportUtteranceEvent)))
    assert all(r.status_code==200 for r in responses)
    assert len(rows)==1
    assert responses[0].json()["utteranceId"]==responses[1].json()["utteranceId"]
    assert len(set(r.raw_audio_id for r in rows))==1

def test_corrupt_original_never_reaches_asr(pipeline_client,monkeypatch):
    client=pipeline_client
    _seed_scene(client)
    raw_id="audit-audio"
    original=b"\x1a\x45\xdf\xa3original-voice-A"
    changed=b"\x1a\x45\xdf\xa3original-voice-B"
    path,digest=audio_store.save_blob(raw_id,original,"audio/webm")
    from app.models import AudioCaptureReceipt
    with Session(client.test_engine) as s:
        s.add(AudioAssetRow(raw_audio_id=raw_id,session_id="S-PIPE",turn_key="关系建立·介绍机构环境#0",audio_format="webm",is_simulation=True,data_classification="simulation",contains_direct_identifier=False,byte_count=len(original),checksum=digest,uploaded_at=datetime.now()))
        s.add(AudioCaptureReceipt(raw_audio_id=raw_id,session_id="S-PIPE",turn_key="关系建立·介绍机构环境#0",duration_seconds=2.0,byte_count=len(original),checksum=digest,data_classification="simulation",is_simulation=True,contains_direct_identifier=False))
        s.commit()
        assert main._audio_capture_evidence_is_verified(s.get(AudioAssetRow,raw_id),s,require_capture_receipt=True)
    # Model accidental disk corruption/wrong backup restoration, not a remote API attack.
    path.write_bytes(changed)
    seen=[]
    class CapturingAsr(_StubAsr):
        def transcribe(self,data,hotwords):
            seen.append(data)
            return super().transcribe(data,hotwords)
    monkeypatch.setattr(asr,"get_engine",lambda:CapturingAsr("我喜欢晒太阳"))
    _use_reply_stub(monkeypatch,"晒太阳舒服呀。")
    response=_create_reply(client,"S-PIPE",BODY)
    with Session(client.test_engine) as s:
        row=s.get(AudioAssetRow,raw_id)
        receipt=s.exec(select(AudioCaptureReceipt).where(AudioCaptureReceipt.raw_audio_id==raw_id)).one()
        assert row.checksum==receipt.checksum==digest
    assert response.status_code==409
    assert seen==[]


def _authenticated_owner(client,monkeypatch):
    _add_user(client.test_engine,username="audit-owner",display_id="PIPELINE-RESEARCHER")
    monkeypatch.setenv("REQUIRE_AUTH","1")
    monkeypatch.setenv("CONSOLE_PIN","24681024")
    login=client.post("/auth/login",json={"username":"audit-owner","password":"password1"})
    assert login.status_code==200,login.text
    client.headers.update({"X-CSRF-Token":client.cookies.get(auth.CSRF_COOKIE_NAME)})

def _cloud_change(client,patient_id,allowed):
    with Session(client.test_engine) as s:
        p=s.get(Patient,patient_id)
        expected={"allowed":p.cloud_processing_allowed,"provider_id":p.cloud_processing_provider_id,"notice_version":p.cloud_processing_notice_version,"consented_at":p.cloud_processing_consented_at.isoformat() if p.cloud_processing_consented_at else None,"revoked_at":p.cloud_processing_revoked_at.isoformat() if p.cloud_processing_revoked_at else None,"withdrawal_status":p.withdrawal_status,"governance_revision":p.governance_revision}
    policy=cloud_processing.current_policy()
    response=client.patch(f"/patients/{patient_id}/cloud-processing",json={"allowed":allowed,"expected":expected,"policy_provider_id":policy.provider_id,"policy_notice_version":policy.notice_version})
    assert response.status_code==200,response.text
    return response

def test_named_owner_abort_fences_inflight_cloud_result(pipeline_client,monkeypatch):
    client=pipeline_client
    setup_scene(client,monkeypatch)
    _authenticated_owner(client,monkeypatch)
    monkeypatch.setenv(cloud_processing.PROVIDER_ID_ENV,"audit-provider")
    monkeypatch.setenv(cloud_processing.NOTICE_VERSION_ENV,"notice-v1")
    _cloud_change(client,"P-PIPE",True)
    entered=threading.Event()
    release=threading.Event()
    class CloudAsr(_StubAsr):
        data_boundary="cloud"
        provider_id="audit-provider"
        def transcribe(self,*args):
            entered.set()
            assert release.wait(timeout=15)
            return super().transcribe(*args)
    monkeypatch.setattr(asr,"get_engine",lambda:CloudAsr("我喜欢晒太阳"))
    _use_reply_stub(monkeypatch,"晒太阳舒服呀。")
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending=pool.submit(_create_reply,client,"S-PIPE",BODY)
        assert entered.wait(timeout=10)
        with Session(client.test_engine) as s:
            rt=s.get(SessionRuntimeState,"S-PIPE")
            revision=rt.revision if rt else 0
        aborted=client.post("/sessions/S-PIPE/abort",json={"reason_code":"clinical_safety","expected_revision":revision,"idempotency_key":"audit-abort-thread0001"})
        release.set()
        response=pending.result(timeout=20)
    with Session(client.test_engine) as s:
        rt=s.get(SessionRuntimeState,"S-PIPE")
        rows=list(s.exec(select(RapportUtteranceEvent)))
    assert aborted.status_code==200 and rt.status=="aborted"
    assert response.status_code==409 and len(rows)==0

def test_paused_session_never_starts_asr(pipeline_client,monkeypatch):
    client=pipeline_client
    setup_scene(client,monkeypatch)
    paused=client.post("/sessions/S-PIPE/pause")
    assert paused.status_code==200,paused.text
    asr_engine=_StubAsr("我喜欢晒太阳")
    monkeypatch.setattr(asr,"get_engine",lambda:asr_engine)
    _use_reply_stub(monkeypatch,"晒太阳舒服呀。")
    response=_create_reply(client,"S-PIPE",BODY)
    assert response.status_code==409 and asr_engine.calls==0

AGGREGATE={"locked_turn_count":1,"weeks_covered":[2],"score_groups":[{"task_type":"单要素","response_role":"命名","locked_turns":1,"mean_locked_score":1.0}],"prompt_level_counts":{"0":1}}

def _questionnaire_scene(client,monkeypatch):
    setup_scene(client,monkeypatch)
    _authenticated_owner(client,monkeypatch)
    monkeypatch.setenv(cloud_processing.PROVIDER_ID_ENV,"aliyun-dashscope")
    monkeypatch.setenv(cloud_processing.NOTICE_VERSION_ENV,"notice-v1")
    _cloud_change(client,"P-PIPE",True)
    created=client.post("/patients/P-PIPE/questionnaire-records",json={"questionnaire_id":"sfacs_v1","phase_label":"前测"})
    assert created.status_code==200,created.text
    monkeypatch.setenv("DASHSCOPE_API_KEY","synthetic-stub-key-no-network")
    calls=[]
    monkeypatch.setattr(questionnaire_ai_draft,"_call_llm",lambda prompt:calls.append(prompt) or json.dumps({"drafts":{"sfacs_01":{"value":"7","rationale":"synthetic"}}}))
    monkeypatch.setattr(questionnaire_ai_draft,"build_evidence",lambda *_:AGGREGATE)
    return created.json()["record_id"],calls

def test_questionnaire_stale_notice_never_sends(pipeline_client,monkeypatch):
    client=pipeline_client
    record_id,calls=_questionnaire_scene(client,monkeypatch)
    monkeypatch.setenv(cloud_processing.NOTICE_VERSION_ENV,"notice-v2")
    response=client.post(f"/questionnaire-records/{record_id}/ai-draft")
    assert response.status_code==200 and len(calls)==0
    assert response.json()["ai_draft_status"]=="unavailable_not_authorized"

def test_questionnaire_revocation_wins_before_send(pipeline_client,monkeypatch):
    client=pipeline_client
    record_id,calls=_questionnaire_scene(client,monkeypatch)
    revoked=[]
    def evidence_then_revoke(*_):
        revoked.append(_cloud_change(client,"P-PIPE",False).status_code)
        return AGGREGATE
    monkeypatch.setattr(questionnaire_ai_draft,"build_evidence",evidence_then_revoke)
    response=client.post(f"/questionnaire-records/{record_id}/ai-draft")
    assert revoked==[200] and len(calls)==0
    assert response.status_code==200 and response.json()["ai_draft_status"]=="unavailable_not_authorized"


def test_questionnaire_save_and_lock_share_real_transaction(pipeline_client,monkeypatch):
    client=pipeline_client
    setup_scene(client,monkeypatch)
    _authenticated_owner(client,monkeypatch)
    created=client.post("/patients/P-PIPE/questionnaire-records",json={"questionnaire_id":"gds15_v1","phase_label":"前测"})
    assert created.status_code==200,created.text
    rid=created.json()["record_id"]
    seeded=client.put(f"/questionnaire-records/{rid}/values",json={"values":_gds_values_payload("是")})
    assert seeded.status_code==200,seeded.text
    checked_draft=threading.Event()
    release_save=threading.Event()
    original_validate=questionnaires.validate_value_write
    def delayed_validate(definition,item,field,value):
        if item=="gds_01" and value=="否":
            checked_draft.set()
            assert release_save.wait(timeout=15)
        return original_validate(definition,item,field,value)
    monkeypatch.setattr(questionnaires,"validate_value_write",delayed_validate)
    with ThreadPoolExecutor(max_workers=2) as pool:
        pending=pool.submit(client.put,f"/questionnaire-records/{rid}/values",json={"values":[{"item_key":"gds_01","field_key":"value","value":"否"}]})
        assert checked_draft.wait(timeout=10)
        lock_done=threading.Event()
        def lock_record():
            result=client.post(f"/questionnaire-records/{rid}/lock")
            lock_done.set()
            return result
        locking=pool.submit(lock_record)
        assert not lock_done.wait(timeout=.15), "lock passed an in-flight save"
        release_save.set()
        saved=pending.result(timeout=20)
        locked=locking.result(timeout=20)
    body=locked.json()
    actual=questionnaires.compute_scoring(GDS,{(v["item_key"],v["field_key"]):v["final_value"] for v in body["values"]})
    assert locked.status_code==200 and saved.status_code==200
    assert body["status"]=="locked"
    assert body["computed_total"]==actual["computed_total"]==12

def test_questionnaire_regrant_cannot_adopt_old_provider_result(pipeline_client, monkeypatch):
    client = pipeline_client
    record_id, calls = _questionnaire_scene(client, monkeypatch)
    original = questionnaire_ai_draft.generate_draft
    def rotate_after_provider(s, patient, definition):
        outcome = original(s, patient, definition)
        assert outcome.status == "generated"
        _cloud_change(client, "P-PIPE", False)
        _cloud_change(client, "P-PIPE", True)
        return outcome
    monkeypatch.setattr(questionnaire_ai_draft, "generate_draft", rotate_after_provider)
    response = client.post(f"/questionnaire-records/{record_id}/ai-draft")
    assert response.status_code == 409
    assert len(calls) == 1
    from app.models import QuestionnaireItemValue, QuestionnaireRecord
    with Session(client.test_engine) as s:
        assert s.get(QuestionnaireRecord, record_id).ai_draft_status == "none"
        assert not list(s.exec(select(QuestionnaireItemValue).where(
            QuestionnaireItemValue.record_id == record_id)))


def test_sqlite_write_fence_waits_for_independent_process_before_authority_read(tmp_path):
    """Independent interpreters cannot share the Python RLock used by threads."""
    import subprocess
    import sys
    from sqlalchemy import create_engine, text
    from app import governance_lock

    path = tmp_path / "authority.sqlite"
    engine = create_engine(f"sqlite:///{path}")
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE authority (id INTEGER PRIMARY KEY, status TEXT)"))
        conn.execute(text("INSERT INTO authority VALUES (1, 'draft')"))
    child = r'''
import sys
from sqlalchemy import create_engine, text
from sqlmodel import Session
from app import governance_lock
engine = create_engine(sys.argv[1])
with Session(engine) as s:
    with governance_lock.subject_fence(s, 'synthetic'):
        governance_lock.begin_sqlite_write_fence(s)
        print('LOCKED', flush=True)
        sys.stdin.readline()
        s.execute(text("UPDATE authority SET status='locked' WHERE id=1"))
        s.commit()
'''
    process = subprocess.Popen(
        [sys.executable, "-c", child, str(engine.url)], stdin=subprocess.PIPE,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        assert process.stdout.readline().strip() == "LOCKED"
        acquired = threading.Event()
        def read_after_writer():
            with Session(engine) as s:
                with governance_lock.subject_fence(s, "synthetic"):
                    governance_lock.begin_sqlite_write_fence(s)
                    acquired.set()
                    result = s.execute(text("SELECT status FROM authority WHERE id=1")).scalar_one()
                    s.rollback()
                    return result
        with ThreadPoolExecutor(max_workers=1) as pool:
            result = pool.submit(read_after_writer)
            assert not acquired.wait(timeout=.2)
            process.stdin.write("release\n")
            process.stdin.flush()
            assert result.result(timeout=10) == "locked"
        assert process.wait(timeout=10) == 0
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)


def test_independent_process_provider_and_http_revoke_share_egress_fence(
        pipeline_client, monkeypatch):
    import os
    import subprocess
    import sys

    client = pipeline_client
    setup_scene(client, monkeypatch)
    _authenticated_owner(client, monkeypatch)
    monkeypatch.setenv(cloud_processing.PROVIDER_ID_ENV, "audit-provider")
    monkeypatch.setenv(cloud_processing.NOTICE_VERSION_ENV, "notice-v1")
    _cloud_change(client, "P-PIPE", True)
    child = r'''
import sys, signal
signal.alarm(30)
from app import db
import app.main as main
provider = type('SyntheticProvider', (), {'data_boundary': 'cloud', 'provider_id': 'audit-provider'})()
with main._serialized_cloud_provider_call(session_id='S-PIPE', patient_id='P-PIPE', provider=provider, bind=db.engine):
    print('PROVIDER_ENTERED', flush=True)
    sys.stdin.readline()
print('PROVIDER_FINISHED', flush=True)
sys.stdin.readline()
try:
    with main._serialized_cloud_provider_call(session_id='S-PIPE', patient_id='P-PIPE', provider=provider, bind=db.engine):
        print('UNEXPECTED_NEW_EGRESS', flush=True)
except main._CloudEgressNotAuthorized:
    print('NEW_EGRESS_REJECTED', flush=True)
'''
    env = dict(os.environ, DATABASE_URL=str(client.test_engine.url))
    process = subprocess.Popen(
        [sys.executable, "-c", child], env=env, stdin=subprocess.PIPE,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        assert process.stdout.readline().strip() == "PROVIDER_ENTERED"
        with ThreadPoolExecutor(max_workers=1) as pool:
            revoked = threading.Event()
            def revoke():
                response = _cloud_change(client, "P-PIPE", False)
                revoked.set()
                return response
            pending = pool.submit(revoke)
            assert not revoked.wait(timeout=.2), "revocation passed an active provider in another process"
            # The egress fence owns no SQLite writer and no clinical stop lock.
            with Session(client.test_engine) as s:
                runtime = s.get(SessionRuntimeState, "S-PIPE")
                revision = runtime.revision if runtime else 0
            aborted = client.post("/sessions/S-PIPE/abort", json={
                "reason_code": "clinical_safety", "expected_revision": revision,
                "idempotency_key": "cross-process-abort-001"})
            assert aborted.status_code == 200, aborted.text
            process.stdin.write("finish provider\n")
            process.stdin.flush()
            assert process.stdout.readline().strip() == "PROVIDER_FINISHED"
            assert pending.result(timeout=10).status_code == 200
        process.stdin.write("attempt after revoke\n")
        process.stdin.flush()
        assert process.stdout.readline().strip() == "NEW_EGRESS_REJECTED"
        assert process.wait(timeout=10) == 0
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)


def test_sqlite_egress_file_fence_is_reentrant_and_releases_after_exception(tmp_path):
    import pytest
    from sqlalchemy import create_engine
    engine = create_engine(f"sqlite:///{tmp_path / 'egress.sqlite'}")
    with pytest.raises(ValueError, match="synthetic"):
        with cloud_processing.serialized_subject_egress("synthetic", bind=engine):
            with cloud_processing.serialized_subject_egress("synthetic", bind=engine):
                raise ValueError("synthetic")
    with cloud_processing.serialized_subject_egress("synthetic", bind=engine):
        pass
    lock_files = list(tmp_path.glob(".cloud-egress-*/stripe-*.lock"))
    assert len(lock_files) == 1
    assert lock_files[0].stat().st_mode & 0o777 == 0o600
    assert lock_files[0].parent.stat().st_mode & 0o777 == 0o700
