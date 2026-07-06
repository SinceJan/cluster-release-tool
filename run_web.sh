#!/bin/bash
set -euo pipefail

# run_web.sh — 启动 cluster 发版工具网页服务
#
# 发版工具独立部署，通过 --repo-root 指向代码工程目录。
#
# 用法:
#   ./run_web.sh                                    # 默认指向 ./code/cluster_framework
#   ./run_web.sh /home/heyi/code/cluster_framework  # 指定代码工程目录
#   PORT=9000 ./run_web.sh                          # 自定义端口
#   HOST=127.0.0.1 ./run_web.sh                     # 仅本机访问
#
# 首次运行会自动创建 venv 并安装依赖。

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TOOL_DIR="${SCRIPT_DIR}"

# 代码工程根目录（通过参数或环境变量指定）
REPO_ROOT="${1:-${REPO_ROOT:-/home/heyi/code/cluster_framework}}"

# Python 路径
PYTHON_BIN="${PYTHON:-python3}"
VENV_DIR="${TOOL_DIR}/.venv"

PORT="${PORT:-8080}"
HOST="${HOST:-0.0.0.0}"

# 首次运行：创建 venv + 安装依赖
if [ ! -f "${VENV_DIR}/bin/python" ] && [ ! -f "${VENV_DIR}/Scripts/python.exe" ]; then
    echo "首次运行，创建 venv..."
    "${PYTHON_BIN}" -m venv "${VENV_DIR}"
    if [ -f "${VENV_DIR}/bin/pip" ]; then
        "${VENV_DIR}/bin/pip" install -r "${TOOL_DIR}/requirements.txt"
    else
        "${VENV_DIR}/Scripts/pip.exe" install -r "${TOOL_DIR}/requirements.txt"
    fi
fi

# 确定 venv python 路径
if [ -f "${VENV_DIR}/bin/python" ]; then
    VENV_PY="${VENV_DIR}/bin/python"
else
    VENV_PY="${VENV_DIR}/Scripts/python.exe"
fi

echo "代码工程: ${REPO_ROOT}"
echo "发版工具启动: http://${HOST}:${PORT}"
exec "${VENV_PY}" "${TOOL_DIR}/app.py" --repo-root "${REPO_ROOT}" --host "${HOST}" --port "${PORT}"
