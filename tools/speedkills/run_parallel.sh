#!/usr/bin/env bash
# tools/speedkills/run_parallel.sh
#
# Backwards-compatible shim for the old `tools/run_parallel.sh` driver
# (full-lifecycle latency sweep across multiple benchmarks). The new
# implementation is the speedkills `sweep` subcommand:
#
#     python3 -m tools.speedkills sweep [args...]
#
# Per-benchmark, `sweep` runs the SALAM Configurator + `make` first
# (serialised per-path so shared-tree variants like mobilenetv2 / _35 /
# _75 don't clobber each other's `sw/main.elf`), then launches one gem5
# per latency value in parallel.
#
# Original shell driver: tools/speedkills/legacy/run_parallel.sh.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT"
exec python3 -m tools.speedkills sweep "$@"
