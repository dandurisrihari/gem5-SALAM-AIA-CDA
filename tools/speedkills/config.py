"""Repository-wide paths and binary locations.

Keeps every other module free of os.path arithmetic. ``M5_PATH`` is the
gem5-SALAM checkout root; everything else is derived from it.
"""
from __future__ import annotations

import os
from pathlib import Path

# Resolve M5_PATH from env if set, otherwise from this file's location.
# tools/speedkills/config.py -> repo root is two levels up.
_HERE = Path(__file__).resolve().parent
_AUTODETECT = _HERE.parent.parent
M5_PATH = Path(os.environ.get("M5_PATH", _AUTODETECT)).resolve()

GEM5_OPT = M5_PATH / "build" / "ARM" / "gem5.opt"
GEM5_DEBUG = M5_PATH / "build" / "ARM" / "gem5.debug"

CONFIGS_DIR = M5_PATH / "configs" / "SALAM"
BENCHMARKS_DIR = M5_PATH / "benchmarks"
COMMON_DISK = BENCHMARKS_DIR / "common" / "fake.iso"
SYSTEMBUILDER = M5_PATH / "tools" / "SALAM-Configurator" / "systembuilder.py"

OUT_ROOT = M5_PATH / "BM_ARM_OUT"

# System-level gem5 options shared by every run.
SYS_OPTS_BASE = (
    "--mem-size=4GB",
    "--mem-type=DDR4_2400_8x8",
    "--machine-type=VExpress_GEM5_V1",
    "--dtb-file=none",
    "--bare-metal",
    "--cpu-type=DerivO3CPU",
    "--caches",
    "--l2cache",
)


def require_binary(debug: bool = False) -> Path:
    """Return path to gem5 binary, raising if missing."""
    binary = GEM5_DEBUG if debug else GEM5_OPT
    if not binary.exists():
        raise FileNotFoundError(
            f"gem5 binary not found at {binary} -- "
            f"run `scons build/ARM/gem5.opt -jN` first."
        )
    return binary
