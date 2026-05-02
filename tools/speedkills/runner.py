"""Process orchestration: regen, SW build, parallel gem5 launches.

Concurrency model:
  - gem5.opt is read-only and process-safe; many can run at once
    against distinct ``--outdir`` paths.
  - SALAM Configurator + ``make`` *write* into the benchmark tree, so
    they MUST be serialised per-bench-path. Variants that share a path
    (e.g. mobilenetv2 / _35 / _75) therefore serialise too.
"""
from __future__ import annotations

import shlex
import subprocess
import sys
import threading
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable, List, Sequence

from . import config as cfg
from .benchmarks import Bench

# Per-path lock for regen+build operations that touch shared files.
_path_locks: dict[str, threading.Lock] = {}
_path_locks_guard = threading.Lock()


def _lock_for(path: Path) -> threading.Lock:
    key = str(path.resolve())
    with _path_locks_guard:
        if key not in _path_locks:
            _path_locks[key] = threading.Lock()
        return _path_locks[key]


@dataclass
class Run:
    """One gem5 invocation."""
    label: str
    bench: Bench
    extra_flags: Sequence[str]
    outdir: Path
    debug_flags: str = ""           # gem5 --debug-flags=...
    regen_before: bool = False      # run SALAM Configurator before launch
    build_before: bool = False      # `make` SW before launch
    setup_log: Path | None = None   # where to spool regen/build output
    rc: int | None = field(default=None, init=False)

    def cmd(self, binary: Path) -> List[str]:
        # --listener-mode=off prevents gem5 from binding TCP ports
        # (vncserver, terminal, gdb, m5term). Without this, parallel
        # launches race on the same default ports and one of them
        # aborts with "ListenSocket(listen): bind() failed!".
        cmd = [str(binary), "--listener-mode=off",
               f"--outdir={self.outdir}"]
        if self.debug_flags:
            cmd += [f"--debug-flags={self.debug_flags}",
                    "--debug-file=trace.log"]
        cmd += [
            str(self.bench.fs_script),
            *cfg.SYS_OPTS_BASE,
            f"--kernel={self.bench.kernel_elf}",
            f"--disk-image={cfg.COMMON_DISK}",
            f"--accpath={self.bench.path}",
            f"--accbench={self.bench.name}",
            *self.extra_flags,
        ]
        return cmd


def regen_bench(bench: Bench, log: Path | None = None) -> None:
    """Run the SALAM Configurator for ``bench``. Serialised per path."""
    with _lock_for(bench.path):
        cmd = [
            sys.executable, str(cfg.SYSTEMBUILDER),
            "--sys-name", bench.name,
            "--bench-path", str(bench.path.relative_to(cfg.M5_PATH)),
            "--config-name", bench.config_yml,
        ]
        _run_logged(cmd, log, cwd=cfg.M5_PATH, tag=f"regen[{bench.name}]")


def build_sw(bench: Bench, log: Path | None = None,
             clean: bool = True) -> None:
    """Compile the accelerator firmware. Serialised per path."""
    with _lock_for(bench.path):
        if clean:
            _run_logged(["make", "-C", str(bench.path), "clean"],
                        log, tag=f"clean[{bench.name}]")
        _run_logged(["make", "-C", str(bench.path)],
                    log, tag=f"build[{bench.name}]")


def _run_logged(cmd: List[str], log: Path | None,
                cwd: Path | None = None, tag: str = "") -> None:
    """Run a command, capturing output to ``log`` (appended) and stderr."""
    pretty = " ".join(shlex.quote(c) for c in cmd)
    print(f"[{tag}] {pretty}", file=sys.stderr)
    if log is None:
        rc = subprocess.run(cmd, cwd=cwd).returncode
    else:
        log.parent.mkdir(parents=True, exist_ok=True)
        with log.open("a") as f:
            f.write(f"\n# {pretty}\n")
            f.flush()
            rc = subprocess.run(cmd, cwd=cwd, stdout=f,
                                stderr=subprocess.STDOUT).returncode
    if rc != 0:
        raise RuntimeError(f"{tag} failed (rc={rc}): {pretty}")


def launch_run(run: Run, binary: Path,
               counter: List[int] | None = None,
               total: int | None = None,
               counter_lock: threading.Lock | None = None) -> Run:
    """Execute one gem5 run, redirecting output to ``run.log``.

    Also writes ``run.cmd`` for reproducibility. If ``counter`` is
    provided it is used to print a live ``[k/N]`` progress prefix.
    """
    run.outdir.mkdir(parents=True, exist_ok=True)
    cmd = run.cmd(binary)
    cmd_file = run.outdir / "run.cmd"
    cmd_file.write_text(
        f"label: {run.label}\ncmd:   "
        + " ".join(shlex.quote(c) for c in cmd) + "\n"
    )
    started = time.monotonic()
    if counter is not None and total is not None and counter_lock is not None:
        with counter_lock:
            counter[1] += 1   # in-flight start count
            in_flight = counter[1] - counter[0]
            pending = total - counter[1]
            print(f"[start {counter[1]:>3}/{total}] {run.label}  "
                  f"(running={in_flight}, pending={pending})",
                  file=sys.stderr, flush=True)
    log_file = run.outdir / "run.log"
    with log_file.open("w") as f:
        proc = subprocess.run(cmd, stdout=f, stderr=subprocess.STDOUT,
                              cwd=cfg.M5_PATH)
    run.rc = proc.returncode
    elapsed = time.monotonic() - started
    status = "OK" if proc.returncode == 0 else f"FAIL(rc={proc.returncode})"
    if counter is not None and total is not None and counter_lock is not None:
        with counter_lock:
            counter[0] += 1
            done = counter[0]
            in_flight = counter[1] - done
            pending = total - counter[1]
            print(f"[done  {done:>3}/{total}] {run.label}  "
                  f"{status}  ({elapsed:.1f}s)  "
                  f"(running={in_flight}, pending={pending})",
                  file=sys.stderr, flush=True)
    else:
        print(f"[done] {run.label}  {status}  ({elapsed:.1f}s)",
              file=sys.stderr, flush=True)
    return run


def run_parallel(runs: Iterable[Run], jobs: int,
                 binary: Path | None = None,
                 on_variant_done: Callable[[str, List[Run]], None]
                 | None = None) -> List[Run]:
    """Launch ``runs`` with at most ``jobs`` concurrent gem5 processes.

    Runs whose benches share a ``bench.path`` (i.e. variants of the
    same source tree such as ``mobilenetv2`` / ``mobilenetv2_35`` /
    ``mobilenetv2_75`` that all read ``benchmarks/mobilenetv2/sw/main.elf``)
    are serialised against each other so a later variant cannot
    rebuild the shared firmware while an earlier variant is still
    consuming it. Variants on distinct paths run fully in parallel.
    All modes (plain / aia-kd / iommu) of the *same* variant share the
    same elf and are safe to fan out concurrently.

    ``on_variant_done(variant_name, runs)`` is invoked as soon as every
    mode of a single variant has finished, allowing callers to harvest
    that bench's results without waiting for the rest of the matrix.
    """
    binary = binary or cfg.require_binary()
    runs = list(runs)
    if not runs:
        return []

    # Group by bench-path; runs in the same group serialise one variant
    # at a time, but modes within a single (variant, group) batch run
    # in parallel.
    by_path: dict[str, List[Run]] = defaultdict(list)
    for r in runs:
        by_path[str(r.bench.path.resolve())].append(r)

    total = len(runs)
    counter = [0, 0]   # [done, started]
    counter_lock = threading.Lock()
    print(f"[launch] {total} runs across {len(by_path)} bench path(s), "
          f"{jobs} parallel slot(s)", file=sys.stderr, flush=True)

    def serialise_group(group: List[Run]) -> List[Run]:
        # Bucket by variant (bench.name); execute variants sequentially,
        # but fan out the modes of one variant across the global pool.
        variants: dict[str, List[Run]] = defaultdict(list)
        order: List[str] = []
        for r in group:
            if r.bench.name not in variants:
                order.append(r.bench.name)
            variants[r.bench.name].append(r)
        out: List[Run] = []
        for name in order:
            modes = variants[name]
            head = modes[0]
            # Regen / build for THIS variant immediately before its
            # mode batch — required for shared-tree variants
            # (mobilenetv2*) so the elf consumed by gem5 is the one
            # this variant just produced.
            if head.regen_before:
                regen_bench(head.bench, log=head.setup_log)
            if head.build_before:
                build_sw(head.bench, log=head.setup_log)
            with ThreadPoolExecutor(max_workers=max(1, len(modes))) as p:
                futures = [p.submit(launch_run, r, binary,
                                    counter, total, counter_lock)
                           for r in modes]
                done = [f.result() for f in as_completed(futures)]
            out.extend(done)
            if on_variant_done is not None:
                try:
                    on_variant_done(name, done)
                except Exception as e:  # noqa: BLE001
                    print(f"[on_variant_done {name}] {e!r}",
                          file=sys.stderr, flush=True)
        return out

    # Across path groups: parallel.
    if len(by_path) == 1:
        return serialise_group(next(iter(by_path.values())))

    results: List[Run] = []
    with ThreadPoolExecutor(max_workers=max(1, jobs)) as pool:
        futures = [pool.submit(serialise_group, g)
                   for g in by_path.values()]
        for f in as_completed(futures):
            results.extend(f.result())
    return results
