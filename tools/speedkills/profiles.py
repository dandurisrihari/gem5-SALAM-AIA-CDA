"""Protection-mode flag sets for the AIA-CDA release.

Three modes only:

    plain    baseline (no protection)
    aia-kd   AIA kernel-driver validation (analytical, per validated page)
    iommu    analytical IOMMU latency model living inside LLVMInterface;
             every LLVM-IR load/store goes through an IOTLB lookup and
             accumulates a hit/miss latency that is reported in the
             stats but does not perturb simulator timing.

The real SMMUv3 SimObject profiles that lived here on `main` are *not*
exposed in this branch. The underlying gem5 code is unchanged; compare
against the upstream branch if you need a cycle-accurate translation
device-class study.
"""
from __future__ import annotations

from typing import Callable, Dict, List


ModeBuilder = Callable[[dict], List[str]]

MODES: Dict[str, ModeBuilder] = {
    "plain": lambda o: [],

    # AIA kernel-driver validation. Default 8,367,000 ticks (8.367 us)
    # per validated page — matches the IRQ-driven measurement from the
    # AIA-KD paper. Override with `compare --aia-kd-latency`.
    "aia-kd": lambda o: [
        "--enable-kernel-validation",
        "--kernel-validation-latency", str(o.get("aia_kd_latency", 8_367_000)),
    ],

    # Analytical IOMMU. Defaults match the LLVMInterface.py Param defaults:
    # 8-entry LRU IOTLB, 2 ns hit, 500 ns miss / page-walk (constrained
    # edge-IoT peripheral IOMMU profile). Override via
    # `compare --iotlb-entries / --iotlb-hit-latency / --iotlb-miss-latency`.
    "iommu": lambda o: [
        "--enable-iommu",
        "--iotlb-entries",      str(o.get("iotlb_entries", 8)),
        "--iotlb-hit-latency",  str(o.get("iotlb_hit_latency", 2_000)),
        "--iotlb-miss-latency", str(o.get("iotlb_miss_latency", 500_000)),
    ],
}

# All three modes are run by default in `compare`.
DEFAULT_MODES = ("plain", "aia-kd", "iommu")
