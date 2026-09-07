#!/usr/bin/env python3
"""Reject empty or unrelated Trivy scans of the controlled Caddy binary.

The CI invocation supplies the scan and Go's actual binary build information.
This checks scanner coverage, not the trustworthiness of arbitrary input files.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import sys


class ScanError(ValueError):
    pass


def verify_build_binding(artifact: Path, revision: str, root: Path) -> str:
    def data(path: Path) -> bytes:
        if path.is_symlink() or not path.is_file() or path.stat().st_size > 128 * 1024 * 1024:
            raise ScanError("build_artifact_invalid")
        return path.read_bytes()

    receipt = json.loads(data(artifact / "build-receipt.json"))
    if (not re.fullmatch(r"[0-9a-f]{40}", revision)
            or not isinstance(receipt, dict)
            or receipt.get("schema_version") != "nmu.caddy-build-receipt.v1"
            or receipt.get("repository_commit") != revision
            or receipt.get("repository_clean") is not True):
        raise ScanError("build_not_bound_to_clean_candidate")
    recipe_bytes = data(root / "deploy/caddy-build.json")
    recipe = json.loads(recipe_bytes)
    if not isinstance(recipe, dict) or not isinstance(recipe.get("go"), dict):
        raise ScanError("build_recipe_invalid")
    sources = receipt.get("controlled_sources_sha256")
    if not isinstance(sources, dict) or set(sources) != {
        "deploy/caddy/main.go", "deploy/caddy/go.mod", "deploy/caddy/go.sum",
    }:
        raise ScanError("controlled_source_receipt_missing")
    for name, digest in sources.items():
        if hashlib.sha256(data(root / name)).hexdigest() != digest:
            raise ScanError("controlled_source_hash_mismatch")
    inputs = {
        "recipe_sha256": recipe_bytes,
        "builder_sha256": data(root / "scripts/build_caddy_release.sh"),
        "binary_sha256": data(artifact / "caddy"),
        "go_build_info_sha256": data(artifact / "go-build-info.txt"),
    }
    if any(receipt.get(key) != hashlib.sha256(value).hexdigest() for key, value in inputs.items()):
        raise ScanError("build_artifact_or_source_hash_mismatch")
    licenses = receipt.get("licenses_sha256")
    if not isinstance(licenses, dict) or set(licenses) != {"LICENSE.caddy", "LICENSE.go"}:
        raise ScanError("build_license_receipt_missing")
    for name, digest in licenses.items():
        license_bytes = data(artifact / name)
        if not license_bytes or hashlib.sha256(license_bytes).hexdigest() != digest:
            raise ScanError("build_license_missing_or_changed")
    if data(artifact / "caddy-build.json") != recipe_bytes:
        raise ScanError("build_recipe_copy_mismatch")
    expected_go = "go" + str(recipe["go"].get("version"))
    build_info = inputs["go_build_info_sha256"].decode("utf-8")
    if (receipt.get("go_version") != expected_go or not build_info.splitlines()
            or not build_info.splitlines()[0].endswith(": " + expected_go)):
        raise ScanError("build_go_version_differs_from_recipe")
    pins = recipe.get("dependency_pins")
    if not isinstance(pins, dict) or set(pins) != {
        "golang.org/x/crypto", "golang.org/x/net", "golang.org/x/text", "google.golang.org/grpc",
    }:
        raise ScanError("dependency_security_pins_missing")
    for name, version in pins.items():
        versions = re.findall(r"^\tdep\t" + re.escape(name) + r"\t([^\t\n]+)\t", build_info, re.MULTILINE)
        if versions != [version]:
            raise ScanError("binary_dependency_differs_from_security_pin")
    return build_info


def verify(report: object, build_info: str) -> int:
    first_line = build_info.splitlines()[0] if build_info else ""
    version = re.search(r": go(\d+\.\d+\.\d+)$", first_line.strip())
    if version is None:
        raise ScanError("go_build_version_missing")
    expected = version[1]
    if not isinstance(report, dict) or not isinstance(report.get("Results"), list):
        raise ScanError("scan_results_missing")
    binaries = [result for result in report["Results"]
                if isinstance(result, dict) and result.get("Type") == "gobinary"
                and isinstance(result.get("Target"), str)
                and PurePosixPath(result["Target"]).name == "caddy"]
    if len(binaries) != 1:
        raise ScanError("caddy_binary_coverage_missing_or_ambiguous")
    packages = binaries[0].get("Packages")
    if not isinstance(packages, list) or any(not isinstance(p, dict) for p in packages):
        raise ScanError("go_package_inventory_missing")
    standard_library = [p for p in packages if p.get("Name") == "stdlib"]
    if len(standard_library) != 1 or standard_library[0].get("Version") not in {
        expected, f"v{expected}", f"go{expected}",
    }:
        raise ScanError("go_standard_library_not_scanned_at_build_version")
    if not any(p.get("Name") == "github.com/caddyserver/certmagic" for p in packages):
        raise ScanError("caddy_dependency_coverage_missing")
    modules = re.findall(r"^\t(?:mod|dep)\tgithub\.com/caddyserver/caddy/v2\t(v2\.\d+\.\d+)\t", build_info, re.MULTILINE)
    caddy = [p for p in packages if p.get("Name") == "github.com/caddyserver/caddy/v2"]
    if len(modules) != 1 or len(caddy) != 1 or caddy[0].get("Version") not in {
        modules[0], modules[0][1:],
    }:
        raise ScanError("caddy_main_module_not_scanned_at_build_version")
    for name, version in re.findall(r"^\tdep\t([^\t\n]+)\t([^\t\n]+)\t", build_info, re.MULTILINE):
        scanned = [p for p in packages if p.get("Name") == name]
        if len(scanned) != 1 or str(scanned[0].get("Version")).removeprefix("v") != version.removeprefix("v"):
            raise ScanError("compiled_dependency_not_scanned_at_build_version")
    for result in report["Results"]:
        if not isinstance(result, dict):
            raise ScanError("scan_result_malformed")
        vulnerabilities = result.get("Vulnerabilities")
        if vulnerabilities is None:
            vulnerabilities = []
        if not isinstance(vulnerabilities, list):
            raise ScanError("scan_vulnerabilities_malformed")
        for vulnerability in vulnerabilities:
            if not isinstance(vulnerability, dict) or vulnerability.get("Severity") not in {
                "UNKNOWN", "LOW", "MEDIUM", "HIGH", "CRITICAL",
            }:
                raise ScanError("scan_vulnerability_malformed")
            if vulnerability.get("Severity") in {"HIGH", "CRITICAL"}:
                raise ScanError("high_or_critical_vulnerability")
    return len(packages)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("scan", type=Path)
    parser.add_argument("--go-build-info", type=Path, required=True)
    parser.add_argument("--revision", required=True, help="exact clean CI source commit")
    args = parser.parse_args()
    try:
        if args.go_build_info.name != "go-build-info.txt":
            raise ScanError("build_info_filename_invalid")
        build_info = verify_build_binding(
            args.go_build_info.parent, args.revision, Path(__file__).resolve().parents[1])
        packages = verify(json.loads(args.scan.read_text()), build_info)
    except (OSError, UnicodeError, ValueError) as error:
        code = str(error) if isinstance(error, ScanError) else "scan_input_unreadable"
        print(f"REJECTED code={code}", file=sys.stderr)
        return 1
    print(f"OK caddy_binary_and_stdlib_scanned packages={packages}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
