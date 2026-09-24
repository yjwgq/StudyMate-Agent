#!/usr/bin/env bash
# 本地跑一遍 CI 的三道门禁（与 .github/workflows/ci.yml 逐步对应）。
#
# 用途：推送前自检 —— 网络/额度受限或想提前发现问题时不必等 GitHub Actions。
# 用法（Git Bash / WSL / Linux）：
#     bash scripts/ci_local.sh
set -uo pipefail

cd "$(dirname "$0")/.." || exit 1
fail=0

step() { echo; echo "=== $* ==="; }
check() { if [ "$1" -ne 0 ]; then echo "✗ 上一步失败"; fail=1; fi; }

step "后端 · 安装依赖（uv sync --frozen）"
uv sync --frozen
check $?

step "后端 · ruff"
uv run ruff check .
check $?

step "后端 · mypy"
uv run mypy agent apps evals worker scripts
check $?

step "后端 · pytest tests/unit"
uv run pytest tests/unit -q
check $?

step "前端 · 安装依赖（npm ci）"
( cd apps/web && npm ci )
check $?

step "前端 · tsc --noEmit"
( cd apps/web && npx tsc --noEmit )
check $?

step "前端 · next build"
( cd apps/web && NEXT_TELEMETRY_DISABLED=1 npm run build > /tmp/next-build.log 2>&1 && tail -3 /tmp/next-build.log )
check $?

step "迁移 · alembic history（不连库）"
uv run alembic history > /dev/null
check $?

step "迁移 · 迁移脚本编译"
uv run python -c "import pathlib; [compile(p.read_text(encoding='utf-8'), str(p), 'exec') for p in pathlib.Path('infra/alembic/versions').glob('*.py')]; print('ok')"
check $?

echo
if [ "$fail" -eq 0 ]; then
  echo "✅ 本地 CI 全绿（与 ci.yml 的三道门禁对应）"
else
  echo "❌ 本地 CI 有失败项，修完再推"
fi
exit "$fail"
