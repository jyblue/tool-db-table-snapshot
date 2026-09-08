#!/bin/sh
set -eu
cd "$(dirname "$0")"
if ! python3 -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)'; then
  echo "Python 3.10 이상이 필요합니다. 현재 인터프리터: $(python3 --version 2>&1)" >&2
  exit 1
fi
if [ ! -x .venv/bin/python ]; then
  python3 -m venv .venv
fi
.venv/bin/python -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)' || {
  echo ".venv가 Python 3.10 미만으로 만들어졌습니다. .venv를 삭제한 뒤 다시 실행하세요." >&2
  exit 1
}
if ! .venv/bin/python -c 'import streamlit, pymysql, keyring, psutil' >/dev/null 2>&1; then
  mirror_url="${SNAPSHOT_PIP_INDEX_URL:-${PIP_INDEX_URL:-}}"
  trusted_host="${SNAPSHOT_PIP_TRUSTED_HOST:-${PIP_TRUSTED_HOST:-}}"
  if [ -z "$mirror_url" ]; then
    mirror_url="$(.venv/bin/python -m pip config get global.index-url 2>/dev/null || true)"
  fi
  if [ -z "$trusted_host" ]; then
    trusted_host="$(.venv/bin/python -m pip config get global.trusted-host 2>/dev/null || true)"
  fi
  if [ -z "$mirror_url" ]; then
    echo "SNAPSHOT_PIP_INDEX_URL에 사내 Python 패키지 미러 주소를 지정하세요." >&2
    echo '예: SNAPSHOT_PIP_INDEX_URL="https://packages.example.local/simple" ./start-macos.sh' >&2
    echo "HTTP 미러 또는 사설 CA이면 SNAPSHOT_PIP_TRUSTED_HOST에 호스트명도 지정하세요." >&2
    exit 1
  fi
  case "$mirror_url" in
    http://*)
      if [ -z "$trusted_host" ]; then
        echo "HTTP 미러는 SNAPSHOT_PIP_TRUSTED_HOST에 미러 호스트명을 지정해야 합니다." >&2
        exit 1
      fi
      ;;
  esac
  if [ -n "$trusted_host" ]; then
    .venv/bin/python -m pip --isolated install --index-url "$mirror_url" --trusted-host "$trusted_host" -r requirements.txt
  else
    .venv/bin/python -m pip --isolated install --index-url "$mirror_url" -r requirements.txt
  fi
fi
exec .venv/bin/python -m streamlit run app.py --server.address 127.0.0.1
