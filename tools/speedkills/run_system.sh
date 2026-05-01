#!/usr/bin/env bash
# tools/speedkills/run_system.sh
#
# Backwards-compatible shim for the old `tools/run_system.sh` debug
# wrapper. The new implementation is the speedkills `run` subcommand:
#
#     python3 -m tools.speedkills run --bench NAME [-d|-v|-p] [-f FLAGS]
#
# Flags: -d gdb (uses gem5.debug), -v valgrind, -p capture stdout to
# debug-trace.txt, -f comma-separated --debug-flags.
#
# Original shell driver: tools/speedkills/legacy/run_system.sh.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT"
exec python3 -m tools.speedkills run "$@"
