#!/usr/bin/env bash
# 前台启动。生产环境用 systemd，见 DEPLOY.md 第六节。
set -euo pipefail
cd "$(dirname "$0")"

PY=${PY:-python3}
"$PY" -c 'import sys; assert sys.version_info >= (3, 10), "需要 Python 3.10 以上"' \
  || { echo "Python 版本不够"; exit 1; }

# 起之前先自检，两秒，能挡掉「数据传了但索引没传」这类问题
"$PY" scripts/selftest.py || {
  echo
  echo "自检没过。修完再来，或者加 --force 跳过（不推荐）。"
  [ "${1:-}" = "--force" ] || exit 1
}

exec "$PY" app/server.py
