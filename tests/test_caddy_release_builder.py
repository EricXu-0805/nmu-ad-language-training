"""Release builder fails before publishing when source/toolchain trust fails."""
from __future__ import annotations

import hashlib
import io
import json
from pathlib import Path
import platform
import shlex
import shutil
import subprocess
import tarfile

import pytest


ROOT = Path(__file__).resolve().parents[1]


def _fixture(tmp_path: Path, go_response: str, *, go_license: bytes | None = b"Go license fixture"):
    root = tmp_path / "repo"
    (root / "scripts").mkdir(parents=True)
    (root / "deploy").mkdir()
    shutil.copytree(ROOT / "deploy/caddy", root / "deploy/caddy")
    shutil.copyfile(ROOT / "deploy/caddy/main.go", root / "upstream-main.go")
    shutil.copyfile(ROOT / "scripts/build_caddy_release.sh", root / "scripts/build_caddy_release.sh")
    recipe = json.loads((ROOT / "deploy/caddy-build.json").read_text())
    archive = tmp_path / "toolchain.tar.gz"
    script = ("#!/bin/sh\nfixture_root=" + shlex.quote(str(root)) + "\n" + go_response + "\n").encode()
    with tarfile.open(archive, "w:gz") as tar:
        member = tarfile.TarInfo("go/bin/go")
        member.size = len(script)
        member.mode = 0o755
        tar.addfile(member, io.BytesIO(script))
        if go_license is not None:
            member = tarfile.TarInfo("go/LICENSE")
            member.size = len(go_license)
            tar.addfile(member, io.BytesIO(go_license))
    host = platform.system().lower() + "-" + {"x86_64": "amd64", "aarch64": "arm64"}.get(platform.machine(), platform.machine())
    recipe["go"]["archives"][host]["sha256"] = hashlib.sha256(archive.read_bytes()).hexdigest()
    (root / "deploy/caddy-build.json").write_text(json.dumps(recipe))
    return root, archive, recipe


def _build(root: Path, archive: Path, output: Path):
    return subprocess.run(
        ["bash", str(root / "scripts/build_caddy_release.sh"), "--output", str(output),
         "--toolchain-archive", str(archive)], capture_output=True, text=True, timeout=30,
    )


def test_wrong_archive_hash_refuses_before_executing_any_archive_content(tmp_path):
    root, archive, _ = _fixture(tmp_path, "echo SHOULD_NOT_EXECUTE >&2; exit 91")
    archive.write_bytes(archive.read_bytes() + b"tampered")
    output = tmp_path / "release"
    result = _build(root, archive, output)
    assert result.returncode != 0
    assert "SHA-256 mismatch" in result.stderr
    assert "SHOULD_NOT_EXECUTE" not in result.stderr
    assert not output.exists()


def test_archive_with_wrong_runtime_go_version_cannot_emit_a_release(tmp_path):
    root, archive, _ = _fixture(tmp_path, "echo go1.26.3")
    output = tmp_path / "release"
    result = _build(root, archive, output)
    assert result.returncode != 0
    assert "runtime version mismatch" in result.stderr
    assert not output.exists()


def test_source_module_checksum_mismatch_is_rejected_after_toolchain_check(tmp_path):
    response = 'if [ "$1" = env ]; then echo go1.26.8; else echo \'{"Path":"github.com/caddyserver/caddy/v2","Version":"v2.11.4","Sum":"h1:wrong","GoModSum":"h1:wrong"}\'; fi'
    root, archive, _ = _fixture(tmp_path, response)
    output = tmp_path / "release"
    result = _build(root, archive, output)
    assert result.returncode != 0
    assert "source module checksum mismatch" in result.stderr
    assert not output.exists()


def test_existing_release_is_preserved_and_never_overwritten(tmp_path):
    root, archive, _ = _fixture(tmp_path, "exit 91")
    output = tmp_path / "release"
    output.mkdir()
    (output / "caddy").write_bytes(b"previous-release")
    result = _build(root, archive, output)
    assert result.returncode != 0
    assert "new or empty" in result.stderr
    assert (output / "caddy").read_bytes() == b"previous-release"


def test_source_commit_must_match_the_pinned_official_tag(tmp_path):
    root, archive, recipe = _fixture(tmp_path, "exit 91")
    info = tmp_path / "source-info.json"
    info.write_text(json.dumps({"Origin": {"Hash": "0" * 40}}))
    caddy = recipe["caddy"]
    response = json.dumps({
        "Path": caddy["module"], "Version": caddy["version"],
        "Sum": caddy["source_sum"], "GoModSum": caddy["go_mod_sum"], "Info": str(info),
    })
    root, archive, _ = _fixture(tmp_path / "second", f'if [ "$1" = env ]; then echo go1.26.8; else echo \'{response}\'; fi')
    result = _build(root, archive, tmp_path / "release")
    assert result.returncode != 0
    assert "source commit mismatch" in result.stderr
    assert not (tmp_path / "release").exists()


def test_recipe_cannot_select_a_go_patch_before_the_security_fix(tmp_path):
    root, archive, recipe = _fixture(tmp_path, "exit 91")
    recipe["go"]["version"] = "1.26.3"
    (root / "deploy/caddy-build.json").write_text(json.dumps(recipe))
    result = _build(root, archive, tmp_path / "release")
    assert result.returncode != 0
    assert "security floor" in result.stderr


def _source_response(*, caddy_version=None, missing_license="", old_crypto=False, replacement=False, mutate_lock=False):
    recipe = json.loads((ROOT / "deploy/caddy-build.json").read_text())
    caddy = recipe["caddy"]
    module_config = {
        "Module": {"Path": recipe["build_module"]["module"]},
        "Require": [{"Path": name, "Version": version} for name, version in {
            caddy["module"]: caddy["version"], **recipe["dependency_pins"],
        }.items()],
        "Replace": [{"Old": {"Path": caddy["module"]}, "New": {"Path": "./replacement"}}] if replacement else None,
    }
    dependencies = dict(recipe["dependency_pins"])
    if old_crypto:
        dependencies["golang.org/x/crypto"] = "v0.52.0"
    dep_lines = "".join(f"\\tdep\\t{name}\\t{version}\\th1:fixture\\n" for name, version in dependencies.items())
    return f'''
case "$1 $2" in
  "env GOVERSION") echo go1.26.8 ;;
  "mod download")
    mkdir -p "$GOMODCACHE/source/cmd/caddy"
    cp "$fixture_root/upstream-main.go" "$GOMODCACHE/source/cmd/caddy/main.go"
    printf 'module fixture\\n' > "$GOMODCACHE/source/go.mod"
    printf 'fixture sum\\n' > "$GOMODCACHE/source/go.sum"
    if [ '{missing_license}' != caddy ]; then printf 'Caddy license fixture' > "$GOMODCACHE/source/LICENSE"; fi
    if [ '{mutate_lock}' = True ]; then printf changed >> "$fixture_root/deploy/caddy/go.sum"; fi
    printf '%s' '{{"Origin":{{"Hash":"{caddy['source_commit']}"}}}}' > "$GOMODCACHE/source/info.json"
    printf '{{"Path":"{caddy['module']}","Version":"{caddy['version']}","Sum":"{caddy['source_sum']}","GoModSum":"{caddy['go_mod_sum']}","Info":"%s/source/info.json","Dir":"%s/source"}}' "$GOMODCACHE" "$GOMODCACHE"
    ;;
  "mod edit") printf '%s' {shlex.quote(json.dumps(module_config))} ;;
  "build -mod=readonly") printf fixture > "$8" ;;
  "mod verify") true ;;
  "version -m")
    printf '%s: go1.26.8\\n' "$3"
    printf '\\tpath\\t{recipe['build_module']['module']}\\n\\tdep\\t{caddy['module']}\\t{caddy_version or caddy['version']}\\t{caddy['source_sum']}\\n'
    printf '{dep_lines}'
    printf '\\tbuild\\tCGO_ENABLED=0\\n\\tbuild\\tGOOS=linux\\n\\tbuild\\tGOARCH=amd64\\n\\tbuild\\tGOAMD64=v1\\n\\tbuild\\t-tags=nobadger\\n'
    ;;
  *) exit 91 ;;
esac
'''


def test_devel_caddy_dependency_cannot_hide_caddy_version_from_binary_scanners(tmp_path):
    root, archive, _ = _fixture(tmp_path, _source_response(caddy_version="(devel)"))
    output = tmp_path / "release"
    result = _build(root, archive, output)
    assert result.returncode != 0
    assert "pinned Caddy module version" in result.stderr
    assert not output.exists()


@pytest.mark.parametrize("missing", ["caddy", "go"])
def test_missing_redistribution_license_cannot_publish_a_release(tmp_path, missing):
    root, archive, _ = _fixture(tmp_path, _source_response(missing_license=missing),
                                go_license=None if missing == "go" else b"Go license fixture")
    output = tmp_path / "release"
    result = _build(root, archive, output)
    assert result.returncode != 0
    assert f"missing or invalid redistribution license: LICENSE.{missing}" in result.stderr
    assert not output.exists()


@pytest.mark.parametrize(("kwargs", "error"), [
    ({"old_crypto": True}, "wrong controlled dependency: golang.org/x/crypto"),
    ({"replacement": True}, "dependency pins or module configuration mismatch"),
    ({"mutate_lock": True}, "source changed before compilation"),
])
def test_controlled_dependency_contract_refuses_drift(tmp_path, kwargs, error):
    root, archive, _ = _fixture(tmp_path, _source_response(**kwargs))
    output = tmp_path / "release"
    result = _build(root, archive, output)
    assert result.returncode != 0
    assert error in result.stderr
    assert not output.exists()


def test_controlled_entrypoint_must_equal_verified_upstream_source(tmp_path):
    root, archive, _ = _fixture(tmp_path, _source_response())
    (root / "deploy/caddy/main.go").write_text("package main\nfunc main() {}\n")
    output = tmp_path / "release"
    result = _build(root, archive, output)
    assert result.returncode != 0
    assert "differs from the verified upstream source" in result.stderr
    assert not output.exists()
