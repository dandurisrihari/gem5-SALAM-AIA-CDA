from m5.params import *
from m5.proxy import *
from m5.SimObject import SimObject
from m5.objects.ComputeUnit import ComputeUnit

class LLVMInterface(ComputeUnit):
    type = 'LLVMInterface'
    cxx_header = "hwacc/llvm_interface.hh"

    in_file = Param.String("LLVM Trace File")
    lockstep_mode = Param.Bool(True, "Stall datapath if any op stalls")
    sched_threshold = Param.UInt32(10000, "Scheduling window threshold")
    clock_period = Param.Int32(10, "System clock speed")
    top_name = Param.String("top", "Top-level function name")

    # Kernel validation parameters (AIA-KD SMID verification)
    enable_kernel_validation = Param.Bool(False, "Enable kernel validation")
    validation_int_num = Param.Int32(172, "Interrupt number for validation")
    kernel_validation_latency = Param.Tick(0, "Kernel validation latency")
    process_id = Param.UInt64(17, "Process ID for SMID validation")

    # IOMMU/SMMU latency model (mutually exclusive with kernel validation).
    # Models per-access translation+permission cost: every memory access
    # pays an IOTLB lookup; misses additionally pay a page-walk cost.
    #
    # Defaults are tuned to a *constrained edge-IoT* peripheral IOMMU
    # (Cortex-M / Cortex-A5-class device, ~400 MHz, DDR3/LPDDR2 walk):
    #   * iotlb_entries     = 8       silicon-area-constrained vendor
    #                                  IPs ship 4-16 entry uTLBs
    #   * iotlb_hit_latency = 2 ns    ~1 cycle SRAM lookup @ 400-500 MHz
    #   * iotlb_miss_latency= 500 ns  4-level walk to slow DRAM, no
    #                                  walk caches, single PTW thread
    # All within published Arm MMU-400 ranges; deliberately a tight,
    # defensible profile so IOMMU overhead is non-trivial on memory-
    # heavy workloads. Override per-run via gem5 CLI / fs_*.py knobs.
    enable_iommu = Param.Bool(False, "Enable IOMMU latency model")
    iotlb_entries = Param.UInt32(8, "IOTLB capacity (LRU; default 8 "
                                    "= edge-IoT uTLB)")
    iotlb_hit_latency = Param.Tick(2000, "IOTLB hit latency (ticks; "
                                         "default 2000 = 2 ns)")
    iotlb_miss_latency = Param.Tick(500000, "IOTLB miss / page-walk "
                                            "latency (ticks; default "
                                            "500000 = 500 ns)")