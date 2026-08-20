#!/bin/sh
set -eu

script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
project_root=$(dirname "$script_dir")
python_bin=${PYTHON:-"$project_root/.venv/bin/python"}
export PYTHONPATH="$project_root/src"
export PYTHONDONTWRITEBYTECODE=1

cd "$project_root"
"$python_bin" tools/codeup_human_agent.py run --workers 64
"$python_bin" tools/codeup_human_agent.py intent --workers 64
"$python_bin" tools/codeup_human_agent.py score
"$python_bin" tools/codeup_human_agent.py report
