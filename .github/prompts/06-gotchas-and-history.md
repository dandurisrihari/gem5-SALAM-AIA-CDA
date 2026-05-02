# 06 — Gotchas and History

> **Use this when** a sanity test or behavioural assertion fails in a
> way that doesn't match a fresh-bug profile. There is a high chance
> it has happened before and is documented here.

## Active gotchas (still relevant)

### Page granularity is hard-coded (4 KiB)

`addr & ~0xFFFULL` is duplicated in `isPageValidated`,
`isPageValidationPending`, `sendValidationRequest`,
`completeValidation`, and `queueWaitingInstruction`. Any
change to page size must touch all five together.

### RAW re-queue must mark `revalidatedUIDs` on both paths

The replay path is duplicated for the originating request and the
coalesced waiters. Both **must** call
`revalidatedUIDs.insert(uid)` before re-pushing the instruction
into the reservation queue. Skipping this on either path
double-counts the eventual cache-hit replay (once as full /
coalesced latency, once as a free hit) and inflates `cacheHitRate`
and `totalMemAccesses`.

### `responseEvent` is chip-wide and single-shot

There is only ever one in-flight schedule for the whole cluster
(it lives on `AiaKdValidator`, not per-CU). New requests rely on
`AiaKdValidator::processResponse()` re-scheduling itself when the
head of `pendingRequests` is not yet ready. Don't add a second
schedule path on `LLVMInterface` — the per-CU
`validationResponseEvent` was deliberately removed.

### Stats parser binds to exact strings

`tools/experiment_monitor.py` (and the speedkills harvester) match
**exact** strings printed by `printKernelValidationStats` and
`printIommuStats`. Change the C++ side → update the parser regexes
in the same commit.

### IOMMU and AIA-KD are mutually exclusive

Both share the `launchRead`/`launchWrite` hook and would
double-count overhead. Enforced first at the fs-config layer
(`tools/SALAM-Configurator/fs_template.py`) and again at the C++
ctor (`llvm_interface.cc` `panic`). Don't loosen either check
without redesigning the cost model.

### `kernel_validation_latency=0` still runs the protocol

Useful as a control run / sanity check (the bookkeeping is
timing-inert at lat=0, used by the sanity suite). A startup warning
fires; do not change this to a panic.

### Denials currently `panic(...)`

If denial becomes a real outcome, cleanup of
`validator->pendingValidationPages`,
`validator->waitingForPage`, reservation queue entries,
`revalidatedUIDs`, and downstream consumers must be added.

### SimObject namespace gotcha (forward decls)

Forward-declaring SALAM-namespace types **inside** `namespace gem5
{ ... }` creates `gem5::SALAM`, which is a different namespace
from the file-scope `SALAM` declared by the real headers. Result:
"ambiguous reference to SALAM" in every TU that includes both.
Always put SALAM forward decls outside the `gem5` namespace block.
See [03-simobject-layout.md](03-simobject-layout.md).

### Generator vs `AccConfig` ordering

`tools/SALAM-Configurator/config_parser.py` `Accelerator.genDefinition()`
emits lines that run **before** `AccConfig()` is called. At that
point `clstr.<acc>.llvm_interface` does **not** exist yet — it is
created inside `AccConfig`. Wiring lines that touch
`.llvm_interface.<thing>` directly will throw
`AttributeError: object 'CommInterface' has no attribute 'llvm_interface'`
at gem5 elaboration. Pass the SimObject through as an `AccConfig`
kwarg instead.

## Historical pitfalls (do NOT re-introduce)

### Upstream-of-CommInterface IOMMU injection (5 failed attempts)

Deferring at `launchRead`/`launchWrite` or anywhere before
`comm->enqueueRead` produced `iommu sim_ticks` **lower** than
`plain sim_ticks` — up to -28 % on `nw`. Root cause: deferring
upstream left `comm` idle while the dependency tracker thought
memory was busy, so downstream dependents drained earlier than
they would under real memory wait. Even the AIA-KD-style "bail
before queue insert" variant failed. The artifact is fundamental
to upstream injection; only response-path placement (current
design — `CommInterface::tryIommuDelay`) avoids it. See git log
on `src/hwacc/llvm_interface.{cc,hh}`.

### MemSidePort-only IOMMU intercept (pre-2026-05)

For a brief window the IOMMU intercept lived only in
`MemSidePort::recvTimingResp`, leaving SPMPort and RegPort traffic
untranslated. Result: `iommu_overhead_us ≈ 0` on SPM-resident
workloads (most of the suite) and `iommu_checks` under-counted by
~25× on workloads like `nw`. Fix: factored the intercept into
`CommInterface::tryIommuDelay()` and called from all three
`recvTimingResp` paths. The lat=0 invariant continues to hold
bit-identically.

### Extra `EventFunctionWrapper` per access (early IOMMU rework)

An earlier IOMMU draft routed every access through an extra
`EventFunctionWrapper` even at `latency=0`. The added event hops
re-ordered unrelated `CommInterface` ticks and DMA programming
MMIOs, causing `nw` to finish ~20 % *faster* than `plain`. Crash
that surfaced it: `assert(readFrameBuffSize != 0)` in
`StreamDma::tick()` because `RD_START` arrived before
`RD_FRAME_BUFF_SIZE`. The current model uses the response-path
hook (no schedule on the request side) and `lat=0` is a no-op
fast-path bypass — keep it that way.

### Process-wide statics for shared state (Stages A and B refactor)

The IOMMU `sIommuNextReadyTick` + IOTLB and the AIA-KD
`validatedPagesPerProcess` / `pendingValidationPages` /
`waitingForPage` used to be file-scope statics. This silently
broke multi-system gem5 simulations (one process-wide cache
shared across unrelated systems) and prevented a clean Param
surface. Both are now SimObjects (`AcceleratorIommu`,
`AiaKdValidator`) instantiated once per `AccCluster`. Do not
re-introduce process-wide statics for shared protection state.

### PID-uniformity startup panic (obsolete after Stage B)

Pre-Stage-B, AIA-KD enforced uniform `process_id` across all CUs
via a static sentinel because the validated-page cache was
process-static. The sentinel is gone — the SimObject simply keys
`validatedPagesPerProcess` by PID, so mismatched PIDs across CUs
create separate cache entries instead of silently missing. Do not
restore the panic; it would over-constrain valid configurations.

### Per-CU FIFO + per-CU response event (obsolete after Stage B+)

Stage B initially left `pendingValidations` (FIFO) and
`validationResponseEvent` per-CU on `LLVMInterface`, with N CUs
potentially having N IRQs in flight at once. That over-modelled
parallelism — real hardware has one kernel-driver thread fielding
one IRQ at a time. The chip-wide-FIFO refactor (May 2026) moved
both onto `AiaKdValidator` (`pendingRequests` + `responseEvent`),
giving one FIFO per cluster. Per-request dispatch now lives in
`LLVMInterface::completeValidation(req, now)`, called by the
validator. Sanity numbers reproduced bit-identically post-refactor
(`nw` AIA-KD = 140.145 µs / 13.116 %; `fft` = -5.531 µs);
correctness is preserved because waiters were already coalescing
on the shared `waitingForPage`, but contention semantics now
match the modelled "single driver" service correctly. Do not
restore per-CU FIFOs; the validator's `enqueue()` is the single
entry point.
