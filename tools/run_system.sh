#!/usr/bin/env bash
# Deprecated shim. Use:  python3 -m tools.speedkills run [...]
# The original script is kept at run_system.legacy.sh.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
exec python3 -m tools.speedkills run "$@"
