#!/usr/bin/env python3
"""Sanity test suite for the aia_cda_rel branch.

Runs a small set of fast sys_validation benchmarks across the three
protection modes (plain, aia-kd, iommu) and asserts the invariants
documented in tests/aia_cda_tests/README.md.

Exits non-zero on the first failed assertion; prints a green summary on
success.
"""
from __future__ import annotations

import argparse
import csv
import shutil
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
DEFAULT_OUTDIR = REPO / "BM_ARM_OUT" / "aia_cda_sanity"

# Selected for short plain-mode runtime (< ~2 ms sim time on the dev
# container). Keep this list small — the suite is supposed to finish
# well under one minute end-to-end with --jobs >= 4.
FAST_BENCHES = ["nw", "fft", "stencil2d", "md_knn"]
MODES = ["plain", "aia-kd", "iommu"]


def run_compare_all(outdir: Path, benches: list[str], jobs: int,
                    regen: bool) -> int:
    cmd = [
        sys.executable, "-m", "tools.speedkills", "compare-all",
        "--outdir", str(outdir),
        "--bench", ",".join(benches),
        "--jobs", str(jobs),
        "--modes", " ".join(MODES),
    ]
    if regen:
        cmd.append("--regen")
    print("[run]", " ".join(cmd))
    return subprocess.call(cmd, cwd=str(REPO))


def parse_summary(tsv: Path) -> dict[str, dict[str, str]]:
    rows: dict[str, dict[str, str]] = {}
    with tsv.open() as fh:
        reader = csv.DictReader(fh, delimiter="\t")
        for row in reader:
            rows[row["mode"]] = row
    return rows


def _num(s: str) -> float:
    if s in ("", "-", "--"):
        return 0.0
    return float(s)


def check(rows: dict[str, dict[str, str]], benches: list[str]) -> list[str]:
    """Return a list of failure messages (empty == all passed)."""
    failures: list[str] = []
    for b in benches:
        for m in MODES:
            key = f"{b}/{m}"
            if key not in rows:
                failures.append(f"{key}: missing from summary.tsv")
        # Invariant 1: every mode produced a row (covered above).
        plain = rows.get(f"{b}/plain")
        iommu = rows.get(f"{b}/iommu")
        aiakd = rows.get(f"{b}/aia-kd")
        if not (plain and iommu and aiakd):
            continue
        # Invariant 2: iommu sim_ticks bit-identical to plain.
        if plain["sim_ticks"] != iommu["sim_ticks"]:
            failures.append(
                f"{b}: iommu sim_ticks ({iommu['sim_ticks']}) != "
                f"plain sim_ticks ({plain['sim_ticks']}) — "
                "analytical IOMMU must not perturb the simulator")
        # Invariant 3: aia-kd reports non-zero overhead with default lat.
        if _num(aiakd["aia_kd_overhead_us"]) <= 0.0:
            failures.append(
                f"{b}: aia-kd overhead is 0 — AIA-KD checkpoints not firing")
        # Invariant 4: iommu reports non-zero check count.
        if _num(iommu["iommu_checks"]) <= 0.0:
            failures.append(
                f"{b}: iommu_checks is 0 — IOMMU hook not firing")
    return failures


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--outdir", default=str(DEFAULT_OUTDIR),
                   help="Output tree (will be cleared).")
    p.add_argument("--bench", default=",".join(FAST_BENCHES),
                   help="Comma-separated bench list. Default: "
                        + ",".join(FAST_BENCHES))
    p.add_argument("--jobs", type=int, default=4)
    p.add_argument("--regen", action="store_true",
                   help="Regenerate configs/SALAM/<bench>.py before runs.")
    p.add_argument("--keep", action="store_true",
                   help="Do not clear --outdir before running.")
    args = p.parse_args()

    benches = [b.strip() for b in args.bench.split(",") if b.strip()]
    outdir = Path(args.outdir).resolve()

    if outdir.exists() and not args.keep:
        shutil.rmtree(outdir)
    outdir.mkdir(parents=True, exist_ok=True)

    rc = run_compare_all(outdir, benches, args.jobs, args.regen)
    if rc != 0:
        print(f"\n[FAIL] compare-all exited with rc={rc}", file=sys.stderr)
        return 1

    summary = outdir / "summary.tsv"
    if not summary.exists():
        print(f"\n[FAIL] {summary} missing", file=sys.stderr)
        return 1

    rows = parse_summary(summary)
    failures = check(rows, benches)
    if failures:
        print("\n===== FAILURES =====", file=sys.stderr)
        for f in failures:
            print(f"  - {f}", file=sys.stderr)
        return 1

    print(f"\n[PASS] {len(benches)} benches x {len(MODES)} modes "
          f"= {len(benches) * len(MODES)} runs OK")
    print(f"       summary: {summary}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
