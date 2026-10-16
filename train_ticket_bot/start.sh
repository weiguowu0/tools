#!/bin/bash
# 12306 自动订票 - 启动脚本（Mac）
cd "$(dirname "$0")"
if [ ! -x ".venv/bin/python" ]; then
  echo "缺少 .venv，请先运行：python3 -m venv .venv && .venv/bin/pip install flask playwright"
  exit 1
fi
exec .venv/bin/python app.py
