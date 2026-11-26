#!/bin/sh
# Linux / WSL terminal entry. Do not modify the user's shell startup files.
set -eu
root=$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd -P)
python=${TSW_PYTHON:-python3}
if ! "$python" -c 'import sys; sys.exit(sys.version_info < (3, 10))' 2>/dev/null; then
    printf '%s\n' '请先安装 Linux 版 Python 3.10+，或设置 TSW_PYTHON 指向解释器。' >&2
    exit 1
fi
if ! command -v git >/dev/null 2>&1; then
    printf '%s\n' '请先用发行版的软件包管理器安装 git，再重新运行。' >&2
    exit 1
fi
exec "$python" -X utf8 "$root/plugin/bin/tsw_launch.py" setup install --interactive "$@"
