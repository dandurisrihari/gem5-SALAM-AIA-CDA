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

    if args.regen:
        regen_bench(bench, log=outroot / "_setup.log")
    if args.build_sw:
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

    run_parallel(runs, jobs=args.jobs)

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

    # Assemble runs across (bench x mode). regen / build_sw are
    # attached to the *first* mode of each variant so the runner can
    # interleave them correctly with launches: variants that share a
    # bench-path (mobilenetv2 / _35 / _75 share sw/main.elf) are
    # serialised end-to-end so each variant's elf is consumed before
    # the next variant rebuilds it.
    runs: List[Run] = []
    per_bench_runs: dict[str, List[Run]] = {}
    for b in benches:
        per_bench_runs[b.name] = []
        for i, mode in enumerate(requested):
            flags = profiles.MODES[mode](mode_opts)
            r = Run(label=f"{b.name}/{mode}", bench=b,
                    extra_flags=flags + extra,
                    outdir=outroot / b.name / mode,
                    regen_before=args.regen and i == 0,
                    build_before=args.build_sw and i == 0,
                    setup_log=outroot / f"{b.name}_setup.log")
            runs.append(r)
            per_bench_runs[b.name].append(r)

    run_parallel(runs, jobs=args.jobs)

    # Phase 3: harvest per-bench and aggregate.
    all_rows = []
    for b in benches:
        rows = [harvest_run(r.label.split("/", 1)[1], r.outdir)
                for r in per_bench_runs[b.name]]
        bench_dir = outroot / b.name
        write_summary(rows, bench_dir / "summary.tsv")
        write_deltas(rows, bench_dir / "deltas.tsv")
        for r, row in zip(per_bench_runs[b.name], rows):
            row.label = f"{b.name}/{row.label}"
            all_rows.append(row)

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
    return 0


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
                   help="Run SALAM Configurator before launching")
    p.add_argument("--build-sw", action="store_true",
                   help="Run `make` in each bench dir before launching")
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
                   help="Run SALAM Configurator before launching")
    p.add_argument("--build-sw", action="store_true",
                   help="Run `make` in the bench dir before launching")
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

    # Phase 1: regen + build_sw per bench (per-path-locked inside).
    benches = [resolve(n, args.bench_path if n == args.bench else None,
                       args.config if n == args.bench else None)
               for n in bench_names]
    for b in benches:
        regen_bench(b, log=outroot / f"{b.name}_setup.log")
        build_sw(b, log=outroot / f"{b.name}_setup.log")

    # Phase 2: assemble runs.
    extra = _split_extra(args.extra)
    debug_flags = args.trace_flags if args.trace else ""
    runs: List[Run] = []
    for b in benches:
        for lat in latencies:
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
    p.set_defaults(func=cmd_sweep)


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
