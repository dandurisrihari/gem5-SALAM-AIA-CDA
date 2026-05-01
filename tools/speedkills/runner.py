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
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, List, Sequence

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
    rc: int | None = field(default=None, init=False)

    def cmd(self, binary: Path) -> List[str]:
        cmd = [str(binary), f"--outdir={self.outdir}"]
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


def launch_run(run: Run, binary: Path) -> Run:
    """Execute one gem5 run, redirecting output to ``run.log``.

    Also writes ``run.cmd`` for reproducibility.
    """
    run.outdir.mkdir(parents=True, exist_ok=True)
    cmd = run.cmd(binary)
    cmd_file = run.outdir / "run.cmd"
    cmd_file.write_text(
        f"label: {run.label}\ncmd:   "
        + " ".join(shlex.quote(c) for c in cmd) + "\n"
    )
    log_file = run.outdir / "run.log"
    with log_file.open("w") as f:
        proc = subprocess.run(cmd, stdout=f, stderr=subprocess.STDOUT,
                              cwd=cfg.M5_PATH)
    run.rc = proc.returncode
    status = "OK" if proc.returncode == 0 else f"FAIL(rc={proc.returncode})"
    print(f"[done] {run.label}  {status}", file=sys.stderr)
    return run


def run_parallel(runs: Iterable[Run], jobs: int,
                 binary: Path | None = None) -> List[Run]:
    """Launch ``runs`` with at most ``jobs`` concurrent gem5 processes."""
    binary = binary or cfg.require_binary()
    runs = list(runs)
    if not runs:
        return []
    print(f"[launch] {len(runs)} runs, {jobs} parallel slot(s)",
          file=sys.stderr)
    with ThreadPoolExecutor(max_workers=jobs) as pool:
        futures = [pool.submit(launch_run, r, binary) for r in runs]
        return [f.result() for f in as_completed(futures)]
