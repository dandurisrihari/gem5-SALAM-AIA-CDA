# speedkills

One-stop modular driver for gem5-SALAM experiments. Replaces the legacy
`tools/run_*.sh` shell drivers, which now live as thin shims here in
`tools/speedkills/run_*.sh` (full originals archived in `legacy/`).

## Layout

```
tools/speedkills/
├── __init__.py        # package marker
├── __main__.py        # CLI dispatcher (argparse + subcommands)
├── config.py          # M5_PATH, gem5 binary, common system flags
├── benchmarks.py      # Bench registry, GROUPS, resolve()
├── profiles.py        # PROFILE_IOT/MMU500/SERVER + MODES catalog
├── runner.py          # Process orchestration (regen, build, gem5)
├── harvest.py         # stats.txt -> summary.tsv / deltas.tsv
├── run_protection_compare.sh   # shim -> `python -m tools.speedkills compare`
├── run_parallel.sh             # shim -> `python -m tools.speedkills sweep`
├── run_system.sh               # shim -> `python -m tools.speedkills run`
├── legacy/                     # pre-package shell drivers (reference only)
└── README.md
```

## Subcommands

Preferred form (works from any cwd):

```
python3 -m tools.speedkills compare  --bench mobilenetv2 --outdir BM_ARM_OUT/cmp
python3 -m tools.speedkills sweep    --bench mobilenetv2 --latencies 0,8367000 --outdir BM_ARM_OUT/sweep
python3 -m tools.speedkills run      --bench mobilenetv2 -d                # gdb
python3 -m tools.speedkills harvest  --outdir BM_ARM_OUT/cmp               # re-parse only
python3 -m tools.speedkills monitor  -- --port 8080                        # dashboard
python3 -m tools.speedkills list                                           # show benches/modes
```

Shim form (same effect, kept for muscle memory and existing scripts):

```
tools/speedkills/run_protection_compare.sh --bench mobilenetv2 --outdir BM_ARM_OUT/cmp
tools/speedkills/run_parallel.sh           --all --latencies 0,8367000 --outdir BM_ARM_OUT/sweep
tools/speedkills/run_system.sh             --bench mobilenetv2 -d
```

### compare (replaces run_protection_compare.sh)

Default modes: `plain aia-kd smmu-bypass smmu-iot smmu-mmu500 smmu-server`.
Add `smmu-tlb-sweep` to expand a per-TLB-size sweep at the IoT profile.

```
python -m speedkills compare --bench mobilenetv2 \
    --outdir BM_ARM_OUT/mobilenetv2_smmu_compare \
    --modes "plain smmu-iot smmu-mmu500 smmu-server" \
    --jobs 4
```

Outputs: per-mode subdir + `summary.tsv` + `deltas.tsv` + `_setup.log`.

### sweep (replaces run_parallel.sh)

Per-bench: regen Configurator, build SW, then launch one gem5 per latency.
Same-path variants serialise automatically (per-path lock in `runner.py`).

```
python -m speedkills sweep --all --latencies 0,8367000 \
    --outdir BM_ARM_OUT --jobs 8
```

### run (replaces run_system.sh)

```
python -m speedkills run --bench gemm                           # plain
python -m speedkills run --bench gemm -f LLVMInterface,SMMUv3   # debug-flags
python -m speedkills run --bench gemm -d                        # gdb (gem5.debug)
python -m speedkills run --bench gemm -v                        # valgrind
```

## Design notes

- **Concurrency**: gem5.opt is process-safe; many can run with distinct
  `--outdir`. Configurator + `make` write into the bench tree, so they
  serialise per-path via `runner._lock_for(path)`.
- **Mode catalog**: `profiles.MODES` is the single source of truth. Add a
  new mode there and it appears in `compare --modes`.
- **Harvest**: multi-cluster aware — sums `system.*.smmu.<stat>` across
  every cluster. Mirrors the awk in the legacy shell driver, so the TSV
  schema is unchanged.
- **AccCluster.py rebuild trap**: editing `src/hwacc/AccCluster.py`
  forces a `scons build/ARM/gem5.opt` rebuild before any subcommand here
  picks up the change. See
  `.github/prompts/context-kernel-validation.md` for the working
  agreements.
