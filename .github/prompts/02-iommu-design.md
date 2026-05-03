# 02 — IOMMU Design (Single Shared SMMU, Edge-IoT Profile)

> **Use this when** editing IOMMU translation logic: the IOTLB cache,
> chip-wide port deadline, response-side hook, or any
> `tryIommuDelay` / `iommuLatencyForAccess` code path. For the
> high-level diagram see
> [00-architecture-overview.md](00-architecture-overview.md).

## What we model

**One** hardware translation engine shared by every accelerator
master in the system, sized as a constrained edge-IoT peripheral
SMMU (Cortex-M / Cortex-A5-class SoC, ~400-500 MHz, DDR3 page-table
walk, no walk caches, single PTW thread, MMU-400-class band, SMMUv2
IHI 0062 reference). Each accelerator's traffic flows through the
same shared IOTLB and the same chip-wide port deadline — there is
**no** per-CU IOTLB on this branch (the cluster-shared model
matches what Stage A intentionally collapsed to).

- **Cost model**: per-access tax, *not* per-page. Every
  **off-cluster** memory transaction (transactions that egress the
  cluster toward `coherency_bus` -> system DRAM/IO) crosses the SMMU
  and pays an IOTLB lookup latency: `iotlb_hit_latency` (default
  2 ns) on hit, `iotlb_miss_latency` (default 500 ns, for a 4-level
  walk to slow DRAM) on miss with LRU install. The IOTLB is small
  (default 8 entries) reflecting an edge-class uTLB. **On-cluster**
  traffic (SPM, RegBank/MMR, local-xbar, stream FIFOs) does **not**
  cross the SMMU — that traffic never leaves the cluster's local
  address space and a real SMMU would not see it.
- **Sharing semantics**: a translation request from any CU reserves
  a serial slot on the shared SMMU timeline; subsequent
  translations (from this or any other CU) cannot start until the
  current one finishes. Membus / DRAM serialization is unchanged
  from `plain` (already exists downstream of the SMMU).
- **What we expect to see**: cost scales with the **off-cluster**
  access count (CommInterface global egress + DMA-engine bursts to
  DRAM), not page count; high IOTLB hit-rate workloads pay only the
  hit latency on every off-cluster access; many-CU workloads
  (MobileNet, GEMM-large) see measured ≈ analytical projection
  because there is no parallel-CU savings.

## SimObject split (Stage A refactor)

The IOTLB, the chip-wide port deadline (`nextReadyTick`), and the
`IommuStats` group live on the **`AcceleratorIommu`** SimObject (one
per `AccCluster`). Both `LLVMInterface` and `CommInterface` carry a
(possibly null) `iommu` pointer:

- `CommInterface::tryIommuDelay(pkt)` calls
  `iommu->translate(pageAddr, isRead)` to get a ready-tick.
- `LLVMInterface::printIommuStats()` reads counters straight off the
  SimObject's stats group.

The legacy file-scope statics (`sIommuNextReadyTick`, the static
IOTLB / sentinels, `iotlbAccess`, `accountIommuAccess`,
`iommuLatencyForAccess`) are **gone**. When IOMMU is disabled the
SimObject's `enabled()` returns false and `tryIommuDelay()`
short-circuits, so the runtime cost is one early-return per access.

## Key files

- Header: [src/hwacc/accelerator_iommu.hh](../../src/hwacc/accelerator_iommu.hh)
- Impl  : [src/hwacc/accelerator_iommu.cc](../../src/hwacc/accelerator_iommu.cc)
- Param : [src/hwacc/AcceleratorIommu.py](../../src/hwacc/AcceleratorIommu.py)
  - `enabled` (Bool)
  - `iotlb_entries` (UInt32, default 8)
  - `hit_latency`   (Tick, default 2 000)
  - `miss_latency`  (Tick, default 500 000)
- Per-CU hook: [src/hwacc/comm_interface.cc](../../src/hwacc/comm_interface.cc)
  - `CommInterface::tryIommuDelay(pkt)`
  - Called **only** from off-cluster response paths:
    - `MemSidePort::recvTimingResp` **iff** `role_ == Role::Global`
      (the `acp` port that egresses the cluster toward
      `coherency_bus`). The `local` and `stream` MemSidePort
      instances bypass.
  - `SPMPort::recvTimingResp` and `RegPort::recvTimingResp` no
    longer call `tryIommuDelay` (they target on-cluster targets).
  - The `Role` enum (`Local | Global | Stream`) is set at
    `MemSidePort` construction in `CommInterface::getPort` based on
    the python port name.
- DMA-engine hook: [src/dev/dma_device.cc](../../src/dev/dma_device.cc)
  - `DmaPort::tryIommuDelay(pkt)` deferred-response path; same
    page-aligned single-deadline serialisation as the CommInterface
    hook. Used by both `NoncoherentDma` and `StreamDma` for their
    inherited `dma` port (the off-cluster master that goes to
    `coherency_bus` -> DRAM). `NoncoherentDma`'s private `accPort`
    (the cluster-local `cluster_dma`) is **not** wired and bypasses.
  - Wiring: [src/hwacc/noncoherent_dma.cc](../../src/hwacc/noncoherent_dma.cc)
    and [src/hwacc/stream_dma.cc](../../src/hwacc/stream_dma.cc) call
    `dmaPort.setIommu(p.iommu)` in their ctor when the python `iommu`
    Param is non-NULL. The Param is emitted by the configurator
    (`tools/SALAM-Configurator/config_parser.py`,
    `DMA.genConfig` and `StreamDMA.genConfig`).
- Stats reporter: `LLVMInterface::printIommuStats()` in
  [src/hwacc/llvm_interface.cc](../../src/hwacc/llvm_interface.cc)

## Coverage caveats (traffic that bypasses the IOMMU intercept)

The intercepts live in (a) `CommInterface::MemSidePort` for
`Role::Global` only and (b) `DmaPort` for `NoncoherentDma` /
`StreamDma`. Traffic paths that intentionally bypass:

- `MemSidePort` with `Role::Local` or `Role::Stream`, plus
  `SPMPort` and `RegPort` — by design: these target on-cluster
  scratchpads, register banks and stream FIFOs. A real SMMU sits
  between the cluster master interface and the system bus, not
  inside the cluster.
- `NoncoherentDma::accPort` (the `cluster_dma` master into the
  cluster-local xbar) — same reason; intra-cluster traffic.
- `IOAcc::MemSidePort` ([src/hwacc/io_acc.hh](../../src/hwacc/io_acc.hh))
  — separate hand-coded I/O accelerator hierarchy parallel to
  `CommInterface`/`LLVMInterface`. Not currently routed through the
  IOMMU; would need an analogous `Role::Global` hook if used.

Standard `LLVMInterface`-based accelerators using `CommInterface`,
`NoncoherentDma`, and `StreamDma` are fully covered for their
off-cluster traffic. If you add an `IOAcc`-based accelerator,
its DRAM-bound packets will not be counted.

## Design rationale (response-path injection)

The IOMMU sits downstream of the accelerator pipeline so the
accelerator runs **identically to plain mode** through
`launchRead`/`launchWrite` and `comm->enqueueRead`. Tick alignment
between accelerator `tickEvent` and `comm` `tickEvent` is therefore
unperturbed; added IOTLB latency lands purely on the critical path
where the accelerator waits for `cu->readCommit/writeCommit`.

`LLVMInterface` carries no IOMMU FSM (no `pendingIotlb*`, no
`iotlbResolveEvent`, no IOMMU branch in `launchRead`/`launchWrite`)
— it only holds the `iommu` pointer and the stats reporter.

### Invariants

- **lat=0 invariant** ([tests/aia_cda_tests/run_sanity.py](../../tests/aia_cda_tests/run_sanity.py)
  `run_iommu_lat0_check`): with both `hit_latency=0` and
  `miss_latency=0`, `translate()` returns `curTick()` for every
  packet → `tryIommuDelay` returns false on every site (CommInterface
  global port and DmaPort) → the inline fast path runs → iommu
  `sim_ticks` is **bit-identical** to plain. This proves the
  bookkeeping is inert when latency is zero.
- **No structural "iommu_overhead_us >= 0" guarantee.** The IOMMU
  stage itself only adds ticks, but downstream side-effects (DRAM
  bank scheduling, see "behavior classes" below) can produce
  negative net deltas on streaming workloads. The lat=0 invariant
  is the only structural guard.

## Three behavior classes observed across the SALAM suite

When interpreting `iommu_overhead_us`, classify the workload first:

- **Class A — IOMMU cost dominates** (e.g. `lenet_a/b/c`,
  `stencil2d`, `bfs`). Wide page footprint × high access count;
  IOTLB miss rate is non-trivial; realized cost tracks
  `iommu_proj_us` to within a constant factor. Realized% can even
  exceed projected% on irregular pointer-chase workloads
  (`bfs`: 14.75 % realized vs 3.93 % projected) because IOMMU
  latency stacks with DRAM row-conflict latency.
- **Class B — IOMMU cost is real but small** (e.g. `md_grid`,
  `md_knn`, `spmv`, `stencil3d`). Tight working set fits in a few
  pages; IOTLB stays hot; per-access cost is mostly absorbed by
  surrounding queueing/compute.
- **Class C — DRAM-scheduling jitter dominates** (e.g. `nw`,
  `mergesort`, sometimes `fft`). Streaming address pattern. Plain
  mode happens to land at a worst-case DRAM cadence (consecutive
  packets just miss the open row, force precharge+activate every
  time). IOMMU deferral spreads packets out enough that consecutive
  accesses now hit a fresh open row; the DRAM throughput gain can
  exceed the IOMMU latency added, yielding
  `iommu_overhead_us < 0`. **This is real microarchitectural
  physics, not a model bug** — proven by the lat=0 bit-identical
  invariant.

Reporting recommendation: for Class C workloads, report
`iommu_proj_us` (analytical sum of per-access latencies) instead of
or alongside `iommu_overhead_us`. For Class A/B, the two columns
should agree to within DRAM-controller noise.

Stat note: `iommu_checks` counts **off-cluster response packets**:
the `CommInterface` `acp`/global port plus `NoncoherentDma`/
`StreamDma` `dma`-port responses. The earlier 2026-05 model that
included SPM and RegPort is **gone** (it over-counted by routing
on-cluster traffic through the SMMU); `iommu_checks` is now
typically dominated by DMA-engine bursts on streaming workloads
(see e.g. `bfs`: 585 checks, ~98 % IOTLB hit-rate after the
refactor) and aligns much more closely with the AIA-KD
`smid_requests` count (which has always been off-cluster only).

## Things to be careful about

- The IOMMU is mutually exclusive with AIA-KD (enforced first in
  the fs-config layer, re-checked in the `LLVMInterface` ctor with
  a `panic`). Both share the `launchRead`/`launchWrite` hook and
  would double-count overhead.
- Do **not** re-introduce upstream-of-`CommInterface` injection.
  Five earlier attempts (deferring at `launchRead`/`launchWrite` or
  anywhere before `comm->enqueueRead`) all produced
  `iommu sim_ticks` **lower** than `plain sim_ticks` — up to -28 %
  on `nw`. Root cause was different from Class-C DRAM jitter:
  deferring upstream left `comm` idle while the dependency tracker
  thought memory was busy, so downstream dependents drained
  earlier than they would under real memory wait. See git log on
  `src/hwacc/llvm_interface.{cc,hh}` for the failed attempts and
  [06-gotchas-and-history.md](06-gotchas-and-history.md) for the
  full history.
- The IOTLB is **shared chip-wide** (Stage A collapsed the old
  per-CU statics into one SimObject). Do not re-introduce per-CU
  IOTLB caches without an explicit design change — the comparison
  to AIA-KD relies on both protection mechanisms sharing one
  centralised resource.

## Multi-cluster behaviour (mobilenetv2)

"Chip-wide" in the bullet above means **per `AccCluster`**, not per
simulation. Single-cluster benchmarks have one `AcceleratorIommu` for
the whole run; mobilenetv2 has **four** (one per cluster — `head`,
`body`, `tail`, `classifier`). See
[00-architecture-overview.md](00-architecture-overview.md#benchmark-topology-mobilenetv2-worked-example)
for the topology.

Implications when working on IOMMU code:

- Each cluster has an **independent** 8-entry IOTLB and an
  independent `nextReadyTick` deadline. Translations queued on
  `body.iommu` do not affect `head.iommu`'s deadline.
- The `ready = max(curTick(), nextReadyTick) + lat` serialisation
  bites hardest in `body` (5 CUs racing on one port deadline);
  `classifier` (2 CUs) sees almost no queueing tax.
- Pages cached in `head.iommu`'s IOTLB are **not** visible to
  `body.iommu` — the same physical page can miss in one cluster's
  IOTLB while hitting in another's. This is intentional and
  symmetric with AIA-KD's per-cluster validated-page cache.
- Per-cluster stats are tagged `system.acccluster_<name>.iommu.*`
  in stats.txt. Sum across clusters for chip-wide cost; look at
  one cluster (usually `body`) to see where contention
  concentrates.
