#!/usr/bin/env bash
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV="${DIR}/.venv"

if [ ! -x "${VENV}/bin/python" ]; then
  python3 -m venv "${VENV}"
  "${VENV}/bin/pip" install --quiet --upgrade pip
  "${VENV}/bin/pip" install --quiet -r "${DIR}/requirements.txt"
fi

"${VENV}/bin/python" "${DIR}/harness.py" --trials 3 --output "${DIR}/results.md"
