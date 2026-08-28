#!/usr/bin/env bash
# 一次跑完所有"合并前必须绿"的检查。
#
# 这个脚本是本地与 CI 共用的核心判据；workflow 只负责准备矩阵环境并调它。
# 文件存在不等于 GitHub required check 已启用或当前 SHA 已经跑绿，那要看云端证据。
#
# 用法：
#   scripts/ci_gate.sh                         # 全跑
#   scripts/ci_gate.sh --offline-osv           # 仅离线重放 OSV 应答
#   scripts/ci_gate.sh --only backend          # CI 与本地共用的后端门
#   scripts/ci_gate.sh --only frontend         # CI 与本地共用的前端门
#   scripts/ci_gate.sh --only supply-chain     # CI 与本地共用的供应链门
#   NMU_GATE_PYTHON=/path/to/python scripts/ci_gate.sh
set -uo pipefail                    # 故意不加 -e：每一关都要跑完再汇总，不是撞到第一个就跑

REPO="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO"

PYTHON="${NMU_GATE_PYTHON:-$REPO/.venv/bin/python}"
command -v "$PYTHON" >/dev/null 2>&1 || PYTHON=python3
OFFLINE_OSV=0
ONLY="all"
EXPECTED_NODE_MAJOR="25"
EXPECTED_RUFF_VERSION="0.15.15"

usage() {
  cat <<'EOF'
用法: scripts/ci_gate.sh [--only backend|frontend|supply-chain] [--offline-osv]

  无参数                    运行所有本地门禁
  --only <suite>           只运行一组共享门禁（供 CI 矩阵调用）
  --offline-osv            只让漏洞扫描重放 security/osv-response.json

--offline-osv 不是“全离线”模式：锁自洽检查仍可能需要从包源取得已锁依赖。
脚本不会往当前 Python 或 web/node_modules 安装依赖；请先准备好环境。
EOF
}

while [ "$#" -gt 0 ]; do
  case "$1" in
    --only)
      if [ "$#" -lt 2 ]; then
        echo "--only 缺少 suite" >&2
        usage >&2
        exit 64
      fi
      ONLY="$2"
      shift 2
      ;;
    --offline-osv)
      OFFLINE_OSV=1
      shift
      ;;
    --offline)
      echo "--offline 会让人误以为全程不出网，已拒绝；仅重放 OSV 请用 --offline-osv。" >&2
      exit 64
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "未知参数: $1" >&2
      usage >&2
      exit 64
      ;;
  esac
done

case "$ONLY" in
  all|backend|frontend|supply-chain) ;;
  *)
    echo "未知 suite: $ONLY" >&2
    usage >&2
    exit 64
    ;;
esac

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

ruff_check() {
  local reported_version
  local runner=()

  if "$PYTHON" -m ruff --version >/dev/null 2>&1; then
    runner=("$PYTHON" -m ruff)
  elif command -v ruff >/dev/null 2>&1; then
    # 保留仓库原有的本机习惯：开发 venv 可不装 ruff，但 PATH 上的
    # runner 必须与 CI 钉住的版本逐字一致，不得悄悄换规则集。
    runner=(ruff)
  else
    echo "找不到 ruff ${EXPECTED_RUFF_VERSION}；脚本不会自动安装"
    return 1
  fi

  reported_version="$("${runner[@]}" --version 2>/dev/null)"
  if [ "$reported_version" != "ruff $EXPECTED_RUFF_VERSION" ]; then
    echo "ruff 版本不一致：当前 ${reported_version:-unknown}，CI 要求 ruff ${EXPECTED_RUFF_VERSION}"
    return 1
  fi
  "${runner[@]}" check .
}

frontend() {
  local actual_node_major
  if ! command -v node >/dev/null 2>&1; then
    echo "找不到 Node.js；前端门禁需要 Node ${EXPECTED_NODE_MAJOR}.x"
    return 1
  fi
  actual_node_major="$(node -p 'process.versions.node.split(".")[0]' 2>/dev/null)"
  if [ "$actual_node_major" != "$EXPECTED_NODE_MAJOR" ]; then
    echo "Node major 不一致：当前 ${actual_node_major:-unknown}，CI 与本地门禁要求 ${EXPECTED_NODE_MAJOR}.x"
    return 1
  fi
  ( cd web && npm run lint && npm run pretest && npm test && npm run build ) 2>&1 \
    | grep -vE '^\s*$|^> ' | tail -20
  return "${PIPESTATUS[0]}"
}

vulnerabilities() {
  if [ "$OFFLINE_OSV" = 1 ]; then
    "$PYTHON" scripts/vuln_scan.py --offline security/osv-response.json
  else
    "$PYTHON" scripts/vuln_scan.py
  fi
}

# 单独建一个只装锁的干净环境，用它证明"锁本身自洽"：--require-hashes 装得上、
# 且装完之后的集合与锁逐个对得上。拿开发机的 .venv 验这件事没有意义,那里面
# 有 pytest 之类锁外的东西。
#
# 解释器版本从锁自己的头部读，不写死：锁是 uv 按某个 --python-version 编出来的，
# 里面的 marker 分叉（例如 websockets 17 要 >=3.11）只在那个版本上成立。曾经这里
# 写死 3.10 而锁早已重出成 3.12，这一关就一直红——而 GitHub 上那份工作流用的是
# 3.12，本地和云上因此对不上账。
lock_python_version() {
  sed -n 's/.*--python-version \([0-9][0-9.]*\).*/\1/p' \
    requirements-deploy.lock.txt | head -1
}

locked_environment() {
  local tmpdir venv
  tmpdir="$(mktemp -d)" || { echo "无法创建锁检查临时目录"; return 1; }
  venv="$tmpdir/venv"
  local pyver; pyver="$(lock_python_version)"
  if [ -z "$pyver" ]; then
    echo "锁头部没写 --python-version，无法确定干净环境该用哪个解释器"
    rm -rf "$tmpdir"
    return 1
  fi
  if command -v uv >/dev/null 2>&1; then
    uv venv --python "$pyver" "$venv" >/dev/null 2>&1 \
      && uv pip install --python "$venv/bin/python" --quiet \
           --require-hashes -r requirements-deploy.lock.txt
  else
    "$PYTHON" -m venv "$venv" \
      && "$venv/bin/python" -m pip install --quiet --require-hashes \
           -r requirements-deploy.lock.txt \
      && "$venv/bin/python" -m pip uninstall -y pip >/dev/null
  fi || {
    echo "按锁装不出干净环境"
    rm -rf "$tmpdir"
    return 1
  }
  "$PYTHON" scripts/supply_chain_check.py --python "$venv/bin/python"
  local code=$?
  rm -rf "$tmpdir"
  return $code
}

if [ "$ONLY" = "all" ] || [ "$ONLY" = "backend" ]; then
  stage "ruff"        ruff_check
  stage "后端 pytest"  "$PYTHON" -m pytest -q
fi

if [ "$ONLY" = "all" ] || [ "$ONLY" = "frontend" ]; then
  stage "前端" frontend
fi

if [ "$ONLY" = "all" ] || [ "$ONLY" = "supply-chain" ]; then
  stage "SBOM 一致" "$PYTHON" scripts/generate_sbom.py --check
  stage "漏洞扫描"  vulnerabilities
  stage "锁自洽"    locked_environment
fi

echo ""
echo "═════ 汇总 ═════"
# 本机门 ≠ GitHub CI 全绿。image job（容器构建 + trivy 扫描）本机没有 docker
# 守护进程时跑不了，2026-08-25 起它红了三天而本机六关一直全绿——「门禁全过」
# 因此被写成了「CI 全绿」。名单由 tests/test_ci_job_coverage.py 钉住。
echo "  （本机不覆盖：image —— 容器构建+trivy 扫描，只在 GitHub CI 上跑）"
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
