# Context: Kernel-Based Memory Validation (AIA ↔ KD) and IOMMU baseline

Reusable background for prompts that touch the kernel-validation feature
or the comparable IOMMU latency model that lives alongside it.

## Goal

Model the latency overhead of the OS kernel validating every unique memory
page accessed by a hardware accelerator (preventing confused-deputy
attacks). Functionality always succeeds — only the **timing** is modeled.

A second, mutually exclusive mode (`enable_iommu`) provides an IOMMU-style
baseline so papers can compare AIA-KD vs hardware translation overhead on
the same workload.

## Working agreements (read first, every session)

These are non-negotiable habits for anyone (human or LLM) editing this
feature. Skipping them has cost real time in the past.

1. **Keep this prompt in sync with the code.** Whenever you change any
   of the following, update the relevant section of *this file* in the
   same commit / turn:
   - SMMU defaults in `src/hwacc/AccCluster.py::_connect_caches_smmu`
     → update *Current experimental parameters* table.
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
       (`PROFILE_IOT`, `PROFILE_MMU500`, `PROFILE_SERVER`, `MODES`),
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
   For SMMU param changes specifically, sketch the cost model
   (working-set vs cache reach, expected miss rate, ns/walk) so the
   predicted overhead can be checked against the measured value.

4. **Rebuild reminder.** `src/hwacc/AccCluster.py` is embedded into
   `gem5.opt` (`[EMBED PY]`). Edits to it have *no effect* until
   `scons build/ARM/gem5.opt -jN` finishes. Check
   `ls -la build/ARM/gem5.opt` after the build before launching runs.

5. **Don't pin parameters to a specific vendor part** unless the user
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

The four experimental modes are:
- **plain** — no flags set. Baseline.
- **aia-kd** — `--enable-kernel-validation` plus latency. First-touch tax,
  then free.
- **iommu** — `--enable-iommu` plus IOTLB knobs. Cheap analytical tax
  (per-access IOTLB look-aside model) living inside `LLVMInterface`.
  Per-accelerator only; defaults to a *constrained edge-IoT*
  peripheral IOMMU (Cortex-M / Cortex-A5-class, ~400 MHz, DDR3 walk):
  8-entry LRU IOTLB, 2 ns hit, 500 ns miss-walk. Deliberately tight
  so reviewers can't dismiss the comparison as an IOMMU strawman with
  unrealistically large TLBs.
- **smmu-iot** (and `smmu-mmu500`, `smmu-server`, `smmu-bypass`) —
  `--enable-real-smmu` plus a profile from `profiles.py`. Instantiates a
  real `SMMUv3` per `AccCluster` between the cluster's coherency bus
  and the system memory bus, with stream-table programming on by
  default. Knobs are the full `--smmu-*` family. The wiring lives in
  `AccCluster._connect_caches_smmu`.

The three protection flags (`--enable-kernel-validation`,
`--enable-iommu`, `--enable-real-smmu`) are **mutually exclusive**:
  - enforced first at the fs-config layer
    (`tools/SALAM-Configurator/fs_template.py` and the 14 generated
    `configs/SALAM/fs_*.py`) — `Error: protection-model flags are
    mutually exclusive: ...` aborts the run before gem5 elaborates;
  - additionally re-checked at the C++ ctor for the AIA-KD/IOMMU pair
    (`llvm_interface.cc` `panic`) since both share the
    `launchRead`/`launchWrite` hook.

`speedkills compare` runs each mode in its own gem5 process / outdir,
so modes can never co-activate inside a single simulation. The default
mode set is `(plain, aia-kd, iommu, smmu-iot)` — the four-way
comparison; the richer SMMU profiles are still selectable via
`--modes "..."`.

Driven in bulk via [`python3 -m tools.speedkills sweep`](../../tools/speedkills/README.md)
and visualised by [tools/experiment_monitor.py](../../tools/experiment_monitor.py)
(also reachable as `python3 -m tools.speedkills monitor -- ...`).

## IOMMU model details

- Lives in the same `LLVMInterface::ActiveFunction::launchRead/launchWrite`
  hook points, ahead of the AIA-KD block. Mutual exclusion is enforced
  in the constructor (`panic`) and in `AccConfig` (`raise`).
- Fully-associative LRU IOTLB of `iotlb_entries` page numbers
  (default **8** — constrained edge-IoT uTLB).
- Hit pays `iotlb_hit_latency` (default 2 000 ticks = 2 ns @ ~400 MHz);
  miss pays `iotlb_miss_latency` (default 500 000 ticks = 500 ns) and
  installs the entry (LRU evict if full). `iotlb_entries=0` => every
  access misses. All three knobs are CLI-overridable and surfaced as
  `compare --iotlb-entries / --iotlb-hit-latency / --iotlb-miss-latency`.
- **Per-device**: each `LLVMInterface` (i.e. each accelerator compute
  unit) owns an independent IOTLB and stats. **No shared L2 IOTLB at
  the TCU is modeled** — this is the *conservative* IOMMU profile
  (a real SoC could amortise some misses through a shared TLB, which
  would slightly reduce IOMMU overhead). The AIA-KD cache, by
  contrast, is keyed on PID and naturally shared across accelerators
  of the same process for free.
- Per-access deferral mirrors AIA: instruction returns `false`,
  `iommuPendingUIDs` keeps it from re-launching, `iommuDispatchEvent`
  fires at the deadline and dispatches via the same RAW-checked path.
- RAW-deferred replays use `iommuClearedUIDs` to bypass the IOMMU block
  and avoid double-charging latency.
- Stats printed by `printIommuStats()` after `printKernelValidationStats()`.

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

## Current experimental parameters (low-end IoT profile, MMU-400-class)

We model the **smallest realistic accelerator-side IOMMU shipped in
low-power IoT / embedded SoCs** — Arm **MMU-400-class**: SMMUv1, small
TLBs, **no walk cache at all** (every miss is a full N-step walk to
DRAM), single PTW thread, single translate slot. This is deliberately
the *most pessimistic* defensible profile so the SMMU column shows a
non-trivial overhead instead of vanishing into noise.

All knobs are CLI-overridable, so the same binary sweeps from this
default up through "MMU-500 small config" (pass `--smmu-walk-enable`
plus walk-S1L* sizes) all the way to gem5's server default — no
rebuild needed.

We deliberately avoid pinning this to one specific commercial part —
the goal is "plausibly representative of a low-end accelerator IOMMU",
not "exact replica of vendor X's product". Reviewers who want a
specific vendor target can re-run the sweep with that vendor's numbers.

Note on naming: Arm "MMU-400/500/600/700" IP are **System MMUs (IOMMUs)**
that sit between devices and memory, *not* CPU-side MMUs.

### Per-cluster SMMU defaults
Set in `src/hwacc/AccCluster.py::_connect_caches_smmu` and exposed as
`--smmu-*` CLI flags by `fs_*.py`. Source for configurable ranges is
the Arm MMU-400 / MMU-500 TRM (main TLB 16–1024, µTLB 4–32, walk-cache
0–1024 per level, 1–8 PTW threads). Our defaults pick the *minimum*.

| Knob | CLI | Low-end IoT default | gem5 server default |
|---|---|---:|---:|
| Translation slots (TCU) | `--smmu-xlate-slots` | 2 | 64 |
| Page-table walk slots | `--smmu-ptw-slots` | 1 | 16 |
| Walk-cache lookup slots | `--smmu-walk-slots` | 1 | 16 |
| Config (STE/CD) cache | `--smmu-cfg-entries` | 4 | 64 |
| Walk cache enabled | `--smmu-walk-enable` | **off (MMU-400)** | on |
| Walk cache S1 (L0/L1/L2/L3) | `--smmu-walk-s1l{0..3}` | 0 / 0 / 0 / 0 | 4 / 28 / 348 / 4 |
| Walk cache S2 (L0/L1/L2/L3) | (constants) | disabled | 4 / 28 / 92 / 4 |
| IPA / stage-2 cache | (constant) | disabled | enabled |
| Shared TLB at TCU | `--smmu-tlb-entries` | 16 | 2048 |
| Shared TLB assoc / lat | `--smmu-tlb-assoc` / `--smmu-tlb-lat` | 2 / 3 cy | 4 / 3 cy |
| Shared TLB lookup slots | `--smmu-tlb-slots` | 1 | – |
| Per-TBU main TLB | `--smmu-ifctlb-entries` | 16 | 2048 |
| Per-TBU micro-TLB | `--smmu-utlb-entries` | 4 (MMU-400/500 min) | 32 |
| Per-TBU translate slots | `--smmu-tbu-xlate-slots` | 1 | 16 |
| ifc↔smmu link latency | `--smmu-ifc-lat` | 12 cy | 8 cy |
| Granule | `--smmu-granule-kib` | 4 KiB | – |

**Cost model with the no-walk-cache default (4 KiB pages, 4-level walk):**
every TBU miss pays a full 4-step walk = 4 × DRAM access ≈ 320 ns. With
a 16-entry TLB and 4-entry µTLB the working set thrashes constantly,
so most boundary accesses miss. This is the dominant cost we expect in
`smmu-prog` runs on this profile.

### Recipes for sweeping up the SoC tier

Preferred: drive everything via the speedkills package — modes are
named, profiles live in `tools/speedkills/profiles.py`, and
`summary.tsv` / `deltas.tsv` are produced automatically.

```bash
# Full protection-mode comparison (mobilenetv2)
python3 -m tools.speedkills compare \
    --bench mobilenetv2 \
    --outdir BM_ARM_OUT/mobilenetv2_smmu_compare \
    --jobs 4
# == plain, aia-kd, smmu-bypass, smmu-iot, smmu-mmu500, smmu-server

# Just the three IOMMU SoC tiers
python3 -m tools.speedkills compare --bench mobilenetv2 \
    --outdir BM_ARM_OUT/iommu_tiers \
    --modes "smmu-iot smmu-mmu500 smmu-server"

# TLB sensitivity sweep at the IoT profile
python3 -m tools.speedkills compare --bench mobilenetv2 \
    --outdir BM_ARM_OUT/tlb_sweep \
    --modes "plain smmu-tlb-sweep" \
    --tlb-sweep "8 16 32 64 256 2048"
```

Equivalent raw-flag invocations (only useful if bypassing the package):

```bash
# (1) Low-end IoT (MMU-400 / no walk cache) — current default
build/ARM/gem5.opt ... fs_mobilenetv2.py ... --enable-real-smmu \
    --smmu-program-stream-table --smmu-granule-kib 4

# (2) MMU-500 small-config (with walk cache)
build/ARM/gem5.opt ... fs_mobilenetv2.py ... --enable-real-smmu \
    --smmu-program-stream-table --smmu-granule-kib 4 \
    --smmu-tlb-entries 32 --smmu-ifctlb-entries 32 \
    --smmu-utlb-entries 4 --smmu-cfg-entries 8 \
    --smmu-xlate-slots 4 --smmu-ptw-slots 2 \
    --smmu-walk-enable \
    --smmu-walk-s1l0 2 --smmu-walk-s1l1 4 \
    --smmu-walk-s1l2 8 --smmu-walk-s1l3 4 \
    --smmu-walk-slots 2 --smmu-walk-assoc 2 \
    --smmu-ifc-lat 8

# (3) Server-class (gem5 SMMUv3 defaults)
build/ARM/gem5.opt ... fs_mobilenetv2.py ... --enable-real-smmu \
    --smmu-program-stream-table --smmu-granule-kib 4 \
    --smmu-tlb-entries 2048 --smmu-ifctlb-entries 2048 \
    --smmu-utlb-entries 32 --smmu-cfg-entries 64 \
    --smmu-xlate-slots 64 --smmu-ptw-slots 16 \
    --smmu-walk-enable \
    --smmu-walk-s1l0 4 --smmu-walk-s1l1 28 \
    --smmu-walk-s1l2 348 --smmu-walk-s1l3 4 \
    --smmu-walk-slots 16 --smmu-walk-assoc 4 \
    --smmu-ifc-lat 8 --smmu-tbu-xlate-slots 16
```

### Latency envelopes (Arm MMU-500-class, sanity-check rubric)

Order-of-magnitude bounds typical of small accelerator-side SMMUs:

| Path | Expected | gem5 knob that drives it |
|---|---|---|
| TBU µTLB hit / TBU main-TLB hit | ≈ 1 access/cycle (no penalty) | `tlb_lat` (3 cy) |
| TBU miss → TCU walk-cache hit | tens of cycles | `walk_lat` × walk-cache levels + IFC link |
| TBU miss → TCU miss → full page-table walk | hundreds–thousands of cycles | above + 3–4 DRAM accesses |

If a sweep ever shows the `smmu-prog` mode breaking through the
~1000-cycle envelope on a per-access basis (after subtracting DRAM
service time), revisit `walk_lat`, `walk_slots`, and the IFC link
latency.

### AIA-KD parameters
- Latency per check (`--kernel-validation-latency`): **8367 ns**
  (≈ matches the cumulative TOTAL SECURITY OVERHEAD observed on
  mobilenetv2 with 1 µs latency × ~8K validations across all clusters)
- IRQ to GIC: **disabled** in `llvm_interface.cc` (`gic->sendInt`/
  `clearInt` removed) — confirmed bit-identical sim_ticks with vs
  without IRQ, ISR was an empty stub.

### Three modes we compare on mobilenetv2

1. **plain** — no protection. Baseline.
2. **aia-kd** — `--enable-kernel-validation --kernel-validation-latency 8367`.
3. **smmu-prog** — `--enable-real-smmu --smmu-program-stream-table
   --smmu-granule-kib 4` with edge-IoT profile defaults above.

All runs: `DerivO3CPU --caches --l2cache --mem-size=4GB
--mem-type=DDR4_2400_8x8` on `VExpress_GEM5_V1` bare-metal with
`benchmarks/mobilenetv2/sw/main.elf`.
