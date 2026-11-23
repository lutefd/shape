#!/usr/bin/env sh
# One command: build a clean venv, install shape[agents], run
# everything in this folder and compare it with the expected outcomes.
set -eu
cd "$(dirname "$0")"
[ -d .venv ] || python3 -m venv .venv
.venv/bin/pip install -q --disable-pip-version-check -e '../..[agents]'
.venv/bin/python matrix.py
