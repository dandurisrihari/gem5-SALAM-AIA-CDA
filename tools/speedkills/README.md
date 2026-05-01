# speedkills (aia_cda_rel branch)

Modular driver for gem5-SALAM AIA-CDA experiments. This branch exposes
**three protection modes only**:

| mode      | what it does                                                       |
|-----------|--------------------------------------------------------------------|
| `plain`   | baseline (no protection, no IOMMU, no validation)                  |
| `aia-kd`  | AIA kernel-driver validation, analytical per-validated-page latency |
| `iommu`   | analytical IOMMU: every LLVM-IR load/store goes through an IOTLB lookup; hit/miss latency reported in stats but never perturbs simulator timing |

The real SMMUv3 SimObject profiles (`smmu-iot`, `smmu-mmu500`,
`smmu-server`) and TLB sweep helpers from `main` are intentionally
removed here. Compare against the upstream branch if you need a
cycle-accurate translation device-class study.

## Layout

```
tools/speedkills/
├── __main__.py        # CLI dispatcher
├── config.py          # M5_PATH, gem5 binary, common system flags
├── benchmarks.py      # Bench registry, GROUPS, resolve()
├── profiles.py        # MODES = {plain, aia-kd, iommu}
├── runner.py          # Process orchestration (regen, build, gem5)
├── harvest.py         # stats.txt -> summary.tsv / deltas.tsv
├── run_protection_compare.sh / run_parallel.sh / run_system.sh   # shims
├── legacy/            # pre-package shell drivers (reference only)
└── README.md
```

## Subcommands

```
python3 -m tools.speedkills compare      --bench nw          --outdir OUT
python3 -m tools.speedkills compare-all  --outdir OUT --jobs 8
python3 -m tools.speedkills sweep        --all --latencies 0,8367000 --outdir OUT
python3 -m tools.speedkills run          --bench gemm -d     # gdb
python3 -m tools.speedkills harvest      --outdir OUT        # re-parse only
python3 -m tools.speedkills monitor      -- --port 8080
python3 -m tools.speedkills list
```

### compare

Three-way protection comparison for a single benchmark. Default
`--modes` is `plain aia-kd iommu`.

```
python3 -m tools.speedkills compare --bench mobilenetv2 \
    --outdir BM_ARM_OUT/mbnet_3way --jobs 3
```

IOMMU and AIA-KD knobs:

```
--aia-kd-latency       ticks/page   (default 8_367_000 = 8.367 us)
--iotlb-entries        N            (default 8)
--iotlb-hit-latency    ticks        (default 2_000  = 2 ns)
--iotlb-miss-latency   ticks        (default 500_000 = 500 ns)
```

### compare-all

Run **every** registered benchmark across all three modes. One subdir
per benchmark + per mode, plus an aggregate `summary.tsv` at the root.

```
python3 -m tools.speedkills compare-all \
    --outdir BM_ARM_OUT/aia_cda_rel_full --jobs 8 --build-sw
```

Subset / exclude:

```
python3 -m tools.speedkills compare-all --outdir OUT \
    --bench nw,gemm,mobilenetv2
python3 -m tools.speedkills compare-all --outdir OUT \
    --exclude lenet_a,lenet_b,lenet_c
```

### sweep

Per-bench latency sweep (still useful for AIA-KD knee-point studies).

```
python3 -m tools.speedkills sweep --all --latencies 0,8367000 \
    --outdir BM_ARM_OUT --jobs 8
```

### run

```
python3 -m tools.speedkills run --bench gemm                       # plain
python3 -m tools.speedkills run --bench gemm -f LLVMInterface      # debug-flags
python3 -m tools.speedkills run --bench gemm -d                    # gdb
python3 -m tools.speedkills run --bench gemm -v                    # valgrind
```

## Sanity invariant

At `--aia-kd-latency 0` (and `--iotlb-hit-latency 0
--iotlb-miss-latency 0`) every protection mode **must** match `plain`
to within a handful of ticks. Both AIA-KD and IOMMU here are analytical
— they accumulate reported overhead but do not schedule extra events
inside the simulator. See
[`.github/prompts/sanity-test.prompt.md`](../../.github/prompts/sanity-test.prompt.md).

## Design notes

- **Concurrency**: gem5.opt is process-safe; many can run with distinct
  `--outdir`. Configurator + `make` write into the bench tree, so they
  serialise per-path via `runner._lock_for(path)`.
- **Mode catalog**: `profiles.MODES` is the single source of truth.
- **AccCluster.py rebuild trap**: editing `src/hwacc/AccCluster.py`
  forces a `scons build/ARM/gem5.opt` rebuild before any subcommand
  here picks up the change. See
  `.github/prompts/context-kernel-validation.md`.
