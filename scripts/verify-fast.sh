#!/usr/bin/env bash
#
# fast lane 便捷入口 —— 「改一行代码」的秒级验证通道。
#
# 用法:
#   ./scripts/verify-fast.sh                 # 验证本次 git 改动（默认）
#   ./scripts/verify-fast.sh --all           # 全量验证（L3）
#   ./scripts/verify-fast.sh --path backend/apps/trading/executor.py
#   ./scripts/verify-fast.sh --changed --json
#   ./scripts/verify-fast.sh --baseline-save # 把当前状态记为验证基线
#   ./scripts/verify-fast.sh --list-rules    # 打印映射规则表
#
# 为什么要有这个 wrapper:
#   1. venv 在 backend/.venv 且**没有 pip**，必须用 `source .venv/bin/activate`
#      或直接用 .venv/bin/python，不能靠 `uv run`（本机 uv 未必可用）。
#   2. 必须把 cwd 切到 backend/，pytest 的 rootdir / Django settings 才对。
#   3. 统一把退出码原样透传，方便 CI / pre-commit / hook 使用。
#
# 退出码: 0=通过  1=失败  2=参数/环境错误  3=通过但有已知收集错误被跳过
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
BACKEND="${ROOT}/backend"

if [[ ! -d "${BACKEND}/.venv" ]]; then
  echo "[verify-fast] 找不到 ${BACKEND}/.venv" >&2
  echo "[verify-fast] 请先初始化后端虚拟环境（见 AGENTS.md 的「环境初始化」）。" >&2
  exit 2
fi

PY="${BACKEND}/.venv/bin/python"
if [[ ! -x "${PY}" ]]; then
  echo "[verify-fast] ${PY} 不可执行" >&2
  exit 2
fi

cd "${BACKEND}"

# 默认走 --changed；若用户已显式给出模式或纯信息类参数，则原样透传。
exec "${PY}" scripts/verify_fast.py "$@"
