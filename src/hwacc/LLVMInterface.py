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
    enable_iommu = Param.Bool(False, "Enable IOMMU latency model")
    iotlb_entries = Param.UInt32(64, "IOTLB capacity (LRU)")
    iotlb_hit_latency = Param.Tick(0, "IOTLB hit latency (ticks)")
    iotlb_miss_latency = Param.Tick(0, "IOTLB miss latency (ticks)")