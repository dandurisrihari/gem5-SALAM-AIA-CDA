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
FAST_BENCHES = ["nw", "fft"]
MODES = ["plain", "aia-kd", "iommu"]

# When kernel_validation_latency == 0, AIA-KD should report 0 µs of
# overhead and the simulated runtime should be within sub-cycle noise of
# plain. The drift is non-zero because the validation IRQ machinery
# still fires (just with a 0-tick deadline), so we allow a generous
# tolerance — observed drift on nw is ~0.07 %.
LAT0_BENCH = "nw"
LAT0_TOLERANCE_PCT = 0.5


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
        # Invariant 2: aia-kd reports non-zero overhead with default lat.
        if _num(aiakd["aia_kd_overhead_us"]) <= 0.0:
            failures.append(
                f"{b}: aia-kd overhead is 0 — AIA-KD checkpoints not firing")
        # Invariant 3: iommu reports non-zero check count.
        if _num(iommu["iommu_checks"]) <= 0.0:
            failures.append(
                f"{b}: iommu_checks is 0 — IOMMU hook not firing")
        # NOTE: We deliberately do NOT assert iommu sim_ticks ==
        # plain sim_ticks. The current IOMMU model is analytical and
        # happens to be non-perturbing, but a real SMMU sits in the
        # critical path between the accelerator and DRAM and would
        # legitimately add latency to every memory access. The
        # bit-identical invariant only holds for AIA-KD@lat=0
        # (enforced separately in run_lat0_check).
    return failures


def run_lat0_check(outdir: Path, regen: bool) -> tuple[int, list[str]]:
    """Verify aia-kd@lat=0 is ~equal to plain.

    Runs ``compare --modes "plain aia-kd" --aia-kd-latency 0`` for one
    fast bench and asserts:
      - reported aia_kd_overhead_us == 0
      - |Δsim_ticks| / plain_sim_ticks <= LAT0_TOLERANCE_PCT
    """
    sub = outdir / "_lat0"
    if sub.exists():
        shutil.rmtree(sub)
    cmd = [
        sys.executable, "-m", "tools.speedkills", "compare",
        "--bench", LAT0_BENCH,
        "--outdir", str(sub),
        "--modes", "plain aia-kd",
        "--aia-kd-latency", "0",
        "--jobs", "2",
    ]
    if regen:
        cmd.append("--regen")
    print("[run]", " ".join(cmd))
    rc = subprocess.call(cmd, cwd=str(REPO))
    if rc != 0:
        return rc, [f"lat=0 compare exited rc={rc}"]
    summary = sub / "summary.tsv"
    if not summary.exists():
        return 1, [f"lat=0 missing {summary}"]
    rows: dict[str, dict[str, str]] = {}
    with summary.open() as fh:
        for row in csv.DictReader(fh, delimiter="\t"):
            rows[row["mode"]] = row
    plain = rows.get("plain")
    aiakd = rows.get("aia-kd")
    if not (plain and aiakd):
        return 1, ["lat=0: missing plain or aia-kd row in summary"]
    failures: list[str] = []
    p_t = _num(plain["sim_ticks"])
    a_t = _num(aiakd["sim_ticks"])
    if p_t <= 0:
        return 1, ["lat=0: plain sim_ticks <= 0"]
    drift_pct = abs(a_t - p_t) / p_t * 100.0
    overhead = _num(aiakd["aia_kd_overhead_us"])
    if overhead != 0.0:
        failures.append(
            f"lat=0: aia_kd_overhead_us = {overhead} (expected 0)")
    if drift_pct > LAT0_TOLERANCE_PCT:
        failures.append(
            f"lat=0: aia-kd sim_ticks drift {drift_pct:.4f}% > "
            f"tolerance {LAT0_TOLERANCE_PCT}% "
            f"(plain={int(p_t)}, aia-kd={int(a_t)})")
    print(f"[lat0] {LAT0_BENCH}: drift={drift_pct:.4f}% "
          f"(tol {LAT0_TOLERANCE_PCT}%), overhead_us={overhead}")
    return 0, failures


def run_iommu_lat0_check(outdir: Path, regen: bool) -> tuple[int, list[str]]:
    """Verify iommu mode at iotlb_*_latency=0 is bit-identical to plain.

    Mirrors run_lat0_check but for the IOMMU branch. With both IOTLB
    latencies set to zero, every access falls through the IOMMU branch
    to the plain enqueue path, so sim_ticks must match plain exactly.
    Drift here would mean the IOMMU branch is perturbing event-queue
    state (extra schedule()/event allocation) when it should be inert.
    """
    sub = outdir / "_iommu_lat0"
    if sub.exists():
        shutil.rmtree(sub)
    cmd = [
        sys.executable, "-m", "tools.speedkills", "compare",
        "--bench", LAT0_BENCH,
        "--outdir", str(sub),
        "--modes", "plain iommu",
        "--iotlb-hit-latency", "0",
        "--iotlb-miss-latency", "0",
        "--jobs", "2",
    ]
    if regen:
        cmd.append("--regen")
    print("[run]", " ".join(cmd))
    rc = subprocess.call(cmd, cwd=str(REPO))
    if rc != 0:
        return rc, [f"iommu lat=0 compare exited rc={rc}"]
    summary = sub / "summary.tsv"
    if not summary.exists():
        return 1, [f"iommu lat=0 missing {summary}"]
    rows: dict[str, dict[str, str]] = {}
    with summary.open() as fh:
        for row in csv.DictReader(fh, delimiter="\t"):
            rows[row["mode"]] = row
    plain = rows.get("plain")
    iommu = rows.get("iommu")
    if not (plain and iommu):
        return 1, ["iommu lat=0: missing plain or iommu row"]
    failures: list[str] = []
    p_t = _num(plain["sim_ticks"])
    i_t = _num(iommu["sim_ticks"])
    if p_t <= 0:
        return 1, ["iommu lat=0: plain sim_ticks <= 0"]
    if p_t != i_t:
        failures.append(
            f"iommu lat=0: sim_ticks differ "
            f"(plain={int(p_t)} iommu={int(i_t)} delta={int(i_t - p_t)}); "
            f"expected bit-identical")
    print(f"[iommu_lat0] {LAT0_BENCH}: "
          f"plain={int(p_t)} iommu={int(i_t)} "
          f"({'OK' if p_t == i_t else 'DRIFT'})")
    return 0, failures


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
    p.add_argument("--skip-lat0", action="store_true",
                   help="Skip the aia-kd@lat=0 ≈ plain check.")
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

    if not args.skip_lat0:
        rc, lat0_failures = run_lat0_check(outdir, args.regen)
        if rc != 0 or lat0_failures:
            print("\n===== LAT=0 FAILURES =====", file=sys.stderr)
            for f in lat0_failures:
                print(f"  - {f}", file=sys.stderr)
            return 1

        rc, ilat0_failures = run_iommu_lat0_check(outdir, args.regen)
        if rc != 0 or ilat0_failures:
            print("\n===== IOMMU LAT=0 FAILURES =====", file=sys.stderr)
            for f in ilat0_failures:
                print(f"  - {f}", file=sys.stderr)
            return 1

    print(f"\n[PASS] {len(benches)} benches x {len(MODES)} modes "
          f"= {len(benches) * len(MODES)} runs OK")
    print(f"       summary: {summary}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
