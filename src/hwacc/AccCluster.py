from m5.params import *
from m5.proxy import *
from m5.objects.Device import BasicPioDevice, PioDevice, IsaFake, BadAddr, DmaDevice
from m5.objects.Platform import Platform
from m5.objects.SimpleMemory import SimpleMemory
from m5.objects.SubSystem import SubSystem
from m5.objects.XBar import *
from m5.objects.Bridge import Bridge
from m5.objects.Cache import Cache
from m5.objects.CommInterface import CommInterface
from m5.objects.NoncoherentDma import NoncoherentDma
from m5.objects.StreamDma import StreamDma

class ClusterCache(Cache):
    assoc = 8
    tag_latency = 20
    data_latency = 20
    response_latency = 20
    mshrs = 20
    tgts_per_mshr = 12
    write_buffers = 8

class AccCluster(Platform):
    type = 'AccCluster'
    cxx_header = "hwacc/acc_cluster.hh"
    system = Param.System(Parent.any, "system")

    # System Cache Parameter
    cache_size = Param.String('32kB', "cache size in bytes")
    local_range_min = Param.Unsigned(0x2f000000, "minimal address of local range")
    local_range_max = Param.Unsigned(0x7fffffff, "maximum address of local range")
    external_range_low_min = Param.Unsigned(0x00000000, "minimal address of external range low")
    external_range_low_max = Param.Unsigned(0x2effffff, "maximum address of external range low")
    external_range_hi_min = Param.Unsigned(0x80000000, "minimal address of external range high")
    external_range_hi_max = Param.Unsigned(0xffffffff, "maximum address of external range high")

    local_bus = NoncoherentXBar(width=2, frontend_latency=1, forward_latency=0, response_latency=1)
    coherency_bus = CoherentXBar(width=2, frontend_latency=1, forward_latency=0, response_latency=1)
    coherency_bus.snoop_filter = SnoopFilter()
    coherency_bus.snoop_response_latency = 4
    coherency_bus.point_of_coherency = True
    coherency_bus.point_of_unification = True

    def _add_spm(self, spm_range, spm_latency):
        self.spm = SimpleMemory(range=spm_range, conf_table_reported=False, latency=spm_latency)
        self.spm.port = self.local_bus.master

    def _connect_spm(self, spm):
        spm.port = self.local_bus.master

    def _attach_bridges(self, system, mem_range, ext_ranges):
        self.mem2cls = Bridge(delay='1ns', ranges = mem_range)
        self.mem2cls.master = self.local_bus.slave
        self.mem2cls.slave = system.membus.master

        # self.cls2mem = Bridge(delay='1ns', ranges = ext_ranges)
        # self.cls2mem.master = system.membus.slave
        # self.cls2mem.slave = self.local_bus.master

    def _connect_hwacc(self, hwacc):
        hwacc.pio = self.local_bus.master

    def _connect_caches(self, system, options, l2coherent, cache_size=0):
        if options.acc_cache and (cache_size!=0):
            self.cluster_cache = ClusterCache()
            self.cluster_cache.size = cache_size

            if options.l2cache and l2coherent:
                self.cluster_cache.mem_side = system.tol2bus.slave
            else:
                self.cluster_cache.mem_side = system.membus.slave
            self.coherency_bus.master = self.cluster_cache.cpu_side
        else:
            if options.l2cache and l2coherent:
                self.coherency_bus.master = system.tol2bus.slave
            else:
                self.coherency_bus.master = system.membus.slave

    def _connect_caches_smmu(self, system, options, l2coherent,
                             cache_size=0):
        """
        SMMU-aware variant of _connect_caches.

        Inserts a per-cluster SMMUv3 between the cluster's outbound
        coherency-bus traffic and the downstream system bus
        (system.tol2bus or system.membus). Optionally still places a
        cluster cache between the coherency_bus and the SMMU when
        --acc_cache is enabled.

        First-pass / static-bypass mode:
          * The SMMU's TLB and walk caches are exercised, contributing
            translation latency to every DMA.
          * No stream-table programming is performed -- this scaffold
            assumes the model is configured to pass through requests
            with timing applied. If your gem5 build requires a valid
            stream context to avoid faulting, switch to the stub-Linux
            programming path before producing paper numbers.
          * Each cluster gets its own SMMU instance with a unique MMIO
            region. The base 0x2b400000 mirrors the upstream RealView
            mapping; subsequent clusters bump by 0x20000.
        """
        from m5.objects import SMMUv3, SMMUv3DeviceInterface

        # Allocate a per-system unique reg_map slot.
        if not hasattr(system, '_salam_smmu_count'):
            system._salam_smmu_count = 0
        smmu_idx = system._salam_smmu_count
        system._salam_smmu_count += 1
        reg_base = 0x2b400000 + smmu_idx * 0x00020000

        smmu = SMMUv3(reg_map=AddrRange(reg_base, size=0x00020000))
        # ----------------------------------------------------------------
        # Low-end IoT IOMMU profile (Arm MMU-400-class, overridable).
        #
        # Target class: smallest realistic accelerator-side IOMMU shipped
        # in low-power IoT / embedded SoCs. Reference is the Arm MMU-400
        # TRM: SMMUv1, optional small main TLB (16-128 entries), 4-8
        # entry micro-TLB, **no walk cache at all** (every miss is a full
        # walk to memory), single PTW thread, single shared TCU.
        #
        # SALAM models one SMMU per accelerator cluster, which slightly
        # over-resources real low-end SoCs (which would share one IOMMU
        # across all DMA masters); we keep that structural choice but
        # size each instance at the MMU-400 minimum band so the per-
        # cluster cost stays defensible.
        #
        # All knobs are CLI-overridable so the same binary can be swept
        # over (a) "no walk cache" (this default), (b) MMU-500 small
        # config (--smmu-walk-enable + walk-S1L* knobs), and (c) server-
        # class config without rebuilding.
        # ----------------------------------------------------------------
        smmu.tlb_entries  = getattr(options, 'smmu_tlb_entries', 16)
        smmu.tlb_assoc    = getattr(options, 'smmu_tlb_assoc',   2)
        smmu.tlb_enable   = True
        smmu.tlb_lat      = getattr(options, 'smmu_tlb_lat',     3)
        smmu.tlb_slots    = getattr(options, 'smmu_tlb_slots',   1)
        # Translation pipeline / page-table walker bandwidth: minimal.
        smmu.xlate_slots  = getattr(options, 'smmu_xlate_slots', 2)
        smmu.ptw_slots    = getattr(options, 'smmu_ptw_slots',   1)
        # Config (STE/CD) cache: tiny.
        smmu.cfg_entries  = getattr(options, 'smmu_cfg_entries', 4)
        smmu.cfg_assoc    = 2
        # No stage-2 / virtualization in low-end IoT.
        smmu.ipa_enable   = False
        # Walk cache: DISABLED by default (MMU-400 has none). Every TBU
        # miss costs a full N-step walk to DRAM. Re-enable + size via
        # CLI knobs to model MMU-500 / server-class profiles.
        smmu.walk_enable  = getattr(options, 'smmu_walk_enable',  False)
        smmu.walk_S1L0    = getattr(options, 'smmu_walk_s1l0',    0)
        smmu.walk_S1L1    = getattr(options, 'smmu_walk_s1l1',    0)
        smmu.walk_S1L2    = getattr(options, 'smmu_walk_s1l2',    0)
        smmu.walk_S1L3    = getattr(options, 'smmu_walk_s1l3',    0)
        smmu.walk_S2L0    = 0
        smmu.walk_S2L1    = 0
        smmu.walk_S2L2    = 0
        smmu.walk_S2L3    = 0
        smmu.walk_assoc   = getattr(options, 'smmu_walk_assoc',   1)
        smmu.walk_lat     = getattr(options, 'smmu_walk_lat',     4)
        smmu.walk_slots   = getattr(options, 'smmu_walk_slots',   1)
        # SMMU<->IFC link latency: low-end SoC NoCs are slower than
        # gem5's server default (8 cy); use 12 cy.
        smmu.ifc_smmu_lat = getattr(options, 'smmu_ifc_lat', 12)
        smmu.smmu_ifc_lat = getattr(options, 'smmu_ifc_lat', 12)
        self.smmu = smmu

        # Downstream side: SMMU's request port goes to the same place
        # the coherency_bus would have gone.
        if options.l2cache and l2coherent:
            smmu.request = system.tol2bus.slave
        else:
            smmu.request = system.membus.slave
        # Control regs are visible from the IOBus so MMIO accesses can
        # reach the SMMU registers (only meaningful when a kernel-side
        # driver is present; harmless otherwise).
        smmu.control = system.iobus.mem_side_ports

        # Build one device interface per cluster and attach the cluster's
        # outbound coherency-bus traffic to it. This is the per-device
        # TBU (Translation Buffer Unit) sitting in front of the central
        # SMMU (TCU) modelled above.
        # ----------------------------------------------------------------
        # Low-end IoT TBU sizing (matches MMU-400-class profile above):
        #   Main TLB depth   : 16
        #   Micro TLB depth  : 4   (MMU-400/500 minimum)
        #   Translate slots  : 1   (single in-flight translation)
        # ----------------------------------------------------------------
        ifc = SMMUv3DeviceInterface()
        ifc.utlb_entries  = getattr(options, 'smmu_utlb_entries',    4)
        ifc.tlb_entries   = getattr(options, 'smmu_ifctlb_entries', 16)
        ifc.xlate_slots   = getattr(options, 'smmu_tbu_xlate_slots', 1)
        if options.acc_cache and (cache_size != 0):
            self.cluster_cache = ClusterCache()
            self.cluster_cache.size = cache_size
            self.cluster_cache.mem_side = ifc.device_port
            self.coherency_bus.master = self.cluster_cache.cpu_side
        else:
            self.coherency_bus.master = ifc.device_port
        smmu.device_interfaces = [ifc]

        # --- Optional: program a real stream table from gem5 side ---
        # When --smmu-program-stream-table is set, materialise a valid
        # AArch64 stage-1 identity-mapping page table in DRAM, build a
        # matching STE + CD + linear stream table, and seed the SMMU
        # registers so it starts up in a fully-translating mode without
        # any guest software involvement. This is what makes ptwTimeDist
        # and mainTLB stats actually populate during a SALAM run.
        if getattr(options, 'smmu_program_stream_table', False):
            import os, tempfile
            from configs.SALAM.smmu_bootstrap import build_blob

            granule_kib = getattr(options, 'smmu_granule_kib', 2048)
            # Per-cluster scratch slot. 4 KiB granule needs ~8 MiB of
            # page tables, so we use a 16 MiB stride per cluster to
            # leave headroom. 2 MiB granule fits comfortably too.
            scratch_pa = 0xa0000000 + smmu_idx * 0x01000000

            # Mark *every* STE in the per-cluster stream table valid.
            # All 64 STEs share the same CD / identity page-tables, so
            # whatever StreamID the device interface ends up presenting
            # within its own SMMU (0 in current gem5, but model-internal
            # remapping can change this) will find a valid STE. Cost is
            # zero extra memory -- the STE table is one 4 KiB page
            # regardless.
            from configs.SALAM.smmu_bootstrap import (
                STREAM_TABLE_NUM_ENTRIES)
            valid_ids = list(range(STREAM_TABLE_NUM_ENTRIES))

            blob, strtab_base, strtab_base_cfg, cr0 = build_blob(
                scratch_pa, valid_ids, granule_kib=granule_kib)

            blob_path = os.path.join(
                tempfile.gettempdir(),
                f"salam_smmu_blob_{smmu_idx}_g{granule_kib}.bin")
            with open(blob_path, "wb") as fh:
                fh.write(blob)

            smmu.bootstrap_enable      = True
            smmu.bootstrap_blob        = blob_path
            smmu.bootstrap_blob_addr   = scratch_pa
            smmu.init_strtab_base      = strtab_base
            smmu.init_strtab_base_cfg  = strtab_base_cfg
            smmu.init_cr0              = cr0
            ifc.stream_id              = smmu_idx

    def _connect_dma(self, system, dma):
        dma.pio = self.local_bus.master
        dma.dma = self.coherency_bus.slave

    def _connect_cluster_dma(self, system, dma):
        self._connect_dma(system, dma)
        dma.cluster_dma = self.local_bus.slave
