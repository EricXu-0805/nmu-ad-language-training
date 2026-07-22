from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
import pytest

from app.main import _mount_spa, _safe_spa_candidate


def _spa(tmp_path):
    dist = tmp_path / "web" / "dist"
    dist.mkdir(parents=True)
    (dist / "index.html").write_text("SAFE-SPA", encoding="utf-8")
    (dist / "app.js").write_text("SAFE-ASSET", encoding="utf-8")
    secret = tmp_path / "research.db"
    secret.write_bytes(b"PRIVATE-DATABASE")
    test_app = FastAPI()
    _mount_spa(test_app, dist)
    return dist, secret, TestClient(test_app)


@pytest.mark.parametrize("attack", [
    "../research.db",
    "../../research.db",
    "%2e%2e/research.db",
    "%252e%252e/research.db",
    "..\\research.db",
    "foo/../img/wk2-01.webp",
    "foo/%2e%2e/img/wk2-01.webp",
    "foo/%252e%252e/img/wk2-01.webp",
])
def test_spa_candidate_rejects_traversal_and_recursive_encoding(tmp_path, attack):
    dist, _secret, _client = _spa(tmp_path)
    with pytest.raises(HTTPException) as exc:
        _safe_spa_candidate(dist, attack)
    assert exc.value.status_code == 404


def test_spa_route_never_serves_parent_files_or_ranges(tmp_path):
    _dist, _secret, client = _spa(tmp_path)
    for path in (
        "/%2e%2e/%2e%2e/research.db",
        "/%252e%252e/%252e%252e/research.db",
        "/..%5c..%5cresearch.db",
    ):
        response = client.get(path, headers={"Range": "bytes=0-0"})
        assert response.status_code == 404
        assert b"PRIVATE-DATABASE" not in response.content


def test_spa_fallback_keeps_safe_files_and_client_routes(tmp_path):
    _dist, _secret, client = _spa(tmp_path)
    assert client.get("/app.js").text == "SAFE-ASSET"
    assert client.get("/console/session/demo").text == "SAFE-SPA"


@pytest.mark.parametrize("path", [
    "/content/item_bank_v1.json",
    "/CONTENT/item_bank_v1.json",
    "/Content/item_bank_v1.json",
    "/cOnTeNt/item_bank_v1.json",
    "/c%6Fntent/item_bank_v1.json",
    "/c%256Fntent/item_bank_v1.json",
    "/content%5Citem_bank_v1.json",
    "/content%255Citem_bank_v1.json",
    "/CONTENT%255Citem_bank_v1.json",
])
@pytest.mark.parametrize("method", ["get", "head"])
def test_spa_never_serves_protected_api_namespace_aliases(tmp_path, path, method):
    dist, _secret, client = _spa(tmp_path)
    content_dir = dist / "content"
    content_dir.mkdir()
    (content_dir / "item_bank_v1.json").write_text(
        '{"target_word":"PRIVATE-ANSWER"}', encoding="utf-8")

    response = getattr(client, method)(path)

    assert response.status_code == 404
    assert b"PRIVATE-ANSWER" not in response.content
    assert b"SAFE-SPA" not in response.content


@pytest.mark.parametrize("path", [
    "/img",
    "/img/wk2-01.webp",
    "/IMG/wk2-01.webp",
    "/img%2Fwk2-01.webp",
    "/img%252Fwk2-01.webp",
    "/img%5Cwk2-01.webp",
    "/foo/%2e%2e/img/wk2-01.webp",
    "/foo/%252e%252e/img/wk2-01.webp",
    "/foo/%25252e%25252e/img/wk2-01.webp",
    "/foo%5C%2e%2e%5Cimg%5Cwk2-01.webp",
])
def test_spa_never_serves_patient_image_namespace(tmp_path, path):
    dist, _secret, client = _spa(tmp_path)
    image_dir = dist / "img"
    image_dir.mkdir()
    (image_dir / "wk2-01.webp").write_bytes(b"RIFFxxxxWEBPprivate")

    response = client.get(path)

    assert response.status_code == 404
    assert b"RIFF" not in response.content


@pytest.mark.parametrize("path", [
    "/img/wk2-01.webp",
    "/foo/%2e%2e/img/wk2-01.webp",
    "/foo/%252e%252e/img/wk2-01.webp",
])
def test_spa_patient_image_namespace_head_is_also_not_found(tmp_path, path):
    dist, _secret, client = _spa(tmp_path)
    image_dir = dist / "img"
    image_dir.mkdir()
    (image_dir / "wk2-01.webp").write_bytes(b"RIFFxxxxWEBPprivate")

    response = client.head(path)

    assert response.status_code == 404
    assert response.content == b""


def test_spa_candidate_rejects_symlink_escape(tmp_path):
    dist, secret, _client = _spa(tmp_path)
    link = dist / "linked.db"
    try:
        link.symlink_to(secret)
    except OSError:
        pytest.skip("当前文件系统不支持符号链接")
    with pytest.raises(HTTPException) as exc:
        _safe_spa_candidate(dist, "linked.db")
    assert exc.value.status_code == 404
