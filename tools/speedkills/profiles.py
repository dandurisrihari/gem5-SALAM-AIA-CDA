"""Protection-mode flag sets and SoC IOMMU profiles.

Each entry returns the *additional* gem5 CLI flags appended to the common
SYS_OPTS for that mode. These are the canonical reference values; anyone
wanting a sweep should add a new entry here rather than hand-rolling
flags at the call site.
"""
from __future__ import annotations

from typing import Callable, Dict, List


# ---- IOMMU SoC profiles ---------------------------------------------------

PROFILE_IOT: List[str] = [
    # MMU-400-class minimum band: walk-cache lookups disabled, smallest TLBs.
    # gem5's SMMUv3 model always *constructs* the walk cache and asserts
    # numEntries >= 1, so we provision a 1-entry-per-level stub. Lookups
    # are gated off via --smmu-walk-enable being absent (walk_enable=False
    # in AccCluster.py), so the stub never satisfies a request -> every
    # PTW falls through to the page-table walker, matching MMU-400 behaviour.
    "--smmu-tlb-entries", "16",  "--smmu-ifctlb-entries", "16",
    "--smmu-utlb-entries", "4",  "--smmu-cfg-entries", "4",
    "--smmu-xlate-slots", "2",   "--smmu-tbu-xlate-slots", "1",
    "--smmu-ptw-slots", "1",     "--smmu-tlb-slots", "1",
    "--smmu-ifc-lat", "12",
    "--smmu-walk-s1l0", "1", "--smmu-walk-s1l1", "1",
    "--smmu-walk-s1l2", "1", "--smmu-walk-s1l3", "1",
    "--smmu-walk-assoc", "1",
    # walk_enable intentionally NOT passed -> lookups bypass the stub
]

PROFILE_MMU500: List[str] = [
    "--smmu-tlb-entries", "32",  "--smmu-ifctlb-entries", "32",
    "--smmu-utlb-entries", "4",  "--smmu-cfg-entries", "8",
    "--smmu-xlate-slots", "4",   "--smmu-tbu-xlate-slots", "2",
    "--smmu-ptw-slots", "2",     "--smmu-tlb-slots", "2",
    "--smmu-ifc-lat", "8",
    "--smmu-walk-enable",
    "--smmu-walk-s1l0", "2", "--smmu-walk-s1l1", "4",
    "--smmu-walk-s1l2", "8", "--smmu-walk-s1l3", "4",
    "--smmu-walk-assoc", "2", "--smmu-walk-slots", "2", "--smmu-walk-lat", "4",
]

PROFILE_SERVER: List[str] = [
    "--smmu-tlb-entries", "2048", "--smmu-ifctlb-entries", "2048",
    "--smmu-utlb-entries", "32",  "--smmu-cfg-entries", "64",
    "--smmu-xlate-slots", "64",   "--smmu-tbu-xlate-slots", "16",
    "--smmu-ptw-slots", "16",     "--smmu-tlb-slots", "4",
    "--smmu-tlb-assoc", "4",
    "--smmu-ifc-lat", "8",
    "--smmu-walk-enable",
    "--smmu-walk-s1l0", "4",   "--smmu-walk-s1l1", "28",
    "--smmu-walk-s1l2", "348", "--smmu-walk-s1l3", "4",
    "--smmu-walk-assoc", "4", "--smmu-walk-slots", "16",
    "--smmu-walk-lat", "4",
]


def _smmu_prog(profile: List[str], granule_kib: int) -> List[str]:
    return [
        "--enable-real-smmu",
        "--smmu-program-stream-table",
        "--smmu-granule-kib", str(granule_kib),
        *profile,
    ]


# ---- Mode catalog ---------------------------------------------------------
# Each builder takes a per-call ``opts`` dict (granule_kib, tlb_entries, ...)
# and returns the gem5 flags for that mode.

ModeBuilder = Callable[[dict], List[str]]

MODES: Dict[str, ModeBuilder] = {
    "plain": lambda o: [],
    "aia-kd": lambda o: [
        "--enable-kernel-validation",
        # Default: 8,367,000 ticks = 8.367 us per validated page.
        # Picked to match the IRQ-driven measurement from the AIA-KD
        # paper; well above per-run scheduler noise so the cost is
        # observable end-to-end. Override with `compare --aia-kd-latency`.
        "--kernel-validation-latency", str(o.get("aia_kd_latency", 8_367_000)),
    ],
    "smmu-iot":    lambda o: _smmu_prog(PROFILE_IOT,
                                        o.get("granule_kib", 4)),
    "smmu-mmu500": lambda o: _smmu_prog(PROFILE_MMU500,
                                        o.get("granule_kib", 4)),
    "smmu-server": lambda o: _smmu_prog(PROFILE_SERVER,
                                        o.get("granule_kib", 4)),
    # Analytical IOMMU latency model living inside LLVMInterface (no real
    # SMMU SimObject). Mutually exclusive with --enable-kernel-validation.
    # Defaults below match the LLVMInterface.py Param defaults, so passing
    # `--enable-iommu` alone yields a constrained edge-IoT peripheral
    # IOMMU profile (Cortex-M / Cortex-A5-class, ~400 MHz, DDR3 walk):
    #   8-entry LRU IOTLB, 2 ns hit, 500 ns miss / page-walk.
    # Override via `compare --iotlb-entries / --iotlb-hit-latency /
    # --iotlb-miss-latency`; nothing is baked in here.
    "iommu": lambda o: [
        "--enable-iommu",
        "--iotlb-entries",      str(o.get("iotlb_entries", 8)),
        "--iotlb-hit-latency",  str(o.get("iotlb_hit_latency", 2_000)),
        "--iotlb-miss-latency", str(o.get("iotlb_miss_latency", 500_000)),
    ],
}

# Modes that should appear by default in `compare`.
# Order matters: `plain` first (baseline), then the two protection models
# we actually want to compare side-by-side, then the real-SMMU profiles
# kept available for deeper sweeps but not run by default.
DEFAULT_MODES = (
    "plain", "aia-kd", "iommu", "smmu-iot", "smmu-mmu500",
)


def expand_tlb_sweep(tlb_sizes: List[int],
                     granule_kib: int = 4) -> List[tuple[str, List[str]]]:
    """Return (label, flags) pairs for a TLB sweep at the IoT profile."""
    out = []
    for tlb in tlb_sizes:
        flags = _smmu_prog(PROFILE_IOT, granule_kib) + [
            "--smmu-tlb-entries", str(tlb),
            "--smmu-ifctlb-entries", str(tlb),
        ]
        out.append((f"smmu-tlb{tlb}", flags))
    return out
