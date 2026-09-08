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
  .venv/bin/python -m pip install -r requirements.txt
fi
exec .venv/bin/python -m streamlit run app.py --server.address 127.0.0.1
