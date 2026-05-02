# Copyright (c) 2026 SALAM contributors
# SPDX-License-Identifier: BSD-3-Clause
#
# AcceleratorIommu -- a single, device-wide IOMMU/SMMU SimObject.
#
# One instance is shared by every LLVMInterface (CU) and CommInterface
# inside an AccCluster: the accelerator device is modeled as ONE unit
# sitting behind ONE chip-wide SMMU. The CUs consult this object to
# translate addresses; the object owns the IOTLB cache and the
# in-order translation port queue.
#
# Replaces the file-scope statics that used to live in LLVMInterface
# and CommInterface (iotlbLru/iotlbSet, sIommuNextReadyTick).
from m5.params import *
from m5.proxy import *
from m5.SimObject import SimObject


class AcceleratorIommu(SimObject):
    type = "AcceleratorIommu"
    cxx_class = "gem5::AcceleratorIommu"
    cxx_header = "hwacc/accelerator_iommu.hh"

    # Master toggle. When False the CU short-circuits its translate()
    # call and behaves bit-identically to plain mode (no overhead, no
    # bookkeeping). The IOMMU object itself is always instantiated so
    # that LLVMInterface's `Parent.any` proxy can resolve, regardless
    # of whether IOMMU mode is active.
    enabled = Param.Bool(False, "Master IOMMU toggle")

    # IOTLB geometry. Defaults are tuned to a low-power IoT peripheral
    # IOMMU (Arm MMU-400 class):
    #   * 8-entry uTLB         -- silicon-area constrained vendor IPs
    #                             ship 4-16 entries
    #   * 2 ns hit latency     -- ~1 cycle SRAM lookup @ 400-500 MHz
    #   * 500 ns miss latency  -- 4-level walk to slow DRAM with no
    #                             walk caches and a single PTW thread
    iotlb_entries     = Param.UInt32(8,        "IOTLB capacity (LRU)")
    hit_latency       = Param.Tick(2000,       "IOTLB hit latency (ticks)")
    miss_latency      = Param.Tick(500000,     "IOTLB miss / page-walk "
                                               "latency (ticks)")
