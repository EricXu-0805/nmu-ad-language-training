"""Isolated HTTP rehearsal from simulated enrolment through reviewed CSV export.

This is TestClient plus deterministic local provider responses and synthetic
WebM audio, not a browser, human speech, clinical approval, or tablet validation.
The canonical demo20 plan is kept intact: positions 1–18 receive explicit skip
receipts, and positions 19/20 follow the full answer/review/closeout path. No
session, answer, score, readiness result or terminal state is seeded directly.
Only three temporary account identities are bootstrapped in the empty database.

Run in a fresh process: python -m harness.isolated_workflow_rehearsal --root DIR
DIR must already exist, be empty/private, and be outside the checkout. All
mutable stores, credentials and generated exports stay in that isolated root.
No production .env is loaded. Requires the application's runtime dependencies
but no pytest, browser, audio device, network service, or ffmpeg at runtime.
"""
from __future__ import annotations

import argparse
import base64
import csv
import hashlib
import json
import os
from pathlib import Path
import secrets
import stat
import sys
import traceback
from typing import Any

from .tts_ack_harness import (
    DEMO_PROFILE_VERSION, HarnessConfigError, install, migrate, resolve_config,
)

SCHEMA = "nmu-isolated-workflow-rehearsal.v1"
PATIENT_ID = "SYN-WORKFLOW-001"

# Generated from deterministic_wav("isolated synthetic tone", duration_seconds=0.6)
# with ffmpeg/libopus at 24 kbit/s; ffprobe: mono Opus 48 kHz, container 0.608s
# (codec padding included). Decode to PCM verified 0.600s. No human audio.
SYNTHETIC_WEBM_SHA256 = "094f7031440442589255dc52081b6305582fe9f1d49f2e1f0e68ecd7c75dd243"
SYNTHETIC_WEBM_BASE64 = (
    "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"
)


def configure_private_root(root: Path):
    """Reject unsafe input before importing any app module or touching its DB."""
    if not __debug__:
        raise HarnessConfigError("rehearsal assertions require Python without -O")
    if any(name == "app" or name.startswith("app.") for name in sys.modules):
        raise HarnessConfigError("rehearsal requires a fresh process before app import")
    root = root.absolute()
    if root.is_symlink() or not root.is_dir():
        raise HarnessConfigError("root must be an existing real directory")
    if stat.S_IMODE(root.stat().st_mode) & 0o077:
        raise HarnessConfigError("root must be private (mode 700)")
    if any(root.iterdir()):
        raise HarnessConfigError("root must be empty; existing data will never be reused")
    password = secrets.token_urlsafe(24)
    os.environ.update({
        "NMU_HARNESS_ROOT": str(root),
        "DATABASE_URL": f"sqlite:///{root}/workflow.db",
        "AUDIO_DIR": str(root / "audio"),
        "NMU_HARNESS_TTS_CACHE_DIR": str(root / "tts-cache"),
        "NMU_HARNESS_TTS_MODE": "synthetic",
        "NMU_HARNESS_ACTOR": "rehearsal-admin",
        "NMU_HARNESS_PASSWORD": password,
        "REQUIRE_AUTH": "1", "CONSOLE_PIN": "93615827",
        "ALLOW_SIMULATION_DATA": "1", "ENABLE_AUTOPILOT_P0A_SIMULATION": "1",
        "TTS_ENGINE": "null", "ASR_ENGINE": "null", "LLM_JUDGE": "off",
        "DEIDENTIFICATION_KEY": secrets.token_urlsafe(48),
        "DEIDENTIFICATION_KEY_ID": "isolated-workflow-test",
    })
    for key in ("DASHSCOPE_API_KEY", "PROVIDER_READINESS_FINGERPRINT_KEY",
                "ENABLE_AUTOPILOT_REAL_SESSIONS", "NMU_TEST_ALLOW_DIRECT_SESSION_CREATE",
                "NMU_HARNESS_TTS_DURATION_PROFILE"):
        os.environ.pop(key, None)
    config = resolve_config()
    if config.tts_mode != "synthetic":
        raise HarnessConfigError("only offline synthetic providers are permitted")
    return config


def synthetic_webm(root: Path) -> bytes:
    payload = base64.b64decode(SYNTHETIC_WEBM_BASE64, validate=True)
    if (len(payload) != 2578 or not payload.startswith(b"\x1a\x45\xdf\xa3")
            or hashlib.sha256(payload).hexdigest() != SYNTHETIC_WEBM_SHA256):
        raise HarnessConfigError("embedded synthetic WebM fixture digest mismatch")
    (root / "synthetic.webm").write_bytes(payload)
    return payload


def run(root: Path) -> dict[str, Any]:
    config = configure_private_root(root)
    audio = synthetic_webm(config.root)
    migrate(config)
    app = install(config)
    # Imports below this line can only bind to the prevalidated isolated DB.
    from fastapi.testclient import TestClient
    from sqlmodel import Session, select
    from app import asr, auth, autopilot_orchestration, content, db, visit_plan_service
    from app import main as main_module
    from app.models import ResearchUser

    accounts = {role: f"rehearsal-{role.replace('_', '-')}"
                for role in ("admin", "researcher", "data_steward")}
    with Session(db.engine) as session:
        for role, username in accounts.items():
            session.add(ResearchUser(username=username, display_id=username, role=role,
                                     password_hash=auth.hash_password(config.actor_password)))
        session.commit()
        if len(list(session.exec(select(ResearchUser)))) != 3:
            raise AssertionError("fresh database account bootstrap mismatch")

    clients = {role: TestClient(app) for role in accounts}
    device = TestClient(app)
    calls: list[dict[str, Any]] = []
    def request(client, method: str, path: str, *, expected: int = 200, **kwargs):
        response = client.request(method, path, **kwargs)
        calls.append({"method": method, "path": path, "status": response.status_code})
        if response.status_code != expected:
            raise AssertionError(f"{method} {path}: expected {expected}, got {response.status_code}: {response.text[:1200]}")
        return response
    def api(client, method: str, path: str, **kwargs):
        return request(client, method, path, **kwargs).json()
    try:
        for role, client in clients.items():
            api(client, "POST", "/auth/login", json={"username": accounts[role], "password": config.actor_password})
            csrf = client.cookies.get(auth.CSRF_COOKIE_NAME)
            assert csrf, "authenticated client has no CSRF proof"
            client.headers["X-CSRF-Token"] = csrf
        admin, researcher, steward = (clients[role] for role in ("admin", "researcher", "data_steward"))
        class ScriptedAsr:
            version = "isolated-workflow-scripted-asr/1"
            cloud = False
            data_boundary = "local"
            provider_id = None
            text: str | None = None
            def transcribe(self, audio_bytes, hotwords):
                if self.text is None:
                    text = next((word for word in hotwords if isinstance(word, str) and word.strip()), "synthetic probe")
                else:
                    assert audio_bytes == audio
                    text = self.text
                return asr.AsrResult(text, 1.0 if text else None, self.version, hotword_hit=bool(text))
        scripted_asr = ScriptedAsr()
        asr.get_engine = lambda: scripted_asr
        probe = api(admin, "POST", "/ai/provider-readiness/probe")
        assert probe["start_allowed"] is True, probe
        patient = api(researcher, "POST", "/patients", json={
            "patient_id": PATIENT_ID, "is_simulation_subject": True,
            "consent_status": "已同意", "recording_allowed": True, "secondary_use_allowed": True,
        })
        assert patient["is_simulation_subject"] is True
        plan = api(researcher, "POST", "/visit-plans", json={
            "idempotency_key": "workflow-plan-create-0001", "patient_id": PATIENT_ID,
            "scheduled_date": visit_plan_service._research_today().isoformat(),
            "week_no": 2, "phase_type": "正式训练", "event_line": "正式训练",
            "autopilot_profile_version_id": DEMO_PROFILE_VERSION,
        })
        plan_id = plan["plan_id"]
        for action in ("approve", "start"):
            plan = api(researcher, "POST", f"/visit-plans/{plan_id}/{action}", json={
                "idempotency_key": f"workflow-plan-{action}-0001", "expected_revision": plan["revision"],
            })
        sid = plan["session_id"]
        assert sid and plan["data_classification"] == "simulation"
        prefix = f"/sessions/{sid}"
        api(researcher, "PUT", "/live/state", json={"kind": "session", "payload": {
            "sessionId": sid, "weekNo": 2, "eventLine": "正式训练", "mode": "task",
            "itemBankVersionId": plan["item_bank_version_id"],
        }})
        paired = api(device, "POST", "/device/pair", headers={"X-Console-Pin": os.environ["CONSOLE_PIN"]},
                     json={"deviceId": "isolated-workflow-device-0001"})
        device.headers["X-Device-Capability"] = paired["capability"]
        state = api(researcher, "POST", prefix + "/autopilot/start", json={
            "idempotency_key": "workflow-autopilot-start-0001", "expected_revision": 0,
            "start_presentation_order": 19, "skip_reason_code": "trained_in_prior_sitting",
            "skip_note": "隔离演练：显式记录前十八题跳过，不代表实际完成",
        })
        assert state["status"] == "waiting_tts"
        def next_command():
            return api(device, "GET", prefix + "/autopilot/next")
        def pause_and_drain(command):
            api(researcher, "POST", prefix + "/pause")
            return api(device, "POST", prefix + f"/autopilot/commands/{command['command_key']}/drain-ack")
        before_pause = next_command()
        drained = pause_and_drain(before_pause)
        resumed = api(researcher, "POST", prefix + "/autopilot/resume", json={
            "idempotency_key": "workflow-resume-0001", "expected_revision": drained["state_revision"],
        })
        resumed_command = next_command()
        assert resumed["status"] == "waiting_tts"
        assert resumed_command["item_ref"] == before_pause["item_ref"]
        assert resumed_command["command_key"] != before_pause["command_key"]

        scheduled: list[str] = []
        autopilot_orchestration.submit = lambda session_id, _worker: scheduled.append(session_id) or True
        seq = 0
        def ack(command, ack_type: str, **facts):
            nonlocal seq
            seq += 1
            return api(device, "POST", prefix + f"/autopilot/commands/{command['command_key']}/acks", json={
                "idempotency_key": f"workflow-device-ack-{seq:04}", "ack_type": ack_type,
                "device_event_seq": seq, "command_revision": command["command_revision"],
                "control_generation": command["control_generation"], "runner_generation": command["runner_generation"],
                **facts,
            })
        def play(command):
            # Exercise real synthesis-serving evidence; the playback ACK is simulated.
            response = request(device, "POST", prefix + f"/autopilot/commands/{command['command_key']}/tts")
            assert response.content.startswith(b"RIFF"), "synthetic TTS did not serve a WAV"
            ack(command, "tts_started", media_duration_ms=600)
            command = next_command()
            assert command["state"] == "started"
            return ack(command, "tts_ended", media_ended=True, media_duration_ms=600)
        def answer(command, text: str):
            permission = api(device, "POST", prefix + f"/autopilot/commands/{command['command_key']}/recording-authorization")
            assert permission["recording_authorized"] is True
            ack(command, "record_started", mime_type="audio/webm;codecs=opus")
            command = next_command()
            audio_id = command["payload"]["raw_audio_id"]
            uploaded = api(device, "PUT", f"/audio/{audio_id}/blob", content=audio,
                           headers={"Content-Type": "audio/webm"})
            saved = api(device, "PUT", "/live/state", json={"kind": "audioSaved", "payload": {
                "rawAudioId": audio_id, "durationSeconds": 0.6, "byteCount": uploaded["bytes"],
                "checksum": uploaded["checksum"], "turnKey": command["payload"]["turn_ref"],
                "sessionId": sid, "containsDirectIdentifier": False,
            }})
            stopped = ack(command, "record_stopped", stop_reason="user_done", raw_audio_id=audio_id,
                          receipt_server_seq=saved["audioReceipt"]["serverSeq"],
                          checksum=uploaded["checksum"], byte_count=uploaded["bytes"], duration_seconds=0.6)
            assert stopped["status"] == "processing_attempt"
            assert scheduled and scheduled.pop(0) == sid
            scripted_asr.text = text
            main_module._run_p0a_attempt_worker(sid)
        first_record = play(resumed_command)["command"]
        assert first_record["kind"] == "record"
        answer(first_record, "")
        cue = next_command()
        assert cue["payload"]["purpose"] == "cue"
        before_adjudication = api(researcher, "GET", prefix + "/attempts")["attempts"]
        assert len(before_adjudication) == 1 and before_adjudication[0]["asr_text"] == ""
        original_ai = before_adjudication[0]["operational_answer_type"]
        drained = pause_and_drain(cue)
        adjudication_body = {"idempotency_key": "workflow-adjudicate-0001",
            "expected_revision": drained["state_revision"], "kind": "confirmed_correct",
            "reason_code": "asr_misrecognized", "note": "隔离演练：模拟识别为空，研究者另行确认"}
        adjudicated = api(researcher, "POST", prefix + "/autopilot/adjudicate", json=adjudication_body)
        assert api(researcher, "POST", prefix + "/autopilot/adjudicate", json=adjudication_body) == adjudicated
        assert api(researcher, "GET", prefix + "/attempts")["attempts"] == before_adjudication
        assert adjudicated["status"] == "waiting_tts", adjudicated
        final_question = next_command()
        assert final_question["payload"]["purpose"] == "question"
        # Researcher receipt owns canonical identity; repeated question wording is not an item key.
        bank = content.load_item_bank(content.CONTENT_DIR / "item_bank_v1.json")
        final_item = next(item for item in bank.single_element
                          if item["item_id"] == adjudicated["position_item_id"])
        assert final_item["initial_prompt"] == final_question["payload"]["speech_text"]
        final_record = play(final_question)["command"]
        answer(final_record, final_item["target_word"])
        feedback = next_command()
        assert feedback["payload"]["purpose"] == "feedback"
        assert play(feedback)["status"] == "scope_completed"
        journal = api(researcher, "GET", prefix + "/journal")
        assert len(journal["items"]) == 2 and len(journal["turns"]) == 2
        item_map = {item["id"]: item for item in journal["items"]}
        assert sorted(item["presentation_order"] for item in journal["items"]) == [19, 20]
        first_turn = next(turn for turn in journal["turns"] if item_map[turn["item_event_id"]]["presentation_order"] == 19)
        assert first_turn["ai_answer_type"] == original_ai and first_turn["score_locked"] is False
        for turn in journal["turns"]:
            item = item_map[turn["item_event_id"]]
            frozen = next(row for row in bank.single_element if row["item_id"] == item["item_id"])
            confirmed = api(researcher, "PATCH", f"/turns/{turn['id']}/confirm", json={
                "confirmed_response_text": frozen["target_word"], "expected_revision": turn["confirmation_revision"],
                "idempotency_key": f"workflow-confirm-{turn['id']:04}",
            })
            assert confirmed["asr_text"] == turn["asr_text"]
            locked = api(researcher, "PATCH", f"/turns/{turn['id']}/lock", json={
                "reviewer_id": accounts["researcher"], "element_value": 1, "prompt_level": turn["prompt_level"],
            })
            assert locked["score_locked"] is True and locked["reviewed_score"] == 1
        closeout_body = {"idempotency_key": "workflow-closeout-0001", "expected_revision": 0,
                         "report_status": "no_additional_observation"}
        closeout = api(researcher, "PUT", prefix + "/closeout", json=closeout_body)
        closeout_replay = api(researcher, "PUT", prefix + "/closeout", json=closeout_body)
        assert closeout["idempotent"] is False and closeout_replay["idempotent"] is True
        assert closeout_replay["revision"] == closeout["revision"] == 1
        completed = api(researcher, "POST", prefix + "/complete")
        assert completed["status"] == "completed"
        assert api(researcher, "GET", prefix + "/closeout")["locked"] is True
        export_body = {"idempotency_key": "workflow-export-" + secrets.token_hex(24)}
        exported = api(steward, "POST", prefix + "/export", json=export_body)
        replayed = api(steward, "POST", prefix + "/export", json=export_body)
        assert replayed == exported and exported["status"] == "published"
        assert api(steward, "GET", f"/exports/{exported['batch_id']}")["batch_id"] == exported["batch_id"]
        def csv_rows(sheet):
            candidates = list(config.export_dir.rglob(f"{sheet}.csv"))
            assert len(candidates) == 1, (sheet, [str(path) for path in candidates])
            with candidates[0].open(encoding="utf-8-sig", newline="") as handle:
                return list(csv.DictReader(handle))
        turns = csv_rows("turns")
        decisions = csv_rows("adjudications")
        assert sorted(int(row["presentation_order"]) for row in turns) == [19, 20]
        assert all(row["score_locked"].lower() == "true" and float(row["reviewed_score"]) == 1 for row in turns)
        skipped = [row for row in decisions if row["kind"] == "skipped"]
        assert sorted(int(row["presentation_order"]) for row in skipped) == list(range(1, 19))
        assert all(row["reason_code"] == "trained_in_prior_sitting" for row in skipped)
        manual = [row for row in decisions if row["kind"] == "confirmed_correct"]
        assert len(manual) == 1 and manual[0]["presentation_order"] == "19"
        assert manual[0]["reason_code"] == "asr_misrecognized"
        first_export = next(row for row in turns if row["presentation_order"] == "19")
        assert first_export["ai_answer_type"] == original_ai and float(first_export["ai_score"]) == 0
        assert first_export["adjudication_kind"] == "confirmed_correct"
        session_csv = csv_rows("session")
        assert len(session_csv) == 1 and session_csv[0]["data_classification"] == "simulation"
        receipt = {
            "schema": SCHEMA, "status": "passed", "evidence_level": "isolated_http_testclient",
            "browser_or_physical_tablet_validated": False, "clinical_approval_created": False,
            "scope": "start_at_19_complete_review_closeout_export",
            "actual_answer_positions": [19, 20], "explicitly_skipped_positions": list(range(1, 19)),
            "raw_asr_preserved_after_adjudication": True, "original_ai_answer_type": original_ai,
            "locked_reviewed_turns": len(turns), "manual_adjudication_position": 19,
            "pause_resume_passed": True, "completed_status": completed["status"],
            "export_batch_id": exported["batch_id"], "export_replay_same_batch": True,
            "export_sheet_counts": exported["sheet_counts"],
            "synthetic_webm_sha256": hashlib.sha256(audio).hexdigest(),
            "http_request_count": len(calls), "isolated_root": str(config.root),
        }
        (config.root / "http-events.json").write_text(json.dumps(calls, ensure_ascii=False, indent=2), encoding="utf-8")
        (config.root / "result.json").write_text(json.dumps(receipt, ensure_ascii=False, indent=2), encoding="utf-8")
        return receipt
    finally:
        for client in [*clients.values(), device]:
            client.close()
        db.engine.dispose()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        receipt = run(args.root)
    except (HarnessConfigError, AssertionError) as error:
        location = traceback.extract_tb(error.__traceback__)[-1]
        print(f"isolated workflow rehearsal failed at {Path(location.filename).name}:{location.lineno}: {error or location.line}", file=sys.stderr)
        return 1
    print(json.dumps(receipt, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
