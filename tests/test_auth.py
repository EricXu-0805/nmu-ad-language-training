"""M1-D 认证层测试：账号会话 + PIN 保底 + 限速锁定 + fail-closed + 身份审计。

cookie 要穿过中间件(用 db.engine)又要被路由(get_session)读到,二者须同库：
故本文件 monkeypatch app.db.engine 到同一 StaticPool 内存库,并用 `with TestClient`
触发 lifespan(走真实启动路径,含 fail-closed 断言)。
"""
from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

from app import auth, db
from app.main import app
from app.models import AuthSession, ResearchUser


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
    _add_user(real_db)
    with TestClient(app) as client:
        assert client.get("/auth/me").status_code == 401
        bad = client.post("/auth/login", json={"username": "u", "password": "nope"})
        assert bad.status_code == 401
        ok = client.post("/auth/login", json={"username": "u", "password": "password1"})
        assert ok.status_code == 200 and ok.json()["display_id"] == "R-U"
        me = client.get("/auth/me")
        assert me.status_code == 200 and me.json()["username"] == "u"
        client.post("/auth/logout")
        assert client.get("/auth/me").status_code == 401


def test_disabled_user_cannot_login(real_db, monkeypatch):
    monkeypatch.setenv("REQUIRE_AUTH", "1")
    _add_user(real_db, disabled=True)
    with TestClient(app) as client:
        assert client.post("/auth/login", json={"username": "u", "password": "password1"}).status_code == 401


# ---------------- cookie 穿过中间件授权写操作 ----------------
def test_cookie_authorizes_protected_write(real_db, monkeypatch):
    monkeypatch.setenv("REQUIRE_AUTH", "1")   # 无 PIN,只靠账号
    _add_user(real_db)
    with TestClient(app) as client:
        assert client.post("/patients", json={"patient_id": "P1"}).status_code == 401
        assert client.get("/patients").status_code == 401     # 名单读口也须认证(修复的泄露洞)
        client.post("/auth/login", json={"username": "u", "password": "password1"})
        assert client.post("/patients", json={"patient_id": "P1"}).status_code == 200
        assert client.get("/patients").status_code == 200


# ---------------- 身份审计：登录账号 → reviewer_id 权威 ----------------
def test_locked_score_stamps_authenticated_identity(real_db, monkeypatch):
    monkeypatch.setenv("REQUIRE_AUTH", "1")
    _add_user(real_db, display_id="丁老师")
    with TestClient(app) as client:
        client.post("/auth/login", json={"username": "u", "password": "password1"})
        client.post("/patients", json={"patient_id": "PL"})
        client.post("/sessions", json={"session_id": "SL", "patient_id": "PL", "week_no": 2,
                                       "phase_type": "正式训练", "event_line": "正式训练",
                                       "item_bank_version_id": "wk2-v1-20260707"})
        ie = client.post("/sessions/SL/items", json={"item_id": "SE_锚", "task_type": "单要素"}).json()
        te = client.post(f"/items/{ie['id']}/turns",
                         json={"turn_seq": 1, "response_role": "命名", "asr_text": "锚"}).json()
        client.patch(f"/turns/{te['id']}/confirm", json={"confirmed_response_text": "锚"})
        locked = client.patch(f"/turns/{te['id']}/lock",
                              json={"reviewer_id": "冒名", "element_value": 1, "prompt_level": 0})
        assert locked.status_code == 200
        # 请求体自报 reviewer_id="冒名" 被登录身份覆盖 → 审计可信
        assert locked.json()["reviewer_id"] == "丁老师"


# ---------------- 限速锁定 ----------------
def test_bad_pin_lockout(real_db, monkeypatch):
    monkeypatch.setenv("CONSOLE_PIN", "246810")
    monkeypatch.setenv("AUTH_FAIL_MAX", "3")
    with TestClient(app) as client:
        bad = {"X-Console-Pin": "000000"}
        for _ in range(3):
            assert client.post("/patients", json={"patient_id": "X"}, headers=bad).status_code == 401
        # 连败达阈值 → 锁定：即便这次给对 PIN 也先吃 429
        assert client.post("/patients", json={"patient_id": "X"},
                           headers={"X-Console-Pin": "246810"}).status_code == 429


def test_unauthenticated_no_credential_not_counted(real_db, monkeypatch):
    # 纯未登录(不带任何凭据)不该计入限速,否则正常用户开页就被锁死。
    monkeypatch.setenv("REQUIRE_AUTH", "1")
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
            auth.assert_deploy_credentials(s)      # REQUIRE_AUTH=1 且无 PIN 无账号 → 拒绝启动
    _add_user(real_db)
    with Session(real_db) as s:
        auth.assert_deploy_credentials(s)          # 有账号即放行


def test_no_auth_configured_stays_open(real_db):
    # 回环纯开发：不设 REQUIRE_AUTH/PIN、无账号 → 全开(老人端 localhost 麦克风照常)
    with TestClient(app) as client:
        assert client.post("/patients", json={"patient_id": "OPEN"}).status_code == 200
        assert client.get("/patients").status_code == 200


# ---------------- 会话吊销 ----------------
def test_logout_revokes_session_server_side(real_db, monkeypatch):
    monkeypatch.setenv("REQUIRE_AUTH", "1")
    _add_user(real_db)
    with TestClient(app) as client:
        client.post("/auth/login", json={"username": "u", "password": "password1"})
        with Session(real_db) as s:
            assert s.exec(select(AuthSession)).first() is not None
        client.post("/auth/logout")
        with Session(real_db) as s:
            assert s.exec(select(AuthSession)).first() is None


# ---------------- 复审确认项的回归守卫 ----------------
def test_revoked_cookie_does_not_lock_out(real_db, monkeypatch):
    # 会话被吊销后浏览器仍带旧 cookie 轮询,不该把该 IP 锁死(否则合法登录被 429)。
    monkeypatch.setenv("REQUIRE_AUTH", "1")
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
    _add_user(real_db)
    with Session(real_db) as s:
        token = auth.create_session(s, "u")
        row = s.get(AuthSession, auth._token_hash(token))
        row.last_seen_at = datetime.now() - timedelta(minutes=10)
        s.add(row); s.commit()
    with TestClient(app) as client:
        r = client.get("/patients", headers={"Cookie": f"nmu_session={token}"})
        assert r.status_code == 200            # 不是 500


def test_bare_sessions_plan_is_gated(real_db, monkeypatch):
    # /sessions/plan(整段)此前被 endswith('/plan') 误豁免,未认证可读 session 详情。
    monkeypatch.setenv("CONSOLE_PIN", "246810")
    with TestClient(app) as client:
        assert client.get("/sessions/plan").status_code == 401          # 现已按敏感读口保护
        # 真正的老人端只读计划口仍免认证(无 PIN 也能进路由;缺参数是 422,但绝不是 401)
        assert client.get("/sessions/SOMEID/plan").status_code != 401


def test_non_ascii_pin_header_is_401_not_500(real_db, monkeypatch):
    # 非 ASCII 的 X-Console-Pin(Starlette 以 latin-1 解为 >127 的 str)曾让
    # secrets.compare_digest 抛 TypeError → 500。用原始 >127 字节复现真实客户端。
    monkeypatch.setenv("CONSOLE_PIN", "246810")
    with TestClient(app) as client:
        r = client.post("/patients", json={"patient_id": "Z"}, headers={"X-Console-Pin": b"\xff\xff\xff"})
        assert r.status_code == 401            # 当作错 PIN,不是 500
