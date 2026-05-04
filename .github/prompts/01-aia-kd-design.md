# 01 — AIA-KD Design (Accelerator Isolation Architecture / Kernel Driver)

> **Use this when** editing AIA-KD validation logic: latency model, the
> validator SimObject, coalescing on `waitingForPage`, RAW re-check on
> dispatch, or anything in `sendValidationRequest` /
> `completeValidation`. For the high-level diagram see
> [00-architecture-overview.md](00-architecture-overview.md).

## What we model

A software-defined protection scheme where the host OS kernel
validates every **unique memory page** an accelerator intends to
touch before the access is allowed to commit. Inspired by the AIA-KD
paper's IRQ-driven kernel-driver path.

- **Cost model**: per-page first-touch tax. The accelerator stalls
  the offending instruction, raises a GIC IRQ to the host, the
  kernel "validates" the page (functionally a no-op; we charge a
  fixed `kernel_validation_latency`, default 8.367 µs to match the
  AIA-KD paper's measured IRQ + driver round-trip), and the page is
  then cached in `validatedPagesPerProcess[PID]`. Subsequent
  accesses to the same page by the same process are free.
- **DMA control-reg exception (per-write re-validation)**: writes
  whose target lies in any DMA engine's PIO range **bypass** both
  the validated-page cache and pending-page coalescing. Every store
  pays full `kernel_validation_latency`. Justification: each write
  to `SRC` / `DST` / `LEN` / `START` reprograms the DMA with a new
  (potentially adversarial) request, so the kernel must inspect
  every program independently. Reads to those same pages are
  unaffected (status polling is benign). The per-cluster set of
  DMA PIO ranges is plumbed by the SALAM-Configurator into the
  `AiaKdValidator.dma_pio_ranges` VectorParam at config time.
- **Coalescing**: a burst of accesses to a fresh **non-DMA-ctrl** page pays the
  latency only once — followers register on `waitingForPage[page]`
  and are dispatched together when the response fires. DMA-ctrl
  writes do not coalesce: each store fires its own IRQ.
- **What we expect to see**: high one-time overhead on first access
  to a page; near-zero steady-state cost once the working set is
  paged in.

## SimObject split (Stage B + chip-wide-FIFO refactor)

The cluster-shared cache + pending set + waiter queue **and** the
chip-wide FIFO + response event live on the **`AiaKdValidator`**
SimObject (one per `AccCluster`). This models a single kernel-driver
thread fielding one IRQ at a time: ALL CUs in the cluster funnel
their cold-miss requests into one FIFO, drained by one timer.

Each CU's `LLVMInterface` carries a (possibly null) `validator`
pointer. Per-CU state — the `pendingValidationUIDs` set used by the
fast path, the reservation queue, RAW dependency machinery, and the
per-CU stats counters — stays on `LLVMInterface` because it is
intrinsically per-CU.

| Lives on `AiaKdValidator` (cluster-shared) | Lives on `LLVMInterface` (per-CU) |
|--------------------------------------------|------------------------------------|
| `validatedPagesPerProcess[pid]`            | `pendingValidationUIDs`            |
| `pendingValidationPages`                   | `revalidatedUIDs` (RAW replay)     |
| `waitingForPage[pageAddr]`                 | per-CU stats counters              |
| `pendingRequests` (chip-wide FIFO)         | `kernelValidationLatency` (mirrored, cross-checked)|
| `responseEvent` (chip-wide timer)          |                                    |
| `latency()`, `enabled()`                   |                                    |

Both `WaitingInstruction::func` and `PendingRequest::func` are stored
as `void*` inside the SimObject (it is unaware of
`LLVMInterface::ActiveFunction`); the type erasure is undone at the
dispatch site in `LLVMInterface::completeValidation()`. The
`PendingRequest` additionally carries `void *cu` (the originating
`LLVMInterface*`), letting the validator route completion back to
the right CU without knowing its layout.

## Key files

- Header: [src/hwacc/aia_kd_validator.hh](../../src/hwacc/aia_kd_validator.hh)
- Impl  : [src/hwacc/aia_kd_validator.cc](../../src/hwacc/aia_kd_validator.cc) — `enqueue()` + `processResponse()` (drain + dispatch)
- Param : [src/hwacc/AiaKdValidator.py](../../src/hwacc/AiaKdValidator.py) (`enabled`, `latency`)
- Per-CU: [src/hwacc/llvm_interface.{hh,cc}](../../src/hwacc/llvm_interface.cc)
  - `ActiveFunction::launchRead` / `launchWrite` — checkpoint
  - `sendValidationRequest` — build PendingRequest, hand to `validator->enqueue()`
  - `completeValidation(req, now)` — per-request dispatch invoked by
    the validator: RAW re-check + launchRead/Write replay for the
    originator and fan-out to coalesced waiters
  - `queueWaitingInstruction` — coalescing helper
  - `validateWithKernel` — currently always `true`
  - `printKernelValidationStats` — final stats dump

## Runtime path (per memory access)

1. Read/write reaches `launchRead` / `launchWrite`.
2. If validation enabled (validator non-null and `enableKernelValidation`):
   - **Cache hit** (`isPageValidated`) → no latency, fall through.
   - **Pending on same page** (`isPageValidationPending`) → enqueue in
     `validator->waitingForPage[page]`, mark UID pending, return `false`.
   - **Cache miss** → `sendValidationRequest` builds a
     `AiaKdValidator::PendingRequest` (carrying `cu`+`func` for
     callback) and calls `validator->enqueue()`. The validator
     marks the page in `pendingValidationPages` (already done by
     caller) and (re)schedules its single chip-wide `responseEvent`
     for the head deadline `head.requestTime + validator->latency()`.
3. `AiaKdValidator::processResponse()` fires:
   - install the head page in `validatedPagesPerProcess[pid]`,
   - clear from `pendingValidationPages`,
   - call `cu->completeValidation(req, now)` on the originator, which:
     - dispatches the originating request (RAW re-checked),
     - replays every entry in `validator->waitingForPage[page]` —
       each waiter routes through **its own** CU
       (`waitingFunc->owner`) so per-CU stats stay coherent and the
       response port returns to the right CU.
   - reschedule `responseEvent` for the next FIFO head deadline if
     work remains.

## Stats reported (`printKernelValidationStats`)

- `totalKernelValidations` — full-latency requests
- `dmaCtrlValidations` — subset of the above attributable to writes
  into a DMA control-reg page (printed as
  `"of which DMA-ctrl writes"`). For benchmarks that reprogram DMAs
  many times (`bfs`, `mobilenetv2/body`), expect this to dominate
  `totalKernelValidations` and the AIA-KD overhead to scale roughly
  with `(num_dma_programs × writes_per_program × latency)`.
- `validationCacheHits` — zero-latency hits
- `validationCoalescedWaits` + `totalCoalescedWaitLatency` — partial-lat
- `kernelValidationDenied` — currently always 0 (panic on deny)
- Cache hit rate, unique pages validated, processes seen
  (the cache totals are read from `validator->validatedPagesPerProcess`)

## Default parameters

- `kernel_validation_latency` (CLI `--kernel-validation-latency`):
  speedkills `aia-kd` mode injects **8 367 000 ticks = 8.367 µs**,
  matching the cumulative TOTAL SECURITY OVERHEAD observed on
  mobilenetv2 with 1 µs latency × ~8 K validations across all
  clusters. The Param default in `LLVMInterface.py` is 0 (opt-in
  via CLI / mode profile). The same value is mirrored onto
  `AiaKdValidator.latency` by the SALAM-Configurator generator; the
  `LLVMInterface` constructor panics on mismatch.
- `validation_int_num` (`--validation-int-num`): 172. The IRQ raise
  itself (`gic->sendInt`/`clearInt`) is currently **disabled** in
  `llvm_interface.cc`; sim_ticks were confirmed bit-identical with
  vs without the IRQ since the ISR is an empty stub.
- `process_id` (`--process-id`): 17. Cache key for
  `validatedPagesPerProcess`. Mismatched PIDs across CUs no longer
  trigger a startup panic (pre-Stage-B did) — the SimObject simply
  keys separate cache entries.

## Things to be careful about

- Page granularity hard-coded as `addr & ~0xFFFULL` (4 KiB). Any
  change must touch `isPageValidated`, `isPageValidationPending`,
  `sendValidationRequest`, `completeValidation`, and
  `queueWaitingInstruction` together.
- An access whose `[addr, addr+size)` straddles a 4 KiB boundary
  only validates the page containing `addr`. Acceptable today
  (SALAM accesses are word/vector aligned); revisit if you add
  coarser DMA-style accesses.
- `responseEvent` is **chip-wide** (lives on the validator)
  and has only one in-flight schedule; new requests rely on it
  being rescheduled inside `AiaKdValidator::processResponse()` when
  the head of `pendingRequests` is not yet ready. The originator's
  `LLVMInterface` no longer owns a per-CU event.
- RAW hazard re-queue logic is duplicated for the originating
  request and for waiters; keep both paths in sync. Both **must**
  call `revalidatedUIDs.insert(uid)` before pushing the instruction
  back to reservation, otherwise the eventual cache-hit replay will
  be counted twice (once as full/coalesced latency, once as a free
  hit) and inflate `cacheHitRate` and `totalMemAccesses`.
- The replay path only re-checks RAW (via `writeActive`); it
  assumes the instruction's other dynamic dependencies were already
  satisfied at the time validation was issued.
- Denials currently `panic(...)`. If denial becomes a real outcome,
  cleanup of `validator->pendingValidationPages`,
  `validator->waitingForPage`, `validator->pendingRequests`,
  reservation queue entries, `revalidatedUIDs`, and downstream
  consumers must all be added.
- Enabling validation with `kernel_validation_latency=0` still runs
  the full protocol (warned at startup) — useful for control runs
  where you want the bookkeeping but no overhead. The sanity suite
  uses this to assert protocol bookkeeping is timing-inert.
- `printKernelValidationStats` prints to stdout (console log). If any
  external script parses those strings, update both sides together.

## Multi-cluster behaviour (mobilenetv2)

Single-cluster benchmarks (sys_validation, gemm, fft, nw, lenet) have
**one** `AiaKdValidator` for the whole simulation. mobilenetv2 has
**four** — one per `AccCluster` (`head`, `body`, `tail`,
`classifier`) — see [00-architecture-overview.md](00-architecture-overview.md#benchmark-topology-mobilenetv2-worked-example)
for the full topology.

Implications when working on AIA-KD code:

- The validated-page cache is **per cluster, not per simulation**. A
  shared input page touched by all four mbnet clusters pays the
  first-touch tax 4×. Do not assume a global cache when reading
  stats or designing tests.
- The chip-wide FIFO + `responseEvent` are **per validator**, so
  mbnet has 4 independent FIFOs that can each have one IRQ in
  flight at the same wall-clock time. Within a single cluster only
  one IRQ is in flight (the model's invariant).
- `body` is the worst-contended cluster (5 CUs); `classifier` (2
  CUs) behaves like sys_validation. When debugging contention,
  start with `system.acccluster_body.validator.*` in stats.txt.
- Per-CU stats (`totalKernelValidations`, `validationCacheHits`,
  `dmaCtrlValidations`, `validationCoalescedWaits`, etc.) still
  live on `LLVMInterface`, so they aggregate per-CU regardless of
  which cluster the CU belongs to.
