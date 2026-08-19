"""当前题私有图片：设备绑定、当前位置、字节完整性与静态枚举回归。"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime
import hashlib
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

from app import auth, content, db, patient_asset
from app.main import app
from app.models import (
    Patient,
    PatientDeviceCapability,
    ResearchUser,
    RuntimeCommand,
    SessionAutopilotState,
    SessionRuntimeState,
)
from app.models import Session as TrainSession


PROJECT_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def asset_client(monkeypatch):
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    monkeypatch.setattr(db, "engine", engine)
    SQLModel.metadata.create_all(engine)
    client = TestClient(app)
    client.test_engine = engine
    yield client
    client.close()
    engine.dispose()


def _create_patient(client: TestClient, patient_id: str) -> None:
    response = client.post("/patients", json={
        "patient_id": patient_id,
        "consent_status": "已同意",
        "consent_type": "本人同意",
        "mandarin_eligible": True,
        "recording_allowed": True,
        "secondary_use_allowed": True,
        "is_simulation_subject": True,
    })
    assert response.status_code == 200, response.text


def _create_session(
        client: TestClient, *, session_id: str, patient_id: str,
        week_no: int = 2) -> None:
    rapport = week_no == 1
    response = client.post("/sessions", json={
        "session_id": session_id,
        "patient_id": patient_id,
        "week_no": week_no,
        "phase_type": "关系建立" if rapport else "正式训练",
        "event_line": "关系建立环节" if rapport else "正式训练",
        "item_bank_version_id": "wk2-v1-20260707",
        "is_simulation": True,
        "trainer_id": "ASSET-RESEARCHER",
    })
    assert response.status_code == 200, response.text


def _activate_session(
        client: TestClient, session_id: str, *, week_no: int = 2,
        item_idx: int = 0, turn_idx: int = 0) -> dict:
    rapport = week_no == 1
    response = client.put("/live/state", json={
        "kind": "session",
        "payload": {
            "sessionId": session_id,
            "weekNo": week_no,
            "eventLine": "关系建立环节" if rapport else "正式训练",
            "mode": "rapport" if rapport else "task",
            "itemBankVersionId": "wk2-v1-20260707",
        },
    })
    assert response.status_code == 200, response.text
    if rapport:
        return {}
    full_plan = client.get(f"/sessions/{session_id}/plan").json()
    turn = full_plan["items"][item_idx]["turns"][turn_idx]
    cursor = client.put("/live/state", json={
        "kind": "cursor",
        "payload": {
            "sessionId": session_id,
            "screen": "present",
            "itemIdx": item_idx,
            "turnIdx": turn_idx,
            "responseRole": turn["response_role"],
            "cueLevel": 0,
            "recording": "idle",
        },
    })
    assert cursor.status_code == 200, cursor.text
    return full_plan


def _pair(client: TestClient, monkeypatch, *, device_id: str) -> dict[str, str]:
    monkeypatch.setenv("CONSOLE_PIN", "246810")
    response = client.post(
        "/device/pair",
        headers={"X-Console-Pin": "246810"},
        json={"deviceId": device_id},
    )
    assert response.status_code == 200, response.text
    return {"X-Device-Capability": response.json()["capability"]}


def _seed_current_week2(asset_client, monkeypatch):
    client = asset_client
    _create_patient(client, "SIM-ASSET-001")
    _create_session(
        client, session_id="S-ASSET-001", patient_id="SIM-ASSET-001")
    full_plan = _activate_session(client, "S-ASSET-001")
    capability = _pair(
        client, monkeypatch, device_id="asset-device-00000001")
    return client, full_plan, capability


def _detail_code(response) -> str | None:
    detail = response.json().get("detail")
    return detail.get("code") if isinstance(detail, dict) else None


def _canonical_manifest_digest(definition: dict) -> str:
    payload = {key: value for key, value in definition.items()
               if key != "definition_sha256"}
    raw = json.dumps(
        payload, ensure_ascii=False, sort_keys=True,
        separators=(",", ":"), allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def test_current_asset_returns_only_current_private_bytes_and_redacts_plan(
        asset_client, monkeypatch):
    client, full_plan, capability = _seed_current_week2(
        asset_client, monkeypatch)
    expected = (PROJECT_ROOT / "content" / "patient_assets" / "wk2-01.webp").read_bytes()

    response = client.get(
        "/sessions/S-ASSET-001/patient-asset/current", headers=capability)

    assert response.status_code == 200, response.text
    assert response.content == expected
    assert response.headers["content-type"] == "image/webp"
    assert response.headers["cache-control"] == "private, no-store"
    assert response.headers["pragma"] == "no-cache"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["cross-origin-resource-policy"] == "same-origin"
    assert response.headers["accept-ranges"] == "none"
    assert "content-disposition" not in response.headers
    assert "wk2-01" not in "\n".join(
        f"{key}:{value}" for key, value in response.headers.items())
    assert "胡萝卜" not in "\n".join(response.headers.values())

    head = client.head(
        "/sessions/S-ASSET-001/patient-asset/current", headers=capability)
    assert head.status_code == 200
    assert head.content == b""
    assert head.headers["content-type"] == "image/webp"

    ranged = client.get(
        "/sessions/S-ASSET-001/patient-asset/current",
        headers={**capability, "Range": "bytes=0-0"},
    )
    assert ranged.status_code == 416
    assert _detail_code(ranged) == "patient_asset_range_forbidden"

    device_plan = client.get(
        "/sessions/S-ASSET-001/patient-plan", headers=capability).json()
    assert device_plan["total_items"] == len(full_plan["items"])
    assert "image_id" not in json.dumps(device_plan, ensure_ascii=False)
    serialized_plan = json.dumps(device_plan, ensure_ascii=False)
    assert all(f"wk2-{index:02d}" not in serialized_plan for index in range(1, 31))


def test_asset_auth_is_capability_only_and_exact_session_bound(
        asset_client, monkeypatch):
    client = asset_client
    _create_patient(client, "SIM-ASSET-A")
    _create_session(client, session_id="S-ASSET-A", patient_id="SIM-ASSET-A")
    _create_patient(client, "SIM-ASSET-B")
    _create_session(client, session_id="S-ASSET-B", patient_id="SIM-ASSET-B")
    _activate_session(client, "S-ASSET-A")
    capability = _pair(client, monkeypatch, device_id="asset-device-auth-0001")

    anonymous = client.get("/sessions/S-ASSET-A/patient-asset/current")
    assert anonymous.status_code == 401
    assert anonymous.json()["code"] == "device_pair_required"

    raw_pin = client.get(
        "/sessions/S-ASSET-A/patient-asset/current",
        headers={"X-Console-Pin": "246810"},
    )
    assert raw_pin.status_code == 401
    assert raw_pin.json()["code"] == "device_pair_required"

    invalid = client.get(
        "/sessions/S-ASSET-A/patient-asset/current",
        headers={"X-Device-Capability": "x" * 43},
    )
    assert invalid.status_code == 401
    assert invalid.json()["code"] == "device_capability_invalid"

    foreign = client.get(
        "/sessions/S-ASSET-B/patient-asset/current", headers=capability)
    assert foreign.status_code == 409
    assert _detail_code(foreign) == "device_session_mismatch"

    # The private current-byte route does not make the answer-bearing staff
    # definitions patient-readable.  The three canonical server routes require
    # a named account for both GET and HEAD.
    staff_bundle_cases = (
        ("/content/item-bank-bundle", "item_bank_version_id"),
        ("/content/week1-script", "script_version_id"),
        ("/content/autopilot-protocol", "protocol_version_id"),
    )
    for path, _version_key in staff_bundle_cases:
        for headers in ({}, capability):
            denied_get = client.get(path, headers=headers)
            assert denied_get.status_code == 401, (path, headers, denied_get.text)
            assert denied_get.json()["code"] == "account_required"
            denied_head = client.head(path, headers=headers)
            assert denied_head.status_code == 401, (path, headers)
            assert denied_head.content == b""

    # Historical static filenames are not compatibility routes.  Conceal them
    # before auth so neither a device nor an account can revive a stale file.
    legacy_paths = (
        "/content/item_bank_v1.json",
        "/content/week1_script.json",
        "/content/autopilot_protocol_v1.json",
    )
    for path in legacy_paths:
        for headers in ({}, capability):
            denied_get = client.get(path, headers=headers)
            assert denied_get.status_code == 404, (path, headers, denied_get.text)
            assert denied_get.json()["code"] == "resource_not_found"
            denied_head = client.head(path, headers=headers)
            assert denied_head.status_code == 404, (path, headers)
            assert denied_head.content == b""

    # Case/separator/encoding/trailing-slash aliases are also a uniform 404
    # before credentials.  This is independent of host filesystem casing and
    # of how many URL-decoding passes a reverse proxy performs.
    answer_bundle_aliases = (
        "/CONTENT/item-bank-bundle",
        "/content/ITEM-BANK-BUNDLE",
        "/cOnTeNt/item-bank-bundle",
        "/content%2Fitem-bank-bundle",
        "/content%255Citem-bank-bundle",
        "/content/item%252Dbank-bundle",
        "/content/item-bank-bundle/",
        "/CONTENT/item_bank_v1.json",
        "/content%5Citem_bank_v1.json",
        "/content%255Citem_bank_v1.json",
        "/c%256Fntent/item_bank_v1.json",
    )
    for alias in answer_bundle_aliases:
        for headers in ({}, capability):
            denied_get = client.get(alias, headers=headers)
            assert denied_get.status_code == 404, (alias, headers, denied_get.text)
            assert "target_word" not in denied_get.text
            denied_head = client.head(alias, headers=headers)
            assert denied_head.status_code == 404, (alias, headers)
            assert denied_head.content == b""

    with Session(client.test_engine) as session:
        session.add(ResearchUser(
            username="asset-researcher",
            display_id="ASSET-RESEARCHER",
            password_hash=auth.hash_password("test-password-1"),
            role="researcher",
            created_at=datetime.now(),
        ))
        session.commit()
    account = TestClient(app)
    login = account.post("/auth/login", json={
        "username": "asset-researcher", "password": "test-password-1",
    })
    assert login.status_code == 200, login.text
    account_only = account.get("/sessions/S-ASSET-A/patient-asset/current")
    assert account_only.status_code == 403
    assert _detail_code(account_only) == "device_capability_required"

    for path, version_key in staff_bundle_cases:
        allowed_get = account.get(path)
        assert allowed_get.status_code == 200, (path, allowed_get.text)
        assert allowed_get.json().get(version_key)
        assert allowed_get.headers["cache-control"] == "private, no-store"
        assert allowed_get.headers["pragma"] == "no-cache"
        assert allowed_get.headers["content-type"].startswith("application/json")
        allowed_head = account.head(path)
        assert allowed_head.status_code == 200, path
        assert allowed_head.content == b""
        assert allowed_head.headers["cache-control"] == "private, no-store"

    for alias in (*legacy_paths, *answer_bundle_aliases):
        denied_alias = account.get(alias)
        assert denied_alias.status_code == 404, (alias, denied_alias.text)
        assert "target_word" not in denied_alias.text
        denied_alias_head = account.head(alias)
        assert denied_alias_head.status_code == 404, alias
        assert denied_alias_head.content == b""
    account.close()


def test_future_asset_selectors_and_legacy_static_paths_are_closed(
        asset_client, monkeypatch):
    client, _full_plan, capability = _seed_current_week2(
        asset_client, monkeypatch)

    for query in ("image_id=wk2-02", "item_idx=1", "path=../wk2-02.webp"):
        selected = client.get(
            f"/sessions/S-ASSET-001/patient-asset/current?{query}",
            headers=capability,
        )
        assert selected.status_code == 422
        assert _detail_code(selected) == "patient_asset_selector_forbidden"

    for path in ("/img", "/img/wk2-01.webp", "/img/wk2-02.webp"):
        public = client.get(path)
        assert public.status_code == 404, (path, public.status_code, public.text)
        assert b"RIFF" not in public.content

    assert not list((PROJECT_ROOT / "web" / "public" / "img").glob("*.webp"))
    assert len(list((PROJECT_ROOT / "content" / "patient_assets").glob("*.webp"))) == 224


@pytest.mark.parametrize("runtime_status", [
    "paused", "intervention_completed", "completed", "aborted", "failed",
])
def test_asset_rejects_paused_and_terminal_runtime(
        asset_client, monkeypatch, runtime_status):
    client, _full_plan, capability = _seed_current_week2(
        asset_client, monkeypatch)
    with Session(client.test_engine) as session:
        row = session.get(SessionRuntimeState, "S-ASSET-001")
        assert row is not None
        row.status = runtime_status
        session.add(row)
        session.commit()

    response = client.get(
        "/sessions/S-ASSET-001/patient-asset/current", headers=capability)

    assert response.status_code == 409
    assert _detail_code(response) == "patient_asset_runtime_not_active"


def test_week1_explicit_no_image_returns_204(asset_client, monkeypatch):
    client = asset_client
    _create_patient(client, "SIM-ASSET-W1")
    _create_session(
        client, session_id="S-ASSET-W1", patient_id="SIM-ASSET-W1", week_no=1)
    _activate_session(client, "S-ASSET-W1", week_no=1)
    capability = _pair(client, monkeypatch, device_id="asset-device-week1-0001")

    response = client.get(
        "/sessions/S-ASSET-W1/patient-asset/current", headers=capability)

    assert response.status_code == 204, response.text
    assert response.content == b""
    assert response.headers["cache-control"] == "private, no-store"
    assert response.headers["x-content-type-options"] == "nosniff"


def test_delivery_manifest_is_research_approved_and_transitively_bound(
        tmp_path, monkeypatch):
    """2026-08-19 起交付清单是研究批准 scope,覆盖 wk2..wk8 全部 224 张私有图。"""
    manifest = json.loads(patient_asset.DELIVERY_MANIFEST_PATH.read_text("utf-8"))

    assert manifest["purpose"] == "research_and_simulation_byte_allowlist"
    assert manifest["release_scope"] == "research_and_simulation"
    assert manifest["research_release_status"] == (
        "approved_for_research_presentation")
    assert set(manifest["research_release_approvals"]) == {
        "source_transform_rights", "content"}
    assert patient_asset.research_release_approved() is True
    assert manifest["image_id_contract_version"] == (
        patient_asset.IMAGE_ID_CONTRACT_VERSION)
    assert manifest["definition_sha256"] == _canonical_manifest_digest(manifest)

    manifest_rows = {row["image_id"]: row for row in manifest["assets"]}
    assert len(manifest_rows) == 224

    index = json.loads(
        (content.CONTENT_DIR / "item_bank_index.json").read_text("utf-8"))
    assert [entry["week_no"] for entry in index["banks"]] == list(range(2, 9))
    week2_bank = None
    all_bank_ids: set[str] = set()
    for entry in index["banks"]:
        bank = content.load_item_bank(content.CONTENT_DIR / entry["file"])
        if entry["week_no"] == 2:
            week2_bank = bank
        assert bank.meta["patient_asset_delivery_manifest"] == {
            "version_id": manifest["manifest_version_id"],
            "definition_sha256": manifest["definition_sha256"],
        }, entry
        all_bank_ids |= {
            row["image_id"]
            for row in (
                *bank.single_element, *bank.double_element, *bank.multi_element)
            if row.get("image_id")
        }
    assert set(manifest_rows) == all_bank_ids
    for image_id, row in manifest_rows.items():
        payload = (
            PROJECT_ROOT / "content" / "patient_assets" / f"{image_id}.webp"
        ).read_bytes()
        assert len(payload) == row["byte_size"]
        assert hashlib.sha256(payload).hexdigest() == row["sha256"]
        assert patient_asset._webp_metadata(payload) == (
            row["width"], row["height"], row["frame_count"])

    changed = deepcopy(week2_bank)
    changed.meta["patient_asset_delivery_manifest"] = {
        **changed.meta["patient_asset_delivery_manifest"],
        "definition_sha256": "0" * 64,
    }
    assert content.item_bank_definition_digest(changed) != (
        content.item_bank_definition_digest(week2_bank))
    with pytest.raises(patient_asset.PatientAssetError, match="未绑定"):
        patient_asset.resolve_runtime_asset(changed, "wk2-01")

    # 旧行为路径(staged 副本):simulation_only 清单按旧契约仍成立,且不算研究批准。
    staged = _simulation_only_manifest()
    rebound = _rebind_delivery(week2_bank, staged)
    staged_path = tmp_path / "delivery-simulation.json"
    staged_path.write_text(
        json.dumps(staged, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(patient_asset, "DELIVERY_MANIFEST_PATH", staged_path)
    assert patient_asset.research_release_approved() is False
    assert len(patient_asset._delivery_manifest(rebound)) == 224


def _rebind_delivery(bank, manifest):
    rebound = deepcopy(bank)
    manifest["definition_sha256"] = _canonical_manifest_digest(manifest)
    rebound.meta["patient_asset_delivery_manifest"] = {
        "version_id": manifest["manifest_version_id"],
        "definition_sha256": manifest["definition_sha256"],
    }
    return rebound


def _resign_research_approvals(manifest: dict) -> None:
    """夹具改动清单正文后,按 app 契约重算两份审批事实的 scope_sha256。

    只在想让校验走到审批之后那道门(如资产行值/定义摘要)时调用;
    伪造审批的场景绝不能调用它,否则断言会空转。
    """
    scope_digest = hashlib.sha256(json.dumps(
        {key: value for key, value in manifest.items()
         if key not in {"definition_sha256", "research_release_approvals"}},
        ensure_ascii=False, sort_keys=True,
        separators=(",", ":"), allow_nan=False,
    ).encode("utf-8")).hexdigest()
    for fact in manifest["research_release_approvals"].values():
        fact["scope_sha256"] = scope_digest


def test_delivery_contract_accepts_explicit_multi_id_without_remapping():
    """2026-08-19 起 wk2 双多要素图真实入册:显式 multi id 直接交付,不做数字重映射。"""
    bank = content.load_item_bank(content.CONTENT_DIR / "item_bank_v1.json")

    rows = patient_asset._delivery_manifest(bank)

    assert "wk2-multi-01" in rows
    assert "wk2-multi-02" in rows
    assert "wk2-31" not in rows
    assert "wk2-32" not in rows

    payload = (
        PROJECT_ROOT / "content" / "patient_assets" / "wk2-multi-01.webp"
    ).read_bytes()
    runtime_asset = patient_asset.resolve_runtime_asset(bank, "wk2-multi-01")
    assert patient_asset.read_runtime_asset(runtime_asset) == payload


@pytest.mark.parametrize("image_id", ["wk2-31", "wk2-32"])
def test_delivery_contract_rejects_numeric_multi_remaps(
        tmp_path, monkeypatch, image_id):
    bank = content.load_item_bank(content.CONTENT_DIR / "item_bank_v1.json")
    manifest = json.loads(patient_asset.DELIVERY_MANIFEST_PATH.read_text("utf-8"))
    manifest["assets"][0]["image_id"] = image_id
    _resign_research_approvals(manifest)
    rebound = _rebind_delivery(bank, manifest)
    path = tmp_path / f"delivery-{image_id}.json"
    path.write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(patient_asset, "DELIVERY_MANIFEST_PATH", path)

    with pytest.raises(patient_asset.PatientAssetError, match="资产值非法"):
        patient_asset._delivery_manifest(rebound)


@pytest.mark.parametrize("frame_count", [True, 1.0, "1", 2])
def test_delivery_contract_rejects_noncanonical_frame_count(
        tmp_path, monkeypatch, frame_count):
    bank = content.load_item_bank(content.CONTENT_DIR / "item_bank_v1.json")
    manifest = json.loads(patient_asset.DELIVERY_MANIFEST_PATH.read_text("utf-8"))
    manifest["assets"][0]["frame_count"] = frame_count
    _resign_research_approvals(manifest)
    rebound = _rebind_delivery(bank, manifest)
    path = tmp_path / "delivery-frame-count.json"
    path.write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(patient_asset, "DELIVERY_MANIFEST_PATH", path)

    with pytest.raises(patient_asset.PatientAssetError, match="资产值非法"):
        patient_asset._delivery_manifest(rebound)


@pytest.mark.parametrize("tamper", ["body_only", "claimed_digest_only"])
def test_delivery_manifest_tamper_fails_closed(tmp_path, monkeypatch, tamper):
    bank = content.load_item_bank(content.CONTENT_DIR / "item_bank_v1.json")
    manifest = json.loads(patient_asset.DELIVERY_MANIFEST_PATH.read_text("utf-8"))
    if tamper == "body_only":
        manifest["assets"][0]["byte_size"] += 1
        # 审批 scope 对齐后,剩下的失配只在 definition_sha256 这一道门。
        _resign_research_approvals(manifest)
    else:
        manifest["definition_sha256"] = "0" * 64
    path = tmp_path / "delivery.json"
    path.write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(patient_asset, "DELIVERY_MANIFEST_PATH", path)

    with pytest.raises(patient_asset.PatientAssetError, match="定义摘要不匹配"):
        patient_asset.resolve_runtime_asset(bank, "wk2-01")


def test_private_webp_byte_tamper_fails_closed(tmp_path, monkeypatch):
    bank = content.load_item_bank(content.CONTENT_DIR / "item_bank_v1.json")
    original = (PROJECT_ROOT / "content" / "patient_assets" / "wk2-01.webp").read_bytes()
    changed = bytearray(original)
    changed[-1] ^= 0x01
    (tmp_path / "wk2-01.webp").write_bytes(changed)
    monkeypatch.setattr(patient_asset, "RUNTIME_ASSET_DIRS", (tmp_path,))

    asset = patient_asset.resolve_runtime_asset(bank, "wk2-01")
    with pytest.raises(patient_asset.PatientAssetError, match="字节摘要"):
        patient_asset.read_runtime_asset(asset)


def test_private_webp_declared_decode_facts_are_verified_at_read_time(
        tmp_path, monkeypatch):
    bank = content.load_item_bank(content.CONTENT_DIR / "item_bank_v1.json")
    manifest = json.loads(patient_asset.DELIVERY_MANIFEST_PATH.read_text("utf-8"))
    manifest["assets"][0]["width"] += 1
    _resign_research_approvals(manifest)
    rebound = _rebind_delivery(bank, manifest)
    path = tmp_path / "delivery-wrong-dimensions.json"
    path.write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(patient_asset, "DELIVERY_MANIFEST_PATH", path)

    asset = patient_asset.resolve_runtime_asset(rebound, "wk2-01")
    with pytest.raises(patient_asset.PatientAssetError, match="解码事实"):
        patient_asset.read_runtime_asset(asset)


def test_private_webp_parser_rejects_extended_and_over_budget_profiles():
    payload = (
        PROJECT_ROOT / "content" / "patient_assets" / "wk2-01.webp"
    ).read_bytes()
    assert patient_asset._webp_metadata(payload)[2] == 1

    extended = bytearray(payload)
    extended[12:16] = b"VP8X"
    with pytest.raises(patient_asset.PatientAssetError, match="单帧受控"):
        patient_asset._webp_metadata(bytes(extended))

    over_budget = bytearray(payload)
    # Simple VP8: RIFF(12) + chunk header(8) + frame tag/signature(6), then
    # little-endian 14-bit width.  9000 remains syntactically representable
    # but exceeds the reviewed runtime budget.
    over_budget[26:28] = (9000).to_bytes(2, "little")
    with pytest.raises(patient_asset.PatientAssetError, match="安全预算"):
        patient_asset._webp_metadata(bytes(over_budget))


@pytest.mark.parametrize("malicious_id", ["../../secret", "wk2-31", "wk2-32"])
def test_symlink_and_malicious_item_bank_path_fail_closed(
        tmp_path, monkeypatch, malicious_id):
    bank = content.load_item_bank(content.CONTENT_DIR / "item_bank_v1.json")
    secret = tmp_path / "secret.webp"
    secret.write_bytes(b"RIFFxxxxWEBP" + b"x" * 32)
    link = tmp_path / "wk2-01.webp"
    try:
        link.symlink_to(secret)
    except OSError:
        pytest.skip("当前文件系统不支持符号链接")
    monkeypatch.setattr(patient_asset, "RUNTIME_ASSET_DIRS", (tmp_path,))
    with pytest.raises(patient_asset.PatientAssetError, match="尚未安全部署"):
        patient_asset.resolve_runtime_asset(bank, "wk2-01")

    malicious = deepcopy(bank)
    malicious.single_element[0]["image_id"] = malicious_id
    with pytest.raises(patient_asset.PatientAssetError, match="定义不合法"):
        patient_asset.resolve_runtime_asset(malicious, malicious_id)


def test_old_session_digest_rejects_simultaneously_rebound_delivery(
        asset_client, monkeypatch, tmp_path):
    client, _full_plan, capability = _seed_current_week2(
        asset_client, monkeypatch)
    original_bank = content.load_item_bank(content.CONTENT_DIR / "item_bank_v1.json")
    rebound_bank = deepcopy(original_bank)
    manifest = json.loads(patient_asset.DELIVERY_MANIFEST_PATH.read_text("utf-8"))
    manifest["manifest_version_id"] = "wk2-private-webp-20260720.3"
    manifest["definition_sha256"] = _canonical_manifest_digest(manifest)
    rebound_bank.meta["draft_revision"] = "2026-07-20.5-test"
    rebound_bank.meta["patient_asset_delivery_manifest"] = {
        "version_id": manifest["manifest_version_id"],
        "definition_sha256": manifest["definition_sha256"],
    }
    assert content.item_bank_definition_digest(rebound_bank) != (
        content.item_bank_definition_digest(original_bank))
    rebound_path = tmp_path / "delivery-v2.json"
    rebound_path.write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(patient_asset, "DELIVERY_MANIFEST_PATH", rebound_path)
    monkeypatch.setattr(content, "load_item_bank", lambda _path: rebound_bank)

    response = client.get(
        "/sessions/S-ASSET-001/patient-asset/current", headers=capability)

    assert response.status_code == 409
    assert _detail_code(response) == "patient_asset_definition_mismatch"


def _simulation_only_manifest() -> dict:
    """由当前研究批准清单派生一份 simulation_only 清单(staged 旧契约场景)。"""
    manifest = json.loads(patient_asset.DELIVERY_MANIFEST_PATH.read_text("utf-8"))
    manifest["manifest_version_id"] = "wk2-8-private-webp-simulation-test.1"
    manifest["purpose"] = (
        "simulation_private_byte_allowlist_not_research_asset_definition")
    manifest["release_scope"] = "simulation_only"
    manifest["research_release_status"] = (
        "blocked_pending_source_transform_rights_and_content_approvals")
    manifest.pop("research_release_approvals", None)
    manifest["definition_sha256"] = _canonical_manifest_digest(manifest)
    return manifest


def _research_approved_manifest() -> dict:
    """由当前交付清单派生一份测试签署的研究批准清单(测试专用签字工件)。"""
    manifest = json.loads(patient_asset.DELIVERY_MANIFEST_PATH.read_text("utf-8"))
    manifest["manifest_version_id"] = "wk2-private-webp-research-test.1"
    manifest["purpose"] = "research_and_simulation_byte_allowlist"
    manifest["release_scope"] = "research_and_simulation"
    manifest["research_release_status"] = "approved_for_research_presentation"
    scope_payload = {key: value for key, value in manifest.items()
                     if key not in {"definition_sha256", "research_release_approvals"}}
    scope_digest = hashlib.sha256(json.dumps(
        scope_payload, ensure_ascii=False, sort_keys=True,
        separators=(",", ":"), allow_nan=False,
    ).encode("utf-8")).hexdigest()
    manifest["research_release_approvals"] = {
        "source_transform_rights": {
            "approved_by": "内容组测试签署人", "approved_at": "2026-08-07",
            "scope_sha256": scope_digest},
        "content": {
            "approved_by": "PI测试签署人", "approved_at": "2026-08-07",
            "scope_sha256": scope_digest},
    }
    manifest["definition_sha256"] = _canonical_manifest_digest(manifest)
    return manifest


def test_research_approved_manifest_serves_research_session(
        asset_client, monkeypatch, tmp_path):
    """签字工件(研究批准清单)到位后,真实研究场次零代码改动即可呈现题图。"""
    client, _full_plan, capability = _seed_current_week2(
        asset_client, monkeypatch)
    manifest = _research_approved_manifest()
    original_bank = content.load_item_bank(content.CONTENT_DIR / "item_bank_v1.json")
    rebound_bank = _rebind_delivery(original_bank, manifest)
    manifest_path = tmp_path / "delivery-research.json"
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(patient_asset, "DELIVERY_MANIFEST_PATH", manifest_path)
    monkeypatch.setattr(content, "load_item_bank", lambda _path: rebound_bank)
    with Session(client.test_engine) as session:
        patient = session.get(Patient, "SIM-ASSET-001")
        assert patient is not None
        patient.is_simulation_subject = False
        session.add(patient)
        row = session.get(TrainSession, "S-ASSET-001")
        assert row is not None
        row.is_simulation = False
        row.data_classification = "research"
        row.item_bank_definition_digest = content.item_bank_definition_digest(
            rebound_bank)
        session.add(row)
        session.commit()

    expected = (PROJECT_ROOT / "content" / "patient_assets" / "wk2-01.webp").read_bytes()
    response = client.get(
        "/sessions/S-ASSET-001/patient-asset/current", headers=capability)

    assert response.status_code == 200, response.text
    assert response.content == expected


def test_research_release_contract_is_fail_closed(tmp_path, monkeypatch):
    approved = _research_approved_manifest()
    # 2026-08-19 起在册清单本身已是研究批准 scope;旧契约路径用 staged 副本重现。
    simulation_staged = _simulation_only_manifest()
    original_bank = content.load_item_bank(content.CONTENT_DIR / "item_bank_v1.json")
    manifest_path = tmp_path / "delivery.json"
    monkeypatch.setattr(patient_asset, "DELIVERY_MANIFEST_PATH", manifest_path)

    def install(manifest: dict):
        bound = _rebind_delivery(original_bank, manifest)
        manifest_path.write_text(
            json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
        return bound

    bank = install(deepcopy(approved))
    assert patient_asset.research_release_approved() is True
    assert patient_asset._delivery_manifest(bank)

    bank = install(deepcopy(simulation_staged))
    assert patient_asset.research_release_approved() is False
    assert patient_asset._delivery_manifest(bank)  # 模拟合同本身仍有效

    broken = deepcopy(approved)
    del broken["research_release_approvals"]
    bank = install(broken)
    assert patient_asset.research_release_approved() is False
    with pytest.raises(patient_asset.PatientAssetError):
        patient_asset._delivery_manifest(bank)

    forged = deepcopy(approved)
    forged["research_release_approvals"]["content"]["scope_sha256"] = "0" * 64
    bank = install(forged)
    assert patient_asset.research_release_approved() is False
    with pytest.raises(patient_asset.PatientAssetError):
        patient_asset._delivery_manifest(bank)

    unsigned = deepcopy(approved)
    unsigned["research_release_approvals"]["content"]["approved_by"] = "  "
    bank = install(unsigned)
    assert patient_asset.research_release_approved() is False

    undated = deepcopy(approved)
    undated["research_release_approvals"]["source_transform_rights"][
        "approved_at"] = "上周"
    bank = install(undated)
    assert patient_asset.research_release_approved() is False

    mismatched = deepcopy(approved)
    mismatched["research_release_status"] = (
        "blocked_pending_source_transform_rights_and_content_approvals")
    bank = install(mismatched)
    assert patient_asset.research_release_approved() is False

    smuggled = deepcopy(simulation_staged)
    smuggled["research_release_approvals"] = deepcopy(
        approved["research_release_approvals"])
    bank = install(smuggled)
    assert patient_asset.research_release_approved() is False
    with pytest.raises(patient_asset.PatientAssetError):
        patient_asset._delivery_manifest(bank)


def test_simulation_only_delivery_rejects_research_classified_session(
        asset_client, monkeypatch, tmp_path):
    """守门:simulation_only 清单(staged 副本)在场时,研究场次必须被拒绝呈现。"""
    client, _full_plan, capability = _seed_current_week2(
        asset_client, monkeypatch)
    staged = _simulation_only_manifest()
    staged_path = tmp_path / "delivery-simulation.json"
    staged_path.write_text(
        json.dumps(staged, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(patient_asset, "DELIVERY_MANIFEST_PATH", staged_path)
    with Session(client.test_engine) as session:
        row = session.get(TrainSession, "S-ASSET-001")
        assert row is not None
        row.is_simulation = False
        row.data_classification = "research"
        session.add(row)
        session.commit()

    response = client.get(
        "/sessions/S-ASSET-001/patient-asset/current", headers=capability)

    assert response.status_code == 409
    assert _detail_code(response) == "patient_asset_research_release_blocked"


def test_autonomous_current_command_selects_server_current_item(
        asset_client, monkeypatch):
    client, full_plan, capability = _seed_current_week2(
        asset_client, monkeypatch)
    with Session(client.test_engine) as session:
        cap = session.exec(select(PatientDeviceCapability)).one()
        train = session.get(TrainSession, "S-ASSET-001")
        assert train is not None
        first = full_plan["items"][0]
        first_turn = first["turns"][0]
        command = RuntimeCommand(
            idempotency_key="asset-command-current-0001",
            session_id=train.session_id,
            command_seq=1,
            item_id=first["item_id"],
            turn_seq=first_turn["turn_seq"],
            turn_key=f"{first['item_id']}#{first_turn['turn_seq']}",
            attempt_seq=1,
            prompt_level=0,
            item_bank_version_id=train.item_bank_version_id,
            item_bank_definition_digest=train.item_bank_definition_digest,
            autopilot_protocol_version_id=train.autopilot_protocol_version_id,
            autopilot_protocol_definition_digest=(
                train.autopilot_protocol_definition_digest),
            response_role=first_turn["response_role"],
            scope_key="p0a_sim_first_single_v1",
            control_generation=1,
            runner_generation=1,
            issued_capability_token_hash=cap.token_hash,
            issued_device_id_hash=cap.device_id_hash,
            issued_at=datetime.now(),
            kind="tts",
            state="pending",
            payload_json="{}",
            created_at=datetime.now(),
            updated_at=datetime.now(),
        )
        session.add(command)
        session.flush()
        session.add(SessionAutopilotState(
            session_id=train.session_id,
            scope_key="p0a_sim_first_single_v1",
            mode="autonomous",
            status="waiting_tts",
            control_generation=1,
            runner_generation=1,
            revision=1,
            next_command_seq=2,
            current_command_id=command.id,
        ))
        session.commit()

    response = client.get(
        "/sessions/S-ASSET-001/patient-asset/current", headers=capability)

    assert response.status_code == 200, response.text
    assert response.content == (
        PROJECT_ROOT / "content" / "patient_assets" / "wk2-01.webp").read_bytes()
