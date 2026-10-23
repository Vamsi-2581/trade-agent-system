#!/usr/bin/env bash
# 部署本次「池化表读面」修复到 txy（腾讯云 43.152.227.67）。
#
# 这次改动包含：
#   - backend/core/settings/base.py        加 GZipMiddleware（传输层压缩）
#   - backend/apps/regime/{serializers,views}.py  证据按内容去重（86.9MB → 0.61MB）
#   - frontend/.../{api.ts,PoolTable,PoolCellDetail}.tsx  按 evidence_key 查表
#
# **没有新迁移**，所以不需要 migrate。后端是 bind mount（./backend:/app），重启即生效；
# 前端构建进镜像（无 mount），必须 --build。
#
# 用法：./scripts/deploy_txy.sh [--dry-run]
set -euo pipefail

HOST=txy
REMOTE_DIR="\$HOME/trade_agent_sys"
DRY=${1:-}

say() { printf '\n\033[1m== %s\033[0m\n' "$*"; }
run() {
  if [ "$DRY" = "--dry-run" ]; then
    echo "  [dry-run] ssh $HOST: $*"
  else
    ssh "$HOST" "$@"
  fi
}

# ---------------------------------------------------------------------------
say "0/6 前置检查：主机必须先在用户态活过来"
# ---------------------------------------------------------------------------
# 仓库里有记录的故障形态（.review/halftrend_rsi_修复记录.md）：
#   ping 通、TCP 22/3000/8000 全部 OPEN，但 SSH banner 与 HTTP 都不出字节。
# 那不是网络问题，是目标机用户态进程集体卡死。三个条件全过才继续。
if ! ssh -o ConnectTimeout=15 -o BatchMode=yes "$HOST" 'echo OK' >/dev/null 2>&1; then
  echo "❌ SSH 不通（banner 超时）。txy 当前不可部署。" >&2
  echo "   先确认主机已恢复：" >&2
  echo "     ssh txy 'echo OK && uptime && df -h / && free -g'" >&2
  echo "     curl -s -m 5 http://43.152.227.67:8000/ -o /dev/null -w '%{http_code}\\n'  # 非 000" >&2
  exit 1
fi
run 'uptime && echo --- && df -h / && echo --- && free -g'

# 磁盘写满是这台机器卡死的头号嫌疑，且写半截文件比不部署更糟。
say "0b/6 磁盘余量检查（<2G 拒绝部署）"
avail_kb=$(ssh "$HOST" "df -k / | tail -1 | awk '{print \$4}'")
if [ "${avail_kb:-0}" -lt 2097152 ]; then
  echo "❌ / 只剩 $((avail_kb/1024)) MB，先清盘再部署（写半截文件比不部署更糟）。" >&2
  exit 1
fi
echo "✓ / 可用 $((avail_kb/1024/1024)) GB"

# ---------------------------------------------------------------------------
say "1/6 记录当前状态（回滚锚点）"
# ---------------------------------------------------------------------------
run "cd $REMOTE_DIR && git rev-parse --short HEAD && git status --short | head -20"

# ---------------------------------------------------------------------------
say "2/6 上传改动（rsync，只同步代码，不带 .venv/db/logs）"
# ---------------------------------------------------------------------------
if [ "$DRY" = "--dry-run" ]; then
  echo "  [dry-run] rsync 后端与前端源码到 $HOST:$REMOTE_DIR"
else
  rsync -az --delete-delay \
    --exclude='.venv/' --exclude='__pycache__/' --exclude='*.pyc' \
    --exclude='db_dev.sqlite3' --exclude='backups/' --exclude='.env' \
    backend/apps/regime/ "$HOST:$REMOTE_DIR/backend/apps/regime/"
  rsync -az --exclude='.venv/' --exclude='__pycache__/' \
    backend/core/settings/base.py "$HOST:$REMOTE_DIR/backend/core/settings/base.py"
  rsync -az --exclude='node_modules/' \
    frontend/tradeclaw-web/src/lib/api.ts \
    "$HOST:$REMOTE_DIR/frontend/tradeclaw-web/src/lib/api.ts"
  rsync -az --exclude='node_modules/' \
    frontend/tradeclaw-web/src/components/regime/ \
    "$HOST:$REMOTE_DIR/frontend/tradeclaw-web/src/components/regime/"
fi

# ---------------------------------------------------------------------------
say "3/6 后端自检：settings 能被 import、GZipMiddleware 在 MIDDLEWARE 里"
# ---------------------------------------------------------------------------
run "cd $REMOTE_DIR/backend && python -c \"
import os, django
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'core.settings.dev')
django.setup()
from django.conf import settings
mw = [m for m in settings.MIDDLEWARE if 'gzip' in m.lower()]
assert mw, 'GZipMiddleware 不在 MIDDLEWARE 里'
print('MIDDLEWARE gzip:', mw)
from apps.regime import serializers as ser
assert hasattr(ser, 'evidence_dedupe'), 'evidence_dedupe 缺失'
print('serializers OK')
\""

# ---------------------------------------------------------------------------
say "4/6 重启后端 + celery（后端是 bind mount，重启即生效）"
# ---------------------------------------------------------------------------
# 只重启 backend / celery，**不碰 db 与 pgbouncer**：本次没有迁移。
run "cd $REMOTE_DIR && sudo -n docker restart trade_agent_sys-backend-1 trade_agent_sys-celery-1"

say "等待 backend 起来…"
for i in $(seq 1 30); do
  code=$(curl -s -m 5 -o /dev/null -w '%{http_code}' http://43.152.227.67:8000/ || echo 000)
  if [ "$code" != "000" ]; then echo "✓ backend 已应答（HTTP $code，等了 ${i}s）"; break; fi
  sleep 2
  [ "$i" = "30" ] && { echo "❌ backend 90 秒未起来，看日志：sudo docker logs --tail 80 trade_agent_sys-backend-1" >&2; exit 1; }
done

# ---------------------------------------------------------------------------
say "5/6 重建前端镜像（前端构建进镜像，无 mount，必须 --build）"
# ---------------------------------------------------------------------------
run "cd $REMOTE_DIR && sudo -n docker compose build frontend && sudo -n docker compose up -d frontend"

# ---------------------------------------------------------------------------
say "6/6 验收：gzip 生效 + 响应体积"
# ---------------------------------------------------------------------------
run "curl -s -o /dev/null -w '不带 gzip: %{size_download} bytes\\n' \
      http://127.0.0.1:8000/api/regime/pool/ || true"
run "curl -s -o /dev/null -w '带 gzip:   %{size_download} bytes\\n' \
      -H 'Accept-Encoding: gzip' http://127.0.0.1:8000/api/regime/pool/ || true"
run "curl -s -D- -o /dev/null -H 'Accept-Encoding: gzip' \
      http://127.0.0.1:8000/api/regime/pool/ | grep -i 'content-encoding' || echo '⚠ 没看到 content-encoding'"

say "完成。前端 http://43.152.227.67:3000/regime"
