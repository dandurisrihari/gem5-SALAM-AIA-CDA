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
# IOMMU stats are owned by the chip-wide AcceleratorIommu SimObject
# (one per cluster). Read them straight from stats.txt -- the console
# `printIommuStats()` is called once per CU and would N-fold the
# chip-wide counter when multiple CUs share one IOMMU. Sum across
# clusters (each cluster has its own line).
_STATS_TOTALCHECKS_RE = re.compile(
    r"\.iommu\.totalChecks\s+(\d+)", re.MULTILINE)
_STATS_TLBHITS_RE = re.compile(
    r"\.iommu\.tlbHits\s+(\d+)", re.MULTILINE)
_STATS_TLBMISSES_RE = re.compile(
    r"\.iommu\.tlbMisses\s+(\d+)", re.MULTILINE)
_STATS_UNIQUEPAGES_RE = re.compile(
    r"\.iommu\.uniquePages\s+(\d+)", re.MULTILINE)
_SMID_REQS_RE      = re.compile(
    r"Total memory accesses \(validated\):\s+(\d+)")
_SMID_VALS_RE      = re.compile(r"Validation requests \(full lat\):\s+(\d+)")
_SMID_DMA_RE       = re.compile(r"of which DMA-ctrl writes:\s+(\d+)")


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
    iommu_unique_pages: str = "-"
    smid_requests: str = "-"
    smid_validations: str = "-"
    # Subset of smid_validations attributable to writes into a DMA
    # control-reg page (printed by AIA-KD as "of which DMA-ctrl
    # writes"). 0 means the workload does not reprogram any DMA
    # while validation is enabled.
    smid_validations_dma_ctrl: str = "-"

    def as_tsv(self) -> str:
        return "\t".join((
            self.label, self.runtime_us, self.sim_ticks, self.sim_seconds,
            self.kernel_runtime_us, self.aia_kd_overhead_us,
            self.iommu_overhead_us, self.iommu_checks,
            self.iotlb_hits, self.iotlb_misses, self.iotlb_hit_rate_pct,
            self.iommu_unique_pages,
            self.smid_requests, self.smid_validations,
            self.smid_validations_dma_ctrl,
        ))


HEADER = ("mode\truntime_us\tsim_ticks\tsim_seconds\tkernel_runtime_us"
          "\taia_kd_overhead_us\tiommu_overhead_us\tiommu_checks"
          "\tiotlb_hits\tiotlb_misses\tiotlb_hit_rate_pct"
          "\tiommu_unique_pages"
          "\tsmid_requests\tsmid_validations\tsmid_validations_dma_ctrl")


def harvest_run(label: str, outdir: Path) -> HarvestRow:
    row = HarvestRow(label=label)
    stats_path = outdir / "stats.txt"
    log_path = outdir / "run.log"

    if not stats_path.exists():
        row.runtime_us = "FAIL"
        return row

    stats_text = stats_path.read_text()
    # gem5 may dump stats more than once per run (mid-sim checkpoints +
    # end-of-sim). Each dump is a self-contained block delimited by
    # `Begin Simulation Statistics` / `End Simulation Statistics`.
    # Use only the LAST block so cumulative counters aren't summed
    # across dumps (which would N-fold them).
    _sep = "---------- Begin Simulation Statistics ----------"
    _last_begin = stats_text.rfind(_sep)
    if _last_begin >= 0:
        stats_text = stats_text[_last_begin:]
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
    checks = [int(x) for x in _STATS_TOTALCHECKS_RE.findall(stats_text)]
    if checks:
        row.iommu_checks = str(sum(checks))
    hits = [int(x) for x in _STATS_TLBHITS_RE.findall(stats_text)]
    if hits:
        row.iotlb_hits = str(sum(hits))
    misses = [int(x) for x in _STATS_TLBMISSES_RE.findall(stats_text)]
    if misses:
        row.iotlb_misses = str(sum(misses))
    if hits or misses:
        total = sum(hits) + sum(misses)
        if total > 0:
            row.iotlb_hit_rate_pct = f"{(sum(hits) / total) * 100.0:.4f}"
    upages = [int(x) for x in _STATS_UNIQUEPAGES_RE.findall(stats_text)]
    if upages:
        row.iommu_unique_pages = str(sum(upages))
    smid_r = [int(x) for x in _SMID_REQS_RE.findall(log_text)]
    if smid_r:
        row.smid_requests = str(sum(smid_r))
    smid_v = [int(x) for x in _SMID_VALS_RE.findall(log_text)]
    if smid_v:
        row.smid_validations = str(sum(smid_v))
    smid_d = [int(x) for x in _SMID_DMA_RE.findall(log_text)]
    if smid_d:
        row.smid_validations_dma_ctrl = str(sum(smid_d))
    return row


def write_summary(rows: Iterable[HarvestRow], path: Path) -> List[HarvestRow]:
    rows = list(rows)
    # Canonical row order: plain first, then aia-kd, then iommu, then
    # any remaining labels (e.g. iommu sweep configs) in their original
    # arrival order. Within a multi-bench summary, "<bench>/<mode>"
    # labels are sorted by bench first, then by mode-rank.
    import re as _re
    _sweep_re = _re.compile(r"^iommu_e(\d+)_m(\d+)ns$")

    def _mode_rank(mode: str) -> tuple:
        if mode == "plain":
            return (0, 0, 0)
        if mode == "aia-kd":
            return (1, 0, 0)
        if mode == "iommu":
            return (2, 0, 0)
        m = _sweep_re.match(mode)
        if m:
            return (3, int(m.group(1)), int(m.group(2)))
        return (4, 0, 0)

    def _row_key(idx_row):
        idx, r = idx_row
        if "/" in r.label:
            bench, mode = r.label.split("/", 1)
        else:
            bench, mode = "", r.label
        return (bench, _mode_rank(mode), idx)

    rows = [r for _, r in sorted(enumerate(rows), key=_row_key)]
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


