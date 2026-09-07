"""Release builder fails before publishing when source/toolchain trust fails."""
from __future__ import annotations

import hashlib
import io
import json
from pathlib import Path
import platform
import shutil
import subprocess
import tarfile


ROOT = Path(__file__).resolve().parents[1]


def _fixture(tmp_path: Path, go_response: str):
    root = tmp_path / "repo"
    (root / "scripts").mkdir(parents=True)
    (root / "deploy").mkdir()
    shutil.copyfile(ROOT / "scripts/build_caddy_release.sh", root / "scripts/build_caddy_release.sh")
    recipe = json.loads((ROOT / "deploy/caddy-build.json").read_text())
    archive = tmp_path / "toolchain.tar.gz"
    script = ("#!/bin/sh\n" + go_response + "\n").encode()
    with tarfile.open(archive, "w:gz") as tar:
        member = tarfile.TarInfo("go/bin/go")
        member.size = len(script)
        member.mode = 0o755
        tar.addfile(member, io.BytesIO(script))
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


def test_devel_main_module_cannot_hide_caddy_version_from_binary_scanners(tmp_path):
    recipe = json.loads((ROOT / "deploy/caddy-build.json").read_text())
    caddy = recipe["caddy"]
    response = f'''
case "$1 $2" in
  "env GOVERSION") echo go1.26.8 ;;
  "mod download")
    mkdir -p "$GOMODCACHE/source"
    printf 'module fixture\\n' > "$GOMODCACHE/source/go.mod"
    printf 'fixture sum\\n' > "$GOMODCACHE/source/go.sum"
    printf '%s' '{{"Origin":{{"Hash":"{caddy['source_commit']}"}}}}' > "$GOMODCACHE/source/info.json"
    printf '{{"Path":"{caddy['module']}","Version":"{caddy['version']}","Sum":"{caddy['source_sum']}","GoModSum":"{caddy['go_mod_sum']}","Info":"%s/source/info.json","Dir":"%s/source"}}' "$GOMODCACHE" "$GOMODCACHE"
    ;;
  "install -mod=readonly")
    mkdir -p "$GOPATH/bin/linux_amd64"
    printf fixture > "$GOPATH/bin/caddy"
    printf fixture > "$GOPATH/bin/linux_amd64/caddy"
    ;;
  "mod verify") true ;;
  "version -m")
    printf '%s: go1.26.8\\n' "$3"
    printf '\\tpath\\t{caddy['module']}/cmd/caddy\\n\\tmod\\t{caddy['module']}\\t(devel)\\t\\n'
    printf '\\tbuild\\tCGO_ENABLED=0\\n\\tbuild\\tGOOS=linux\\n\\tbuild\\tGOARCH=amd64\\n\\tbuild\\tGOAMD64=v1\\n'
    ;;
  *) exit 91 ;;
esac
'''
    root, archive, _ = _fixture(tmp_path, response)
    output = tmp_path / "release"
    result = _build(root, archive, output)
    assert result.returncode != 0
    assert "pinned Caddy module version" in result.stderr
    assert not output.exists()
