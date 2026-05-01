#!/usr/bin/env bash
# tools/speedkills/run_protection_compare.sh
#
# Backwards-compatible shim for the old `tools/run_protection_compare.sh`
# entry point. The real implementation now lives in the speedkills Python
# package as the `compare` subcommand:
#
#     python3 -m tools.speedkills compare [args...]
#
# All arguments are forwarded verbatim. The shim cd's to the repo root so
# Python can resolve the `tools.speedkills` namespace package no matter
# from where the script is launched.
#
# The original (pre-package) shell driver is preserved at
# tools/speedkills/legacy/run_protection_compare.sh for reference. Do NOT
# call the legacy script for new work — extend
# `tools/speedkills/profiles.py::MODES` instead and the `compare`
# subcommand will pick up the new mode automatically.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT"
exec python3 -m tools.speedkills compare "$@"
