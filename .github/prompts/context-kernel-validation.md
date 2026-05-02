# Context: Kernel-Based Memory Validation (AIA ↔ KD) and IOMMU baseline

Reusable background for prompts that touch the kernel-validation feature
or the comparable IOMMU latency model that lives alongside it.

## Goal

Model and compare two different protection mechanisms that gate an
accelerator's access to system memory, on the same workload, on the
same gem5-SALAM platform. Functionality always succeeds — only the
**timing** of the protection check is modeled. Each mode is enabled
by a CLI flag (mutually exclusive); plain is the unprotected
baseline.

### AIA-KD (Accelerator Isolation Architecture — Kernel Driver)

What we are modeling: a software-defined protection scheme where the
host OS kernel validates every **unique memory page** an accelerator
intends to touch before the access is allowed to commit. Inspired by
the AIA-KD paper's IRQ-driven kernel-driver path.

  - **Cost model**: per-page first-touch tax. The accelerator stalls
    the offending instruction, raises a GIC IRQ to the host, the
    kernel "validates" the page (functionally a no-op; we charge a
    fixed `kernel_validation_latency`, default 8.367 µs to match the
    AIA-KD paper's measured IRQ + driver round-trip), and the page
    is then cached in `validatedPagesPerProcess[PID]`. Subsequent
    accesses to the same page by the same process are free.
  - **What we expect to see**: high one-time overhead on first
    access to a page; near-zero steady-state cost once the working
    set is paged in. Coalescing of in-flight requests on the same
    page is supported (`waitingForPage`) so a burst of accesses to
    a fresh page pays the latency only once.
  - **Key design property**: software-style (one cost per *page*),
    not hardware-style (one cost per *access*). This is why AIA-KD
    typically beats IOMMU on workloads with high access counts and
    small page footprints.

### IOMMU baseline (single shared SMMU, edge-IoT)

What we are modeling: **one** hardware translation engine shared by
every accelerator master in the system, sized as a constrained
edge-IoT peripheral SMMU (Cortex-M / Cortex-A5-class SoC,
~400-500 MHz, DDR3 page-table walk, no walk caches, single PTW
thread, MMU-400-class band, SMMUv2 IHI 0062 reference). Each
accelerator has its own private IOTLB — no shared L2 IOTLB at the
TCU — but **all CUs queue against one shared SMMU port**
(`sIommuNextReadyTick`), one translation in flight chip-wide.

  - **Cost model**: per-access tax, *not* per-page. Every memory
    transaction the accelerator emits (off-chip DRAM, on-chip SPM,
    or RegBank/MMR) crosses the SMMU and pays an IOTLB lookup
    latency: `iotlb_hit_latency` (default 2 ns) on hit,
    `iotlb_miss_latency` (default 500 ns, for a 4-level walk to
    slow DRAM) on miss with LRU install. The IOTLB is small
    (default 8 entries) reflecting an edge-class uTLB.
  - **Sharing semantics**: a translation request from any CU
    reserves a serial slot on the shared SMMU timeline; subsequent
    translations (from this or any other CU) cannot start until the
    current one finishes. Membus / DRAM serialization is unchanged
    from plain (it already exists downstream of the SMMU).
  - **What we expect to see**: cost scales with **system-wide**
    access count, not page count; high IOTLB hit-rate workloads
    pay only the hit latency on every access; many-CU workloads
    (MobileNet, GEMM-large) see measured ≈ analytical projection
    because there is no parallel-CU savings.
  - **Key design property**: hardware-style (one cost per *access*),
    centralized (one cost per *chip-cycle*). This is why IOMMU
    typically loses to AIA-KD on access-heavy workloads even when
    the page footprint is small.

### Why both, side-by-side

The protection mechanism's choice (software-driver vs hardware-MMU)
trades off setup cost vs steady-state cost. Running both on the
identical SALAM platform with identical accelerators, identical
benchmarks, and identical traffic lets a paper quote a clean
apples-to-apples overhead delta per workload — without needing to
argue away differences in the underlying accelerator timing model.

### Architecture diagrams

Both modes serialize all CUs through one shared resource. The
difference is the trigger rate: AIA-KD fires on kernel launch with
a cold page (rare); IOMMU fires on every memory access response
(constant). This is the entire reason AIA-KD beats IOMMU on
memory-heavy workloads.

#### AIA-KD: per-page first-touch tax through the host CPU

```
  ┌──────────┐  ┌──────────┐  ┌──────────┐ ... ┌──────────┐
  │   CU 0   │  │   CU 1   │  │   CU 2   │     │  CU N-1  │
  │ (LLVM IF)│  │ (LLVM IF)│  │ (LLVM IF)│     │ (LLVM IF)│
  └────┬─────┘  └────┬─────┘  └────┬─────┘     └────┬─────┘
       │ kernel-entry / basic-block start          │
       ▼             ▼             ▼               ▼
  ┌─────────────────────────────────────────────────────────┐
  │  Per-CU validation cache (validatedPagesPerProcess)     │
  │                                                         │
  │  cache HIT  ──► fast path: 0 ticks, run kernel          │
  │  cache MISS ──► slow path:                              │
  │                  1. raise GIC IRQ to host CPU           │
  │                  2. host CPU runs the AIA validator     │
  │                  3. inject 8.367 us stall               │
  │                  4. host acks ─► CU resumes ─► run      │
  │                  5. install page in CU's cache          │
  └─────────────────────────────────────────────────────────┘
              │                          ▲
              │ IRQ                      │ ack
              ▼                          │
         ┌──────────────────────────────────┐
         │     GIC ──► host CPU             │
         │  (one IRQ serviced at a time)    │  ◄── shared resource
         └──────────────────────────────────┘

  --- unchanged from plain ----------------------------------
  memory accesses INSIDE the kernel are NOT inspected
  (no per-access tax)
  membus / iobus ──► DRAM controller ──► DDR3 banks
```

#### IOMMU (single-SMMU edge-IoT): per-access tax through one shared port

```
  ┌──────────┐  ┌──────────┐  ┌──────────┐ ... ┌──────────┐
  │   CU 0   │  │   CU 1   │  │   CU 2   │     │  CU N-1  │
  └────┬─────┘  └────┬─────┘  └────┬─────┘     └────┬─────┘
  ┌────┴────┐   ┌────┴────┐   ┌────┴────┐      ┌────┴────┐
  │ Comm IF │   │ Comm IF │   │ Comm IF │      │ Comm IF │
  │ (Mem/SPM│   │ (Mem/SPM│   │ (Mem/SPM│      │ (Mem/SPM│
  │  /Reg)  │   │  /Reg)  │   │  /Reg)  │      │  /Reg)  │
  └────┬────┘   └────┬────┘   └────┬────┘      └────┬────┘
       │             │             │                │
       │  every memory-access response goes through the SMMU
       ▼             ▼             ▼                ▼
  ┌─────────────────────────────────────────────────────────┐
  │            Shared SMMU arbiter (one chip-wide)          │
  │                                                         │
  │   sIommuNextReadyTick  ◄── single monotonic timeline    │
  │                                                         │
  │   ┌──────────────┐                                      │
  │   │  IOTLB (LRU) │   8 entries  per CU                  │
  │   │              │   (lookup time accounted into the    │
  │   └──────┬───────┘    shared timeline)                  │
  │          │  hit ──► +2 ns                               │
  │          │  miss ─► +500 ns (4-level walk, DRAM-rate)   │
  │          ▼                                              │
  │   ONE translation in flight at a time                   │
  │   ready = max(curTick(), sIommuNextReadyTick) + lat     │
  └────────────────────────────┬────────────────────────────┘
                               ▼
                  (response delivered to the
                   originating CU's CommIF)

  --- unchanged from plain ----------------------------------
  membus / iobus ──► DRAM controller ──► DDR3 banks
  (already serialized in plain mode; no extra delta from IOMMU)
```

#### Symmetry: both serialize all CUs, at very different rates

```
        AIA-KD                            IOMMU (shared SMMU)
        ───────                           ───────────────────
  trigger:  cold-page kernel launch      every memory response
  rate  :  1×10^1 .. 1×10^3 events       1×10^5 .. 1×10^8 events
  shared:  GIC ─► host CPU               sIommuNextReadyTick
  cost  :  8.367 us per event            2 ns hit / 500 ns miss per event
  ─────────────────────────────────────────────────────────────────
  result:  bursty, rare, amortizes        constant, frequent, no amortization
```

## Working agreements (read first, every session)

These are non-negotiable habits for anyone (human or LLM) editing this
feature. Skipping them has cost real time in the past.

1. **Keep this prompt in sync with the code.** Whenever you change any
   of the following, update the relevant section of *this file* in the
   same commit / turn:
   - CLI flags added/removed in `configs/SALAM/fs_*.py` or
     `tools/SALAM-Configurator/fs_template.py`
     → update *CLI Surface* and the recipe blocks.
   - New stats, new struct fields, new event paths in
     `src/hwacc/llvm_interface.{hh,cc}` or `LLVMInterface.py`
     → update *Key Files* and *Runtime Path*.
   - New gotchas you hit during debugging
     → append to *Things To Be Careful About When Modifying*.
   If you didn't update the prompt, the change isn't done.

2. **Use `python3 -m tools.speedkills` as the single run driver.**
   Do not hand-roll one-off `build/ARM/gem5.opt ...` invocations for
   protection-comparison experiments. The package:
     - holds the canonical per-mode flag sets in
       [`tools/speedkills/profiles.py`](../../tools/speedkills/profiles.py)
       (`MODES` — `plain`, `aia-kd`, `iommu`),
     - holds the benchmark registry in
       [`tools/speedkills/benchmarks.py`](../../tools/speedkills/benchmarks.py),
     - records `run.cmd` per run for reproducibility,
     - parallelises and harvests `summary.tsv` + `deltas.tsv`.
   Subcommands:
     - `compare` — protection-mode comparison (was `run_protection_compare.sh`)
     - `sweep`   — latency sweep across benchmarks (was `run_parallel.sh`)
     - `run`     — single-bench debug shortcut (was `run_system.sh`)
     - `harvest` — re-parse an existing outdir
     - `list`    — show benches and modes
   The legacy `tools/run_*.sh` files have moved into the package as thin
   wrappers at `tools/speedkills/run_*.sh`; the original shell drivers
   are archived at `tools/speedkills/legacy/run_*.sh` for reference but
   should not be invoked. When a new mode / profile / flag is needed,
   **extend `profiles.py::MODES`** rather than bypassing the package,
   and update the *Recipes* block in this file.

3. **Reason before you implement.** Before edits, state in the chat:
     - what file(s) and symbol(s) you intend to change,
     - why (which observed behaviour or requirement drives the change),
     - what could break (other call sites, stats accounting, RAW
       hazard handling, AccCluster.py needing a rebuild, etc.),
     - how you will verify (which run, which stat, expected delta).

4. **Rebuild reminder.** `src/hwacc/AccCluster.py` is embedded into
   `gem5.opt` (`[EMBED PY]`). Edits to it have *no effect* until
   `scons build/ARM/gem5.opt -jN` finishes. Check
   `ls -la build/ARM/gem5.opt` after the build before launching runs.

   **Always pipe `yes ""` into the build command** so scons doesn't
   stall on the one-time gem5 git-style hook installer prompt
   (`Press enter to continue, or ctrl-c to abort:`):
   ```bash
   yes "" | scons build/ARM/gem5.opt -j$(nproc) 2>&1 | tail -20
   ```
   Without the `yes ""` the build hangs silently waiting for stdin
   the first time it's run in a fresh worktree / container.

5. **Run the sanity suite after every rebuild.** Any change that
   requires `scons build/ARM/gem5.opt` to re-link the binary
   (anything under `src/hwacc/`, `src/dev/arm/`, embedded SimObject
   `.py` files, or the SALAM Configurator templates that change
   generated `configs/SALAM/*.py`) **must** be followed by:
   ```bash
   python3 tests/aia_cda_tests/run_sanity.py --regen
   ```
   The suite is short (2 fast benches × 3 modes plus an aia-kd@lat=0
   noise-floor check, well under a minute on the dev container) and
   asserts the protection-mode invariants that have regressed before:
     - every (bench, mode) row appears in `summary.tsv`,
     - `aia_kd_overhead_us > 0` at default latency (checkpoints fire),
     - `iommu_checks > 0` (IOMMU hook fires),
     - aia-kd@lat=0 sim_ticks within 0.5 % of plain (event-queue
       reordering noise floor; proves the fast-path bypasses real work).

     Note: we deliberately do *not* assert `iommu sim_ticks == plain
     sim_ticks`. The current IOMMU model is analytical and happens to
     be non-perturbing, but a real SMMU sits in the critical path and
     would legitimately perturb sim_ticks. The bit-identical property
     is only meaningful for AIA-KD@lat=0.

   If the suite fails, **do not commit** — diagnose first. Pass
   `--bench n1,n2` to narrow the scope while iterating, but the full
   default set must pass before the change is considered done. Pure
   doc / prompt / README edits that don't touch built code are
   exempt.

6. **Don't pin parameters to a specific vendor part** unless the user
   explicitly asks. Cite the configurable range (Arm MMU-400/500 TRM)
   and pick the band; that keeps the result defensible without
   invitining "but vendor X actually ships Y" reviewer pushback.

## Key Files

- Params: [src/hwacc/LLVMInterface.py](../../src/hwacc/LLVMInterface.py)
  - `enable_kernel_validation` (Bool, default False)
  - `kernel_validation_latency` (Tick, default 0 — opt-in via CLI; the
    `tools.speedkills` `aia-kd` mode injects 8 367 000 ticks = 8.367 µs
    by default to match IRQ-driven KD cost from the AIA-KD paper)
  - `validation_int_num` (Int32, default 172) — GIC IRQ raised to kernel
  - `process_id` (UInt64, default 17) — SMID for per-process cache
  - `enable_iommu` (Bool, default False)
  - `iotlb_entries` (UInt32, default **8** — constrained edge-IoT uTLB)
  - `iotlb_hit_latency` (Tick, default **2 000 = 2 ns @ ~400-500 MHz**)
  - `iotlb_miss_latency` (Tick, default **500 000 = 500 ns**; 4-level
    walk to slow DRAM, no walk caches, single PTW thread — MMU-400
    band)
- Header: [src/hwacc/llvm_interface.hh](../../src/hwacc/llvm_interface.hh)
  - `PendingValidationRequest`, `WaitingInstruction` structs
  - `validatedPagesPerProcess` — per-PID cache of 4 KiB-aligned pages
  - `pendingValidationPages` / `pendingValidationUIDs` — in-flight tracking
  - `waitingForPage` — instructions coalesced behind an in-flight request
  - `revalidatedUIDs` — UIDs of accesses that completed validation but
    were re-queued for a RAW hazard; consumed by `consumeRevalidatedUID`
    on replay so the cache-hit counter does not double-count them.
  - `validationResponseEvent` — `EventFunctionWrapper`
- Implementation: [src/hwacc/llvm_interface.cc](../../src/hwacc/llvm_interface.cc)
  - `ActiveFunction::launchRead` — checkpoint for read-side validation
  - `ActiveFunction::launchWrite` — checkpoint for write-side validation
  - `sendValidationRequest` — enqueues req, raises GIC IRQ, schedules event
  - `processValidationResponse` — drains queue, caches page, dispatches
    waiting instructions (handles RAW hazards on the way out)
  - `validateWithKernel` — currently always returns `true`
  - `printKernelValidationStats` — final stats dump
  - `queueWaitingInstruction` — coalescing helper

## Runtime Path (per memory access)

1. Read/write reaches `launchRead` / `launchWrite`.
2. If validation enabled:
   - **Cache hit** (`isPageValidated`) → no latency, fall through.
   - **Pending on same page** (`isPageValidationPending`) → enqueue in
     `waitingForPage[page]`, mark UID pending, return `false`.
   - **Cache miss** → `sendValidationRequest` → IRQ + scheduled event at
     `curTick + kernelValidationLatency`.
3. `processValidationResponse` fires, marks page validated for the PID,
   issues the original `MemoryRequest`, and replays coalesced waiters
   (re-checking RAW hazards via `writeActive` / `getActiveWrite`).

## Stats Reported (`printKernelValidationStats`)

- `totalKernelValidations` — full-latency requests
- `validationCacheHits` — zero-latency hits
- `validationCoalescedWaits` + `totalCoalescedWaitLatency` — partial-lat
- `kernelValidationDenied` — currently always 0 (panic on deny)
- Cache hit rate, unique pages validated, processes seen

## CLI Surface

Forwarded by [tools/SALAM-Configurator/fs_template.py](../../tools/SALAM-Configurator/fs_template.py)
through `addHWAccOptions`:

```
# AIA-KD mode
--enable-kernel-validation
--kernel-validation-latency=<ticks>
--validation-int-num=<irq>
--process-id=<pid>

# IOMMU baseline (mutually exclusive with the above)
--enable-iommu
--iotlb-entries=<N>
--iotlb-hit-latency=<ticks>
--iotlb-miss-latency=<ticks>
```

The three experimental modes on this branch (`aia_cda_rel`) are:
- **plain** — no flags set. Baseline.
- **aia-kd** — `--enable-kernel-validation` plus latency. First-touch tax,
  then free.
- **iommu** — `--enable-iommu` plus IOTLB knobs. Analytical tax
  (per-access IOTLB look-aside model) living inside `LLVMInterface`.
  Per-accelerator only; defaults to a *constrained edge-IoT*
  peripheral IOMMU (Cortex-M / Cortex-A5-class, ~400 MHz, DDR3 walk):
  8-entry LRU IOTLB, 2 ns hit, 500 ns miss-walk. Charges every
  LLVM-IR load/store (including SPM-resident accesses) so the model
  reflects "every access is security-checked" — the design goal of
  this release branch.

The two protection flags (`--enable-kernel-validation`,
`--enable-iommu`) are **mutually exclusive**:
  - enforced first at the fs-config layer
    (`tools/SALAM-Configurator/fs_template.py` and the generated
    `configs/SALAM/fs_*.py`) — `Error: protection-model flags are
    mutually exclusive: ...` aborts the run before gem5 elaborates;
  - additionally re-checked at the C++ ctor (`llvm_interface.cc`
    `panic`) since both share the `launchRead`/`launchWrite` hook.

`speedkills compare` runs each mode in its own gem5 process / outdir,
so modes can never co-activate inside a single simulation. The default
mode set is `(plain, aia-kd, iommu)`. The real SMMUv3 SimObject path
that existed on `main` has been removed from this branch — see
`tools/speedkills/README.md` for the rationale.

Driven in bulk via [`python3 -m tools.speedkills sweep`](../../tools/speedkills/README.md)
and visualised by [tools/experiment_monitor.py](../../tools/experiment_monitor.py)
(also reachable as `python3 -m tools.speedkills monitor -- ...`).

## IOMMU model details

### Requirement (what we model)

A per-accelerator stage-1 SMMU translation engine that sits on the
accelerator's master interface. **Every memory transaction emitted
by the accelerator** — off-chip DRAM, on-chip SPM, or RegBank/MMR —
crosses the SMMU and pays IOTLB lookup latency. The model is
deliberately *not* address-range-aware: real hardware does not
distinguish on-chip vs off-chip destinations at the SMMU stage; a
StreamID-tagged transaction is translated regardless of where it
lands.

### What is implemented

- **Hook**: virtual `Tick ComputeUnit::iommuLatencyForAccess(Addr,
  bool isRead)` (default returns 0). `LLVMInterface` overrides it
  to perform the IOTLB lookup + stat accounting.
- **Injection point**: `CommInterface::tryIommuDelay(pkt)` in
  [src/hwacc/comm_interface.cc](../../src/hwacc/comm_interface.cc).
  Called from **all three** response-port paths:
  - `MemSidePort::recvTimingResp` (off-chip / global)
  - `SPMPort::recvTimingResp`     (on-chip scratchpad)
  - `RegPort::recvTimingResp`     (RegBank / MMR)
  If the helper returns true, the packet is parked in
  `pendingIommuResps` and `recvPacket(pkt)` is replayed via
  `iommuRespEvent` after `lat` ticks. If false (lat==0 or `cu` not
  yet bound), the caller takes the inline `recvPacket(pkt)` fast
  path → bit-identical to plain mode.
- **In-order port**: monotonic `iommuNextReadyTick` ensures a fast
  hit cannot overtake a slower miss already in flight (matches a
  real SMMU request port).
- **IOTLB**: fully-associative LRU of `iotlb_entries` page numbers
  (default **8** — constrained edge-IoT uTLB). Hit pays
  `iotlb_hit_latency` (default 2 000 ticks = 2 ns @ ~400 MHz); miss
  pays `iotlb_miss_latency` (default 500 000 ticks = 500 ns) and
  installs the entry (LRU evict if full). `iotlb_entries=0` ⇒
  every access misses. All three knobs are surfaced as
  `compare --iotlb-entries / --iotlb-hit-latency / --iotlb-miss-latency`.
- **Per-accelerator IOTLB**: each `LLVMInterface` owns an
  independent IOTLB and stats. **No shared L2 IOTLB at the TCU is
  modeled** — conservative profile (a real SoC could amortise some
  misses through a shared TLB). AIA-KD's cache, by contrast, is
  keyed on PID and naturally shared across accelerators of the
  same process.
- **Mutual exclusion with AIA-KD**: enforced first at the
  fs-config layer and re-checked in `LLVMInterface` ctor (`panic`).
- **Stats**: `printIommuStats()` runs after
  `printKernelValidationStats()` and reports per-accelerator
  `iommu_checks`, `iotlb_hits`, `iotlb_misses`, `iotlb_hit_rate_pct`,
  `iommu_total_latency` (analytical projection).

### Coverage caveats (traffic that bypasses the IOMMU intercept)

The intercept lives in `CommInterface`. Two non-`CommInterface`
master paths exist in SALAM and currently bypass the IOMMU:
  - `ScratchpadRequestPort` ([src/hwacc/scratchpad_memory.hh](../../src/hwacc/scratchpad_memory.hh))
    — when the SPM itself acts as a DMA master ("SPM-fill"), that
    traffic does not flow through any accelerator's `CommInterface`.
    Real hardware *would* translate it (the DMA carries a StreamID).
  - `IOAcc::MemSidePort` ([src/hwacc/io_acc.hh](../../src/hwacc/io_acc.hh))
    — separate hand-coded I/O accelerator hierarchy parallel to
    `CommInterface`/`LLVMInterface`. Not currently routed through
    the IOMMU.
Standard `LLVMInterface`-based accelerators using `CommInterface`
are fully covered. If you add an `IOAcc`-based accelerator or
enable SPM-DMA-fill mode, those packets will not be counted.

### Design rationale (response-path injection in CommInterface)

The IOMMU sits downstream of the accelerator pipeline so the
accelerator runs **identically to plain mode** through
`launchRead`/`launchWrite` and `comm->enqueueRead`. Tick alignment
between accelerator `tickEvent` and `comm` `tickEvent` is therefore
unperturbed; added IOTLB latency lands purely on the critical path
where the accelerator waits for `cu->readCommit/writeCommit`.

`LLVMInterface` no longer carries any IOMMU FSM (no
`pendingIotlb*`, no `iotlbResolveEvent`, no IOMMU branch in
`launchRead`/`launchWrite`) — it only owns the IOTLB cache + stats
and the `iommuLatencyForAccess` override.

#### Invariants and what they guarantee

- **lat=0 invariant** (`tests/aia_cda_tests/run_sanity.py
  ::run_iommu_lat0_check`): with both `iotlb_hit_latency=0` and
  `iotlb_miss_latency=0`, `iommuLatencyForAccess` returns 0 for
  every packet → `tryIommuDelay` returns false → all three ports
  take the inline `recvPacket(pkt)` fast path → iommu `sim_ticks`
  is **bit-identical** to plain. This proves the bookkeeping is
  inert when latency is zero.
- **No structural "iommu_overhead_us >= 0" guarantee.** Earlier
  drafts of this prompt claimed this; that claim is wrong. The
  IOMMU stage itself only adds ticks, but downstream side-effects
  (DRAM bank scheduling — see next section) can produce negative
  net deltas on streaming workloads. The lat=0 invariant is the
  only structural guard.

#### Three behavior classes observed across the SALAM suite

When interpreting `iommu_overhead_us`, classify the workload first:

  - **Class A — IOMMU cost dominates** (e.g. `lenet_a/b/c`,
    `stencil2d`, `bfs`). Wide page footprint × high access count;
    IOTLB miss rate is non-trivial; realized cost tracks
    `iommu_proj_us` to within a constant factor. Realized% can
    even exceed projected% on irregular pointer-chase workloads
    (`bfs`: 14.75% realized vs 3.93% projected) because IOMMU
    latency stacks with DRAM row-conflict latency.
  - **Class B — IOMMU cost is real but small** (e.g. `md_grid`,
    `md_knn`, `spmv`, `stencil3d`). Tight working set fits in a
    few pages; IOTLB stays hot; per-access cost is mostly absorbed
    by surrounding queueing/compute.
  - **Class C — DRAM-scheduling jitter dominates** (e.g. `nw`,
    `mergesort`, sometimes `fft`). Streaming address pattern.
    Plain mode happens to land at a worst-case DRAM cadence
    (consecutive packets just miss the open row, force
    precharge+activate every time). IOMMU deferral spreads packets
    out enough that consecutive accesses now hit a fresh open row;
    the DRAM throughput gain can exceed the IOMMU latency added,
    yielding `iommu_overhead_us < 0`. **This is real
    microarchitectural physics, not a model bug** — proven by the
    lat=0 bit-identical invariant.

Reporting recommendation: for Class C workloads, report
`iommu_proj_us` (analytical sum of per-access latencies) instead of
or alongside `iommu_overhead_us`. For Class A/B, the two columns
should agree to within DRAM-controller noise.

Stat note: `iommu_checks` counts **response packets** (one per
memory transaction across all three ports). After the SPM/Reg
inclusion fix it is ~1-2 orders of magnitude larger than the old
MemSidePort-only count seen in pre-2026-05 logs.

#### History (do NOT re-introduce upstream-of-CommInterface injection)

Five earlier upstream attempts (deferring at `launchRead`/
`launchWrite` or anywhere before `comm->enqueueRead`) all produced
`iommu sim_ticks` **lower** than `plain sim_ticks` — up to -28% on
`nw`. Root cause was different from the Class-C DRAM jitter
described above: deferring upstream left `comm` idle while the
dependency tracker thought memory was busy, so downstream
dependents drained earlier than they would under real memory wait.
Even the AIA-KD-style "bail before queue insert" variant failed.
The artifact is fundamental to upstream injection; only the
response-path placement (current design) avoids it. See git log on
`src/hwacc/llvm_interface.{cc,hh}` for the failed attempts.

#### History — SPM/Reg port intercept fix (2026-05)

For a brief window the IOMMU intercept lived only in
`MemSidePort::recvTimingResp`, leaving SPMPort and RegPort traffic
untranslated. This caused `iommu_overhead_us ≈ 0` on SPM-resident
workloads (most of the suite) and `iommu_checks` to under-count by
~25× on workloads like `nw`. The fix factored the intercept into
`CommInterface::tryIommuDelay()` and called it from all three
recvTimingResp paths. The lat=0 invariant continues to hold
bit-identically.

## Things To Be Careful About When Modifying

- Page granularity is hard-coded as `addr & ~0xFFFULL` (4 KiB). Any change
  to page size must touch `isPageValidated`, `isPageValidationPending`,
  `sendValidationRequest`, `processValidationResponse`, and
  `queueWaitingInstruction` together.
- An access whose `[addr, addr+size)` straddles a 4 KiB boundary only
  validates the page containing `addr`. SALAM accesses are typically
  word/vector aligned so this is acceptable today; revisit if you add
  coarser DMA-style accesses.
- `validationResponseEvent` is shared — only one in-flight schedule at a
  time; new requests rely on it being rescheduled inside
  `processValidationResponse` when the head of `pendingValidations` is
  not yet ready.
- RAW hazard re-queue logic is duplicated for the originating request and
  for waiters; keep both paths in sync. Both paths must call
  `revalidatedUIDs.insert(uid)` before pushing the instruction back to
  reservation, otherwise the eventual cache-hit replay will be counted
  twice (once as full/coalesced latency, once as a free hit) and inflate
  `cacheHitRate` and `totalMemAccesses`.
- The replay path only re-checks RAW (via `writeActive`); it assumes the
  instruction's other dynamic dependencies were already satisfied at the
  time validation was issued. This is true under the current scheduler
  but is fragile if the dependency model changes.
- Stats parsing in `experiment_monitor.py` matches exact strings printed
  by `printKernelValidationStats` — update both sides together.
- Denials currently `panic(...)`. If denial becomes a real outcome,
  cleanup of `pendingValidationPages`, `waitingForPage`, reservation
  queue entries, `revalidatedUIDs`, and downstream consumers must be
  added.
- Enabling validation with `kernel_validation_latency=0` is now warned
  about at startup, but still runs the full protocol — useful for
  control runs where you want the bookkeeping but no overhead.

## Current experimental parameters

### IOMMU (analytical, low-end IoT profile)
Defaults model the **smallest realistic accelerator-side IOMMU shipped
in low-power IoT / embedded SoCs** (Arm MMU-400-class band): 8-entry
LRU IOTLB, 2 ns hit, 500 ns miss-walk. The model is per-`LLVMInterface`,
charges every LLVM-IR load/store (including SPM-resident accesses), and
lives entirely inside `launchRead`/`launchWrite`. All three knobs are
CLI-overridable.

### AIA-KD parameters
- Latency per check (`--kernel-validation-latency`): **8367 ns**
  (≈ matches the cumulative TOTAL SECURITY OVERHEAD observed on
  mobilenetv2 with 1 µs latency × ~8K validations across all clusters).
- IRQ to GIC: **disabled** in `llvm_interface.cc` (`gic->sendInt`/
  `clearInt` removed) — confirmed bit-identical sim_ticks with vs
  without IRQ, ISR was an empty stub.

### Recipes

Drive everything via the speedkills package; `summary.tsv` /
`deltas.tsv` are produced automatically.

```bash
# Default 3-way comparison
python3 -m tools.speedkills compare \
    --bench mobilenetv2 \
    --outdir BM_ARM_OUT/mobilenetv2_compare \
    --jobs 3
# == plain, aia-kd, iommu

# All registered benchmarks, all 3 modes
python3 -m tools.speedkills compare-all \
    --outdir BM_ARM_OUT/all_compare --jobs 4
```

All runs: `DerivO3CPU --caches --l2cache --mem-size=4GB
--mem-type=DDR4_2400_8x8` on `VExpress_GEM5_V1` bare-metal.
