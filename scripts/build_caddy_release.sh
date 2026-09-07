#!/usr/bin/env bash
# Build the standard Caddy modules with a reviewed, checksum-pinned Go toolchain.
# This produces a candidate only. CI must scan it before publishing the artifact.
set -euo pipefail
script_dir="$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
exec "${NMU_CADDY_BUILD_PYTHON:-python3}" - "$script_dir/.." "$@" <<'PY'
import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.request


def sha256(path):
    with path.open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()


def run(argv, *, env, cwd, capture=True):
    return subprocess.check_output(argv, env=env, cwd=cwd, text=True) if capture else subprocess.check_call(argv, env=env, cwd=cwd)


def build(root, args):
    recipe_path = root / "deploy/caddy-build.json"
    builder_path = root / "scripts/build_caddy_release.sh"
    recipe_hash, builder_hash = sha256(recipe_path), sha256(builder_path)
    recipe = json.loads(recipe_path.read_text())
    if recipe["schema_version"] != "nmu.caddy-build.v1":
        raise ValueError("unsupported Caddy build contract")
    caddy, toolchain, target = recipe["caddy"], recipe["go"], recipe["target"]
    build_module, dependency_pins = recipe["build_module"], recipe["dependency_pins"]
    if (caddy["module"] != "github.com/caddyserver/caddy/v2"
            or not re.fullmatch(r"v2\.\d+\.\d+", caddy["version"])
            or not re.fullmatch(r"[0-9a-f]{40}", caddy["source_commit"])
            or caddy["entrypoint"] != "./cmd/caddy"
            or caddy["build_tags"] != ["nobadger"]
            or target != {"goos": "linux", "goarch": "amd64", "cgo_enabled": "0"}
            or not re.fullmatch(r"1\.26\.\d+", toolchain["version"])
            or int(toolchain["version"].split(".")[-1]) < 6):
        raise ValueError("unsafe Caddy source, target, or Go security floor")
    if (build_module != {"path": "deploy/caddy", "module": "nmu.local/caddy-release"}
            or set(dependency_pins) != {"golang.org/x/crypto", "golang.org/x/net", "golang.org/x/text", "google.golang.org/grpc"}
            or any(not re.fullmatch(r"v\d+\.\d+\.\d+", version) for version in dependency_pins.values())):
        raise ValueError("invalid controlled Caddy build module")
    controlled_paths = {"deploy/caddy/" + name: root / "deploy/caddy" / name for name in ("main.go", "go.mod", "go.sum")}
    if any(path.is_symlink() or not path.is_file() for path in controlled_paths.values()):
        raise ValueError("missing or invalid controlled Caddy source")
    controlled_hashes = {name: sha256(path) for name, path in controlled_paths.items()}
    host = platform.system().lower() + "-" + {"x86_64": "amd64", "arm64": "arm64", "aarch64": "arm64"}.get(platform.machine(), platform.machine())
    archive_pin = toolchain["archives"][host]
    if (archive_pin["url"] != f"https://go.dev/dl/go{toolchain['version']}.{host}.tar.gz"
            or not re.fullmatch(r"[0-9a-f]{64}", archive_pin["sha256"])):
        raise ValueError("invalid official Go archive pin")
    output = args.output.absolute()
    if output.is_symlink() or (output.exists() and (not output.is_dir() or any(output.iterdir()))):
        raise ValueError("output must be a new or empty ordinary directory")
    # Use a private build tree and publish nothing until every check succeeds.
    with tempfile.TemporaryDirectory(prefix="nmu-caddy-build-") as temporary:
        work = Path(temporary)
        archive = work / "toolchain.tar.gz"
        if args.toolchain_archive:
            shutil.copyfile(args.toolchain_archive, archive)
        else:
            with urllib.request.urlopen(archive_pin["url"], timeout=120) as response, archive.open("wb") as dest:
                shutil.copyfileobj(response, dest)
        if sha256(archive) != archive_pin["sha256"]:
            raise ValueError("Go toolchain archive SHA-256 mismatch")
        with tarfile.open(archive, "r:gz") as contents:
            contents.extractall(work, filter="data")
        go = work / "go/bin/go"
        # Ignore user Go configuration, alternate checksum databases and module
        # replacements. Every dependency stays governed by the controlled lock.
        env = {key: os.environ[key] for key in ("PATH", "HOME", "SYSTEMROOT", "TMPDIR", "SSL_CERT_FILE", "SSL_CERT_DIR", "HTTPS_PROXY", "HTTP_PROXY", "NO_PROXY") if key in os.environ}
        env.update({
            "GOENV": "off", "GOWORK": "off", "GOAUTH": "off",
            "GOTOOLCHAIN": "local", "GOSUMDB": "sum.golang.org",
            "GOPROXY": "https://proxy.golang.org", "GOPRIVATE": "",
            "GONOPROXY": "", "GONOSUMDB": "", "GOFLAGS": "-mod=readonly",
            "GOPATH": str(work / "gopath"), "GOMODCACHE": str(work / "modules"),
            "GOCACHE": str(work / "cache"), "GOTELEMETRY": "off",
            "CGO_ENABLED": "0", "GOOS": "linux", "GOARCH": "amd64", "GOAMD64": "v1",
        })
        expected_go = "go" + toolchain["version"]
        if run([str(go), "env", "GOVERSION"], env=env, cwd=work).strip() != expected_go:
            raise ValueError("Go toolchain runtime version mismatch")
        module = json.loads(run([str(go), "mod", "download", "-json", caddy["module"] + "@" + caddy["version"]], env=env, cwd=work))
        if (module.get("Path") != caddy["module"] or module.get("Version") != caddy["version"]
                or module.get("Sum") != caddy["source_sum"] or module.get("GoModSum") != caddy["go_mod_sum"]):
            raise ValueError("Caddy source module checksum mismatch")
        info = json.loads(Path(module["Info"]).read_text())
        if info.get("Origin", {}).get("Hash") != caddy["source_commit"]:
            raise ValueError("Caddy source commit mismatch")
        source = Path(module["Dir"])
        if not source.is_relative_to(work / "modules") or source.is_symlink():
            raise ValueError("Caddy source escaped the private module cache")
        before = {name: sha256(source / name) for name in ("go.mod", "go.sum")}
        wrapper = work / "wrapper"
        wrapper.mkdir()
        for path in controlled_paths.values():
            shutil.copyfile(path, wrapper / path.name)
        if (wrapper / "main.go").read_bytes() != (source / "cmd/caddy/main.go").read_bytes():
            raise ValueError("controlled Caddy entrypoint differs from the verified upstream source")
        if controlled_hashes != {name: sha256(wrapper / path.name) for name, path in controlled_paths.items()}:
            raise ValueError("controlled Caddy source changed before compilation")
        module_config = json.loads(run([str(go), "mod", "edit", "-json"], env=env, cwd=wrapper))
        required = {item["Path"]: item["Version"] for item in module_config["Require"]}
        if (module_config["Module"]["Path"] != build_module["module"] or module_config.get("Replace")
                or module_config.get("Exclude") or module_config.get("Toolchain")
                or any(required.get(name) != version for name, version in {caddy["module"]: caddy["version"], **dependency_pins}.items())):
            raise ValueError("controlled Caddy dependency pins or module configuration mismatch")
        staged = work / "artifact"
        staged.mkdir()
        binary = staged / "caddy"
        # A minimal upstream entrypoint makes Caddy a versioned dependency while
        # allowing the reviewed security fixes in the complete controlled lock.
        run([str(go), "build", "-mod=readonly", "-trimpath", "-buildvcs=false",
             "-tags=" + ",".join(caddy["build_tags"]), "-ldflags=-s -w",
             "-o", str(binary), "."], env=env, cwd=wrapper, capture=False)
        binary.chmod(0o755)
        run([str(go), "mod", "verify"], env=env, cwd=wrapper, capture=False)
        if before != {name: sha256(source / name) for name in before}:
            raise ValueError("Caddy dependency lock changed during build")
        build_info = run([str(go), "version", "-m", str(binary)], env=env, cwd=work)
        if not build_info.splitlines()[0].endswith(": " + expected_go):
            raise ValueError("emitted binary has the wrong Go version")
        if "\tpath\t" + build_module["module"] + "\n" not in build_info:
            raise ValueError("emitted binary has the wrong Caddy entrypoint")
        if "\tdep\t" + caddy["module"] + "\t" + caddy["version"] + "\t" + caddy["source_sum"] + "\n" not in build_info:
            raise ValueError("emitted binary lacks the pinned Caddy module version")
        for name, version in dependency_pins.items():
            if "\tdep\t" + name + "\t" + version + "\t" not in build_info:
                raise ValueError("emitted binary has the wrong controlled dependency: " + name)
        for setting in ("CGO_ENABLED=0", "GOOS=linux", "GOARCH=amd64", "GOAMD64=v1", "-tags=nobadger"):
            if "\tbuild\t" + setting not in build_info:
                raise ValueError("emitted binary has the wrong build target")
        (staged / "go-build-info.txt").write_text(build_info)
        shutil.copyfile(recipe_path, staged / "caddy-build.json")
        # Preserve the redistribution terms from the verified source archives.
        licenses_sha256 = {}
        for name, license_path in {"LICENSE.caddy": source / "LICENSE", "LICENSE.go": work / "go/LICENSE"}.items():
            if license_path.is_symlink() or not license_path.is_file() or not license_path.stat().st_size:
                raise ValueError("missing or invalid redistribution license: " + name)
            shutil.copyfile(license_path, staged / name)
            licenses_sha256[name] = sha256(staged / name)
        # These are source/build receipts, not vulnerability or deployment claims.
        commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=root, text=True).strip()
        dirty = bool(subprocess.check_output(["git", "status", "--porcelain"], cwd=root, text=True).strip())
        receipt = {
            "schema_version": "nmu.caddy-build-receipt.v1",
            "repository_commit": commit, "repository_clean": not dirty,
            "recipe_sha256": recipe_hash, "builder_sha256": builder_hash,
            "caddy": caddy, "go_version": expected_go, "toolchain_host": host,
            "toolchain_archive": archive_pin, "target": target,
            "source_lock_sha256": before, "binary_sha256": sha256(binary),
            "controlled_sources_sha256": controlled_hashes,
            "binary_size": binary.stat().st_size,
            "go_build_info_sha256": sha256(staged / "go-build-info.txt"),
            "licenses_sha256": licenses_sha256,
            "vulnerability_scan": "required separately before release",
            "deployment_status": "not deployed",
        }
        if (sha256(recipe_path), sha256(builder_path)) != (recipe_hash, builder_hash):
            raise ValueError("build contract changed during compilation")
        if (controlled_hashes != {name: sha256(path) for name, path in controlled_paths.items()}
                or controlled_hashes != {name: sha256(wrapper / path.name) for name, path in controlled_paths.items()}):
            raise ValueError("controlled Caddy source changed during compilation")
        (staged / "build-receipt.json").write_text(json.dumps(receipt, ensure_ascii=False, indent=2) + "\n")
        output.parent.mkdir(parents=True, exist_ok=True)
        # Atomic directory rename on the output filesystem; no partial receipt.
        publish = Path(tempfile.mkdtemp(prefix=".nmu-caddy-", dir=output.parent))
        try:
            shutil.copytree(staged, publish, dirs_exist_ok=True)
            os.replace(publish, output)
        finally:
            if publish.exists():
                shutil.rmtree(publish)
    print("Caddy candidate built and verified; image/binary scan remains required.")


parser = argparse.ArgumentParser(description="Build a checksum-pinned Caddy Linux amd64 release candidate")
parser.add_argument("--output", type=Path, required=True)
parser.add_argument("--toolchain-archive", type=Path, help="reuse an archive; its pinned SHA-256 is still mandatory")
args = parser.parse_args(sys.argv[2:])
try:
    build(Path(sys.argv[1]).resolve(), args)
except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError, tarfile.TarError) as exc:
    print(f"Caddy build refused: {exc}", file=sys.stderr)
    sys.exit(1)
PY
