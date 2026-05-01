"""Stats / log harvesting -> summary.tsv + deltas.tsv.

AIA-CDA branch schema: only the columns relevant to plain / aia-kd /
iommu are emitted (the SMMU-specific columns from `main` are gone).
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, List


_SIM_TICKS_RE   = re.compile(r"^simTicks\s+(\d+)", re.MULTILINE)
_SIM_SECONDS_RE = re.compile(r"^simSeconds\s+([0-9.eE+-]+)", re.MULTILINE)
_RUNTIME_RE     = re.compile(r"Runtime:\s+(\S+)")
_AIA_OVERHEAD_RE   = re.compile(r"TOTAL SECURITY OVERHEAD:\s+(\S+)\s+us")
_IOMMU_OVERHEAD_RE = re.compile(r"TOTAL IOMMU OVERHEAD:\s+(\S+)\s+us")
_IOMMU_CHECKS_RE   = re.compile(r"Total IOMMU checks:\s+(\S+)")


@dataclass
class HarvestRow:
    """One row of the comparison TSV.

    ``runtime_us`` is derived from ``simTicks`` (1 tick == 1 ps -> us =
    ticks / 1e6) and is the end-to-end ground truth. The per-cluster
    ``Runtime: N us`` printed by SALAM accelerators is reported
    separately as ``kernel_runtime_us`` because it only reflects the
    last cluster's elapsed time.

    ``aia_kd_overhead_us`` and ``iommu_overhead_us`` are the analytical
    overheads reported by the protection model in run.log. They are NOT
    added to ``runtime_us`` (which is purely simulated). Effective
    runtime under each protection model =
    ``runtime_us`` + the matching overhead column (the analytical models
    deliberately do not perturb simulator timing).
    """
    label: str
    runtime_us: str = "-"
    sim_ticks: str = "-"
    sim_seconds: str = "-"
    kernel_runtime_us: str = "-"
    aia_kd_overhead_us: str = "-"
    iommu_overhead_us: str = "-"
    iommu_checks: str = "-"

    def as_tsv(self) -> str:
        return "\t".join((
            self.label, self.runtime_us, self.sim_ticks, self.sim_seconds,
            self.kernel_runtime_us, self.aia_kd_overhead_us,
            self.iommu_overhead_us, self.iommu_checks,
        ))


HEADER = ("mode\truntime_us\tsim_ticks\tsim_seconds\tkernel_runtime_us"
          "\taia_kd_overhead_us\tiommu_overhead_us\tiommu_checks")


def harvest_run(label: str, outdir: Path) -> HarvestRow:
    row = HarvestRow(label=label)
    stats_path = outdir / "stats.txt"
    log_path = outdir / "run.log"

    if not stats_path.exists():
        row.runtime_us = "FAIL"
        return row

    stats_text = stats_path.read_text()
    log_text = log_path.read_text() if log_path.exists() else ""

    if (m := _SIM_TICKS_RE.search(stats_text)):
        row.sim_ticks = m.group(1)
        row.runtime_us = f"{int(m.group(1)) / 1e6:.3f}"
    if (m := _SIM_SECONDS_RE.search(stats_text)):
        row.sim_seconds = m.group(1)
    runtimes = _RUNTIME_RE.findall(log_text)
    if runtimes:
        row.kernel_runtime_us = runtimes[-1]
    # Sum overheads across all clusters (IOMMU prints once per cluster).
    aia = [float(x) for x in _AIA_OVERHEAD_RE.findall(log_text)]
    if aia:
        row.aia_kd_overhead_us = f"{sum(aia):.3f}"
    iommu = [float(x) for x in _IOMMU_OVERHEAD_RE.findall(log_text)]
    if iommu:
        row.iommu_overhead_us = f"{sum(iommu):.3f}"
    checks = [int(x) for x in _IOMMU_CHECKS_RE.findall(log_text)]
    if checks:
        row.iommu_checks = str(sum(checks))
    return row


def write_summary(rows: Iterable[HarvestRow], path: Path) -> List[HarvestRow]:
    rows = list(rows)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w") as f:
        f.write(HEADER + "\n")
        for r in rows:
            f.write(r.as_tsv() + "\n")
    return rows


def write_deltas(rows: List[HarvestRow], path: Path) -> None:
    """Compute Δticks / Δ% vs the row labelled 'plain' (if present).

    Uses ``simTicks`` as the runtime ground truth (1 tick == 1 ps). For
    analytical protection modes also reports the *effective* delta
    including the overhead column.
    """
    plain = next((r for r in rows if r.label == "plain"), None)
    if plain is None or not plain.sim_ticks.isdigit():
        return
    base = int(plain.sim_ticks)
    if base <= 0:
        return
    base_us = base / 1e6
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w") as f:
        f.write("mode\tsim_ticks\truntime_us\tdelta_us\tdelta_pct"
                "\teffective_us\teffective_delta_pct\n")
        for r in rows:
            if not r.sim_ticks.isdigit():
                f.write(f"{r.label}\t{r.sim_ticks}\t{r.runtime_us}"
                        f"\t-\t-\t-\t-\n")
                continue
            v = int(r.sim_ticks)
            d_us = (v - base) / 1e6
            pct = (d_us / base_us) * 100.0
            overhead_us = 0.0
            for col in (r.aia_kd_overhead_us, r.iommu_overhead_us):
                try:
                    overhead_us += float(col)
                except ValueError:
                    pass
            eff_us = (v / 1e6) + overhead_us
            eff_pct = ((eff_us - base_us) / base_us) * 100.0
            f.write(f"{r.label}\t{v}\t{r.runtime_us}\t"
                    f"{d_us:.3f}\t{pct:.3f}\t"
                    f"{eff_us:.3f}\t{eff_pct:.3f}\n")


def harvest_outdir(outroot: Path,
                   labels: Iterable[str] | None = None) -> List[HarvestRow]:
    """Harvest one outdir tree. If ``labels`` is None, use every subdir."""
    if labels is None:
        labels = sorted(p.name for p in outroot.iterdir() if p.is_dir())
    return [harvest_run(lbl, outroot / lbl) for lbl in labels]


def pretty_print(rows: Iterable[HarvestRow]) -> str:
    """Aligned text dump for stdout."""
    cols = HEADER.split("\t")
    out = ["  ".join(c.ljust(20) for c in cols)]
    for r in rows:
        out.append("  ".join(v.ljust(20) for v in r.as_tsv().split("\t")))
    return "\n".join(out)
