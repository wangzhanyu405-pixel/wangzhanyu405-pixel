#!/bin/sh
set -eu
cd "$(dirname "$0")"
if [ ! -x .venv/bin/python ]; then
  echo "请先按 README 安装 .venv 和依赖。" >&2
  exit 1
fi
exec .venv/bin/python -m streamlit run app.py --server.address 127.0.0.1 --server.port "${LABEL_DEMO_PORT:-18501}" --server.headless true --browser.gatherUsageStats false
