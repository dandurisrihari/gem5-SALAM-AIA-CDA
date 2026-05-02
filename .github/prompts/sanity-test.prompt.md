# Sanity test — protection-mode latency models

**TL;DR — after every rebuild of `gem5.opt`** (i.e. any change to
`src/hwacc/`, `src/dev/arm/`, embedded SimObject `.py`, or
SALAM-Configurator templates that change generated `configs/SALAM/*.py`)
run the packaged suite first:

```bash
python3 tests/aia_cda_tests/run_sanity.py --regen
```

It executes 4 fast benches × 3 modes (~under a minute) and asserts the
core invariants (iommu sim_ticks bit-identical to plain, AIA-KD and
IOMMU hooks firing). Only after it reports `[PASS]` should you fall
back to the focused zero-latency tests below for diagnosing a deeper
regression. **Do not commit if the suite fails.**

## When to also run the focused zero-latency tests

Any time you touch `LLVMInterface` IOMMU / AIA-KD code paths
in `src/hwacc/llvm_interface.{cc,hh}` (in particular `launchRead`,
`launchWrite`, `launchReadAfter`, `launchWriteAfter`,
`sendValidationRequest`, `processValidationResponse`, the IOTLB helpers,
or any of the request-/response-side scheduling).

**Why:** these models are *analytical* — they must add reported latency
without perturbing simulator timing. A previous IOMMU rework went through
an extra `EventFunctionWrapper` per access; even with `latency=0` it made
NW finish ~20 % *faster* than `plain` because the extra event hops
re-ordered unrelated `CommInterface` ticks and DMA programming MMIOs (the
crash that exposed it: `assert(readFrameBuffSize != 0)` in
`StreamDma::tick()`). Always sanity-check at zero latency before trusting
the overhead numbers at production latency.

## The test

Run a small benchmark (`nw` is ideal — finishes in ~1 ms sim time, has
~100 k accesses, low variance) with the latency parameter forced to 0.
The protection-mode runtime **must** equal the `plain` runtime to within
a few ticks (sub-microsecond noise is fine, percent-level deltas are a
bug).

Build first if you changed C++:

```bash
scons build/ARM/gem5.opt -j4 2>&1 | tail -5
```

### IOMMU sanity

```bash
rm -rf BM_ARM_OUT/sanity_iommu_zero
python3 -m tools.speedkills compare \
    --bench nw \
    --outdir "$PWD/BM_ARM_OUT/sanity_iommu_zero" \
    --modes "plain iommu" \
    --iotlb-hit-latency 0 --iotlb-miss-latency 0 \
    --jobs 2
```

Expected:

| mode  | sim_ticks                | TOTAL IOMMU OVERHEAD |
|-------|--------------------------|----------------------|
| plain | 1 068 516 500 (1068.516 µs) | n/a                  |
| iommu | **identical** sim_ticks    | 0 µs                 |

If `iommu` ticks differ from `plain` by more than ~10 ticks, the model is
perturbing the event queue. The fix is to make the IOMMU branch a
zero-cost no-op at lat=0: enqueue inline (no `EventFunctionWrapper`,
no `schedule()`), and account latency analytically via
`accountIommuAccess()` only.

### AIA-KD sanity

```bash
rm -rf BM_ARM_OUT/sanity_aiakd_zero
python3 -m tools.speedkills compare \
    --bench nw \
    --outdir "$PWD/BM_ARM_OUT/sanity_aiakd_zero" \
    --modes "plain aia-kd" \
    --aia-kd-latency 0 \
    --jobs 2
```

Expected: aia-kd within < 0.1 % of plain (NW baseline ~1068 µs, observed
delta ~0.7 µs ≈ −0.06 %). The AIA-KD path involves real IRQ scheduling
even at lat=0, so a few hundred picoseconds of skew is unavoidable;
anything larger is a bug.

### What to grep in `run.log`

```bash
grep -A 16 "IOMMU Stats"             BM_ARM_OUT/<dir>/iommu/run.log
grep -E "Cache hit|Validations|Avg"  BM_ARM_OUT/<dir>/aia-kd/run.log
```

Verify:
- `Total IOMMU checks` > 0 (model is actually running, not bypassed)
- `IOTLB hit rate` ≈ 99 %+ for steady-state benchmarks
- `Validation requests (full lat)` matches the unique-page count expected
  for the workload

## When the sanity test fails

1. **Don't** "fix" it by adjusting timings or tolerances — find the
   simulator-side perturbation.
2. Common culprits introduced by edits:
   - Adding `schedule(EventFunctionWrapper, curTick() + lat)` in the
     request path (perturbs `CommInterface::tick` / `StreamDma::tick`
     ordering — same bug class as the IOMMU rework).
   - Re-issuing the same UID through the scheduler twice (RAW replay
     bookkeeping; check `uidActive()` membership tests).
   - Calling `comm->enqueueRead/Write` outside the original tick on the
     MMIO programming path of `StreamDma` (causes
     `assert(readFrameBuffSize != 0)` because `RD_START` arrives before
     `RD_FRAME_BUFF_SIZE`).
3. The honest analytical pattern: enqueue the request **inline** (same
   call as `plain`), bump the stats counters, and let the report add
   `sim_runtime + accumulated_latency` for the "effective with-IOMMU"
   number. (The cycle-accurate SMMUv3 path is not available on the
   `aia_cda_rel` branch.)

## Larger sweeps (after sanity passes)

Only after both sanity tests pass should you run the production
3-way comparison on bigger workloads:

```bash
python3 -m tools.speedkills compare \
    --bench mobilenetv2 \
    --outdir "$PWD/BM_ARM_OUT/<run>" \
    --modes "plain aia-kd iommu" \
    --jobs 3
```
