# 00 — Architecture Overview

> **Read this first.** One-page mental model of what the
> `aia_cda_rel` branch does and how the pieces fit. After this, jump
> to the focused prompt that matches your task (see
> [README.md](README.md)).

## Goal

Compare two protection mechanisms that gate an accelerator's access to
system memory, on the same workload, on the same gem5-SALAM platform.
Functionality always succeeds — only the **timing** of the protection
check is modeled. Each mode is enabled by a CLI flag (mutually
exclusive); plain is the unprotected baseline.

| Mode      | Trigger rate                | Cost per event           | Where the cost lives |
|-----------|-----------------------------|--------------------------|----------------------|
| `plain`   | n/a                         | 0                        | n/a                  |
| `aia-kd`  | first touch of a 4 KiB page | 8.367 µs (default)       | Host CPU via GIC IRQ |
| `iommu`   | every memory transaction    | 2 ns hit / 500 ns miss   | One shared SMMU port |

**IOMMU coverage (strict / all-ports enforcement):** every response
delivered to a CU pays one IOTLB lookup -- all five CommInterface
port types (MemSidePort Local, Global, Stream; SPMPort; RegPort)
plus the off-cluster DmaPort on NoncoherentDma / StreamDma engines.
On-cluster *and* intra-cluster SPM / register-bank traffic are both
translated by the single chip-wide AcceleratorIommu. This is
stricter than a textbook Arm SMMU (which would translate only
cluster-master egress); we model it that way for symmetry with
AIA-KD (both fire on the same five response ports) and to close the
SPM-as-staging-buffer side channel. See 02-iommu-design.md for the
caveat on "Class A" workloads and the relaxed-ablation knob.

**AIA-KD coverage (response-side defer; Option C):** every LLVM-IR
load/store decides its validation cost at launch time
(`LLVMInterface::chargeValidation`) and stamps the resulting tick
delta on `MemoryRequest::aiaKdDefer`. The same five CommInterface
response ports realize that cost via `tryAiaKdDelay()`, mirroring
the IOMMU mechanism so both protections produce comparable
`abs_overhead_us`. AIA-KD does NOT touch the off-cluster DmaPort —
DMA engines do not execute LLVM IR, so there is no launch-time
decision to realize.

**Key insight (the whole reason this comparison exists):** AIA-KD pays
once per **page**; IOMMU pays once per **access**. AIA-KD wins on
access-heavy workloads with small page footprints; IOMMU wins on
workloads with cold page churn.

## SimObject layout (the single source of truth)

The protection state used to live in file-scope statics in
`llvm_interface.cc` / `comm_interface.cc`. It now lives in **two
SimObjects**, one shared per-`AccCluster`:

```
  AccCluster (one per simulation)
  │
  ├── iommu     : AcceleratorIommu       ← shared IOTLB + chip-wide port deadline + stats
  │       wired into:  every CommInterface  via  acc.iommu = clstr.iommu
  │       wired into:  every LLVMInterface via  acc.llvm_interface.iommu = clstr.iommu
  │
  ├── validator : AiaKdValidator         ← shared per-PID validated-page cache + in-flight set + chip-wide kernel-driver deadline (`nextReadyTick`)
  │       wired into:  every CommInterface  via  acc.validator = clstr.validator     (response defer)
  │       wired into:  every LLVMInterface via  AccConfig(... validator=clstr.validator)  (launch decision)
  │
  └── <accel_0>, <accel_1>, ...   (each has llvm_interface + comm_interface)
```

Generation is automatic: the SALAM-Configurator
([tools/SALAM-Configurator/config_parser.py](../../tools/SALAM-Configurator/config_parser.py))
emits the two `clstr.iommu = AcceleratorIommu(...)` and
`clstr.validator = AiaKdValidator(...)` lines into the generated
`configs/SALAM/AccCluster.py`, plus the per-accelerator wiring lines.

Per-CU state (GIC IRQ raise, reservation queue replay, RAW re-check,
per-CU stats counters) **stays on `LLVMInterface`** because it
touches per-CU machinery. Cross-CU shared state — including the
chip-wide AIA-KD FIFO + response event modelling a single kernel
driver — lives on the SimObjects.

For the full SimObject design (file triplets, Param wiring, the
generator contract, why some things stay per-CU), see
[03-simobject-layout.md](03-simobject-layout.md).

## Architecture diagrams

### AIA-KD: per-page first-touch tax, realised on the response port

```
  ┌──────────┐  ┌──────────┐  ┌──────────┐ ... ┌──────────┐
  │   CU 0   │  │   CU 1   │  │   CU 2   │     │  CU N-1  │
  │ (LLVM IF)│  │ (LLVM IF)│  │ (LLVM IF)│     │ (LLVM IF)│
  └────┬─────┘  └────┬─────┘  └────┬─────┘     └────┬─────┘
       │  At launch each CU calls validator->checkAndCharge()
       │  and stamps memReq->aiaKdDefer (0 / partial / full).
       ▼             ▼             ▼                ▼
  ┌─────────────────────────────────────────────────────────┐
  │  AiaKdValidator (one per AccCluster)                    │
  │   * validatedPagesPerProcess[pid] : set<pageAddr>       │
  │   * pendingValidationPages[page]  : {readyTick, pid}    │
  │   * nextReadyTick                 : chip-wide deadline  │
  │                                                         │
  │  CacheHit  ──► defer = 0                                │
  │  Coalesced ──► defer = readyTick - now (tail of wait)   │
  │  ColdMiss  ──► defer = latency, bump nextReadyTick      │
  │  DmaCtrl   ──► defer = latency, no caching, bump deadline│
  └─────────────────────────────────────────────────────────┘
       │
       ▼  Then the request flows through the CommInterface ports
          exactly as in plain mode. On the response side:
  ┌─────────────────────────────────────────────────────────┐
  │  CommInterface::tryAiaKdDelay(pkt)                      │
  │    if (defer > 0) { hold pkt for `defer` ticks; }       │
  │    one defer per LLVM-IR access (zeroed after first pkt)│
  │    pendingAiaKdResps  +  aiaKdRespEvent  drains FIFO    │
  └─────────────────────────────────────────────────────────┘
```

### IOMMU: per-access tax through one shared SMMU port

```
  ┌──────────┐  ┌──────────┐  ┌──────────┐ ... ┌──────────┐
  │   CU 0   │  │   CU 1   │  │   CU 2   │     │  CU N-1  │
  └────┬─────┘  └────┬─────┘  └────┬─────┘     └────┬─────┘
  ┌────┴────┐   ┌────┴────┐   ┌────┴────┐      ┌────┴────┐
  │ Comm IF │   │ Comm IF │   │ Comm IF │      │ Comm IF │
  └────┬────┘   └────┬────┘   └────┬────┘      └────┬────┘
       │             │             │                │
       │  every memory-access response goes through the SMMU
       ▼             ▼             ▼                ▼
  ┌─────────────────────────────────────────────────────────┐
  │  AcceleratorIommu (one per AccCluster)                  │
  │   * IOTLB (LRU, 8 entries default)                      │
  │   * nextReadyTick : monotonic chip-wide port deadline   │
  │   * IommuStats: totalChecks, tlbHits, tlbMisses,        │
  │                 uniquePages (cumulative distinct pages), │
  │                 totalLatencyTicks                        │
  │                                                         │
  │   translate(pageAddr, isRead) returns ready-tick:       │
  │     hit  ──► +2 ns                                      │
  │     miss ──► +500 ns (4-level walk to slow DRAM)        │
  │                                                         │
  │   ONE translation in flight at a time                   │
  │   ready = max(curTick(), nextReadyTick) + lat           │
  └────────────────────────────┬────────────────────────────┘
                               ▼
                  (response delivered to the
                   originating CU's CommIF)
```

## Key files (jump map)

| Component                 | File                                                                                                            |
|---------------------------|-----------------------------------------------------------------------------------------------------------------|
| AIA-KD SimObject          | [src/hwacc/AiaKdValidator.py](../../src/hwacc/AiaKdValidator.py), [aia_kd_validator.{hh,cc}](../../src/hwacc/)  |
| IOMMU SimObject           | [src/hwacc/AcceleratorIommu.py](../../src/hwacc/AcceleratorIommu.py), [accelerator_iommu.{hh,cc}](../../src/hwacc/) |
| Per-CU AIA-KD protocol    | [src/hwacc/llvm_interface.{hh,cc}](../../src/hwacc/llvm_interface.cc)                                           |
| Per-CU IOMMU response hook| [src/hwacc/comm_interface.{hh,cc}](../../src/hwacc/comm_interface.cc) (`tryIommuDelay`)                         |
| Param surfaces            | [LLVMInterface.py](../../src/hwacc/LLVMInterface.py), [CommInterface.py](../../src/hwacc/CommInterface.py)      |
| Build registration        | [src/hwacc/SConscript](../../src/hwacc/SConscript)                                                              |
| AccConfig + cluster glue  | [configs/SALAM/HWAccConfig.py](../../configs/SALAM/HWAccConfig.py), [tools/SALAM-Configurator/config_parser.py](../../tools/SALAM-Configurator/config_parser.py) |
| Run driver                | `python3 -m tools.speedkills` ([tools/speedkills/](../../tools/speedkills/))                                    |
| Sanity suite              | [tests/aia_cda_tests/run_sanity.py](../../tests/aia_cda_tests/run_sanity.py)                                    |

## Benchmark topology: mobilenetv2 (worked example)

Most benchmarks are single-cluster (sys_validation: 1 cluster × 2 CUs;
gemm/fft/nw: 1 cluster × ~2 CUs). **mobilenetv2 is the only
multi-cluster workload** and is the best case for understanding how
AIA-KD and IOMMU behave when the platform has more than one
`AccCluster`.

mbnet partitions the network into 4 stages, **each is its own
`AccCluster`** with its own validator and IOMMU SimObject. Hits in
one cluster do **not** carry into another.

```
  mobilenetv2 simulation (15 CUs total)
  ├── AccCluster: head        4 CUs   1× AiaKdValidator   1× AcceleratorIommu
  │     head_Top  head_NormalConv  head_DWConv  head_PWConv
  ├── AccCluster: body        5 CUs   1× AiaKdValidator   1× AcceleratorIommu
  │     body_top  body_residual  body_PWConv0  body_DWConv  body_PWConv1
  ├── AccCluster: tail        4 CUs   1× AiaKdValidator   1× AcceleratorIommu
  │     tail_Top  tail_PWConv  tail_Reshape  tail_AvgPool
  └── AccCluster: classifier  2 CUs   1× AiaKdValidator   1× AcceleratorIommu
        classifier_Top  classifier_Linear

   ⇒ 4 validators, 4 IOMMUs, 15 CUs.
```

Per-cluster YAML lives under
[benchmarks/mobilenetv2/configs/sys_configs/per_cluster/{1,35,75}/](../../benchmarks/mobilenetv2/configs/sys_configs/per_cluster/35/)
(the `1`/`35`/`75` directories are width sweeps; same shape, different
data sizes). The `*_Top` accelerator in each cluster is the
orchestrator — it dispatches; the others are kernel CUs.

### AIA-KD on mobilenetv2

Each cluster has an **independent** validated-page cache. A page that
holds shared input activations and is touched by all four clusters
pays the 8.367 µs first-touch tax **four times** (once per cluster's
validator). Within a cluster:

- `body` is the heaviest case (5 CUs racing on cold pages). Multiple
  body CUs cold-missing on **different** pages serialize through the
  chip-wide `nextReadyTick` deadline on `body.validator` — concurrent
  cold misses across CUs queue behind the one kernel-driver thread
  rather than firing N parallel IRQs. Multiple body CUs hitting the
  **same** in-flight page coalesce ("lazy promotion"): the late-comer
  pays only the tail of the existing wait.
- `classifier` (2 CUs) behaves like sys_validation — barely any
  cross-CU contention.
- `head` and `tail` (4 CUs each) are intermediate.

### IOMMU on mobilenetv2

Each cluster has its own `nextReadyTick` deadline and its own 8-entry
IOTLB. So the SMMU port-contention queueing tax **does not cross
cluster boundaries** — `body`'s 5 CUs queue against each other;
`head`'s 4 CUs queue against each other; the two queues do not
interact.

`body` again pays the most queueing tax for the same reason.
`classifier` pays the least.

### Reading mbnet stats

Each cluster's SimObject stats are tagged with the cluster name in
the gem5 stats file. The cluster name comes from the benchmark YAML
(`sys_name`), **not** a fixed `acccluster_` prefix — e.g. mobilenetv2
uses `head`, `body`, `tail`, `classifier`; sys_validation/nw uses
`nw_clstr`. Example paths:

- `system.head.iommu.totalChecks`
- `system.body.iommu.totalChecks`
- `system.body.iommu.uniquePages`
- `system.body.iommu.tlbHits`
- `system.body.iommu.tlbMisses`
- `system.nw_clstr.iommu.totalChecks`   ← single-cluster bench

(The `validator.*` stats are printed to run.log by
`LLVMInterface::printIommuStats()` / `printAiaKdStats()`, not to
`stats.txt`.)

Sum across clusters to get the chip-wide cost; look at one cluster to
see where the contention concentrates (almost always `body`).

## Where to go next

- Editing AIA-KD logic (validator, latency, coalescing, RAW re-check) → [01-aia-kd-design.md](01-aia-kd-design.md)
- Editing IOMMU logic (IOTLB, port deadline, response hook)         → [02-iommu-design.md](02-iommu-design.md)
- Adding/refactoring SimObjects, generator changes                   → [03-simobject-layout.md](03-simobject-layout.md)
- Before any change: rules of the road                               → [04-working-agreements.md](04-working-agreements.md)
- Running experiments / CLI flags                                    → [05-cli-and-recipes.md](05-cli-and-recipes.md)
- Subtle invariants that have bitten us before                       → [06-gotchas-and-history.md](06-gotchas-and-history.md)
- Verifying a change after rebuild                                   → [sanity-test.prompt.md](sanity-test.prompt.md)
