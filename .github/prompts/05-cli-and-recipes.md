# 05 — CLI Surface and Recipes

> **Use this when** running experiments, adding a new CLI flag, or
> wiring a new mode into the speedkills profile registry.

## CLI flags

Forwarded by [tools/SALAM-Configurator/fs_template.py](../../tools/SALAM-Configurator/fs_template.py)
through `addHWAccOptions` and surfaced by every generated
`configs/SALAM/fs_*.py`:

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

The two protection flags (`--enable-kernel-validation`,
`--enable-iommu`) are **mutually exclusive**:

- enforced first at the fs-config layer
  (`tools/SALAM-Configurator/fs_template.py` and the generated
  `configs/SALAM/fs_*.py`) — `Error: protection-model flags are
  mutually exclusive: ...` aborts the run before gem5 elaborates;
- additionally re-checked at the C++ ctor (`llvm_interface.cc`
  `panic`) since both share the `launchRead`/`launchWrite` hook.

`speedkills compare` runs each mode in its own gem5 process /
outdir, so modes can never co-activate inside a single simulation.
The default mode set is `(plain, aia-kd, iommu)`. The real SMMUv3
SimObject path that existed on `main` has been removed from this
branch — see [tools/speedkills/README.md](../../tools/speedkills/README.md).

## The three experimental modes

- **plain** — no flags set. Baseline.
- **aia-kd** — `--enable-kernel-validation` plus latency. First-touch
  tax, then free.
- **iommu** — `--enable-iommu` plus IOTLB knobs. Analytical tax
  (per-access IOTLB look-aside model). Defaults to a constrained
  edge-IoT peripheral IOMMU (Cortex-M / Cortex-A5-class, ~400 MHz,
  DDR3 walk): 8-entry LRU IOTLB, 2 ns hit, 500 ns miss-walk. Charges
  every LLVM-IR load/store (including SPM-resident accesses).

## Recipes

Drive everything via the speedkills package; `summary.tsv` /
`deltas.tsv` are produced automatically.

### 3-way comparison on one benchmark

```bash
python3 -m tools.speedkills compare \
    --bench mobilenetv2 \
    --outdir BM_ARM_OUT/mobilenetv2_compare \
    --jobs 3
# == plain, aia-kd, iommu
```

### All registered benchmarks, all 3 modes

```bash
python3 -m tools.speedkills compare-all \
    --outdir BM_ARM_OUT/all_compare --jobs 4
```

### Sanity suite (after every rebuild)

```bash
python3 tests/aia_cda_tests/run_sanity.py --regen
```

### Latency sweep (single bench, varying AIA-KD latency)

```bash
python3 -m tools.speedkills sweep \
    --bench nw \
    --outdir BM_ARM_OUT/nw_aiakd_sweep \
    --latencies 0,1000,8367000,16734000 \
    --jobs 4
```

### Single-bench debug

```bash
python3 -m tools.speedkills run \
    --bench nw --mode aia-kd \
    --outdir BM_ARM_OUT/nw_one
```

### Re-parse stats from an existing outdir

```bash
python3 -m tools.speedkills harvest --outdir BM_ARM_OUT/<old_run>
```

### List available benches and modes

```bash
python3 -m tools.speedkills list
```

## Run platform

All runs: `DerivO3CPU --caches --l2cache --mem-size=4GB
--mem-type=DDR4_2400_8x8` on `VExpress_GEM5_V1` bare-metal.

Tick base: 1 tick = 1 ps (gem5 default). All latencies in this
codebase are expressed in ticks; convert to time as ticks × 1 ps.

## Visualisation

`tools/experiment_monitor.py` and the `monitor` subcommand have been
removed from this branch. Parse stats directly from `stats.txt`
(last `Begin Simulation Statistics` block) or from the
`summary.tsv` / `deltas.tsv` files produced by `speedkills harvest`.
If you change a printed line in `llvm_interface.cc`, update the
harvester regexes in `tools/speedkills/harvest.py` too.
