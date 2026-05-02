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
    # Only the on/off switch lives here. The IOTLB geometry (capacity,
    # hit latency, miss latency) lives on the AcceleratorIommu SimObject
    # itself -- those Params used to be duplicated here too but were
    # never read on the C++ side and have been removed to avoid the
    # appearance that twiddling them does anything. Tune the IOTLB via
    # `--iotlb-entries / --iotlb-hit-latency / --iotlb-miss-latency`,
    # which the generator forwards into AcceleratorIommu(...).
    enable_iommu = Param.Bool(False, "Enable IOMMU latency model")

    # Shared device-wide IOMMU SimObject. NULL when IOMMU is disabled.
    # Wired by the SALAM-Configurator generator (config_parser.py)
    # to a single AcceleratorIommu instance per AccCluster, so that
    # all CUs in the cluster share one IOTLB cache and one chip-wide
    # translation port. Replaces the file-scope statics that used to
    # back the IOMMU model in llvm_interface.cc.
    iommu = Param.AcceleratorIommu(NULL,
        "Device-wide accelerator IOMMU shared across all CUs in the "
        "cluster (NULL when IOMMU disabled)")

    # Shared device-wide AIA-KD validator SimObject. NULL when AIA-KD
    # is disabled. Wired by the SALAM-Configurator generator to a
    # single AiaKdValidator instance per AccCluster, so that all CUs
    # share the same validated-pages cache and pending/waiter
    # coalescing structures. Replaces the file-scope statics that
    # used to back the AIA-KD model in llvm_interface.cc.
    validator = Param.AiaKdValidator(NULL,
        "Device-wide AIA-KD validator shared across all CUs in the "
        "cluster (NULL when AIA-KD disabled)")