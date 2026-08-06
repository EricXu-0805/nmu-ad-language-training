#!/usr/bin/env bash
# 一次跑完所有"合并前必须绿"的检查。
#
# 这个脚本是 CI 的本体，.github/workflows/ci.yml 只是在云上调它。这样做的理由：
# 仓库直到 2026-08-06 都还没 push 过，一份只存在于 GitHub 的流水线等于没有；
# 而本地能一条命令跑完的东西，今天就在挡问题。
#
# 用法：
#   scripts/ci_gate.sh              # 全跑
#   scripts/ci_gate.sh --offline    # 不出网(漏洞扫描改用存好的 OSV 应答)
#   NMU_GATE_PYTHON=/path/to/python scripts/ci_gate.sh
set -uo pipefail                    # 故意不加 -e：每一关都要跑完再汇总，不是撞到第一个就跑

REPO="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO"

PYTHON="${NMU_GATE_PYTHON:-$REPO/.venv/bin/python}"
[ -x "$PYTHON" ] || PYTHON=python3
OFFLINE=0
[ "${1:-}" = "--offline" ] && OFFLINE=1

names=(); codes=()

stage() {
  local name="$1"; shift
  echo ""
  echo "───── $name ─────"
  "$@"
  local code=$?
  names+=("$name"); codes+=("$code")
  [ $code -eq 0 ] || echo "!! $name 退出码 $code"
}

frontend() {
  ( cd web && npm run lint && npm run pretest && npm test && npm run build ) 2>&1 \
    | grep -vE '^\s*$|^> ' | tail -20
  return "${PIPESTATUS[0]}"
}

vulnerabilities() {
  if [ "$OFFLINE" = 1 ]; then
    "$PYTHON" scripts/vuln_scan.py --offline security/osv-response.json
  else
    "$PYTHON" scripts/vuln_scan.py
  fi
}

# 单独建一个只装锁的干净环境，用它证明"锁本身自洽"：--require-hashes 装得上、
# 且装完之后的集合与锁逐个对得上。拿开发机的 .venv 验这件事没有意义,那里面
# 有 pytest 之类锁外的东西。
locked_environment() {
  local venv; venv="$(mktemp -d)/venv"
  if command -v uv >/dev/null 2>&1; then
    uv venv --python 3.10 "$venv" >/dev/null 2>&1 \
      && uv pip install --python "$venv/bin/python" --quiet \
           --require-hashes -r requirements-deploy.lock.txt
  else
    "$PYTHON" -m venv "$venv" \
      && "$venv/bin/python" -m pip install --quiet --require-hashes \
           -r requirements-deploy.lock.txt
  fi || { echo "按锁装不出干净环境"; return 1; }
  "$PYTHON" scripts/supply_chain_check.py --python "$venv/bin/python"
  local code=$?
  rm -rf "$(dirname "$venv")"
  return $code
}

stage "ruff"        ruff check .
stage "后端 pytest"  "$PYTHON" -m pytest -q
stage "前端"         frontend
stage "SBOM 一致"    "$PYTHON" scripts/generate_sbom.py --check
stage "漏洞扫描"     vulnerabilities
stage "锁自洽"       locked_environment

echo ""
echo "═════ 汇总 ═════"
failed=0
for i in "${!names[@]}"; do
  if [ "${codes[$i]}" -eq 0 ]; then
    printf '  [PASS] %s\n' "${names[$i]}"
  else
    printf '  [FAIL] %s (退出码 %s)\n' "${names[$i]}" "${codes[$i]}"
    failed=1
  fi
done
exit $failed
