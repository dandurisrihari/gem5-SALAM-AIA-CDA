"""speedkills CLI: ``python -m speedkills [<subcommand>]``.

Default (no subcommand) is equivalent to ``compare-all`` over every
registered benchmark across the three protection modes (plain, aia-kd,
iommu) in parallel, with output under ``BM_ARM_OUT/aia_cda_default``.

Subcommands:
    compare       Protection-mode comparison for one benchmark
    compare-all   Same, fanned out over every registered benchmark
    sweep         Latency sweep across benchmarks
    run           Single-bench debug run
    harvest       Re-parse an existing outdir without re-running gem5
    list          Show benchmarks and modes
"""
from __future__ import annotations

import argparse
import os
import shlex
import subprocess
import sys
import threading
from pathlib import Path
from typing import List

from . import config as cfg
from . import profiles
from .benchmarks import REGISTRY, GROUPS, group_for, resolve
from .harvest import (HEADER, harvest_outdir, harvest_run, pretty_print,
                      write_deltas, write_overhead_summary, write_summary)
from .runner import Run, build_sw, regen_bench, run_parallel


# ---- helpers --------------------------------------------------------------

def _split_extra(s: str | None) -> List[str]:
    return shlex.split(s) if s else []


def _ints(s: str) -> List[int]:
    return [int(x) for x in s.replace(",", " ").split() if x]


# ---- compare --------------------------------------------------------------

def cmd_compare(args: argparse.Namespace) -> int:
    bench = resolve(args.bench, args.bench_path, args.config)
    outroot = Path(args.outdir).resolve()
    outroot.mkdir(parents=True, exist_ok=True)

    # --regen rewrites benchmarks/<bench>/*_hw_defines.h. The host
    # firmware (sw/main.elf) embeds those addresses, so regen WITHOUT
    # rebuild leaves a stale elf and gem5 fatals on "Unable to find
    # destination for ..." at the first MMR poke. Force rebuild.
    if args.regen:
        regen_bench(bench, log=outroot / "_setup.log")
        build_sw(bench, log=outroot / "_setup.log")
    elif args.build_sw:
        build_sw(bench, log=outroot / "_setup.log")

    extra = _split_extra(args.extra)
    mode_opts = {
        "aia_kd_latency": args.aia_kd_latency,
        "iotlb_entries": args.iotlb_entries,
        "iotlb_hit_latency": args.iotlb_hit_latency,
        "iotlb_miss_latency": args.iotlb_miss_latency,
    }

    runs: List[Run] = []
    requested = args.modes.split()
    for mode in requested:
        if mode not in profiles.MODES:
            print(f"Unknown mode: {mode}", file=sys.stderr)
            return 2
        flags = profiles.MODES[mode](mode_opts)
        runs.append(Run(label=mode, bench=bench,
                        extra_flags=flags + extra,
                        outdir=outroot / mode))

    if not runs:
        print("No runs queued", file=sys.stderr)
        return 1

    run_parallel(runs, jobs=args.jobs, serial_modes=args.serial_modes)

    rows = [harvest_run(r.label, r.outdir) for r in runs]
    write_summary(rows, outroot / "summary.tsv")
    write_deltas(rows, outroot / "deltas.tsv")

    print()
    print(f"===== Summary ({bench.name}) =====")
    print(pretty_print(rows))
    print(f"\nOutput tree: {outroot}")
    print(f"Summary:     {outroot / 'summary.tsv'}")
    print(f"Deltas:      {outroot / 'deltas.tsv'}")
    return 0


# ---- compare-all ----------------------------------------------------------

def cmd_compare_all(args: argparse.Namespace) -> int:
    """Run every registered benchmark across the 3 protection modes.

    Each benchmark gets its own subdir under --outdir, with one
    sub-subdir per mode plus that bench's summary.tsv / deltas.tsv.
    A top-level summary.tsv aggregates all (bench, mode) rows.
    """
    if args.bench:
        bench_names = args.bench.split(",")
    else:
        excl = set(args.exclude.split(",")) if args.exclude else set()
        bench_names = [n for n in sorted(REGISTRY) if n not in excl]

    benches = [resolve(n) for n in bench_names]
    outroot = Path(args.outdir).resolve()
    outroot.mkdir(parents=True, exist_ok=True)
    extra = _split_extra(args.extra)

    mode_opts = {
        "aia_kd_latency": args.aia_kd_latency,
        "iotlb_entries": args.iotlb_entries,
        "iotlb_hit_latency": args.iotlb_hit_latency,
        "iotlb_miss_latency": args.iotlb_miss_latency,
    }
    requested = args.modes.split()
    for mode in requested:
        if mode not in profiles.MODES:
            print(f"Unknown mode: {mode}", file=sys.stderr)
            return 2

    # Optional inline IOMMU sweep: expand into per-config modes
    # (entries x miss_ns) that run in the SAME parallel batch as
    # plain / aia-kd. The single generic `iommu` mode is dropped
    # because the matrix already covers it (avoids duplicate runs).
    sweep_entries: List[int] = []
    sweep_miss_ns: List[int] = []
    sweep_specs: List[tuple] = []
    if args.iommu_sweep:
        sweep_entries = _ints(args.iommu_sweep_entries)
        sweep_miss_ns = _ints(args.iommu_sweep_miss_ns)
        if not sweep_entries or not sweep_miss_ns:
            print("--iommu-sweep needs non-empty --iommu-sweep-entries "
                  "and --iommu-sweep-miss-ns", file=sys.stderr)
            return 2
        for e in sweep_entries:
            for m in sweep_miss_ns:
                sweep_specs.append((e, m, f"iommu_e{e}_m{m}ns"))
        if "iommu" in requested:
            requested = [m for m in requested if m != "iommu"]

    # Assemble runs across (bench x mode). regen / build_sw are
    # attached to the *first* mode of each variant so the runner can
    # interleave them correctly with launches: variants that share a
    # bench-path (mobilenetv2 / _35 / _75 share sw/main.elf) are
    # serialised end-to-end so each variant's elf is consumed before
    # the next variant rebuilds it.
    runs: List[Run] = []
    for b in benches:
        bench_modes: List[tuple] = []
        for mode in requested:
            bench_modes.append(
                (mode, profiles.MODES[mode](mode_opts),
                 outroot / b.name / mode))
        for e, m, label in sweep_specs:
            miss_ticks = m * 1000
            flags = ["--enable-iommu",
                     "--iotlb-entries",      str(e),
                     "--iotlb-hit-latency",  str(args.iotlb_hit_latency),
                     "--iotlb-miss-latency", str(miss_ticks)]
            bench_modes.append((label, flags, outroot / b.name / label))
        for i, (mode, flags, mode_outdir) in enumerate(bench_modes):
            # --regen forces a rebuild (see cmd_compare); shared-tree
            # variants (mobilenetv2 / _35 / _75) MUST rebuild after
            # regen overwrites their *_hw_defines.h.
            r = Run(label=f"{b.name}/{mode}", bench=b,
                    extra_flags=flags + extra,
                    outdir=mode_outdir,
                    regen_before=args.regen and i == 0,
                    build_before=(args.regen or args.build_sw) and i == 0,
                    setup_log=outroot / f"{b.name}_setup.log")
            runs.append(r)

    # Per-variant harvest: as soon as all modes of a single bench
    # finish, write that bench's summary/deltas/overhead files
    # immediately so users get incremental results without waiting
    # for the full matrix.
    all_rows: List = []
    all_rows_lock = threading.Lock()

    def on_variant_done(variant_name: str, finished: List[Run]) -> None:
        rows = [harvest_run(r.label.split("/", 1)[1], r.outdir)
                for r in finished]
        bench_dir = outroot / variant_name
        write_summary(rows, bench_dir / "summary.tsv")
        write_deltas(rows, bench_dir / "deltas.tsv")
        for row in rows:
            row.label = f"{variant_name}/{row.label}"
        with all_rows_lock:
            all_rows.extend(rows)
            snapshot = [r for r in all_rows
                        if r.label.startswith(f"{variant_name}/")]
        write_overhead_summary(
            snapshot, bench_dir / "overhead_summary.tsv")
        # Unified per-(bench, mode) pivot. Includes every config
        # (plain, aia-kd, iommu_e<E>_m<M>ns) as its own row so the
        # sweep matrix is visible per-bench, not only in the
        # aggregate.
        _write_all_modes_overhead(
            snapshot, bench_dir / "all_modes_overhead.tsv")
        print(f"[harvest] {variant_name}: wrote "
              f"{bench_dir / 'all_modes_overhead.csv'}",
              flush=True)

    run_parallel(runs, jobs=args.jobs, on_variant_done=on_variant_done,
                 serial_modes=args.serial_modes)

    # Phase 3: aggregate across all benches once everything is done.
    write_summary(all_rows, outroot / "summary.tsv")
    print()
    print(f"===== Aggregate ({len(benches)} benches "
          f"x {len(requested)} modes) =====")
    print(pretty_print(all_rows))
    overhead_text = write_overhead_summary(
        all_rows, outroot / "overhead_summary.tsv")
    print()
    print("===== Per-benchmark overhead vs plain baseline =====")
    print(overhead_text)
    print(f"\nOutput tree:        {outroot}")
    print(f"Aggregate summary:  {outroot / 'summary.tsv'}")
    print(f"Overhead summary:   {outroot / 'overhead_summary.tsv'}")

    # Unified per-mode overhead pivot: one row per (bench, mode),
    # each compared to that bench's `plain` baseline. Includes
    # plain / aia-kd / iommu and every iommu_e<E>_m<M>ns sweep
    # config so the user can read the whole matrix in one CSV.
    if args.iommu_sweep or len(requested) > 1:
        unified_text = _write_all_modes_overhead(
            all_rows, outroot / "all_modes_overhead.tsv")
        print()
        print("===== All-modes overhead vs plain (one row per config) =====")
        print(unified_text)
        print(f"All-modes overhead: {outroot / 'all_modes_overhead.tsv'}")
    return 0


def _write_all_modes_overhead(rows, path: Path) -> str:
    """Per-(bench, mode) overhead pivot vs the bench's `plain` row.

    Handles arbitrary mode labels including ``iommu_e<E>_m<M>ns``
    sweep configs. Columns:

      benchmark, mode, entries, miss_ns, plain_us, runtime_us,
      abs_overhead_us, abs_overhead_pct, proj_overhead_us,
      proj_overhead_pct, iommu_checks, iotlb_hits, iotlb_misses,
      iotlb_hit_rate_pct, smid_requests, smid_validations
    """
    import re as _re
    by_bench: dict = {}
    for r in rows:
        if "/" not in r.label:
            continue
        bench, mode = r.label.split("/", 1)
        by_bench.setdefault(bench, {})[mode] = r

    cols = ("benchmark", "mode", "entries", "miss_ns",
            "plain_us", "runtime_us",
            "abs_overhead_us", "abs_overhead_pct",
            "proj_overhead_us", "proj_overhead_pct",
            "iommu_checks", "iotlb_hits", "iotlb_misses",
            "iotlb_hit_rate_pct",
            "smid_requests", "smid_validations")
    lines = ["\t".join(cols)]
    pretty = ["  ".join(c.ljust(18) for c in cols)]
    sweep_re = _re.compile(r"^iommu_e(\d+)_m(\d+)ns$")

    def _mode_sort_key(m: str) -> tuple:
        if m == "plain":
            return (0, 0, 0, m)
        if m == "aia-kd":
            return (1, 0, 0, m)
        if m == "iommu":
            return (2, 0, 0, m)
        mt = sweep_re.match(m)
        if mt:
            return (3, int(mt.group(1)), int(mt.group(2)), m)
        return (4, 0, 0, m)

    for bench in sorted(by_bench):
        modes = by_bench[bench]
        plain = modes.get("plain")
        plain_ticks = (int(plain.sim_ticks)
                       if plain and plain.sim_ticks.isdigit() else None)
        plain_us = (plain_ticks / 1e6) if plain_ticks else None
        plain_us_s = f"{plain_us:.3f}" if plain_us is not None else "-"

        for mode in sorted(modes, key=_mode_sort_key):
            row = modes[mode]
            mt = sweep_re.match(mode)
            entries = mt.group(1) if mt else "-"
            miss_ns = mt.group(2) if mt else "-"
            runtime_us = row.runtime_us
            if (plain_ticks is not None and row.sim_ticks.isdigit()):
                d_us = (int(row.sim_ticks) - plain_ticks) / 1e6
                pct = ((d_us / plain_us) * 100.0
                       if plain_us else 0.0)
                abs_o = f"{d_us:.3f}"
                abs_p = f"{pct:.3f}%"
            else:
                abs_o = "-"
                abs_p = "-"
            # Analytical projection: aia-kd reports its own us value;
            # iommu modes report iommu_overhead_us; plain has neither.
            # Pick by mode label so a 0.000 sibling column doesn't win.
            if mode == "aia-kd":
                proj_us = row.aia_kd_overhead_us
            elif mode == "iommu" or sweep_re.match(mode):
                proj_us = row.iommu_overhead_us
            else:
                proj_us = "-"
            try:
                pv = float(proj_us)
                proj_pct = (f"{(pv / plain_us) * 100.0:.3f}%"
                            if plain_us else "-")
            except (TypeError, ValueError):
                proj_pct = "-"

            vals = (bench, mode, entries, miss_ns,
                    plain_us_s, runtime_us,
                    abs_o, abs_p,
                    proj_us, proj_pct,
                    row.iommu_checks, row.iotlb_hits, row.iotlb_misses,
                    (f"{row.iotlb_hit_rate_pct}%"
                     if row.iotlb_hit_rate_pct != "-" else "-"),
                    row.smid_requests, row.smid_validations)
            lines.append("\t".join(vals))
            pretty.append("  ".join(v.ljust(18) for v in vals))

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n")
    csv_path = path.with_suffix(".csv")
    csv_path.write_text(
        "\n".join(l.replace("\t", ",") for l in lines) + "\n")
    return "\n".join(pretty)


def add_compare_all(sp: argparse._SubParsersAction) -> None:
    p = sp.add_parser("compare-all",
                      help="Run every benchmark across the 3 protection modes")
    p.add_argument("--outdir", required=True)
    p.add_argument("--bench", default=None,
                   help="Comma-separated subset of benchmarks "
                        "(default: every registered bench)")
    p.add_argument("--exclude", default="",
                   help="Comma-separated benchmark names to skip")
    p.add_argument("--modes", default=" ".join(profiles.DEFAULT_MODES),
                   help="Modes to run for each bench (default: all 3)")
    p.add_argument("--aia-kd-latency", type=int, default=8_367_000)
    p.add_argument("--iotlb-entries", type=int, default=8)
    p.add_argument("--iotlb-hit-latency", type=int, default=2_000)
    p.add_argument("--iotlb-miss-latency", type=int, default=500_000)
    p.add_argument("--jobs", type=int, default=os.cpu_count() or 4)
    p.add_argument("--extra", default="")
    p.add_argument("--regen", action="store_true",
                   help="Run SALAM Configurator before launching "
                        "(implies --build-sw)")
    p.add_argument("--build-sw", action="store_true",
                   help="Run `make` in each bench dir before launching")
    p.add_argument("--serial-modes", action="store_true",
                   help="Run modes within a variant one at a time "
                        "(slower, useful when host RAM is tight). "
                        "Variants of the same bench-path family "
                        "(mobilenetv2 / _35 / _75) are ALWAYS serialised "
                        "against each other regardless of this flag.")
    p.add_argument("--iommu-sweep", action="store_true",
                   help="Expand the matrix of IOMMU configs "
                        "(entries x miss_ns) into the SAME parallel "
                        "batch as plain + aia-kd. Each config gets "
                        "label iommu_e<E>_m<M>ns and appears as its "
                        "own row in <outdir>/all_modes_overhead.csv "
                        "(per-bench and aggregate), comparing each "
                        "config's overhead vs that bench's plain "
                        "baseline. Drops the generic `iommu` mode to "
                        "avoid a duplicate run.")
    p.add_argument("--iommu-sweep-entries", default=" ".join(
        str(x) for x in _DEFAULT_IOTLB_ENTRIES),
        help="IOTLB capacities for the chained sweep (default: "
             f"{' '.join(str(x) for x in _DEFAULT_IOTLB_ENTRIES)})")
    p.add_argument("--iommu-sweep-miss-ns", default=" ".join(
        str(x) for x in _DEFAULT_IOMMU_MISS_NS),
        help="Page-walk miss latencies (ns) for the chained sweep "
             f"(default: {' '.join(str(x) for x in _DEFAULT_IOMMU_MISS_NS)})")
    p.set_defaults(func=cmd_compare_all)


def add_compare(sp: argparse._SubParsersAction) -> None:
    p = sp.add_parser("compare", help="Protection-mode comparison")
    p.add_argument("--bench", default="mobilenetv2")
    p.add_argument("--bench-path", default=None)
    p.add_argument("--config", dest="config", default=None,
                   help="Configurator YAML name")
    p.add_argument("--outdir", required=True)
    p.add_argument("--modes", default=" ".join(profiles.DEFAULT_MODES),
                   help="Space-separated mode list. Modes: "
                        + ", ".join(profiles.MODES))
    p.add_argument("--aia-kd-latency", type=int, default=8_367_000,
                   help="kernel_validation_latency in ticks (1 tick = 1 ps; "
                        "default 8,367,000 = 8.367 us per validated page)")
    # Analytical IOMMU knobs (used by mode `iommu`). 1 tick = 1 ps.
    # Defaults match a constrained edge-IoT profile (see profiles.py):
    # 8-entry LRU IOTLB, 2 ns hit, 500 ns page-walk miss.
    p.add_argument("--iotlb-entries", type=int, default=8,
                   help="IOTLB capacity for `iommu` mode (LRU; default 8)")
    p.add_argument("--iotlb-hit-latency", type=int, default=2_000,
                   help="IOTLB hit latency in ticks (default 2000 = 2 ns)")
    p.add_argument("--iotlb-miss-latency", type=int, default=500_000,
                   help="IOTLB miss / page-walk latency in ticks "
                        "(default 500000 = 500 ns)")
    p.add_argument("--jobs", type=int, default=4)
    p.add_argument("--extra", default="",
                   help="Extra gem5 flags appended to every run (quoted)")
    p.add_argument("--regen", action="store_true",
                   help="Run SALAM Configurator before launching "
                        "(implies --build-sw)")
    p.add_argument("--build-sw", action="store_true",
                   help="Run `make` in the bench dir before launching")
    p.add_argument("--serial-modes", action="store_true",
                   help="Run modes one at a time instead of in parallel "
                        "(slower wall-clock; sim results are identical)")
    p.set_defaults(func=cmd_compare)


# ---- sweep ----------------------------------------------------------------

def cmd_sweep(args: argparse.Namespace) -> int:
    if args.all:
        bench_names = sorted(REGISTRY)
    elif args.bench:
        bench_names = [args.bench]
    else:
        print("Specify --all or --bench NAME", file=sys.stderr)
        return 2
    if args.exclude:
        excl = set(args.exclude.split(","))
        bench_names = [b for b in bench_names if b not in excl]

    outroot = Path(args.outdir).resolve()
    outroot.mkdir(parents=True, exist_ok=True)

    latencies = _ints(args.latencies)

    benches = [resolve(n, args.bench_path if n == args.bench else None,
                       args.config if n == args.bench else None)
               for n in bench_names]

    # Assemble runs. regen / build_sw are attached to the FIRST run of
    # each variant so run_parallel can interleave them per-variant.
    # Doing all regens then all builds up-front (the previous Phase 1
    # model) silently corrupted shared-tree variants like
    # mobilenetv2 / _35 / _75 because they share *_hw_defines.h and
    # sw/main.elf -- only the last variant's elf survived. --regen
    # implies a rebuild for the same reason as in cmd_compare.
    extra = _split_extra(args.extra)
    debug_flags = args.trace_flags if args.trace else ""
    runs: List[Run] = []
    for b in benches:
        for i, lat in enumerate(latencies):
            if lat == 0:
                label = "baseline_no_validation"
                mode_flags: List[str] = []
            else:
                label = f"latency_{lat}"
                mode_flags = ["--enable-kernel-validation",
                              "--kernel-validation-latency", str(lat)]
            runs.append(Run(
                label=f"{b.name}/{label}",
                bench=b,
                extra_flags=mode_flags + extra,
                outdir=outroot / b.name / label,
                debug_flags=debug_flags,
                regen_before=args.regen and i == 0,
                build_before=(args.regen or args.build_sw) and i == 0,
                setup_log=outroot / f"{b.name}_setup.log",
            ))

    run_parallel(runs, jobs=args.jobs)

    completed = sum(1 for r in runs if (r.outdir / "stats.txt").exists())
    print(f"\nCompleted: {completed} / {len(runs)}")
    print(f"Results:   {outroot}")
    return 0 if completed == len(runs) else 1


def add_sweep(sp: argparse._SubParsersAction) -> None:
    p = sp.add_parser("sweep",
                      help="Latency sweep across benchmarks")
    src = p.add_mutually_exclusive_group()
    src.add_argument("--all", action="store_true")
    src.add_argument("--bench", default=None)
    p.add_argument("--bench-path", default=None)
    p.add_argument("--config", default=None)
    p.add_argument("--latencies", default="0,8367000",
                   help="Comma-separated kernel_validation_latency values")
    p.add_argument("--exclude", default="", help="Comma-separated names")
    p.add_argument("--outdir", required=True)
    p.add_argument("--jobs", type=int, default=os.cpu_count() or 4)
    p.add_argument("--extra", default="")
    p.add_argument("--trace", action="store_true")
    p.add_argument("--trace-flags", default="LLVMInterface")
    p.add_argument("--regen", action="store_true",
                   help="Run SALAM Configurator before launching "
                        "(implies --build-sw)")
    p.add_argument("--build-sw", action="store_true",
                   help="Run `make` in each bench dir before launching")
    p.set_defaults(func=cmd_sweep)


# ---- iommu-sweep ----------------------------------------------------------

# Default IOMMU parameter sweep:
#   entries: 8 -> 2048 covers IoT (8) through DRAM-resident IOTLB (2048)
#   miss_ns: 100 / 250 / 500 / 1000 ns covers fast on-die walker through
#            slow DRAM-walking SMMUv3-class translation
_DEFAULT_IOTLB_ENTRIES = (8, 16, 32, 64, 256, 2048)
_DEFAULT_IOMMU_MISS_NS = (100, 250, 500, 1000)


def _iommu_sweep_runs(bench, requested_entries, requested_miss_ns,
                      hit_latency_ticks, include_baselines, aia_kd_latency,
                      extra_flags, outroot):
    """Build the run matrix for one bench.

    Labels are ``iommu_e<N>_m<M>ns``, ``plain`` and ``aia-kd``. Each
    config gets its own outdir under ``outroot/<bench>/<label>``.
    """
    runs: List[Run] = []
    if include_baselines:
        runs.append(Run(
            label=f"{bench.name}/plain", bench=bench,
            extra_flags=list(extra_flags),
            outdir=outroot / bench.name / "plain",
        ))
        runs.append(Run(
            label=f"{bench.name}/aia-kd", bench=bench,
            extra_flags=[
                "--enable-kernel-validation",
                "--kernel-validation-latency", str(aia_kd_latency),
                *extra_flags,
            ],
            outdir=outroot / bench.name / "aia-kd",
        ))
    for e in requested_entries:
        for miss_ns in requested_miss_ns:
            miss_ticks = int(miss_ns) * 1000  # 1 ns = 1000 ticks
            label_short = f"iommu_e{e}_m{miss_ns}ns"
            runs.append(Run(
                label=f"{bench.name}/{label_short}", bench=bench,
                extra_flags=[
                    "--enable-iommu",
                    "--iotlb-entries",      str(e),
                    "--iotlb-hit-latency",  str(hit_latency_ticks),
                    "--iotlb-miss-latency", str(miss_ticks),
                    *extra_flags,
                ],
                outdir=outroot / bench.name / label_short,
            ))
    return runs


def _write_iommu_sweep_csv(rows_by_bench, requested_entries,
                           requested_miss_ns, path: Path) -> str:
    """Wide CSV: one row per (bench, entries, miss_ns).

    Columns: bench, entries, miss_ns, plain_us, runtime_us, abs_overhead_us,
    abs_overhead_pct, iommu_proj_us, iommu_proj_pct, iommu_checks,
    iotlb_hits, iotlb_misses, iotlb_hit_rate_pct.
    """
    cols = ("benchmark", "entries", "miss_ns",
            "plain_us", "runtime_us",
            "abs_overhead_us", "abs_overhead_pct",
            "iommu_proj_us", "iommu_proj_pct",
            "iommu_checks", "iotlb_hits", "iotlb_misses",
            "iotlb_hit_rate_pct")
    lines = ["\t".join(cols)]
    pretty = ["  ".join(c.ljust(18) for c in cols)]
    for bench in sorted(rows_by_bench):
        modes = rows_by_bench[bench]
        plain = modes.get("plain")
        plain_ticks = (int(plain.sim_ticks)
                       if plain and plain.sim_ticks.isdigit() else None)
        plain_us = (plain_ticks / 1e6) if plain_ticks else None
        plain_us_s = f"{plain_us:.3f}" if plain_us is not None else "-"
        for e in requested_entries:
            for miss_ns in requested_miss_ns:
                lbl = f"iommu_e{e}_m{miss_ns}ns"
                r = modes.get(lbl)
                if r is None or not r.sim_ticks.isdigit():
                    runtime_us = (r.runtime_us if r else "-")
                    vals = (bench, str(e), str(miss_ns),
                            plain_us_s, runtime_us,
                            "-", "-", "-", "-", "-", "-", "-", "-")
                    lines.append("\t".join(vals))
                    pretty.append("  ".join(v.ljust(18) for v in vals))
                    continue
                v_ticks = int(r.sim_ticks)
                if plain_ticks is not None and plain_us:
                    d_us = (v_ticks - plain_ticks) / 1e6
                    pct = f"{(d_us / plain_us) * 100.0:.3f}%"
                    d_us_s = f"{d_us:.3f}"
                else:
                    d_us_s = "-"
                    pct = "-"
                proj_us = r.iommu_overhead_us
                if proj_us != "-" and plain_us:
                    proj_pct = f"{(float(proj_us) / plain_us) * 100.0:.3f}%"
                else:
                    proj_pct = "-"
                vals = (bench, str(e), str(miss_ns),
                        plain_us_s, r.runtime_us,
                        d_us_s, pct, proj_us, proj_pct,
                        r.iommu_checks, r.iotlb_hits, r.iotlb_misses,
                        (f"{r.iotlb_hit_rate_pct}%"
                         if r.iotlb_hit_rate_pct != "-" else "-"))
                lines.append("\t".join(vals))
                pretty.append("  ".join(v.ljust(18) for v in vals))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n")
    csv_path = path.with_suffix(".csv")
    csv_path.write_text(
        "\n".join(l.replace("\t", ",") for l in lines) + "\n")
    return "\n".join(pretty)


def cmd_iommu_sweep(args: argparse.Namespace) -> int:
    """Sweep the IOMMU (entries x miss_ns) matrix per benchmark.

    Default matrix: 6 IOTLB sizes x 4 page-walk latencies = 24 IOMMU
    runs per benchmark, plus optional plain + aia-kd baselines.
    Modes within a variant fan out across --jobs slots; cross-variant
    runs serialise via the per-bench-path lock as usual.
    """
    if args.bench:
        bench_names = args.bench.split(",")
    else:
        excl = set(args.exclude.split(",")) if args.exclude else set()
        bench_names = [n for n in sorted(REGISTRY) if n not in excl]
    benches = [resolve(n) for n in bench_names]

    entries = _ints(args.entries)
    miss_ns = _ints(args.miss_ns)
    if not entries or not miss_ns:
        print("Need at least one --entries and one --miss-ns", file=sys.stderr)
        return 2

    outroot = Path(args.outdir).resolve()
    outroot.mkdir(parents=True, exist_ok=True)
    extra = _split_extra(args.extra)

    runs: List[Run] = []
    for b in benches:
        bench_runs = _iommu_sweep_runs(
            b, entries, miss_ns,
            hit_latency_ticks=args.iotlb_hit_latency,
            include_baselines=not args.no_baselines,
            aia_kd_latency=args.aia_kd_latency,
            extra_flags=extra,
            outroot=outroot,
        )
        # Attach regen / build_sw to the very first run of each bench so
        # the runner triggers them once before launching that bench's
        # batch (cross-variant lock serialises shared-tree families).
        if bench_runs:
            bench_runs[0].regen_before = args.regen
            bench_runs[0].build_before = args.regen or args.build_sw
            bench_runs[0].setup_log = outroot / f"{b.name}_setup.log"
        runs.extend(bench_runs)

    rows_by_bench: dict = {b.name: {} for b in benches}
    rows_by_bench_lock = threading.Lock()

    def on_variant_done(variant_name: str, finished: List[Run]) -> None:
        modes_dict = {}
        for r in finished:
            short = r.label.split("/", 1)[1]
            modes_dict[short] = harvest_run(short, r.outdir)
        with rows_by_bench_lock:
            rows_by_bench[variant_name].update(modes_dict)
            snapshot = {variant_name: dict(rows_by_bench[variant_name])}
        bench_dir = outroot / variant_name
        _write_iommu_sweep_csv(
            snapshot, entries, miss_ns, bench_dir / "iommu_sweep.tsv")
        print(f"[harvest] {variant_name}: wrote "
              f"{bench_dir / 'iommu_sweep.csv'}", flush=True)

    print(f"[iommu-sweep] {len(benches)} bench(es) x "
          f"({len(entries)} entries x {len(miss_ns)} miss_ns"
          f"{' + 2 baselines' if not args.no_baselines else ''}) "
          f"= {len(runs)} runs total", file=sys.stderr)

    run_parallel(runs, jobs=args.jobs, on_variant_done=on_variant_done,
                 serial_modes=args.serial_modes)

    pretty = _write_iommu_sweep_csv(
        rows_by_bench, entries, miss_ns, outroot / "iommu_sweep.tsv")
    print()
    print("===== IOMMU sweep (per bench x entries x miss_ns) =====")
    print(pretty)
    print(f"\nOutput tree:    {outroot}")
    print(f"Wide CSV:       {outroot / 'iommu_sweep.csv'}")
    return 0


def add_iommu_sweep(sp: argparse._SubParsersAction) -> None:
    p = sp.add_parser("iommu-sweep",
                      help="Sweep IOTLB entries x page-walk miss latency")
    p.add_argument("--outdir", required=True)
    p.add_argument("--bench", default=None,
                   help="Comma-separated subset (default: every "
                        "registered bench)")
    p.add_argument("--exclude", default="",
                   help="Comma-separated names to skip")
    p.add_argument("--entries", default=" ".join(
        str(x) for x in _DEFAULT_IOTLB_ENTRIES),
        help="IOTLB capacities to sweep (default: "
             f"{' '.join(str(x) for x in _DEFAULT_IOTLB_ENTRIES)})")
    p.add_argument("--miss-ns", default=" ".join(
        str(x) for x in _DEFAULT_IOMMU_MISS_NS),
        help="Page-walk miss latencies in ns (default: "
             f"{' '.join(str(x) for x in _DEFAULT_IOMMU_MISS_NS)})")
    p.add_argument("--iotlb-hit-latency", type=int, default=2_000,
                   help="IOTLB hit latency in ticks (1 tick = 1 ps; "
                        "default 2000 = 2 ns)")
    p.add_argument("--no-baselines", action="store_true",
                   help="Skip the plain + aia-kd reference rows")
    p.add_argument("--aia-kd-latency", type=int, default=8_367_000,
                   help="kernel_validation_latency for the aia-kd "
                        "baseline (default 8367000 = 8.367 us)")
    p.add_argument("--jobs", type=int, default=os.cpu_count() or 4)
    p.add_argument("--extra", default="",
                   help="Extra gem5 flags appended to every run")
    p.add_argument("--regen", action="store_true",
                   help="Run SALAM Configurator before launching "
                        "(implies --build-sw)")
    p.add_argument("--build-sw", action="store_true",
                   help="Run `make` in each bench dir before launching")
    p.add_argument("--serial-modes", action="store_true",
                   help="Serialise modes within a variant (default: parallel)")
    p.set_defaults(func=cmd_iommu_sweep)


# ---- run ------------------------------------------------------------------

def cmd_run(args: argparse.Namespace) -> int:
    bench = resolve(args.bench, args.bench_path, args.config)
    outdir = Path(args.outdir).resolve() if args.outdir else (
        cfg.OUT_ROOT / bench.path.relative_to(cfg.M5_PATH).as_posix()
    )
    outdir.mkdir(parents=True, exist_ok=True)

    if args.regen:
        regen_bench(bench)
    if args.build:
        build_sw(bench)

    binary = cfg.require_binary(debug=args.debug)
    cmd: List[str] = []
    if args.debug:
        cmd = ["gdb", "--args", str(binary)]
    elif args.valgrind:
        log = outdir / f"{bench.name}.valgrind.log"
        cmd = [
            "valgrind", "--leak-check=yes",
            f"--log-file={log}", "--error-limit=no",
            "--leak-check=full", "--show-leak-kinds=definite,possible",
            str(cfg.GEM5_DEBUG),
        ]
    else:
        cmd = [str(binary)]

    if args.flags:
        cmd.append(f"--debug-flags={args.flags}")
    cmd += [
        f"--outdir={outdir}",
        str(bench.fs_script),
        *cfg.SYS_OPTS_BASE,
        f"--kernel={bench.kernel_elf}",
        f"--disk-image={cfg.COMMON_DISK}",
        f"--accpath={bench.path}",
        f"--accbench={bench.name}",
        *_split_extra(args.extra),
    ]

    print("[run]", " ".join(shlex.quote(c) for c in cmd), file=sys.stderr)
    if args.print:
        with (outdir / "debug-trace.txt").open("w") as f:
            return subprocess.run(cmd, stdout=f, stderr=subprocess.STDOUT,
                                  cwd=cfg.M5_PATH).returncode
    return subprocess.run(cmd, cwd=cfg.M5_PATH).returncode


def add_run(sp: argparse._SubParsersAction) -> None:
    p = sp.add_parser("run", help="Single-bench run (debug shortcut)")
    p.add_argument("--bench", required=True)
    p.add_argument("--bench-path", default=None)
    p.add_argument("--config", default=None)
    p.add_argument("--outdir", default=None)
    p.add_argument("--regen", action="store_true",
                   help="Run SALAM Configurator first")
    p.add_argument("--build", action="store_true",
                   help="`make` the SW first")
    p.add_argument("-f", "--flags", default="",
                   help="gem5 --debug-flags (comma-separated)")
    p.add_argument("-d", "--debug", action="store_true",
                   help="Run under gdb (uses gem5.debug)")
    p.add_argument("-v", "--valgrind", action="store_true",
                   help="Run under valgrind (uses gem5.debug)")
    p.add_argument("-p", "--print", action="store_true",
                   help="Redirect stdout to outdir/debug-trace.txt")
    p.add_argument("--extra", default="")
    p.set_defaults(func=cmd_run)


# ---- harvest --------------------------------------------------------------

def cmd_harvest(args: argparse.Namespace) -> int:
    outroot = Path(args.outdir).resolve()
    if not outroot.is_dir():
        print(f"No such dir: {outroot}", file=sys.stderr)
        return 2
    rows = harvest_outdir(outroot, labels=args.labels or None)
    write_summary(rows, outroot / "summary.tsv")
    write_deltas(rows, outroot / "deltas.tsv")
    print(pretty_print(rows))
    print(f"\nSummary: {outroot / 'summary.tsv'}")
    return 0


def add_harvest(sp: argparse._SubParsersAction) -> None:
    p = sp.add_parser("harvest",
                      help="Re-parse an existing outdir, no gem5 launch")
    p.add_argument("--outdir", required=True)
    p.add_argument("--labels", nargs="*", default=None,
                   help="Specific subdir names to include (default: all)")
    p.set_defaults(func=cmd_harvest)


# ---- monitor subcommand removed (experiment_monitor.py was deleted) ----


# ---- list -----------------------------------------------------------------

def cmd_list(args: argparse.Namespace) -> int:
    print("Benchmark groups (variants share path; build serially):")
    for g, members in sorted(GROUPS.items()):
        print(f"  {g}: {', '.join(members)}")
    print("\nProtection modes (`compare --modes ...`):")
    for m in profiles.MODES:
        print(f"  {m}")
    return 0


def add_list(sp: argparse._SubParsersAction) -> None:
    p = sp.add_parser("list", help="List benchmarks and modes")
    p.set_defaults(func=cmd_list)


# ---- main -----------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="speedkills",
                                description=__doc__,
                                 formatter_class=argparse
                                 .RawDescriptionHelpFormatter)
    sp = p.add_subparsers(dest="cmd")
    add_compare(sp)
    add_compare_all(sp)
    add_sweep(sp)
    add_iommu_sweep(sp)
    add_run(sp)
    add_harvest(sp)
    add_list(sp)
    return p


DEFAULT_OUTDIR = cfg.M5_PATH / "BM_ARM_OUT" / "aia_cda_default"


def _default_compare_all_argv() -> list[str]:
    """Build the argv for the implicit no-subcommand default action.

    Default = compare-all, every registered benchmark, every default
    mode, jobs = os.cpu_count(), regen on so a fresh checkout / pull
    Just Works. Output goes to BM_ARM_OUT/aia_cda_default.
    """
    DEFAULT_OUTDIR.mkdir(parents=True, exist_ok=True)
    return [
        "compare-all",
        "--outdir", str(DEFAULT_OUTDIR),
        "--jobs", str(os.cpu_count() or 4),
        "--regen",
    ]


def main(argv: list[str] | None = None) -> int:
    raw = list(argv if argv is not None else sys.argv[1:])
    parser = build_parser()
    if not raw:
        print("[speedkills] no subcommand given — running default "
              "compare-all over every benchmark x DEFAULT_MODES.\n"
              f"             output: {DEFAULT_OUTDIR}",
              file=sys.stderr)
        raw = _default_compare_all_argv()
    args = parser.parse_args(raw)
    if not getattr(args, "func", None):
        parser.print_help()
        return 2
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
