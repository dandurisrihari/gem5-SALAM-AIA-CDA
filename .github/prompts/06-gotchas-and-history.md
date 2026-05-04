# 06 — Gotchas and History

> **Use this when** a sanity test or behavioural assertion fails in a
> way that doesn't match a fresh-bug profile. There is a high chance
> it has happened before and is documented here.

## Active gotchas (still relevant)

### Page granularity is hard-coded (4 KiB)

Under Option C the `addr & ~0xFFFULL` mask is consolidated inside
`AiaKdValidator::checkAndCharge`. Nothing else in `LLVMInterface`
sees page bits. If you change page size, change it there.

### Multi-packet AIA-KD defer must be one-shot

A single LLVM-IR access can fire N cache-line-sized packets
sequentially. `CommInterface::tryAiaKdDelay` reads
`MemoryRequest::aiaKdDefer` and **immediately zeros it**, so only
the first packet is deferred. Subsequent packets pass through
inline, but their issue is naturally pushed back by the same `D`
ticks because they only fire after the previous packet's response
has been handled. This enforces "one validation tax per LLVM-IR
access". Do NOT remove the zeroing; without it a 4-cache-line
load would be taxed 4×D.

### `findMemRequest()` panics on unknown packets

Both `tryIommuDelay` and `tryAiaKdDelay` use `findMemRequest()`
to locate the originating `MemoryRequest`. The function panics
on unknown packets. This is fine because the request is in
`accRdQ` / `accWrQ` by the time the response arrives, but if you
ever change the enqueue ordering, both the IOMMU and the AIA-KD
paths will break together.

### Stats parser binds to exact strings

`tools/speedkills/harvest.py` matches **exact** strings printed
by `printKernelValidationStats` and `printIommuStats`:
- `Validation requests (full lat):`
- `of which DMA-ctrl writes:`
- `TOTAL SECURITY OVERHEAD: ... us`
- `========= IOMMU Stats =====================`

Change the C++ side → update the parser regexes in the same commit.

### IOMMU and AIA-KD are mutually exclusive

Both share the response-port hook (`tryIommuDelay` then
`tryAiaKdDelay` in every `recvTimingResp` lambda) and would
double-count overhead. Enforced first at the fs-config layer
(`tools/SALAM-Configurator/fs_template.py`) and again at the C++
ctor (`llvm_interface.cc` `panic`). Don't loosen either check
without redesigning the cost model.

### `kernel_validation_latency=0` MUST be bit-identical to plain

Sanity asserts this. Path: `AiaKdValidator::enabled()` returns
false when latency is 0 (or `enabled` Param is false), so
`chargeValidation` returns 0, `aiaKdDefer` stays 0,
`tryAiaKdDelay` returns false, `recvPacket` runs inline. If you
add code paths that touch counters/state when enabled, gate them
on `validator->enabled()`.

### Denials currently `panic(...)` (or rather, are not modelled)

Option C has no scheduler re-queue path: the decision is captured
as a tick delta and the response is delivered no matter what. If
denial becomes a real outcome, you need a way for `tryAiaKdDelay`
to abort the response — likely a fault packet on the response
port. The `kernelValidationDenied` counter survives but is always
0.

### SimObject namespace gotcha (forward decls)

Forward-declaring SALAM-namespace types **inside** `namespace gem5
{ ... }` creates `gem5::SALAM`, which is a different namespace
from the file-scope `SALAM` declared by the real headers. Result:
"ambiguous reference to SALAM" in every TU that includes both.
Always put SALAM forward decls outside the `gem5` namespace
block. See [03-simobject-layout.md](03-simobject-layout.md).

### Generator vs `AccConfig` ordering

`tools/SALAM-Configurator/config_parser.py` `Accelerator.genDefinition()`
emits lines that run **before** `AccConfig()` is called. At that
point `clstr.<acc>.llvm_interface` does **not** exist yet — it is
created inside `AccConfig`. Wiring lines that touch
`.llvm_interface.<thing>` directly will throw
`AttributeError: object 'CommInterface' has no attribute
'llvm_interface'` at gem5 elaboration. Pass the SimObject through
as an `AccConfig` kwarg instead. (The CommInterface itself **is**
constructed by `genDefinition`, so wiring `clstr.<acc>.iommu`
and `clstr.<acc>.validator` directly is fine and is exactly what
the generator emits today.)

## Historical pitfalls (do NOT re-introduce)

### Option A — post-hoc AIA-KD accounting (mid-2026, abandoned)

For one iteration AIA-KD was modelled by simply *counting* a
cold-miss latency into `totalKernelValidationLatency` from inside
`launchRead`/`launchWrite` and immediately falling through (no
stamp on the request, no response defer). `simTicks` matched
`plain` exactly, so `runtime_us` was identical and
`abs_overhead_us` was always 0 — only the analytical counters
moved. This is meaningless for a comparison study because the
"runtime" axis (which is what the IOMMU overhead axis lives on)
ignores the protection cost entirely.

Replaced by Option C, which stamps the cost on
`MemoryRequest::aiaKdDefer` at launch and realises it on the
response port. AIA-KD now produces real `runtime_us` deltas
comparable to the IOMMU mechanism (sanity nw: 47.6%, fft: 31.6%).

### Option B — in-sim FIFO + GIC IRQ + scheduler replay (abandoned)

Earlier still, AIA-KD stalled the LLVMInterface scheduler on a
cold miss: `launchRead`/`launchWrite` returned `false`, the
instruction was parked in `pendingValidationUIDs`, a chip-wide
FIFO (`AiaKdValidator::pendingRequests`) drove a `responseEvent`
that called `LLVMInterface::completeValidation()` to replay the
access. This produced **negative** `abs_overhead_us` on some
benchmarks (aia-kd `simTicks` lower than plain). Root cause: when
the scheduler stalls on a memory access, it runs through ready
compute instructions and reshuffles DRAM scheduling for the
parallel CUs. The decongestion can outweigh the validation cost.

Lesson: any protection mechanism we want to compare to the IOMMU
must be a **response-side** latency tax. Don't reintroduce
scheduler-side stalls (no `return false` from
`launchRead`/`launchWrite`, no per-instruction replay, no per-CU
`validationResponseEvent`).

### Upstream-of-CommInterface IOMMU injection (5 failed attempts)

Deferring at `launchRead`/`launchWrite` or anywhere before
`comm->enqueueRead` produced `iommu sim_ticks` **lower** than
`plain sim_ticks` — up to -28 % on `nw`. Same root cause as
Option B above: upstream stalls leave `comm` idle while
dependents drain earlier than they would under real memory wait.
Only response-path placement (`CommInterface::tryIommuDelay`,
`tryAiaKdDelay`) avoids it. See git log on
`src/hwacc/llvm_interface.{cc,hh}`.

### "All-three-ports" IOMMU intercept (mid-2026-05, superseded)

For a window the IOMMU intercept fired on **all** of `MemSidePort`,
`SPMPort`, and `RegPort`. That model **over**-counted: it treated
on-cluster scratchpad and register-bank traffic as if it crossed
the SMMU, which a real SMMU sitting between the cluster master
interface and the system bus would never see. Result: on `nw`
`iommu_checks` ballooned to ~241k while AIA-KD (which has always
been off-cluster only) reported `smid_requests` ≈ 121k, leaving the
two mechanisms misaligned by ~2×.

**Current model (post-2026-05 refactor)**: intercept is gated to
**off-cluster** traffic only — `MemSidePort` with `Role::Global`
(the `acp` egress port) plus a new `DmaPort::tryIommuDelay` hook on
the inherited `dma` ports of `NoncoherentDma` and `StreamDma`. The
`Role` enum (`Local | Global | Stream`) is set in
`CommInterface::getPort` based on the python port name. `SPMPort`,
`RegPort`, `MemSidePort`'s `Local`/`Stream` instances, and
`NoncoherentDma::accPort` (`cluster_dma`) all bypass. The `iommu`
SimObject pointer is plumbed to the DMA engines via a new python
`Param.AcceleratorIommu` on `NoncoherentDma`/`StreamDma`, emitted by
`DMA.genConfig`/`StreamDMA.genConfig` in the configurator. The
lat=0 invariant continues to hold bit-identically. Sanity post-
refactor: `nw` iommu_checks dropped 16, `bfs` 585 (DMA-dominated,
98 % IOTLB hit-rate).

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
