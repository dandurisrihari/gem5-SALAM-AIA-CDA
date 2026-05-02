# 04 — Working Agreements (Read Every Session)

> **Non-negotiable habits** for anyone (human or LLM) editing this
> feature. Skipping them has cost real time in the past.

## 1. Keep the prompts in sync with the code

Whenever you change any of the following, update the relevant prompt
file in the same commit / turn:

- CLI flags added/removed in `configs/SALAM/fs_*.py` or
  `tools/SALAM-Configurator/fs_template.py`
  → update [05-cli-and-recipes.md](05-cli-and-recipes.md).
- New stats, new struct fields, new event paths in
  `src/hwacc/llvm_interface.{hh,cc}` or `LLVMInterface.py`
  → update [01-aia-kd-design.md](01-aia-kd-design.md) /
  [02-iommu-design.md](02-iommu-design.md).
- New SimObject or generator change
  → update [03-simobject-layout.md](03-simobject-layout.md).
- New gotchas you hit during debugging
  → append to [06-gotchas-and-history.md](06-gotchas-and-history.md).
- High-level architectural change (new mode, new shared resource)
  → update [00-architecture-overview.md](00-architecture-overview.md).

If you didn't update the prompt, the change isn't done.

## 2. Use `python3 -m tools.speedkills` as the single run driver

Do not hand-roll one-off `build/ARM/gem5.opt ...` invocations for
protection-comparison experiments. The package
([tools/speedkills/](../../tools/speedkills/)):

- holds the canonical per-mode flag sets in
  [`tools/speedkills/profiles.py`](../../tools/speedkills/profiles.py)
  (`MODES` — `plain`, `aia-kd`, `iommu`),
- holds the benchmark registry in
  [`tools/speedkills/benchmarks.py`](../../tools/speedkills/benchmarks.py),
- records `run.cmd` per run for reproducibility,
- parallelises and harvests `summary.tsv` + `deltas.tsv`.

Subcommands:

- `compare`     — protection-mode comparison
- `compare-all` — every registered bench × every mode
- `sweep`       — latency sweep across benchmarks
- `run`         — single-bench debug shortcut
- `harvest`     — re-parse an existing outdir
- `list`        — show benches and modes

When a new mode / profile / flag is needed, **extend
`profiles.py::MODES`** rather than bypassing the package, and update
the recipes in [05-cli-and-recipes.md](05-cli-and-recipes.md).

## 3. Reason before you implement

Before edits, state in the chat:

- what file(s) and symbol(s) you intend to change,
- why (which observed behaviour or requirement drives the change),
- what could break (other call sites, stats accounting, RAW hazard
  handling, AccCluster.py needing a rebuild, generator-vs-AccConfig
  ordering, etc.),
- how you will verify (which run, which stat, expected delta).

## 4. Rebuild reminder (the AccCluster trap)

`configs/SALAM/AccCluster.py` is **embedded into `gem5.opt`**
(`[EMBED PY]`). Edits to it (or to the
SALAM-Configurator generator that produces it) have *no effect*
until `scons build/ARM/gem5.opt -jN` finishes. Check
`ls -la build/ARM/gem5.opt` after the build before launching runs.

**Always pipe `yes ""` into the build command** so scons doesn't
stall on the one-time gem5 git-style hook installer prompt
(`Press enter to continue, or ctrl-c to abort:`):

```bash
yes "" | scons build/ARM/gem5.opt -j$(nproc) 2>&1 | tail -20
```

Without the `yes ""` the build hangs silently waiting for stdin the
first time it's run in a fresh worktree / container.

## 5. Run the sanity suite after every rebuild

Any change that requires `scons build/ARM/gem5.opt` to re-link the
binary (anything under `src/hwacc/`, `src/dev/arm/`, embedded
SimObject `.py` files, or the SALAM-Configurator templates that
change generated `configs/SALAM/*.py`) **must** be followed by:

```bash
python3 tests/aia_cda_tests/run_sanity.py --regen
```

The suite is short (2 fast benches × 3 modes plus an aia-kd@lat=0
noise-floor check, well under a minute on the dev container) and
asserts the protection-mode invariants that have regressed before:

- every (bench, mode) row appears in `summary.tsv`,
- `aia_kd_overhead_us > 0` at default latency (checkpoints fire),
- `iommu_checks > 0` (IOMMU hook fires),
- aia-kd@lat=0 sim_ticks within 0.5 % of plain (event-queue
  reordering noise floor; proves the fast-path bypasses real work),
- iommu@lat=0 sim_ticks bit-identical to plain.

If the suite fails, **do not commit** — diagnose first. Pass
`--bench n1,n2` to narrow scope while iterating, but the full
default set must pass before the change is considered done. Pure
doc / prompt / README edits that don't touch built code are exempt.

## 6. Don't pin parameters to a specific vendor part

…unless the user explicitly asks. Cite the configurable range (Arm
MMU-400/500 TRM) and pick the band; that keeps the result
defensible without inviting "but vendor X actually ships Y"
reviewer pushback.
