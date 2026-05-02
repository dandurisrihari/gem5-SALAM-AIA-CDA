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
