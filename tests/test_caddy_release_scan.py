"""An empty scanner result must never authorize an HTTPS gateway release."""
from copy import deepcopy
import hashlib
import json

import pytest

from scripts.verify_caddy_release_scan import ScanError, verify, verify_build_binding


BUILD_INFO = (
    "/private/build/caddy: go1.26.8\n\tpath\tgithub.com/caddyserver/caddy/v2/cmd/caddy\n"
    "\tmod\tgithub.com/caddyserver/caddy/v2\tv2.11.4\th1:source-checksum\n"
)
REPORT = {"Results": [{
    "Target": "caddy", "Class": "lang-pkgs", "Type": "gobinary",
    "Packages": [{"Name": "stdlib", "Version": "v1.26.8"},
                 {"Name": "github.com/caddyserver/certmagic", "Version": "v0.25.3"},
                 {"Name": "github.com/caddyserver/caddy/v2", "Version": "v2.11.4"}],
}]}


def test_accepts_actual_binary_and_dependency_inventory():
    assert verify(REPORT, BUILD_INFO) == 3


@pytest.mark.parametrize("report", [{}, {"Results": []}, {"Results": [{"Type": "gomod"}]}])
def test_empty_or_source_only_report_cannot_pass(report):
    with pytest.raises(ScanError):
        verify(report, BUILD_INFO)


def test_scan_of_another_binary_cannot_substitute_for_caddy():
    report = deepcopy(REPORT)
    report["Results"][0]["Target"] = "another-tool"
    with pytest.raises(ScanError, match="coverage_missing"):
        verify(report, BUILD_INFO)


def test_old_standard_library_cannot_match_new_build_receipt():
    report = deepcopy(REPORT)
    report["Results"][0]["Packages"][0]["Version"] = "v1.26.3"
    with pytest.raises(ScanError, match="build_version"):
        verify(report, BUILD_INFO)


def test_missing_stdlib_or_caddy_dependencies_is_not_a_clean_scan():
    for index in (0, 1, 2):
        report = deepcopy(REPORT)
        del report["Results"][0]["Packages"][index]
        with pytest.raises(ScanError):
            verify(report, BUILD_INFO)


@pytest.mark.parametrize("version", ["(devel)", "v2.11.3"])
def test_unknown_or_different_main_module_cannot_hide_caddy_vulnerabilities(version):
    report = deepcopy(REPORT)
    report["Results"][0]["Packages"][2]["Version"] = version
    with pytest.raises(ScanError, match="caddy_main_module"):
        verify(report, BUILD_INFO)


@pytest.mark.parametrize("severity", ["HIGH", "CRITICAL"])
def test_high_vulnerability_blocks_even_without_an_upstream_fix(severity):
    report = deepcopy(REPORT)
    report["Results"][0]["Vulnerabilities"] = [{"Severity": severity, "FixedVersion": ""}]
    with pytest.raises(ScanError, match="high_or_critical"):
        verify(report, BUILD_INFO)


def test_build_information_requires_exact_go_version():
    with pytest.raises(ScanError, match="go_build_version_missing"):
        verify(REPORT, "caddy: unknown\n")


@pytest.mark.parametrize("vulnerabilities", [{}, "", [{}]])
def test_malformed_vulnerability_results_do_not_mean_zero_findings(vulnerabilities):
    report = deepcopy(REPORT)
    report["Results"][0]["Vulnerabilities"] = vulnerabilities
    with pytest.raises(ScanError):
        verify(report, BUILD_INFO)


def _artifact(tmp_path):
    root = tmp_path / "repo"
    artifact = tmp_path / "artifact"
    (root / "deploy").mkdir(parents=True)
    (root / "scripts").mkdir()
    artifact.mkdir()
    recipe = b'{"go":{"version":"1.26.8"}}\n'
    builder = b"reviewed build recipe\n"
    binary = b"compiled artifact bytes"
    (root / "deploy/caddy-build.json").write_bytes(recipe)
    (root / "scripts/build_caddy_release.sh").write_bytes(builder)
    (artifact / "caddy-build.json").write_bytes(recipe)
    (artifact / "caddy").write_bytes(binary)
    (artifact / "go-build-info.txt").write_text(BUILD_INFO)
    receipt = {
        "schema_version": "nmu.caddy-build-receipt.v1", "repository_commit": "a" * 40,
        "repository_clean": True, "go_version": "go1.26.8",
        **{name: hashlib.sha256(value).hexdigest() for name, value in {
            "recipe_sha256": recipe, "builder_sha256": builder,
            "binary_sha256": binary, "go_build_info_sha256": BUILD_INFO.encode(),
        }.items()},
    }
    (artifact / "build-receipt.json").write_text(json.dumps(receipt))
    return root, artifact, receipt


def test_clean_candidate_receipt_binds_exact_compiled_bytes(tmp_path):
    root, artifact, _ = _artifact(tmp_path)
    assert verify_build_binding(artifact, "a" * 40, root) == BUILD_INFO
    (artifact / "caddy").write_bytes(b"binary changed after build")
    with pytest.raises(ScanError, match="hash_mismatch"):
        verify_build_binding(artifact, "a" * 40, root)


@pytest.mark.parametrize("change", [{"repository_clean": False}, {"repository_commit": "b" * 40}])
def test_dirty_or_different_candidate_is_not_a_release_receipt(tmp_path, change):
    root, artifact, receipt = _artifact(tmp_path)
    receipt.update(change)
    (artifact / "build-receipt.json").write_text(json.dumps(receipt))
    with pytest.raises(ScanError, match="clean_candidate"):
        verify_build_binding(artifact, "a" * 40, root)
