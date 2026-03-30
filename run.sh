#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

PYTHON_BIN=""
for candidate in python3.12 python3.11 python3.10 python3 python; do
  if command -v "$candidate" >/dev/null 2>&1; then
    PYTHON_BIN="$candidate"
    break
  fi
done

if [[ -z "$PYTHON_BIN" ]]; then
  echo "No Python interpreter found. Please install Python 3.10+." >&2
  exit 1
fi

if [[ -d ".venv" ]]; then
  if ! .venv/bin/python -V >/dev/null 2>&1; then
    echo "Existing .venv is not usable on this machine. Recreating..."
    rm -rf .venv
  fi
fi

if [[ ! -d ".venv" ]]; then
  "$PYTHON_BIN" -m venv .venv
fi

# shellcheck disable=SC1091  # source path is generated at runtime; shellcheck cannot resolve it
source .venv/bin/activate

pip install --upgrade pip
pip install -r python-requirements.txt

flet run src/main.py
