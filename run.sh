#!/usr/bin/env bash
# PDF VL Assistant を LAN 内で起動
set -e
cd "$(dirname "$0")"

# install.sh が作る .venv を優先。無ければ PYTHON か python3 を使う。
if [ -z "${PYTHON:-}" ]; then
  if [ -x ".venv/bin/python" ]; then
    PYTHON=".venv/bin/python"
  else
    PYTHON="$(command -v python3 || true)"
  fi
fi
[ -n "$PYTHON" ] || { echo "python が見つかりません。install.sh を実行してください" >&2; exit 1; }

PORT=${PORT:-8090}
HOST=${HOST:-0.0.0.0}
export PORT HOST

LAN_IP=$(ip -4 -o addr show scope global 2>/dev/null | awk '{print $4}' | cut -d/ -f1 | head -1)
echo "[*] PDF VL Assistant"
echo "[*] http://localhost:${PORT}"
[ -n "$LAN_IP" ] && echo "[*] LAN: http://${LAN_IP}:${PORT}"
exec "$PYTHON" server.py
