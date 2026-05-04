# 01 — AIA-KD Design (Accelerator Isolation Architecture / Kernel Driver)

> **Use this when** editing AIA-KD validation logic: latency model,
> the `AiaKdValidator` SimObject, page-cache / in-flight bookkeeping,
> the launch-time decision in `LLVMInterface::chargeValidation()`,
> the response-side defer in `CommInterface::tryAiaKdDelay()`, or
> the `MemoryRequest::aiaKdDefer` field that links the two.
> For the high-level diagram see
> [00-architecture-overview.md](00-architecture-overview.md).
> For the historical journey through Options A/B see
> [06-gotchas-and-history.md](06-gotchas-and-history.md).

## What we model

A software-defined protection scheme where the host OS kernel
validates every **unique memory page** an accelerator intends to
touch before the access is allowed to commit. Inspired by the
AIA-KD paper's IRQ-driven kernel-driver path, but modelled as a pure
**latency tax on the response path** so it is apples-to-apples
comparable with the IOMMU mechanism (which already taxes responses).

- **Cost model**: per-page first-touch tax. At each LLVM-IR memory
  access we ask the chip-wide validator to classify the access; the
  result is a per-access tick delta that the response port holds the
  returning packet for. The accelerator scheduler is **not** stalled
  in software — the realised latency is purely a response-side
  stretch (see "Why response-side defer", below).
- **Outcomes returned by `AiaKdValidator::checkAndCharge()`**:
  - `Disabled`  — validator is null or `enabled()` is false; defer = 0.
  - `CacheHit`  — `(pid, page)` already validated; defer = 0.
  - `Coalesced` — the page is currently in flight for any CU; defer
    is the **remaining** ticks until the in-flight entry completes
    (the late-comer pays only the tail of the existing wait, not a
    fresh full-latency request). Does NOT bump `nextReadyTick`.
  - `ColdMiss`  — first chip-wide touch for `(pid, page)`; defer is
    the configured `kernel_validation_latency`. The chip-wide
    kernel-driver deadline (`nextReadyTick`) is bumped by `latency`
    so concurrent cold misses serialize behind one another (one
    driver thread).
  - `DmaCtrl`   — write whose target lies in any DMA engine's PIO
    range; defer is full `latency`, **does NOT enter the page
    cache** (every reprogram pays again), and bumps
    `nextReadyTick` like a cold miss.
- **DMA control-reg policy**: stores into a DMA's PIO range
  reprogram the engine with a (potentially adversarial) (src, dst,
  len) triple, so each one must be inspected by the kernel. Reads
  to those same pages are unaffected (status polling is benign).
- **Coalescing** is "lazy promotion": a follower access pays only
  the *tail* of the existing wait. There is no per-CU wake-up;
  whichever access arrives next pays the remaining ticks. See
  `AiaKdValidator::promoteReadyPages()` for the sweep that moves
  expired in-flight entries into the per-PID cache.
- **Multi-packet rule**: a single LLVM-IR access may fire several
  cache-line-sized packets sequentially. Only the FIRST packet
  carries the defer (`tryAiaKdDelay` zeroes
  `MemoryRequest::aiaKdDefer` after consuming it); subsequent
  packets pass through inline. This enforces "one validation tax
  per LLVM-IR access" rather than "per cache line".

## Option C — design rationale

Two earlier designs ran in this branch and were discarded; this
section records why the current design (Option C) is correct.

| Option | Decision time | Stall site | Result |
|--------|---------------|------------|--------|
| **A** (post-hoc accounting) | LLVM IF | none — counters only | `abs_overhead_us` was the sum of latency in counters but did not appear in `simTicks`; `runtime_us == plain` always. Wrong by definition. |
| **B** (in-sim FIFO + IRQ + replay) | LLVM IF | LLVMInterface scheduler (`return false` from launchRead/Write, replay later) | Could push `aia-kd` simTicks **below** `plain`: stalling the scheduler reshapes DDR queue dynamics across the parallel CUs and decongests memory. Negative `abs_overhead_us`. |
| **C** (current) | LLVM IF | response port (`CommInterface::tryAiaKdDelay`) | Tax is realised as a wall-clock stretch on the returning packet. Mirrors `tryIommuDelay` exactly so AIA-KD and IOMMU produce comparable `abs_overhead_us`. |

Why response-side defer specifically: the IOMMU has always been
modelled as a response-side delay (its hardware sits between memory
and the CU; latency materialises when data comes back). AIA-KD's
kernel-driver round-trip is conceptually upstream, but if we hold
the *issue* the LLVM-IR scheduler runs through alternative ready
instructions and reshuffles DRAM scheduling. Holding the *response*
preserves DRAM scheduling identical to plain, isolating the
protection cost as a pure additive stretch — exactly what we want
to measure.

## SimObject split

The cluster-shared cache, in-flight set, and chip-wide kernel-driver
deadline live on the **`AiaKdValidator`** SimObject (one per
`AccCluster`). This models a single kernel-driver thread that can
service one IRQ at a time — concurrent cold misses across all CUs
serialize through the same `nextReadyTick` counter.

Per-CU mirror counters (used only for the
`printKernelValidationStats` text the harvester regexes bind to)
stay on `LLVMInterface`. There is no per-CU pending-UID set, no
per-CU response event, and no scheduler-side stall.

| Lives on `AiaKdValidator` (cluster-shared) | Lives on `LLVMInterface` (per-CU) |
|--------------------------------------------|------------------------------------|
| `validatedPagesPerProcess[pid]`            | `totalKernelValidations`           |
| `pendingValidationPages[page] = {readyTick, originatorPid}` | `validationCacheHits` |
| `nextReadyTick` (chip-wide deadline)       | `validationCoalescedWaits`         |
| `latency()`, `enabled()`, `isDmaCtrl()`    | `dmaCtrlValidations`               |
| chip-wide stats: `totalColdMisses`, `totalCoalesced`, `totalDmaCtrl`, `totalLatencyTicks` | `totalKernelValidationLatency`, `totalCoalescedWaitLatency` |
| `uniquePages()`, `numCachedProcesses()`    | `kernelValidationDenied` (always 0)|

## Key files

- Header: [src/hwacc/aia_kd_validator.hh](../../src/hwacc/aia_kd_validator.hh)
- Impl  : [src/hwacc/aia_kd_validator.cc](../../src/hwacc/aia_kd_validator.cc) — `checkAndCharge()` + `promoteReadyPages()`
- Param : [src/hwacc/AiaKdValidator.py](../../src/hwacc/AiaKdValidator.py) (`enabled`, `latency`, `dma_pio_ranges`)
- Per-access stamp: [src/hwacc/LLVMRead/src/mem_request.hh](../../src/hwacc/LLVMRead/src/mem_request.hh) — `Tick aiaKdDefer`
- Launch decision: [src/hwacc/llvm_interface.cc](../../src/hwacc/llvm_interface.cc) — `chargeValidation()`, `ActiveFunction::launchRead/launchWrite` stamp the request
- Response defer: [src/hwacc/comm_interface.cc](../../src/hwacc/comm_interface.cc) — `tryAiaKdDelay()`, `processAiaKdRespQueue()`, called from MemSidePort, SPMPort, RegPort `recvTimingResp` lambdas
- Wiring  : [tools/SALAM-Configurator/config_parser.py](../../tools/SALAM-Configurator/config_parser.py) — emits `clstr.<acc>.validator = clstr.validator`
- Stats reporter: [src/hwacc/llvm_interface.cc](../../src/hwacc/llvm_interface.cc) — `printKernelValidationStats()`

## Runtime path (per LLVM-IR memory access)

1. `ActiveFunction::launchRead` / `launchWrite` builds the
   `MemoryRequest` for the access.
2. If AIA-KD is enabled it calls `owner->chargeValidation(addr,
   isWrite)`, which delegates to `validator->checkAndCharge(...)`
   and updates the per-CU mirror counters based on the returned
   `Outcome`. The returned tick delta is stamped onto
   `memReq->aiaKdDefer`.
3. The request is enqueued normally (`comm->enqueueRead/Write`) —
   no scheduler stall, no replay, no pending-UID set.
4. The cache-line packets fire as in plain mode. Each response
   reaches the appropriate `recvTimingResp` lambda
   (MemSidePort / SPMPort / RegPort) and runs the gate:
   ```cpp
   if (owner->tryIommuDelay(pkt)) return true;
   if (owner->tryAiaKdDelay(pkt, pkt->isRead())) return true;
   owner->recvPacket(pkt);
   ```
   `tryAiaKdDelay` looks up the originating `MemoryRequest`, reads
   `aiaKdDefer`, **zeroes it**, and pushes
   `{pkt, curTick() + defer}` onto `pendingAiaKdResps`. The
   `aiaKdRespEvent` is (re)scheduled for the front of the queue.
5. When `aiaKdRespEvent` fires, `processAiaKdRespQueue()` drains
   ready entries (FIFO, ordered by readyTick) and calls
   `recvPacket(pkt)` on each. The access commits at
   `original_arrival + defer`.

## Mutual exclusion with IOMMU

The IOMMU and AIA-KD mechanisms target the same response-port hook
and produce overlapping latency. `LLVMInterface::LLVMInterface()`
panics if both `enable_kernel_validation` and `enable_iommu` are
true. As a result, only one of `pendingIommuResps` /
`pendingAiaKdResps` is ever non-empty in a given run.

## Stats reported (`printKernelValidationStats`)

The harvester (`tools/speedkills/harvest.py`) binds to the **exact
text** of the lines below. Do not rename:

- `Validation requests (full lat):` — full-latency requests
  (cold misses + DMA-ctrl writes)
- `of which DMA-ctrl writes:` — subset attributable to writes
  into any DMA control-reg page. For benchmarks that reprogram
  DMAs many times (`bfs`, `mobilenetv2/body`), expect this to
  dominate the full-latency request count.
- `Cache hits (zero lat):` — page already validated this PID
- `Coalesced waits (partial lat):` — accesses that paid only
  the tail of an in-flight cold miss
- `TOTAL SECURITY OVERHEAD: ... us` — sum of all defers; the
  harvester binds this to compute `abs_overhead_us`.
- Cache stats: `Processes with cached pages`, `Unique pages
  validated (total)`, `Cache hit rate`. Read from the validator
  (`validator->uniquePages()` / `numCachedProcesses()`), so they
  are device-wide regardless of which CU printed.

## Default parameters

- `kernel_validation_latency` (CLI `--kernel-validation-latency`):
  speedkills `aia-kd` mode injects **8 367 000 ticks = 8.367 µs**,
  matching the AIA-KD paper's measured IRQ + driver round-trip.
  The Param default in `LLVMInterface.py` is 0 (opt-in via CLI /
  mode profile). The same value is mirrored onto
  `AiaKdValidator.latency` by the SALAM-Configurator generator;
  the `LLVMInterface` constructor panics on mismatch.
- `validation_int_num` (`--validation-int-num`): 172. The IRQ
  raise is currently NOT used in Option C (the entire model lives
  in `AiaKdValidator` + `MemoryRequest::aiaKdDefer` + the
  response port). The Param survives so old CLI flags keep
  working.
- `process_id` (`--process-id`): 17. Cache key for
  `validatedPagesPerProcess`. Different PIDs across CUs in the
  same cluster simply key separate cache entries.
- `dma_pio_ranges` (`AiaKdValidator.dma_pio_ranges`): the union
  of every DMA engine's `(pio_addr, pio_addr + pio_size)`
  interval, emitted by the SALAM-Configurator. `isDmaCtrl(addr)`
  returns true iff `addr` falls in any of these.

## Things to be careful about

- **Page granularity** is hard-coded as `addr & ~0xFFFULL`
  (4 KiB) inside `AiaKdValidator::checkAndCharge`. Any change
  must touch it there; nothing else in `LLVMInterface` knows
  about page bits in Option C.
- An access whose `[addr, addr+size)` straddles a 4 KiB boundary
  only validates the page containing `addr`. Acceptable today
  (SALAM accesses are word/vector aligned); revisit if you add
  coarser DMA-style accesses.
- The **multi-packet zeroing** in `tryAiaKdDelay` is
  load-bearing: without it, a request split into N cache-line
  packets would be taxed N×D. The first response defers by D and
  pushes every subsequent same-request packet's `tryRead` issue
  back by D, so the access correctly commits at
  `last_packet_arrival + D`.
- `findMemRequest()` panics on unknown packets. The AIA-KD path
  uses it the same way the IOMMU path does; if you change either,
  keep them parallel.
- `chargeValidation()` updates per-CU mirror counters BEFORE the
  request is enqueued, so a panic mid-launch leaves the validator
  and the per-CU counters consistent. Do not split the
  call/stamp/enqueue trio across event boundaries.
- `kernelValidationDenied` is always 0 (no policy implemented).
  If you add a real deny policy, you also need a way for
  `tryAiaKdDelay` to abort the response (Option C has no
  scheduler re-queue path) — likely via a fault packet on the
  response port.
- Enabling validation with `kernel_validation_latency=0` is
  bit-identical to plain (sanity test asserts this). Useful as a
  control to exercise bookkeeping without timing impact.
- `printKernelValidationStats` prints to stdout (console log).
  If any external script parses those strings, update both sides
  together. The strings called out above are tied to
  `tools/speedkills/harvest.py` regexes.

## Multi-cluster behaviour (mobilenetv2)

Single-cluster benchmarks (sys_validation, gemm, fft, nw, lenet)
have **one** `AiaKdValidator` for the whole simulation.
mobilenetv2 has **four** — one per `AccCluster` (`head`, `body`,
`tail`, `classifier`) — see
[00-architecture-overview.md](00-architecture-overview.md#benchmark-topology-mobilenetv2-worked-example)
for the full topology.

Implications when working on AIA-KD code:

- The validated-page cache is **per cluster, not per simulation**.
  A shared input page touched by all four mbnet clusters pays the
  first-touch tax 4×. Do not assume a global cache when reading
  stats or designing tests.
- The chip-wide kernel-driver deadline (`nextReadyTick`) is **per
  validator**, so mbnet has 4 independent deadlines that can each
  be servicing one cold miss at the same wall-clock time. Within
  a single cluster, the next cold miss starts at
  `max(curTick(), nextReadyTick)`.
- `body` is the worst-contended cluster (5 CUs); `classifier`
  (2 CUs) behaves like sys_validation. When debugging contention,
  start with `system.acccluster_body.validator.*` in stats.txt.
- Per-CU mirror counters live on `LLVMInterface`; the chip-wide
  totals live on the validator.
