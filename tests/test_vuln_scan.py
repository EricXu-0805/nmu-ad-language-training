"""OSV 漏洞扫描。

重点守两件容易变成安慰剂的事：查不通网络必须报失败（不能因为"没返回漏洞"就
算通过），以及豁免必须会过期（不然一条当年的判断能无限期躺着）。
"""
from __future__ import annotations

from datetime import date
import json
from pathlib import Path
import sys
import urllib.error

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import vuln_scan as vs                                      # noqa: E402

ROOT = Path(__file__).resolve().parents[1]

SBOM = {"components": [
    {"type": "library", "name": "alembic", "version": "1.18.5",
     "purl": "pkg:pypi/alembic@1.18.5"},
    {"type": "library", "name": "react-router", "version": "7.18.1",
     "purl": "pkg:npm/react-router@7.18.1"},
    {"type": "container", "name": "python", "version": "sha256:x",
     "purl": "pkg:oci/python@sha256:x"},
    {"type": "operating-system", "name": "bash", "version": "5.3.9-r1",
     "purl": "pkg:apk/alpine/bash@5.3.9-r1"},
]}
RESPONSE = {
    "hits": {"pkg:npm/react-router@7.18.1": ["GHSA-aaaa", "GHSA-gone"]},
    "details": {
        "GHSA-aaaa": {"summary": "一条真的", "database_specific": {"severity": "HIGH"}},
        "GHSA-gone": {"summary": "已撤回", "withdrawn": "2026-01-01T00:00:00Z"},
    },
}
TODAY = date(2026, 8, 6)


def _waiver_file(tmp_path: Path, **overrides) -> Path:
    item = {"id": "GHSA-aaaa", "reason": "不可达的代码路径", "expires": "2026-11-06"}
    item.update(overrides)
    path = tmp_path / "waivers.json"
    path.write_text(json.dumps({"waivers": [item]}), encoding="utf-8")
    return path


def _files(tmp_path: Path, sbom=None, response=None):
    sbom_path = tmp_path / "sbom.json"
    sbom_path.write_text(json.dumps(sbom or SBOM), encoding="utf-8")
    response_path = tmp_path / "osv.json"
    response_path.write_text(json.dumps(response or RESPONSE), encoding="utf-8")
    return sbom_path, response_path


# ---------------- 坐标 ----------------

def test_coordinates_split_queryable_from_unscannable():
    targets, skipped = vs.coordinates(SBOM)
    assert [(c.ecosystem, c.name, c.version) for c in targets] == [
        ("PyPI", "alembic", "1.18.5"), ("npm", "react-router", "7.18.1")]
    assert sum(skipped.values()) == 2
    assert all("镜像" in k or "Alpine" in k for k in skipped)


def test_an_unknown_purl_prefix_is_named_not_dropped():
    _, skipped = vs.coordinates({"components": [
        {"name": "x", "version": "1", "purl": "pkg:cargo/x@1"}]})
    assert any("未知的 purl 前缀" in key for key in skipped)
    assert sum(skipped.values()) == 1


# ---------------- 判定 ----------------

def test_withdrawn_records_do_not_count():
    findings = vs.evaluate(RESPONSE, {})
    assert [f.identifier for f in findings] == ["GHSA-aaaa"]


def test_a_waived_finding_stops_blocking_but_still_shows_up():
    findings = vs.evaluate(RESPONSE, {"GHSA-aaaa": {"reason": "r", "expires": "2026-11-06"}})
    assert len(findings) == 1 and findings[0].waived
    assert "WAIVED" in findings[0].line()


def test_severity_falls_back_to_unknown_rather_than_inventing_one():
    assert vs._severity({}) == "UNKNOWN"
    assert vs._severity({"database_specific": {"severity": "moderate"}}) == "MODERATE"


# ---------------- 豁免 ----------------

def test_a_waiver_missing_a_required_field_is_rejected(tmp_path):
    for missing in ("id", "reason", "expires"):
        path = _waiver_file(tmp_path, **{missing: ""})
        with pytest.raises(ValueError, match=missing):
            vs.load_waivers(path, TODAY)


def test_an_expired_waiver_counts_as_no_waiver(tmp_path):
    path = _waiver_file(tmp_path, expires="2026-08-05")
    live, expired = vs.load_waivers(path, TODAY)
    assert live == {} and expired == ["GHSA-aaaa"]

    live, expired = vs.load_waivers(_waiver_file(tmp_path), TODAY)
    assert set(live) == {"GHSA-aaaa"} and expired == []


def test_a_waiver_expiring_today_is_still_live(tmp_path):
    live, _ = vs.load_waivers(_waiver_file(tmp_path, expires="2026-08-06"), TODAY)
    assert set(live) == {"GHSA-aaaa"}


def test_missing_waiver_file_is_simply_no_waivers(tmp_path):
    assert vs.load_waivers(tmp_path / "nope.json", TODAY) == ({}, [])


# ---------------- CLI ----------------

def _run(tmp_path, waivers: Path | None, **extra) -> int:
    sbom_path, response_path = _files(tmp_path, **extra)
    argv = ["--sbom", str(sbom_path), "--offline", str(response_path),
            "--today", "2026-08-06"]
    if waivers is not None:
        argv += ["--waivers", str(waivers)]
    else:
        argv += ["--waivers", str(tmp_path / "absent.json")]
    return vs.main(argv)


def test_an_unwaived_finding_fails_the_run(tmp_path):
    assert _run(tmp_path, None) == 1


def test_everything_waived_passes(tmp_path):
    assert _run(tmp_path, _waiver_file(tmp_path)) == 0


def test_an_expired_waiver_fails_the_run(tmp_path):
    assert _run(tmp_path, _waiver_file(tmp_path, expires="2026-08-05")) == 1


def test_a_clean_response_passes(tmp_path):
    assert _run(tmp_path, None, response={"hits": {}, "details": {}}) == 0


def test_a_network_failure_is_a_failure_not_a_pass(tmp_path, monkeypatch, capsys):
    """查不动就是没查过。这里如果退 0，整道门禁在断网时会静默变成永远通过。"""
    sbom_path, _ = _files(tmp_path)
    monkeypatch.setattr(vs, "query_osv", lambda *a, **k: (_ for _ in ()).throw(
        urllib.error.URLError("断网")))
    code = vs.main(["--sbom", str(sbom_path), "--waivers", str(tmp_path / "absent.json")])
    assert code == 2
    assert "不当作通过" in capsys.readouterr().err


def test_save_writes_a_replayable_response(tmp_path):
    sbom_path, response_path = _files(tmp_path)
    saved = tmp_path / "out" / "osv.json"
    vs.main(["--sbom", str(sbom_path), "--offline", str(response_path),
             "--waivers", str(tmp_path / "absent.json"), "--save", str(saved),
             "--today", "2026-08-06"])
    assert json.loads(saved.read_text(encoding="utf-8")) == RESPONSE


# ---------------- 仓库里那份 ----------------

def test_the_committed_waivers_parse_and_carry_a_real_argument():
    """挡住把 reason 写成'风险低'。豁免要能被人推翻才有意义。"""
    path = ROOT / "security" / "vuln-waivers.json"
    raw = json.loads(path.read_text(encoding="utf-8"))
    live, _ = vs.load_waivers(path, date(2026, 8, 6))
    assert live, "至少解析得出来"
    for item in raw["waivers"]:
        assert len(item["reason"]) >= 60, f"{item['id']} 的理由太短，说不清为什么不适用"
        assert item.get("exit"), f"{item['id']} 没写什么时候解除"


def test_replaying_the_recorded_response_is_clean_as_of_2026_08_06():
    """钉住 2026-08-06 那次真扫的结论。它证明的是那份记录，不是今天的世界——
    今天的结论要靠 CI 里连网那一步重新查。"""
    assert vs.main([
        "--sbom", str(ROOT / "sbom.cdx.json"),
        "--offline", str(ROOT / "security" / "osv-response.json"),
        "--waivers", str(ROOT / "security" / "vuln-waivers.json"),
        "--today", "2026-08-06"]) == 0
