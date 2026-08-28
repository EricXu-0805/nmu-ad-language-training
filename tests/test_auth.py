"""M1-D 认证层测试：账号会话 + PIN 保底 + 限速锁定 + fail-closed + 身份审计。

cookie 要穿过中间件(用 db.engine)又要被路由(get_session)读到,二者须同库：
故本文件 monkeypatch app.db.engine 到同一 StaticPool 内存库,并用 `with TestClient`
触发 lifespan(走真实启动路径,含 fail-closed 断言)。
"""
import threading
from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

from app import audio_store, auth, db
from app.main import app
from app.models import (AttemptEvent, AudioAssetRow, AuthSession, ResearchUser,
                        SessionRuntimeState)


@pytest.fixture
def real_db(monkeypatch):
    eng = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    monkeypatch.setattr(db, "engine", eng)
    SQLModel.metadata.create_all(eng)
    return eng


def _add_user(eng, username="u", pw="password1", display_id="R-U", role="researcher", disabled=False):
    with Session(eng) as s:
        s.add(ResearchUser(username=username, display_id=display_id,
                           password_hash=auth.hash_password(pw), role=role,
                           disabled=disabled, created_at=datetime.now()))
        s.commit()


def _enable_csrf(client: TestClient) -> dict[str, str]:
    token = client.cookies.get(auth.CSRF_COOKIE_NAME)
    assert token
    headers = {"X-CSRF-Token": token}
    client.headers.update(headers)
    return headers


# ---------------- 口令散列 ----------------
def test_password_hash_roundtrip():
    h = auth.hash_password("correct horse")
    assert h.startswith("pbkdf2_sha256$") and "correct horse" not in h
    assert auth.verify_password("correct horse", h)
    assert not auth.verify_password("wrong", h)
    assert not auth.verify_password("correct horse", "garbage")


def test_password_min_length():
    with pytest.raises(ValueError):
        auth.hash_password("short")


# ---------------- 登录 / 会话 / 登出 ----------------
def test_login_sets_cookie_and_me(real_db, monkeypatch):
    monkeypatch.setenv("REQUIRE_AUTH", "1")
    monkeypatch.setenv("CONSOLE_PIN", "24681024")
    _add_user(real_db)
    with TestClient(app) as client:
        assert client.get("/auth/me").status_code == 401
        bad = client.post("/auth/login", json={"username": "u", "password": "nope"})
        assert bad.status_code == 401
        ok = client.post("/auth/login", json={"username": "u", "password": "password1"})
        assert ok.status_code == 200 and ok.json()["display_id"] == "R-U"
        assert client.cookies.get(auth.CSRF_COOKIE_NAME)
        me = client.get("/auth/me")
        assert me.status_code == 200 and me.json()["username"] == "u"
        _enable_csrf(client)
        client.post("/auth/logout")
        assert client.get("/auth/me").status_code == 401


def test_disabled_user_cannot_login(real_db, monkeypatch):
    monkeypatch.setenv("REQUIRE_AUTH", "1")
    monkeypatch.setenv("CONSOLE_PIN", "24681024")
    _add_user(real_db, disabled=True)
    with TestClient(app) as client:
        assert client.post("/auth/login", json={"username": "u", "password": "password1"}).status_code == 401


# ---------------- cookie 穿过中间件授权写操作 ----------------
def test_cookie_authorizes_protected_write(real_db, monkeypatch):
    monkeypatch.setenv("REQUIRE_AUTH", "1")
    monkeypatch.setenv("CONSOLE_PIN", "24681024")  # console 仍只靠账号；PIN 仅供 patient 配对
    _add_user(real_db)
    with TestClient(app) as client:
        assert client.post("/patients", json={"patient_id": "P1"}).status_code == 401
        assert client.get("/patients").status_code == 401     # 名单读口也须认证(修复的泄露洞)
        client.post("/auth/login", json={"username": "u", "password": "password1"})
        _enable_csrf(client)
        assert client.post("/patients", json={"patient_id": "P1"}).status_code == 200
        assert client.get("/patients").status_code == 200


# ---------------- 身份审计：登录账号 → reviewer_id 权威 ----------------
def test_locked_score_stamps_authenticated_identity(real_db, monkeypatch):
    monkeypatch.setenv("REQUIRE_AUTH", "1")
    monkeypatch.setenv("CONSOLE_PIN", "24681024")
    _add_user(real_db, display_id="丁老师")
    with TestClient(app) as client:
        client.post("/auth/login", json={"username": "u", "password": "password1"})
        _enable_csrf(client)
        client.post("/patients", json={"patient_id": "PL", "consent_status": "已同意",
                                       "consent_type": "本人同意", "mandarin_eligible": True,
                                       "recording_allowed": True,
                                       "is_simulation_subject": True})
        client.post("/sessions", json={"session_id": "SL", "patient_id": "PL", "week_no": 2,
                                       "phase_type": "正式训练", "event_line": "正式训练",
                                       "item_bank_version_id": "wk2-v1-20260707",
                                       "is_simulation": True,
                                       "trainer_id": "丁老师"})
        ie = client.post("/sessions/SL/items", json={"item_id": "SE_锚", "task_type": "单要素"}).json()
        # Model the server/autopilot-created registration; the account path is
        # intentionally limited to recovering this exact immutable ACK.
        audio_bytes = b"\x1a\x45\xdf\xa3auth-audio"
        audio_path, checksum = audio_store.save_blob(
            "auth-turn-audio", audio_bytes, "audio/webm")
        with Session(real_db) as session:
            session.add(AudioAssetRow(
                raw_audio_id="auth-turn-audio", session_id="SL",
                turn_key="SE_锚#1", is_simulation=True,
                data_classification="simulation",
                audio_format=audio_path.suffix.lstrip("."),
                checksum=checksum,
                byte_count=len(audio_bytes),
                uploaded_at=datetime.now(),
            ))
            session.commit()
        assert client.post("/audio", json={
            "raw_audio_id": "auth-turn-audio", "session_id": "SL",
            "turn_key": "SE_锚#1",
        }).status_code == 200
        assert client.put("/audio/auth-turn-audio/blob",
                          content=audio_bytes,
                          headers={"content-type": "audio/webm"}).status_code == 200
        with Session(real_db) as session:
            session.add(AttemptEvent(
                session_id="SL", item_id="SE_锚", turn_seq=1,
                response_role="命名", attempt_seq=1, raw_audio_id="auth-turn-audio",
                prompt_level=0, asr_text="锚", asr_confidence=.9,
                asr_engine_version="test-asr", operational_answer_type="正确",
                operational_score=1, operational_needs_review=False,
                judge_mode="规则确定式", judge_engine_version="rule-test",
                processing_status="completed", is_simulation=True,
            ))
            session.commit()
        te = client.post(f"/items/{ie['id']}/turns",
                         json={"turn_seq": 1, "response_role": "命名",
                               "raw_audio_id": "auth-turn-audio"}).json()
        with Session(real_db) as session:
            session.add(SessionRuntimeState(
                session_id="SL", status="intervention_completed",
                revision=1, intervention_completed_at=datetime.now(),
            ))
            session.commit()
        client.patch(f"/turns/{te['id']}/confirm", json={
            "confirmed_response_text": "锚", "expected_revision": 0,
            "idempotency_key": "test-auth-confirm-0001",
        })
        locked = client.patch(f"/turns/{te['id']}/lock",
                              json={"reviewer_id": "冒名", "element_value": 1, "prompt_level": 0})
        assert locked.status_code == 200
        # 请求体自报 reviewer_id="冒名" 被登录身份覆盖 → 审计可信
        assert locked.json()["reviewer_id"] == "丁老师"


# ---------------- 限速锁定 ----------------
def test_bad_pin_lockout(real_db, monkeypatch):
    monkeypatch.setenv("CONSOLE_PIN", "24681024")
    monkeypatch.setenv("AUTH_FAIL_MAX", "3")
    _add_user(real_db)
    with TestClient(app) as client:
        bad = {"X-Console-Pin": "000000"}
        for _ in range(3):
            assert client.post("/device/pair", json={
                "deviceId": "lockout-device-000001",
            }, headers=bad).status_code == 401
        # 连败达阈值 → 锁定：即便这次给对 PIN 也先吃 429
        assert client.post("/device/pair", json={
            "deviceId": "lockout-device-000001",
        }, headers={"X-Console-Pin": "24681024"}).status_code == 429


def test_failure_state_sweeps_expired_entries_with_injected_clock(monkeypatch):
    monkeypatch.setenv("AUTH_FAIL_MAX", "2")
    monkeypatch.setenv("AUTH_FAIL_WINDOW_SECONDS", "5")
    monkeypatch.setenv("AUTH_LOCK_SECONDS", "7")
    monkeypatch.setenv("AUTH_GLOBAL_FAIL_MAX", "100")

    auth.record_failure("login:stale-window", now=10.0)
    auth.record_failure("pair:expired-lock", now=10.0)
    auth.record_failure("pair:expired-lock", now=10.0)
    assert auth.is_locked("pair:expired-lock", now=10.0)

    # Advancing the injected monotonic clock drives both window and lock expiry;
    # an unrelated lookup triggers the amortized full sweep.
    assert not auth.is_locked("login:unrelated", now=18.0)
    assert not auth.is_locked("pair:expired-lock", now=18.0)
    assert "login:stale-window" not in auth._FAILURES
    assert "pair:expired-lock" not in auth._LOCKED_UNTIL
    assert not auth._TRACKED_KEYS


def test_unique_ip_flood_is_lru_bounded_for_failures_and_locks(monkeypatch):
    monkeypatch.setenv("AUTH_TRACKED_KEYS_MAX", "4")
    monkeypatch.setenv("AUTH_FAIL_MAX", "2")
    monkeypatch.setenv("AUTH_GLOBAL_FAIL_MAX", "100")

    # Populate both tables with unique attacker-controlled identities.  The
    # union, not merely each individual dictionary, stays within the hard cap.
    auth.record_failure("login:failure-only", now=20.0)
    for index in range(12):
        key = f"pair:spoofed-{index}"
        auth.record_failure(key, now=20.0)
        auth.record_failure(key, now=20.0)

    assert len(auth._TRACKED_KEYS) <= 4
    assert len(set(auth._FAILURES) | set(auth._LOCKED_UNTIL)) <= 4
    assert set(auth._FAILURES) | set(auth._LOCKED_UNTIL) == set(auth._TRACKED_KEYS)
    # 只有还没锁上的那个会被腾掉;先到的四把锁一把不少地留着。表满之后新来的
    # 伪造来源不再入表——它们交给按 scope 的全局限速器,见下一条用例。
    assert "login:failure-only" not in auth._TRACKED_KEYS
    assert all(auth.is_locked(f"pair:spoofed-{index}", now=20.0) for index in range(4))
    assert "pair:spoofed-11" not in auth._LOCKED_UNTIL


def test_a_locked_attacker_cannot_flush_their_own_lock_by_flooding(monkeypatch):
    """锁定表满了也不能拿"解锁"去换"腾位"。

    这条对着的是真实攻击路径:X-Forwarded-For 的最左一跳客户端可写,所以
    _client_ip 看到的来源是攻击者自己填的。旧实现无条件 LRU 弹最旧,攻击者
    被锁之后灌 4096 个伪造来源就能把自己那把锁挤掉,锁定形同虚设。
    """
    monkeypatch.setenv("AUTH_TRACKED_KEYS_MAX", "3")
    monkeypatch.setenv("AUTH_FAIL_MAX", "2")
    monkeypatch.setenv("AUTH_LOCK_SECONDS", "300")
    monkeypatch.setenv("AUTH_GLOBAL_FAIL_MAX", "10000")

    auth.record_failure("pair:attacker", now=100.0)
    auth.record_failure("pair:attacker", now=100.0)
    assert auth.is_locked("pair:attacker", now=100.0)

    for index in range(200):
        forged = f"pair:forged-{index}"
        auth.record_failure(forged, now=100.0 + index * 0.01)
        auth.record_failure(forged, now=100.0 + index * 0.01)

    assert auth.is_locked("pair:attacker", now=100.0 + 200 * 0.01)
    assert len(auth._TRACKED_KEYS) <= 3


def test_distributed_failure_fallback_is_scoped_and_expires(monkeypatch):
    monkeypatch.setenv("AUTH_FAIL_MAX", "99")
    monkeypatch.setenv("AUTH_GLOBAL_FAIL_MAX", "3")
    monkeypatch.setenv("AUTH_GLOBAL_LOCK_SECONDS", "4")

    for index in range(3):
        auth.record_failure(f"login:distributed-{index}", now=30.0)

    # Many one-shot IPs now stop before another PBKDF2.  Pairing keeps its own
    # failure domain, so a login spray cannot silently disable bedside pairing.
    assert auth.is_locked("login:new-address", now=30.0)
    assert not auth.is_locked("pair:new-address", now=30.0)
    assert not auth.is_locked("login:new-address", now=34.1)


def test_pbkdf2_concurrency_gate_denies_before_account_lookup(monkeypatch):
    monkeypatch.setenv("AUTH_PBKDF2_MAX_CONCURRENCY", "1")
    entered = threading.Event()
    release = threading.Event()
    verify_calls: list[str] = []

    class MissingUserSession:
        def get(self, _model, _username):
            return None

    class LookupMustNotRun:
        def get(self, _model, _username):  # pragma: no cover - assertion is the test
            raise AssertionError("saturated authentication must not look up an account")

    def blocking_verify(password: str, _stored: str) -> bool:
        verify_calls.append(password)
        entered.set()
        assert release.wait(timeout=5)
        return False

    monkeypatch.setattr(auth, "verify_password", blocking_verify)
    first_result: list[object] = []
    first = threading.Thread(
        target=lambda: first_result.append(
            auth.authenticate(MissingUserSession(), "missing", "first-password")),
    )
    first.start()
    assert entered.wait(timeout=5)

    # Saturation is decided before querying the username, so known/unknown
    # account paths cannot diverge and no additional PBKDF2 work begins.
    assert auth.authenticate(LookupMustNotRun(), "any-name", "second-password") is None
    assert verify_calls == ["first-password"]

    release.set()
    first.join(timeout=5)
    assert not first.is_alive()
    assert first_result == [None]
    assert auth._PASSWORD_VERIFY_IN_FLIGHT == 0


def test_pbkdf2_gate_releases_slot_and_normal_login_still_succeeds(real_db, monkeypatch):
    monkeypatch.setenv("AUTH_PBKDF2_MAX_CONCURRENCY", "1")
    _add_user(real_db, username="gate-user")
    with Session(real_db) as session:
        assert auth.authenticate(session, "missing", "wrong-password") is None
        user = auth.authenticate(session, "gate-user", "password1")
        assert user is not None and user.username == "gate-user"
    assert auth._PASSWORD_VERIFY_IN_FLIGHT == 0


def test_unauthenticated_no_credential_not_counted(real_db, monkeypatch):
    # 纯未登录(不带任何凭据)不该计入限速,否则正常用户开页就被锁死。
    monkeypatch.setenv("REQUIRE_AUTH", "1")
    monkeypatch.setenv("CONSOLE_PIN", "24681024")
    monkeypatch.setenv("AUTH_FAIL_MAX", "3")
    _add_user(real_db)
    with TestClient(app) as client:
        for _ in range(6):
            assert client.get("/patients").status_code == 401     # 无凭据,不罚
        assert not auth.is_locked("testclient")
        # 登录仍可用(没被误锁)
        assert client.post("/auth/login", json={"username": "u", "password": "password1"}).status_code == 200


# ---------------- fail-closed 启动 ----------------
def test_fail_closed_requires_credentials(real_db, monkeypatch):
    monkeypatch.setenv("REQUIRE_AUTH", "1")
    with Session(real_db) as s:
        with pytest.raises(RuntimeError):
            auth.assert_deploy_credentials(s)      # 无 PIN 无账号 → 拒绝启动
    monkeypatch.setenv("CONSOLE_PIN", "24681024")
    with Session(real_db) as s:
        with pytest.raises(RuntimeError):
            auth.assert_deploy_credentials(s)      # 只有 PIN，console 无具名账号
    monkeypatch.delenv("CONSOLE_PIN")
    _add_user(real_db)
    with Session(real_db) as s:
        with pytest.raises(RuntimeError):
            auth.assert_deploy_credentials(s)      # 只有账号，patient 仍无法配对
    monkeypatch.setenv("CONSOLE_PIN", "24681024")
    with Session(real_db) as s:
        auth.assert_deploy_credentials(s)          # 账号 + 床旁 PIN 才放行


def test_implicit_account_auth_and_pin_only_are_not_partial_deployments(
        real_db, monkeypatch):
    monkeypatch.setenv("CONSOLE_PIN", "24681024")
    with Session(real_db) as s:
        with pytest.raises(RuntimeError):
            auth.assert_deploy_credentials(s)      # PIN-only 也会让 console 全部 401
    monkeypatch.delenv("CONSOLE_PIN")
    _add_user(real_db)
    with Session(real_db) as s:
        with pytest.raises(RuntimeError):
            auth.assert_deploy_credentials(s)      # 库内账号会隐式开认证，仍需 PIN
    monkeypatch.setenv("CONSOLE_PIN", "24681024")
    with Session(real_db) as s:
        auth.assert_deploy_credentials(s)


@pytest.mark.parametrize("invalid_pin", ["", "   ", "12345", "abcdef", "1" * 33])
def test_invalid_pairing_pin_configuration_fails_closed(
        real_db, monkeypatch, invalid_pin):
    monkeypatch.setenv("CONSOLE_PIN", invalid_pin)
    with pytest.raises(RuntimeError):
        auth.pin_configured()
    with Session(real_db) as s:
        with pytest.raises(RuntimeError):
            auth.assert_deploy_credentials(s)


def test_no_auth_configured_stays_open(real_db):
    # 回环纯开发：不设 REQUIRE_AUTH/PIN、无账号 → 全开(老人端 localhost 麦克风照常)
    with TestClient(app) as client:
        assert client.post("/patients", json={"patient_id": "OPEN"}).status_code == 200
        assert client.get("/patients").status_code == 200
        # 回环 M0 也不能重新开放旧自由量表容器。
        scale = client.post("/patients/OPEN/scales", json={
            "phase_type": "前测", "scale_name": "M0-SIM", "score": 0,
        })
        assert scale.status_code == 409, scale.text
        assert scale.json()["detail"]["code"] == "scale_protocol_not_frozen"


def test_direct_app_security_headers_and_host_allowlist(real_db, monkeypatch):
    monkeypatch.setenv("TRUSTED_HOSTS", "nmu.test,127.0.0.1")
    with TestClient(app, base_url="http://nmu.test") as client:
        response = client.get("/health")
        assert response.status_code == 200
        assert response.headers["x-content-type-options"] == "nosniff"
        assert response.headers["x-frame-options"] == "DENY"
        assert "frame-ancestors 'none'" in response.headers["content-security-policy"]
        denied = client.get("/health", headers={"Host": "evil.invalid"})
        assert denied.status_code == 400 and denied.json()["code"] == "untrusted_host"
        assert denied.headers["x-content-type-options"] == "nosniff"


# ---------------- 会话吊销 ----------------
def test_logout_revokes_session_server_side(real_db, monkeypatch):
    monkeypatch.setenv("REQUIRE_AUTH", "1")
    monkeypatch.setenv("CONSOLE_PIN", "24681024")
    _add_user(real_db)
    with TestClient(app) as client:
        client.post("/auth/login", json={"username": "u", "password": "password1"})
        _enable_csrf(client)
        with Session(real_db) as s:
            assert s.exec(select(AuthSession)).first() is not None
        assert client.post("/auth/logout").status_code == 200
        with Session(real_db) as s:
            assert s.exec(select(AuthSession)).first() is None


def test_account_writes_require_session_bound_csrf(real_db, monkeypatch):
    monkeypatch.setenv("REQUIRE_AUTH", "1")
    monkeypatch.setenv("CONSOLE_PIN", "24681024")
    _add_user(real_db)
    with TestClient(app) as client:
        assert client.post("/auth/login", json={
            "username": "u", "password": "password1",
        }).status_code == 200
        session_token = client.cookies.get(auth.COOKIE_NAME)
        csrf = client.cookies.get(auth.CSRF_COOKIE_NAME)
        assert session_token and csrf == auth.csrf_token(session_token)

        missing = client.post("/patients", json={"patient_id": "NO-CSRF"})
        assert missing.status_code == 403 and missing.json()["code"] == "csrf_required"
        wrong = client.post("/patients", json={"patient_id": "WRONG-CSRF"},
                            headers={"X-CSRF-Token": "0" * 64})
        assert wrong.status_code == 403 and wrong.json()["code"] == "csrf_required"
        cross_site = client.post(
            "/patients", json={"patient_id": "CROSS-SITE"},
            headers={"X-CSRF-Token": csrf, "Sec-Fetch-Site": "same-site"},
        )
        assert cross_site.status_code == 403
        assert cross_site.json()["code"] == "request_origin_rejected"
        assert client.post(
            "/patients", json={"patient_id": "WITH-CSRF"},
            headers={"X-CSRF-Token": csrf, "Sec-Fetch-Site": "same-origin"},
        ).status_code == 200


def test_logout_requires_csrf_and_does_not_revoke_on_failure(real_db, monkeypatch):
    monkeypatch.setenv("REQUIRE_AUTH", "1")
    monkeypatch.setenv("CONSOLE_PIN", "24681024")
    _add_user(real_db)
    with TestClient(app) as client:
        client.post("/auth/login", json={"username": "u", "password": "password1"})
        denied = client.post("/auth/logout")
        assert denied.status_code == 403 and client.get("/auth/me").status_code == 200
        _enable_csrf(client)
        assert client.post("/auth/logout").status_code == 200
        assert client.get("/auth/me").status_code == 401


def test_login_rejects_extra_and_oversized_credentials(real_db, monkeypatch):
    monkeypatch.setenv("REQUIRE_AUTH", "1")
    monkeypatch.setenv("CONSOLE_PIN", "24681024")
    _add_user(real_db)
    with TestClient(app) as client:
        assert client.post("/auth/login", json={
            "username": "u", "password": "password1", "redirect": "https://evil.invalid",
        }).status_code == 422
        assert client.post("/auth/login", json={
            "username": "u", "password": "x" * 1025,
        }).status_code == 422
    with pytest.raises(ValueError):
        auth.hash_password("界" * 400)


# ---------------- 复审确认项的回归守卫 ----------------
def test_revoked_cookie_does_not_lock_out(real_db, monkeypatch):
    # 会话被吊销后浏览器仍带旧 cookie 轮询,不该把该 IP 锁死(否则合法登录被 429)。
    monkeypatch.setenv("REQUIRE_AUTH", "1")
    monkeypatch.setenv("CONSOLE_PIN", "24681024")
    monkeypatch.setenv("AUTH_FAIL_MAX", "3")
    _add_user(real_db)
    with TestClient(app) as client:
        client.post("/auth/login", json={"username": "u", "password": "password1"})
        with Session(real_db) as s:            # 服务端吊销(等价 disable/passwd/过期)
            for row in s.exec(select(AuthSession)):
                s.delete(row)
            s.commit()
        for _ in range(6):                     # 旧 cookie 持续轮询 → 401,但不计失败
            assert client.get("/patients").status_code == 401
        assert not auth.is_locked("testclient")
        assert client.post("/auth/login", json={"username": "u", "password": "password1"}).status_code == 200


def test_stale_last_seen_session_does_not_500(real_db, monkeypatch):
    # last_seen 超 5 分钟触发节流写 commit;修复前中间件在 session 关闭后读 user → DetachedInstanceError 500。
    monkeypatch.setenv("REQUIRE_AUTH", "1")
    monkeypatch.setenv("CONSOLE_PIN", "24681024")
    _add_user(real_db)
    with Session(real_db) as s:
        token = auth.create_session(s, "u")
        row = s.get(AuthSession, auth._token_hash(token))
        row.last_seen_at = datetime.now() - timedelta(minutes=10)
        s.add(row)
        s.commit()
    with TestClient(app) as client:
        r = client.get("/patients", headers={"Cookie": f"nmu_session={token}"})
        assert r.status_code == 200            # 不是 500


def test_bare_sessions_plan_is_gated(real_db, monkeypatch):
    # /sessions/plan(整段)此前被 endswith('/plan') 误豁免,未认证可读 session 详情。
    monkeypatch.setenv("CONSOLE_PIN", "24681024")
    _add_user(real_db)
    with TestClient(app) as client:
        assert client.get("/sessions/plan").status_code == 401          # 现已按敏感读口保护
        # 真正的老人端计划口也不再裸读；PIN 只能配对，不能直接探测历史计划。
        assert client.get("/sessions/SOMEID/plan").status_code == 401
        denied = client.get("/sessions/SOMEID/plan",
                            headers={"X-Console-Pin": "24681024"})
        assert denied.status_code == 401
        assert denied.json()["code"] == "device_pair_required"


def test_non_ascii_pin_header_is_401_not_500(real_db, monkeypatch):
    # 非 ASCII 的 X-Console-Pin(Starlette 以 latin-1 解为 >127 的 str)曾让
    # secrets.compare_digest 抛 TypeError → 500。用原始 >127 字节复现真实客户端。
    monkeypatch.setenv("CONSOLE_PIN", "24681024")
    _add_user(real_db)
    with TestClient(app) as client:
        r = client.post("/patients", json={"patient_id": "Z"}, headers={"X-Console-Pin": b"\xff\xff\xff"})
        assert r.status_code == 401            # 当作错 PIN,不是 500
