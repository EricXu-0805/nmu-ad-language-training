from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
GATE = ROOT / "scripts/ci_gate.sh"
GATE_TEXT = GATE.read_text(encoding="utf-8")
SKIP_ON_MAIN_PUSH_AND_SCHEDULE = "github.event_name != 'push' && github.event_name != 'schedule'"


def _jobs() -> dict[str, str]:
    body = WORKFLOW[WORKFLOW.index("\njobs:\n") + len("\njobs:\n"):]
    parts = re.split(r"^  ([A-Za-z][\w-]*):\n", body, flags=re.M)
    return dict(zip(parts[1::2], parts[2::2]))


def _job_level_ifs(job: str) -> list[str]:
    return re.findall(r"^    if: (.+)$", _jobs()[job], re.M)


def test_workflow_calls_the_shared_gate_for_every_core_suite() -> None:
    assert 'python: ["3.12", "3.14"]' in WORKFLOW
    assert 'node-version: "25"' in WORKFLOW
    assert "pytest==9.1.1 pytest-xdist==3.8.0 ruff==0.15.15" in WORKFLOW
    assert "scripts/ci_gate.sh --only backend" in WORKFLOW
    assert "scripts/ci_gate.sh --only frontend" in WORKFLOW
    assert "scripts/ci_gate.sh --only supply-chain" in WORKFLOW

    # 核心判据不得再在 workflow 里复制一份，否则本地与云端会再次漂移。
    assert "run: ruff check ." not in WORKFLOW
    assert "run: python -m pytest -q" not in WORKFLOW
    assert "run: npm run lint" not in WORKFLOW
    assert "run: python scripts/vuln_scan.py" not in WORKFLOW


def test_cloud_only_image_scan_remains_an_extra_gate() -> None:
    assert "docker build -t nmu-platform:ci ." in WORKFLOW
    assert "aquasecurity/trivy-action@" in WORKFLOW
    assert "severity: HIGH,CRITICAL" in WORKFLOW


def test_python_312_archives_a_source_bound_full_scale_receipt() -> None:
    assert "python -m harness.quality_release_scale --profile full" in WORKFLOW
    assert '--receipt "$receipt_dir/quality-release-scale.json"' in WORKFLOW
    assert "if: matrix.python == '3.12'" in WORKFLOW
    assert "name: quality-release-scale-python-3.12" in WORKFLOW
    assert "if-no-files-found: error" in WORKFLOW


def test_shared_frontend_gate_checks_the_same_node_major_without_installing() -> None:
    assert 'EXPECTED_NODE_MAJOR="25"' in GATE_TEXT
    assert "actual_node_major" in GATE_TEXT
    assert "npm run lint && npm run pretest && npm test && npm run build" in GATE_TEXT
    assert "npm install" not in GATE_TEXT
    assert "npm ci" not in GATE_TEXT


def test_shared_backend_gate_pins_the_same_ruff_version() -> None:
    assert 'EXPECTED_RUFF_VERSION="0.15.15"' in GATE_TEXT
    assert 'reported_version" != "ruff $EXPECTED_RUFF_VERSION' in GATE_TEXT


def test_backend_gate_falls_back_to_the_exact_pinned_path_ruff(tmp_path: Path) -> None:
    fake_python = tmp_path / "python"
    fake_ruff = tmp_path / "ruff"
    ruff_log = tmp_path / "ruff.log"
    ruff_log.write_text("", encoding="utf-8")
    fake_python.write_text(
        "#!/bin/sh\n"
        "if [ \"$1\" = \"-m\" ] && [ \"$2\" = \"ruff\" ]; then exit 1; fi\n"
        "if [ \"$1\" = \"-m\" ] && [ \"$2\" = \"pytest\" ]; then exit 0; fi\n"
        "exit 99\n",
        encoding="utf-8",
    )
    fake_ruff.write_text(
        "#!/bin/sh\n"
        "if [ \"$1\" = \"--version\" ]; then echo 'ruff 0.15.15'; exit 0; fi\n"
        "printf '%s\\n' \"$*\" >> \"$RUFF_LOG\"\n"
        "exit 0\n",
        encoding="utf-8",
    )
    fake_python.chmod(0o755)
    fake_ruff.chmod(0o755)
    env = os.environ.copy()
    env.update(
        {
            "NMU_GATE_PYTHON": str(fake_python),
            "PATH": f"{tmp_path}:{env['PATH']}",
            "RUFF_LOG": str(ruff_log),
        }
    )

    completed = subprocess.run(
        [str(GATE), "--only", "backend"],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
        env=env,
    )

    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert ruff_log.read_text(encoding="utf-8") == "check .\n"


def test_backend_gate_rejects_a_different_path_ruff_version(tmp_path: Path) -> None:
    fake_python = tmp_path / "python"
    fake_ruff = tmp_path / "ruff"
    ruff_log = tmp_path / "ruff.log"
    ruff_log.write_text("", encoding="utf-8")
    fake_python.write_text(
        "#!/bin/sh\n"
        "if [ \"$1\" = \"-m\" ] && [ \"$2\" = \"ruff\" ]; then exit 1; fi\n"
        "if [ \"$1\" = \"-m\" ] && [ \"$2\" = \"pytest\" ]; then exit 0; fi\n"
        "exit 99\n",
        encoding="utf-8",
    )
    fake_ruff.write_text(
        "#!/bin/sh\n"
        "if [ \"$1\" = \"--version\" ]; then echo 'ruff 0.15.14'; exit 0; fi\n"
        "printf '%s\\n' \"$*\" >> \"$RUFF_LOG\"\n"
        "exit 0\n",
        encoding="utf-8",
    )
    fake_python.chmod(0o755)
    fake_ruff.chmod(0o755)
    env = os.environ.copy()
    env.update(
        {
            "NMU_GATE_PYTHON": str(fake_python),
            "PATH": f"{tmp_path}:{env['PATH']}",
            "RUFF_LOG": str(ruff_log),
        }
    )

    completed = subprocess.run(
        [str(GATE), "--only", "backend"],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
        env=env,
    )

    assert completed.returncode == 1
    assert "CI 要求 ruff 0.15.15" in completed.stdout
    assert ruff_log.read_text(encoding="utf-8") == ""


def test_ambiguous_offline_flag_is_rejected_with_the_honest_replacement() -> None:
    completed = subprocess.run(
        [str(GATE), "--offline"],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 64
    assert "--offline-osv" in completed.stderr


def test_help_does_not_promise_a_fully_offline_gate() -> None:
    completed = subprocess.run(
        [str(GATE), "--help"],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0
    assert "--offline-osv" in completed.stdout
    assert "不是“全离线”模式" in completed.stdout


def test_main_runs_are_never_cancelled_or_replaced_by_a_later_merge() -> None:
    # 上线脚本按 SHA 取 main 上成功的 push run;同组排队的 run 会被后来者顶掉,
    # 所以 main 必须按 SHA 分组,而且不取消在跑的。
    assert "github.ref == 'refs/heads/main' && github.sha || github.ref" in WORKFLOW
    assert "cancel-in-progress: ${{ github.ref != 'refs/heads/main' }}" in WORKFLOW


def test_push_runs_only_on_main_and_the_other_triggers_stay() -> None:
    # 分支上的提交由 pull_request 那次跑;分支 push 再跑一遍是同一棵树的第二份分钟账单。
    triggers = WORKFLOW[WORKFLOW.index("\non:\n"):WORKFLOW.index("\npermissions:")]
    assert '\n  push:\n    branches: ["main"]\n' in triggers
    assert '"**"' not in triggers
    for trigger in ("pull_request", "workflow_dispatch", "schedule"):
        assert f"\n  {trigger}:\n" in triggers
    # pull_request 不加任何过滤:PR 少跑一次,五项必需检查就缺名字。
    assert not re.search(r"^  pull_request:\n    ", triggers, re.M)
    assert 'cron: "0 2 * * 1"' in triggers


def test_main_push_and_schedule_skip_the_test_suites_but_still_scan_and_build() -> None:
    # strict 分支保护下,进 main 的树已由 PR run 测过;main 的 push 只为出上线产物,
    # 每周定时只为让 OSV 与镜像扫描看到新通告。backend/frontend 在这两种事件上不重跑。
    for job in ("backend", "frontend"):
        assert _job_level_ifs(job) == [SKIP_ON_MAIN_PUSH_AND_SCHEDULE], job
    for job in ("supply-chain", "image"):
        assert _job_level_ifs(job) == [], f"{job} 必须每种事件都跑"
    jobs = _jobs()
    assert "scripts/ci_gate.sh --only supply-chain" in jobs["supply-chain"]
    assert jobs["image"].count("uses: aquasecurity/trivy-action@") == 2


def test_the_gateway_scan_stays_strict_and_only_waives_dated_entries() -> None:
    """11-02 起 go-jose v3 的 HIGH 没有修复版(见 security/trivy-gateway.trivyignore)。放行只能
    逐条登记、带到期日、写明理由;扫描本身照旧不忽略无修复版的漏洞,镜像那一步不带豁免。"""
    image = _jobs()["image"]
    container_scan, gateway_scan = [step for step in image.split("\n      - ")
                                    if "uses: aquasecurity/trivy-action@" in step]
    assert "trivyignores" not in container_scan
    # Trivy 默认还会读工作目录的这几份;放在仓库根上,镜像那一步会被悄悄放行。
    for default in (".trivyignore", ".trivyignore.yaml", "trivy.yaml"):
        assert not (ROOT / default).exists(), default
    for setting in ("severity: HIGH,CRITICAL", "ignore-unfixed: false",
                    "trivyignores: security/trivy-gateway.trivyignore"):
        assert f"\n          {setting}\n" in gateway_scan + "\n", setting
    text = (ROOT / "security/trivy-gateway.trivyignore").read_text(encoding="utf-8")
    comments = "\n".join(line for line in text.splitlines() if line.startswith("#"))
    entries = [line for line in text.splitlines() if line.strip() and not line.startswith("#")]
    assert entries
    for line in entries:
        matched = re.fullmatch(r"(CVE-\d{4}-\d+|GHSA(?:-[0-9a-z]{4}){3}) exp:(\d{4}-\d{2}-\d{2})", line)
        assert matched, f"每条都要是「编号 exp:日期」:{line}"
        identifier, expires = matched.groups()
        block = next(b for b in comments.split("\n#\n") if identifier in b)
        registered = re.search(r"登记 (\d{4}-\d{2}-\d{2})", block)
        assert registered, f"{identifier} 的说明里没写登记日期"
        from datetime import date
        span = date.fromisoformat(expires) - date.fromisoformat(registered.group(1))
        assert 0 < span.days <= 45, f"{identifier} 的豁免期 {span.days} 天,上限 45 天"
    recipe = json.loads((ROOT / "deploy/caddy-build.json").read_text(encoding="utf-8"))
    unfixed = recipe["security_fix"]["not_fixed_in_this_build"]
    assert f"\n{unfixed['advisory']} exp:{unfixed['waiver_expires']}" in "\n" + text, \
        "配方里记的未修条目要和豁免文件里的编号、到期日一致(它随发布产物走)"


def test_the_go_jose_waiver_holds_only_while_the_gateway_runs_no_acme_server_or_internal_ca() -> None:
    """那条豁免的前提:网关不当 ACME 服务器,外来 JWK 就到不了 go-jose v3。内部 CA 本身不解析外来
    JWK,这里也一并拦,宽一点不吃亏。生产 /opt/nmu/caddy/Caddyfile 不在仓库里,管不到。"""
    waivers = (ROOT / "security/trivy-gateway.trivyignore").read_text(encoding="utf-8")
    if "\nCVE-2026-116587 exp:" not in waivers:
        return
    live = [line.split("#", 1)[0] for line in (ROOT / "Caddyfile").read_text(encoding="utf-8").splitlines()]
    offenders = [line.strip() for line in live
                 if re.search(r"\bacme_server\b|^\s*pki\b|\btls\s+internal\b|\blocal_certs\b", line)]
    assert not offenders, f"Caddyfile 打开了 ACME 服务器或内部 CA,go-jose v3 的豁免不再成立:{offenders}"


def test_backend_legs_have_a_timeout_so_a_hang_cannot_burn_the_minute_budget() -> None:
    # 私有仓库按分钟计;不设 timeout-minutes 时卡住的一条腿要跑满 GitHub 默认 360 分钟,
    # 两条腿一起卡就是 720 分钟。PR 上后端全量约 15–22 分钟。
    backend = _jobs()["backend"]
    assert re.search(r"^    timeout-minutes: 45$", backend, re.M), backend[:400]


def test_release_artifact_is_uploaded_by_main_push_only() -> None:
    # 上线脚本只按 --event push 取 main 上该 SHA 的产物;PR run 构建的是合并预演提交,
    # 每周定时与手动 run 不是上线来源,传了只占私有仓库按量计费的存储。
    image = _jobs()["image"]
    (upload,) = [step for step in image.split("\n      - ") if "name: nmu-release-artifacts" in step]
    assert "\n        if: github.event_name == 'push'\n" in upload
    assert "\n          retention-days: 30\n" in upload
    assert "\n          if-no-files-found: error" in upload
    # PR run 照样构建、扫描、核 dist 与源码的绑定,只少上传这一步。
    assert len(re.findall(r"^        if: ", image, re.M)) == 1


def _run_backend_gate_with_fake_python(tmp_path: Path, *, xdist_importable: bool) -> str:
    tmp_path.mkdir(parents=True, exist_ok=True)
    fake_python = tmp_path / "python"
    fake_ruff = tmp_path / "ruff"
    pytest_log = tmp_path / "pytest.log"
    pytest_log.write_text("", encoding="utf-8")
    fake_python.write_text(
        "#!/bin/sh\n"
        "if [ \"$1\" = \"-m\" ] && [ \"$2\" = \"ruff\" ]; then exit 1; fi\n"
        f"if [ \"$1\" = \"-c\" ]; then exit {0 if xdist_importable else 1}; fi\n"
        "if [ \"$1\" = \"-m\" ] && [ \"$2\" = \"pytest\" ]; then "
        "printf '%s\\n' \"$*\" >> \"$PYTEST_LOG\"; exit 0; fi\n"
        "exit 99\n",
        encoding="utf-8",
    )
    fake_ruff.write_text(
        "#!/bin/sh\n"
        "if [ \"$1\" = \"--version\" ]; then echo 'ruff 0.15.15'; exit 0; fi\n"
        "exit 0\n",
        encoding="utf-8",
    )
    fake_python.chmod(0o755)
    fake_ruff.chmod(0o755)
    env = os.environ.copy()
    env.update({"NMU_GATE_PYTHON": str(fake_python), "PATH": f"{tmp_path}:{env['PATH']}",
                "PYTEST_LOG": str(pytest_log)})
    completed = subprocess.run([str(GATE), "--only", "backend"], cwd=ROOT, check=False,
                               capture_output=True, text=True, env=env)
    assert completed.returncode == 0, completed.stdout + completed.stderr
    return pytest_log.read_text(encoding="utf-8")


def test_backend_gate_runs_pytest_in_parallel_only_when_xdist_is_installed(tmp_path: Path) -> None:
    # 仓库转私有后 CI 按分钟计费;CI 装了 pytest-xdist 就按核数并行,本机没装照旧串行。
    assert _run_backend_gate_with_fake_python(tmp_path / "with", xdist_importable=True) == (
        "-m pytest -q -n auto\n")
    assert _run_backend_gate_with_fake_python(tmp_path / "without", xdist_importable=False) == (
        "-m pytest -q\n")
