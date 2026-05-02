# aia_cda_tests — sanity suite for the `aia_cda_rel` branch

Fast end-to-end checks that exercise all three protection modes
(`plain`, `aia-kd`, `iommu`) on a handful of short-running sys_validation
benchmarks. The suite is intended to catch regressions in the
analytical IOMMU model and the AIA-KD checkpoint hooks **without**
incurring the cost of the long (mobilenetv2 / gemm) workloads.

## What it checks

For each benchmark in `FAST_BENCHES` the suite runs `plain`, `aia-kd`,
and `iommu` and asserts:

1. All three modes finish (rc=0) and produce a `summary.tsv` row.
2. `iommu` mode is **bit-identical to `plain` in `sim_ticks`** (the
   analytical IOMMU only inflates the *reported* effective runtime —
   it must never perturb the simulator).
3. `aia-kd` mode reports `aia_kd_overhead_us > 0` whenever the default
   8.367 µs latency is used (i.e. it is actually accounted for).
4. `iommu` mode reports `iommu_checks > 0` (the LLVM-IR access count
   must populate).

## Usage

```bash
# Default fast set, default 3 modes, 4 parallel slots
python3 tests/aia_cda_tests/run_sanity.py

# Custom output dir, custom parallelism
python3 tests/aia_cda_tests/run_sanity.py \
    --outdir BM_ARM_OUT/aia_cda_sanity --jobs 6

# Restrict to one bench
python3 tests/aia_cda_tests/run_sanity.py --bench nw

# Force regeneration of configs/SALAM/<bench>.py
python3 tests/aia_cda_tests/run_sanity.py --regen
```

Exit code is `0` on success and `1` on the first failed assertion.

## Fast-benchmark selection

Picked from `tools/speedkills/benchmarks.py::REGISTRY` based on
measured plain-mode wall-clock on the dev container (`DerivO3CPU
--caches --l2cache`, DDR4_2400_8x8). Anything with a baseline plain
sim-time under ~2 ms is in the suite; gemm / mobilenetv2 / lenet are
deliberately excluded — use `tools.speedkills compare-all` for the
full sweep.
