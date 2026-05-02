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
_IOTLB_HITS_RE     = re.compile(r"IOTLB hits:\s+(\d+)")
_IOTLB_MISSES_RE   = re.compile(r"IOTLB misses \(page walks\):\s+(\d+)")
_SMID_REQS_RE      = re.compile(
    r"Total memory accesses \(validated\):\s+(\d+)")
_SMID_VALS_RE      = re.compile(r"Validation requests \(full lat\):\s+(\d+)")


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
    iotlb_hits: str = "-"
    iotlb_misses: str = "-"
    iotlb_hit_rate_pct: str = "-"
    smid_requests: str = "-"
    smid_validations: str = "-"

    def as_tsv(self) -> str:
        return "\t".join((
            self.label, self.runtime_us, self.sim_ticks, self.sim_seconds,
            self.kernel_runtime_us, self.aia_kd_overhead_us,
            self.iommu_overhead_us, self.iommu_checks,
            self.iotlb_hits, self.iotlb_misses, self.iotlb_hit_rate_pct,
            self.smid_requests, self.smid_validations,
        ))


HEADER = ("mode\truntime_us\tsim_ticks\tsim_seconds\tkernel_runtime_us"
          "\taia_kd_overhead_us\tiommu_overhead_us\tiommu_checks"
          "\tiotlb_hits\tiotlb_misses\tiotlb_hit_rate_pct"
          "\tsmid_requests\tsmid_validations")


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
    hits = [int(x) for x in _IOTLB_HITS_RE.findall(log_text)]
    if hits:
        row.iotlb_hits = str(sum(hits))
    misses = [int(x) for x in _IOTLB_MISSES_RE.findall(log_text)]
    if misses:
        row.iotlb_misses = str(sum(misses))
    if hits or misses:
        total = sum(hits) + sum(misses)
        if total > 0:
            row.iotlb_hit_rate_pct = f"{(sum(hits) / total) * 100.0:.4f}"
    smid_r = [int(x) for x in _SMID_REQS_RE.findall(log_text)]
    if smid_r:
        row.smid_requests = str(sum(smid_r))
    smid_v = [int(x) for x in _SMID_VALS_RE.findall(log_text)]
    if smid_v:
        row.smid_validations = str(sum(smid_v))
    return row


def write_summary(rows: Iterable[HarvestRow], path: Path) -> List[HarvestRow]:
    rows = list(rows)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w") as f:
        f.write(HEADER + "\n")
        for r in rows:
            f.write(r.as_tsv() + "\n")
    csv_path = path.with_suffix(".csv")
    with csv_path.open("w") as f:
        f.write(HEADER.replace("\t", ",") + "\n")
        for r in rows:
            f.write(r.as_tsv().replace("\t", ",") + "\n")
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
    csv_path = path.with_suffix(".csv")
    header = ("mode\tsim_ticks\truntime_us\tdelta_us\tdelta_pct"
             "\teffective_us\teffective_delta_pct")
    with path.open("w") as f, csv_path.open("w") as fc:
        f.write(header + "\n")
        fc.write(header.replace("\t", ",") + "\n")
        for r in rows:
            if not r.sim_ticks.isdigit():
                line = (f"{r.label}\t{r.sim_ticks}\t{r.runtime_us}"
                        f"\t-\t-\t-\t-")
                f.write(line + "\n")
                fc.write(line.replace("\t", ",") + "\n")
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
            line = (f"{r.label}\t{v}\t{r.runtime_us}\t"
                    f"{d_us:.3f}\t{pct:.3f}\t"
                    f"{eff_us:.3f}\t{eff_pct:.3f}")
            f.write(line + "\n")
            fc.write(line.replace("\t", ",") + "\n")


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


def _safe_float(s: str) -> float | None:
    try:
        return float(s)
    except (TypeError, ValueError):
        return None


def write_overhead_summary(rows: Iterable[HarvestRow], path: Path) -> str:
    """Per-benchmark overhead pivot vs the bench's `plain` row.

    Reports raw microsecond overheads (not %) for aia-kd and iommu,
    plus SMID Request / SMID Validations (from the aia-kd row) and
    IOMMU check / hit / miss / hit-rate (from the iommu row).
    Returns the formatted text and writes a TSV at ``path``.
    """
    by_bench: dict[str, dict[str, HarvestRow]] = {}
    for r in rows:
        if "/" not in r.label:
            continue
        bench, mode = r.label.split("/", 1)
        by_bench.setdefault(bench, {})[mode] = r

    cols = ("benchmark", "plain_us", "aia_kd_overhead_us",
            "aia_kd_overhead_pct", "iommu_overhead_us",
            "iommu_overhead_pct", "smid_requests", "smid_validations",
            "iommu_checks", "iotlb_hits", "iotlb_misses",
            "iotlb_hit_rate_pct")
    lines = ["\t".join(cols)]
    pretty = ["  ".join(c.ljust(20) for c in cols)]
    for bench in sorted(by_bench):
        modes = by_bench[bench]
        plain = modes.get("plain")
        plain_us = _safe_float(plain.runtime_us) if plain else None
        plain_us_s = (f"{plain_us:.3f}" if plain_us is not None else "-")

        aia_row = modes.get("aia-kd")
        iommu_row = modes.get("iommu")
        aia_us = aia_row.aia_kd_overhead_us if aia_row else "-"
        iommu_us = iommu_row.iommu_overhead_us if iommu_row else "-"

        def pct(us_str: str) -> str:
            ov = _safe_float(us_str)
            if ov is None or plain_us in (None, 0.0):
                return "-"
            return f"{(ov / plain_us) * 100.0:.3f}%"

        aia_pct = pct(aia_us)
        iommu_pct = pct(iommu_us)
        smid_r = aia_row.smid_requests if aia_row else "-"
        smid_v = aia_row.smid_validations if aia_row else "-"
        iommu_checks = iommu_row.iommu_checks if iommu_row else "-"
        iotlb_h = iommu_row.iotlb_hits if iommu_row else "-"
        iotlb_m = iommu_row.iotlb_misses if iommu_row else "-"
        iotlb_hr = (f"{iommu_row.iotlb_hit_rate_pct}%"
                    if iommu_row and iommu_row.iotlb_hit_rate_pct != "-"
                    else "-")

        vals = (bench, plain_us_s, aia_us, aia_pct, iommu_us, iommu_pct,
                smid_r, smid_v, iommu_checks, iotlb_h, iotlb_m, iotlb_hr)
        lines.append("\t".join(vals))
        pretty.append("  ".join(v.ljust(20) for v in vals))

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n")
    # Also emit a sibling .csv for spreadsheet import. The TSV columns
    # never contain commas so a naive replace is safe.
    csv_path = path.with_suffix(".csv")
    csv_path.write_text("\n".join(l.replace("\t", ",") for l in lines) + "\n")
    return "\n".join(pretty)
