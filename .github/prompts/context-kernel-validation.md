# Context: Kernel-Based Memory Validation (AIA ↔ KD)

Reusable background for prompts that touch the kernel-validation feature.

## Goal

Model the latency overhead of the OS kernel validating every unique memory
page accessed by a hardware accelerator (preventing confused-deputy
attacks). Functionality always succeeds — only the **timing** is modeled.

## Key Files

- Params: [src/hwacc/LLVMInterface.py](../../src/hwacc/LLVMInterface.py)
  - `enable_kernel_validation` (Bool, default False)
  - `kernel_validation_latency` (Tick, default 0)
  - `validation_int_num` (Int32, default 172) — GIC IRQ raised to kernel
  - `process_id` (UInt64, default 17) — SMID for per-process cache
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
--enable-kernel-validation
--kernel-validation-latency=<ticks>
--validation-int-num=<irq>
--process-id=<pid>
```

Driven in bulk via [tools/run_parallel.sh](../../tools/run_parallel.sh)
and visualised by [tools/experiment_monitor.py](../../tools/experiment_monitor.py).

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
